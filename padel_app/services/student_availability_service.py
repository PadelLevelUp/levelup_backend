"""
Student availability blockers.

A student marks windows when they are unavailable so the smart notification
engine will NOT send them AUTOMATIC class invitations during those windows.

Implementation note (PAD-28): rather than a dedicated StudentBlocker table, we
REUSE the existing `CalendarBlock` model (already user-scoped, one-time AND
recurring, with full CRUD + recurrence machinery). Student blockers are
`CalendarBlock` rows with `type="unavailable"` and
`blocks_auto_invitations=True`. This keeps them visible on the shared calendar
feed for free and avoids duplicating recurrence logic.

Timezone (locked decision): datetimes are stored as UTC (naive == UTC, matching
LessonInstance storage). Recurring-occurrence expansion is evaluated against the
club / Lisbon timezone so that "every Monday 18:00-20:00" tracks Lisbon wall
clock, not UTC.
"""

from datetime import timedelta, datetime, timezone
from zoneinfo import ZoneInfo

from padel_app.models import CalendarBlock
from padel_app.tools.calendar_tools import expand_occurrences, ensure_utc
from padel_app.services.calendar_service import (
    add_event_service,
    edit_event_service,
    remove_block_service,
)


CLUB_TZ = ZoneInfo("Europe/Lisbon")

# Marker values that identify a student availability blocker.
BLOCKER_TYPE = "unavailable"


# ---------------------------------------------------------------------------
# CRUD (thin wrappers over calendar_service, forcing blocker semantics)
# ---------------------------------------------------------------------------

def list_student_blockers(user_id):
    """Return all availability blockers owned by the user, newest first."""
    return (
        CalendarBlock.query
        .filter(
            CalendarBlock.user_id == user_id,
            CalendarBlock.blocks_auto_invitations.is_(True),
        )
        .order_by(CalendarBlock.start_datetime.desc())
        .all()
    )


def create_student_blocker(user_id, data):
    """
    Create an availability blocker for the student.

    `data` matches the frontend add_event payload:
      { title, date, startTime, endTime, isRecurring, recurrenceRule, endDate }
    We force type="unavailable" and blocks_auto_invitations=True.
    """
    payload = dict(data)
    payload["type"] = BLOCKER_TYPE
    block = add_event_service(user_id, payload)
    block.blocks_auto_invitations = True
    block.type = BLOCKER_TYPE
    block.save()
    return block


def update_student_blocker(block_id, user_id, data):
    """Edit an availability blocker owned by the student."""
    # Ownership is enforced by edit_event_service (filter_by user_id).
    payload = dict(data)
    payload["type"] = BLOCKER_TYPE
    block = edit_event_service(block_id, user_id, payload)
    block.blocks_auto_invitations = True
    block.type = BLOCKER_TYPE
    block.save()
    return block


def delete_student_blocker(block_id, user_id, occ_date=None, scope="all"):
    """Delete an availability blocker (scope-aware for recurring series)."""
    remove_block_service(block_id, user_id, occ_date, scope)


# ---------------------------------------------------------------------------
# Eligibility suppression — used by the notification engine
# ---------------------------------------------------------------------------

def _windows_overlap(a_start, a_end, b_start, b_end):
    """Half-open overlap test: [a_start, a_end) intersects [b_start, b_end)."""
    return a_start < b_end and b_start < a_end


def user_is_blocked_for_window(user_id, window_start, window_end):
    """
    True if the user has an availability blocker (blocks_auto_invitations=True)
    whose one-time or recurring occurrence overlaps [window_start, window_end).

    Recurrence occurrences are expanded and compared in the club (Lisbon)
    timezone, per the locked timezone decision.
    """
    if user_id is None:
        return False

    win_start = ensure_utc(window_start)
    win_end = ensure_utc(window_end)
    if win_start is None or win_end is None:
        return False

    # Widen the expansion range by a day on each side so a block that starts
    # just before the window (but overlaps it) is still surfaced.
    range_start = win_start - timedelta(days=1)
    range_end = win_end + timedelta(days=1)

    blocks = (
        CalendarBlock.query
        .filter(
            CalendarBlock.user_id == user_id,
            CalendarBlock.blocks_auto_invitations.is_(True),
        )
        .all()
    )

    for block in blocks:
        duration = (block.end_datetime - block.start_datetime)
        occurrences = expand_occurrences(
            block.start_datetime,
            block.recurrence_rule,
            block.recurrence_end,
            range_start,
            range_end,
        )
        for occ_start in occurrences:
            occ_start = ensure_utc(occ_start)
            occ_end = occ_start + duration
            if _windows_overlap(win_start, win_end, occ_start, occ_end):
                return True

    return False


def filter_blocked_coach_players(coach_players, instance):
    """
    Given a list of Association_CoachPlayer candidates for an auto-invitation,
    drop any whose owning User has an availability blocker overlapping the
    instance window. Only affects AUTO invitations (this helper is not called
    on the manual path).
    """
    if not coach_players:
        return coach_players

    window_start = instance.start_datetime
    window_end = instance.end_datetime

    kept = []
    for cp in coach_players:
        player = cp.player
        user_id = player.user_id if player else None
        if user_is_blocked_for_window(user_id, window_start, window_end):
            continue
        kept.append(cp)
    return kept
