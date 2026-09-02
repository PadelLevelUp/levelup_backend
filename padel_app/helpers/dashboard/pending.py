from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from padel_app.sql_db import db
from padel_app.models import NotificationEvent, LessonInstance
from padel_app.utils.dates import club_day_start_utc, utcnow_naive


def _tomorrow_window(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """
    Half-open [start, end) range covering the *next* calendar day.

    Returned as naive UTC to match how ``LessonInstance.start_datetime`` is
    stored, but the day itself is the coach's CLUB-LOCAL one — `dashboard.blocks`
    rules 6 and 7.

    PAD-144: this previously did `.replace(hour=0, ...)` on a naive-UTC instant,
    which pins the window to UTC midnight. In Portuguese summer time that shifts
    it an hour, so a 00:30-local class tomorrow fell outside the window while a
    00:30-local class *today* fell inside it. That matters beyond a wrong count:
    the same window picks the targets of ``notify_pending_confirmations``, which
    actually sends messages, so the coach nudged the wrong students.
    """
    base = now or utcnow_naive()
    start = club_day_start_utc(base, days_offset=1)
    end = club_day_start_utc(base, days_offset=2)
    return start, end


def _pending_pairs(coach_id: int, now: Optional[datetime] = None):
    """
    Distinct (lesson_instance_id, player_id) pairs that are *pending confirmation*
    for the coach's classes tomorrow.

    A pair is pending when there is a NotificationEvent still in the ``sent``
    state: the student was invited/notified but has neither confirmed (event ->
    ``confirmed``) nor declined / timed out (event -> ``expired``).
    """
    start, end = _tomorrow_window(now)
    return (
        db.session.query(
            NotificationEvent.lesson_instance_id,
            NotificationEvent.player_id,
        )
        .join(LessonInstance, LessonInstance.id == NotificationEvent.lesson_instance_id)
        .filter(NotificationEvent.coach_id == coach_id)
        .filter(NotificationEvent.status == "sent")
        .filter(LessonInstance.start_datetime >= start)
        .filter(LessonInstance.start_datetime < end)
        .distinct()
    )


def count_pending_confirmations(*, coach_id: int, now: Optional[datetime] = None) -> int:
    """Number of students still pending confirmation for tomorrow's classes."""
    return _pending_pairs(coach_id, now).count()


def get_pending_confirmation_targets(
    *, coach_id: int, now: Optional[datetime] = None
) -> List[Tuple[int, List[int]]]:
    """
    Pending targets grouped by lesson instance:
        [(lesson_instance_id, [player_id, ...]), ...]
    """
    by_instance: Dict[int, List[int]] = {}
    for instance_id, player_id in _pending_pairs(coach_id, now).all():
        by_instance.setdefault(instance_id, []).append(player_id)
    return list(by_instance.items())


def notify_pending_confirmations(
    *, coach_id: int, now: Optional[datetime] = None
) -> Dict[str, int]:
    """
    Send an extra *manual* notification to every student who is still pending
    confirmation for tomorrow's classes. Students who already confirmed or
    declined are never targeted (they are not in the pending set).

    Reuses the existing notification engine (``send_manual_notifications``) so
    behaviour, templating and messaging stay consistent with automatic sends.
    Sending an extra nudge does not change the pending count — the students are
    still pending until they respond.
    """
    from padel_app.services.notification_service import send_manual_notifications

    targets = get_pending_confirmation_targets(coach_id=coach_id, now=now)
    sent = 0
    for instance_id, player_ids in targets:
        events = send_manual_notifications(instance_id, player_ids, coach_id)
        sent += len(events)
    return {"instances": len(targets), "sent": sent}
