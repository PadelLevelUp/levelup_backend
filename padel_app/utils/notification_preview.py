"""
notification_preview.py — dry-run / simulation utilities for the notification engine.

None of the functions here touch the database with writes or send any messages.
They are purely computational: given a snapshot of config + instances, they return
what WOULD happen at each point in time.

Usage
-----
From a Flask shell or script::

    from padel_app.utils.notification_preview import preview_notification_schedule
    rows = preview_notification_schedule(coach_id=1)
    for r in rows:
        print(r["at"].strftime("%a %d %b %H:%M"), "|", r["event"], "|", r["instance_title"])

To simulate a specific week::

    from datetime import datetime
    rows = preview_notification_schedule(
        coach_id=1,
        from_dt=datetime(2025, 9, 1),
        to_dt=datetime(2025, 9, 8),
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class ScheduledEvent:
    at: datetime
    event: str          # "reminder" | "invite_start" | "batch_check"
    instance_id: int
    instance_title: str
    instance_start: datetime
    will_fire: bool     # False when blocked by a restriction at that time
    blocked_by: str     # populated when will_fire is False


def _quiet_hours_blocked(dt: datetime, restrictions: dict) -> bool:
    if not restrictions.get("quietHours", {}).get("enabled"):
        return False
    h = dt.hour
    return h >= 22 or h < 7


def _min_time_blocked(dt: datetime, instance_start: datetime, restrictions: dict) -> bool:
    min_time = restrictions.get("minTimeBeforeClass", {})
    if not min_time.get("enabled"):
        return False
    minutes_until = (instance_start - dt).total_seconds() / 60
    return minutes_until < min_time["value"]


def preview_notification_schedule(
    coach_id: int,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> list[dict]:
    """
    Return a sorted list of events that WOULD fire for a coach's upcoming classes.

    Each entry is a dict with keys:
      at             – datetime the event would fire (UTC)
      event          – "reminder" | "invite_start"
      instance_id    – int
      instance_title – str  (level + weekday + time, or id fallback)
      instance_start – datetime
      will_fire      – bool (False if blocked by quiet-hours or min-time)
      blocked_by     – reason string when will_fire is False, else ""

    Nothing is written to the database.
    """
    from padel_app.models import Association_CoachLessonInstance, LessonInstance
    from padel_app.scheduler import _compute_timing_dt
    from padel_app.services.notification_service import get_or_create_config

    now = from_dt or datetime.utcnow()
    end = to_dt or (now + timedelta(days=14))

    config = get_or_create_config(coach_id)
    reminder_timing = config.get_reminder_timing()
    invite_timing = config.get_invitation_start_timing()
    restrictions = config.get_restrictions()

    future_instances = (
        LessonInstance.query
        .join(
            Association_CoachLessonInstance,
            LessonInstance.id == Association_CoachLessonInstance.lesson_instance_id,
        )
        .filter(
            Association_CoachLessonInstance.coach_id == coach_id,
            LessonInstance.start_datetime > now,
            LessonInstance.start_datetime <= end,
            LessonInstance.status != "canceled",
        )
        .order_by(LessonInstance.start_datetime)
        .all()
    )

    events: list[dict] = []

    for instance in future_instances:
        level_code = instance.level.code if getattr(instance, "level", None) else "?"
        title = f"{level_code} — {instance.start_datetime.strftime('%a %d %b %H:%M')}"

        for event_type, timing in (("reminder", reminder_timing), ("invite_start", invite_timing)):
            fire_dt = _compute_timing_dt(instance.start_datetime, timing)
            if fire_dt is None or fire_dt <= now or fire_dt > end:
                continue

            blocked_by = ""
            if _quiet_hours_blocked(fire_dt, restrictions):
                blocked_by = "quiet hours"
            elif _min_time_blocked(fire_dt, instance.start_datetime, restrictions):
                blocked_by = "min time before class"

            events.append({
                "at": fire_dt,
                "event": event_type,
                "instance_id": instance.id,
                "instance_title": title,
                "instance_start": instance.start_datetime,
                "will_fire": blocked_by == "",
                "blocked_by": blocked_by,
            })

    events.sort(key=lambda e: e["at"])
    return events


def simulate_batch_processor(
    vacancy_snapshots: list[dict],
    from_dt: datetime,
    to_dt: datetime,
    *,
    max_inactive_minutes: int = 120,
    step_minutes: int = 2,
) -> list[dict]:
    """
    Simulate the recurring ``process_invitation_batches`` job over a time range,
    without touching the database.

    Parameters
    ----------
    vacancy_snapshots:
        Each dict must have:
          - ``id``                  int
          - ``last_activity_at``    datetime | None  (None = fresh vacancy)
          - ``instance_start``      datetime
          - ``coach_id``            int  (informational only)
    from_dt, to_dt:
        Inclusive time range to simulate.
    max_inactive_minutes:
        How long without activity before the next batch fires.
    step_minutes:
        How often the recurring job would run (default 2).

    Returns
    -------
    List of dicts, one per simulated batch send::

        {"at": datetime, "vacancy_id": int, "reason": "fresh" | "inactivity"}
    """
    threshold = timedelta(minutes=max_inactive_minutes)
    step = timedelta(minutes=step_minutes)

    # Track mutable last_activity per vacancy
    last_activity: dict[int, datetime | None] = {
        v["id"]: v.get("last_activity_at") for v in vacancy_snapshots
    }
    instance_starts: dict[int, datetime] = {v["id"]: v["instance_start"] for v in vacancy_snapshots}

    fired: list[dict] = []
    cursor = from_dt

    while cursor <= to_dt:
        for v in vacancy_snapshots:
            vid = v["id"]
            istart = instance_starts[vid]

            # Skip past/future instances
            if istart <= cursor:
                continue

            last = last_activity[vid]

            if last is None:
                fired.append({"at": cursor, "vacancy_id": vid, "reason": "fresh"})
                last_activity[vid] = cursor
            elif cursor - last >= threshold:
                fired.append({"at": cursor, "vacancy_id": vid, "reason": "inactivity"})
                last_activity[vid] = cursor

        cursor += step

    return fired


def print_schedule(coach_id: int, days: int = 14) -> None:
    """
    Pretty-print the upcoming notification schedule to stdout.
    Intended for use in a Flask shell.
    """
    from_dt = datetime.utcnow()
    to_dt = from_dt + timedelta(days=days)
    rows = preview_notification_schedule(coach_id, from_dt=from_dt, to_dt=to_dt)

    if not rows:
        print("No notifications scheduled in the next", days, "days.")
        return

    print(f"\n{'Time (UTC)':<22} {'Event':<14} {'Fires?':<8} {'Class'}")
    print("-" * 80)
    for r in rows:
        fires = "YES" if r["will_fire"] else f"NO ({r['blocked_by']})"
        print(
            f"{r['at'].strftime('%a %d %b %H:%M'):<22}"
            f"{r['event']:<14}"
            f"{fires:<20}"
            f"{r['instance_title']}"
        )
    print()
