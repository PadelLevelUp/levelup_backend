"""Datetime utilities. The codebase stores naive UTC datetimes in the DB
because SQLAlchemy's `DateTime` column is naive by default. Use these helpers
instead of the deprecated `datetime.utcnow()` so behaviour is consistent in
any host timezone."""

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# The club's wall clock. Every human-facing "day", "hour" or "week" in the
# product is read off THIS clock, while every instant in the DB is naive UTC.
#
# PAD-144: this constant previously existed in three places (`scheduler`,
# `student_availability_service`, and lazily imported into `notification_service`
# from the first). It lives here now so the next site cannot invent a fourth.
# `utils.dates` imports nothing from the app, so there is no circular-import
# risk in depending on it from anywhere.
CLUB_TZ = ZoneInfo("Europe/Lisbon")


def utcnow_naive() -> datetime:
    """Return the current time as a naive UTC datetime.

    Equivalent to the deprecated `datetime.utcnow()`, but uses a timezone-aware
    intermediate to avoid host-timezone drift.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as a UTC-aware ISO 8601 string.

    Datetimes are stored naive-UTC in the DB. Calling `.isoformat()` on those
    produces a string with no timezone offset (e.g. "2026-07-01T17:00:00"),
    which browsers' `new Date(...)` interpret as *local* time — displaying the
    wrong wall-clock (e.g. 1 hour behind in Lisbon summer time). Attaching the
    UTC offset here lets clients convert to the viewer's local timezone.

    A naive datetime is assumed to be UTC. An already-aware datetime is
    converted to UTC. Returns None for None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def club_day_start_utc(instant: datetime, *, days_offset: int = 0) -> datetime:
    """Naive-UTC instant at which a CLUB-LOCAL calendar day begins.

    `instant` is a naive UTC datetime (as stored). The returned value is the
    naive-UTC instant of local midnight starting the club-local day that
    contains `instant`, shifted by `days_offset` calendar days.

    PAD-144. Day boundaries that a coach reads off their own calendar ("per
    day", "tomorrow") are club-local, but they must be compared against columns
    stored as naive UTC. Deriving them with a bare `.replace(hour=0, ...)` on a
    naive-UTC instant silently pins them to UTC midnight, which is 01:00 local
    through Portuguese summer time (WEST = UTC+1) and correct only in winter
    (WET = UTC+0) — which is why the resulting bugs always look intermittent.
    So: convert out to local, take the calendar day there, then convert back.

    `days_offset` walks CALENDAR days rather than adding 24-hour blocks, so a
    step across a DST transition still lands on real local midnight.
    """
    local = instant.replace(tzinfo=timezone.utc).astimezone(CLUB_TZ)
    target_date = local.date() + timedelta(days=days_offset)
    local_midnight = datetime.combine(target_date, time.min, tzinfo=CLUB_TZ)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
