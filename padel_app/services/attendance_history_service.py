"""Attendance history for one player (PAD-114 / spec `attendance.history`).

The "Presenças" page shows a player the classes they actually ATTENDED — a chart
of counts per period plus the underlying list of classes. It is deliberately not
a present-vs-absent comparison and not a missed-classes page.

Two facts shape everything here:

* `presences` has no date column of its own. Every timestamp comes from the
  joined `lesson_instances.start_datetime`, which is stored naive-UTC.
* "attended" is `Presence.status == "present"` — the same predicate as
  `helpers.dashboard.kpis.compute_player_kpis().lessons_attended`, so this page
  and the dashboard "Attended" KPI can never disagree.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from sqlalchemy.orm import joinedload

from padel_app.models import LessonInstance, Presence
from padel_app.sql_db import db

#: Granularities the chart can be bucketed at, coarsest last.
GRANULARITIES = ("day", "month", "year")

#: Span thresholds (in days) for automatic granularity selection.
_MAX_DAILY_SPAN = 31
_MAX_MONTHLY_SPAN = 550  # ~18 months


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize an aware-or-naive datetime to the naive-UTC the DB stores."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def pick_granularity(range_start: datetime, range_end: datetime) -> str:
    """Choose a bucket size from the span (spec `attendance.history` rule 4).

    <= 31 days -> daily, <= ~18 months -> monthly, longer -> yearly. The caller
    always echoes the result back to the client so the frontend labels its axis
    from the payload instead of re-deriving the rule.
    """
    span_days = (range_end.date() - range_start.date()).days
    if span_days <= _MAX_DAILY_SPAN:
        return "day"
    if span_days <= _MAX_MONTHLY_SPAN:
        return "month"
    return "year"


def _bucket_start(day: date, granularity: str) -> date:
    if granularity == "day":
        return day
    if granularity == "month":
        return day.replace(day=1)
    return day.replace(month=1, day=1)


def _next_bucket(bucket: date, granularity: str) -> date:
    if granularity == "day":
        return bucket + timedelta(days=1)
    if granularity == "month":
        if bucket.month == 12:
            return bucket.replace(year=bucket.year + 1, month=1)
        return bucket.replace(month=bucket.month + 1)
    return bucket.replace(year=bucket.year + 1)


def _bucket_series(
    range_start: date, range_end: date, granularity: str
) -> List[date]:
    """Contiguous, gap-filled bucket starts covering the range (rule 5).

    Empty periods must still be present (with count 0) so the chart's x-axis is
    continuous rather than silently skipping periods with no attendance.
    """
    buckets: List[date] = []
    cursor = _bucket_start(range_start, granularity)
    last = _bucket_start(range_end, granularity)
    # Guard against a pathological range producing an unbounded series.
    while cursor <= last and len(buckets) < 2000:
        buckets.append(cursor)
        cursor = _next_bucket(cursor, granularity)
    return buckets


def default_range(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """The current month — the default when the caller pins no range (rule 6)."""
    now = _as_naive_utc(now or datetime.now(timezone.utc))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    last_day = monthrange(now.year, now.month)[1]
    end = start.replace(day=last_day, hour=23, minute=59, second=59)
    return start, end


def _calendar_href(instance_id: int, day: date) -> str:
    """Calendar deep link for one attended class.

    Format is fixed by `dashboard.navigation` rule 8 —
    `/calendar?classId=lessoninstance-<id>&date=<YYYY-MM-DD>`. An attended class
    is by definition a materialized instance, so it is always the
    `lessoninstance-` form, never the virtual `lesson-<id>-<date>` one.
    """
    params = {
        "classId": f"lessoninstance-{instance_id}",
        "date": day.isoformat(),
    }
    return f"/calendar?{urlencode(params)}"


def build_attendance_history(
    *,
    player_id: int,
    range_start: datetime,
    range_end: datetime,
    granularity: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the attendance-history payload for one player.

    Args:
        player_id: The player whose attendance is being read. Authorization is
            the caller's job — this service trusts the id it is handed.
        range_start: Inclusive start of the window.
        range_end: Inclusive end of the window.
        granularity: Optional pin (``day`` | ``month`` | ``year``). When absent
            (or unrecognized) it is derived from the span.

    Returns:
        dict with ``playerId``, ``from``, ``to``, ``granularity``, a gap-filled
        ``buckets`` series and the ``sessions`` list of attended classes.
    """
    start = _as_naive_utc(range_start)
    end = _as_naive_utc(range_end)
    if end < start:
        start, end = end, start

    if granularity not in GRANULARITIES:
        granularity = pick_granularity(start, end)

    rows: List[LessonInstance] = (
        db.session.query(LessonInstance)
        .join(Presence, Presence.lesson_instance_id == LessonInstance.id)
        .options(joinedload(LessonInstance.lesson))
        .filter(Presence.player_id == player_id)
        .filter(Presence.status == "present")
        .filter(LessonInstance.start_datetime >= start)
        .filter(LessonInstance.start_datetime <= end)
        .order_by(LessonInstance.start_datetime.desc())
        .all()
    )

    counts: Dict[date, int] = {}
    sessions: List[Dict[str, Any]] = []
    for instance in rows:
        started = instance.start_datetime
        day = started.date()
        bucket = _bucket_start(day, granularity)
        counts[bucket] = counts.get(bucket, 0) + 1
        sessions.append(
            {
                "lessonInstanceId": instance.id,
                "calendarEventId": f"lessoninstance-{instance.id}",
                "title": instance.title,
                "startDatetime": started.isoformat(),
                "date": day.isoformat(),
                "color": getattr(instance.lesson, "color", None),
                "href": _calendar_href(instance.id, day),
            }
        )

    buckets = [
        {"start": bucket.isoformat(), "count": counts.get(bucket, 0)}
        for bucket in _bucket_series(start.date(), end.date(), granularity)
    ]

    return {
        "playerId": player_id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "granularity": granularity,
        "total": len(sessions),
        "buckets": buckets,
        "sessions": sessions,
    }
