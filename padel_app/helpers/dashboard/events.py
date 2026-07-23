from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from padel_app.helpers.calendar_helpers import (
    build_lesson_events,
    load_lessons_for_coach,
    load_lesson_instances_for_coach,
    load_lessons_for_player,
    load_lesson_instances_for_player,
)

from padel_app.tools.tools import _date_label, _safe_int


def build_dashboard_event_lists(
    *,
    range_start,
    range_end,
    coach_id: Optional[int] = None,
    player_id: Optional[int] = None,
) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Build scheduled lesson lists for the dashboard.

    Coach mode:
      - upcoming_items: top 5 scheduled by earliest
      - secondary list: "needs players" (top 5 by missing seats desc, then earliest)

    Player mode:
      - upcoming_items: top 5 scheduled by earliest
      - secondary list: "invites to confirm" (top 5 earliest) OR empty if you prefer

    Returns:
        (scheduled_count, primary_items, secondary_items)
    """
    if (coach_id is None) == (player_id is None):
        raise ValueError("Provide exactly one of coach_id or player_id")

    # ----------------------------
    # Load lessons + instances by role
    # ----------------------------
    if coach_id is not None:
        lessons = load_lessons_for_coach(coach_id, range_start, range_end)
        instances_by_key = load_lesson_instances_for_coach(coach_id, range_start, range_end)
    else:
        lessons = load_lessons_for_player(player_id, range_start, range_end)
        instances_by_key = load_lesson_instances_for_player(player_id, range_start, range_end)

    lesson_events = build_lesson_events(lessons, instances_by_key, range_start, range_end)

    scheduled = [e for e in lesson_events if e.get("status") == "scheduled"]
    scheduled_sorted = sorted(scheduled, key=_event_dt)
    upcoming_top5 = scheduled_sorted[:5]
    upcoming_items = [_to_list_item(e, with_missing_badge=False) for e in upcoming_top5]

    # ----------------------------
    # Secondary list differs by role
    # ----------------------------
    if coach_id is not None:
        needs_players = [
            e for e in scheduled
            if _safe_int(e.get("participantCount"), 0) < _safe_int(e.get("maxPlayers"), 0)
        ]

        needs_players_sorted = sorted(
            needs_players,
            key=lambda e: (-_missing_seats(e), _event_dt(e)),
        )[:5]

        secondary_items = [_to_list_item(e, with_missing_badge=True) for e in needs_players_sorted]
    else:
        # Player secondary list: invites to confirm (only if your serializer exposes presence flags)
        invites = [
            e for e in scheduled
            if (e.get("invited") is True) and (e.get("confirmed") is False)
        ]

        invites_sorted = sorted(invites, key=_event_dt)[:5]
        secondary_items = [_to_list_item(e, with_missing_badge=False) for e in invites_sorted]
        # If you want a badge like "Confirm", you can adapt _to_list_item or post-process here.

    return len(scheduled), upcoming_items, secondary_items


def _event_dt(e: Dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(f"{e['date']}T{e['startTime']}")
    except Exception:
        return datetime.max.replace(tzinfo=None)


def _missing_seats(e: Dict[str, Any]) -> int:
    return _safe_int(e.get("maxPlayers"), 0) - _safe_int(e.get("participantCount"), 0)


def _calendar_href(e: Dict[str, Any]) -> str:
    """
    Deep link into the calendar for one specific occurrence (dashboard.navigation rule 8).

    Both params are required: `classId` is the calendar event id (so the calendar can open
    the right ClassDetailSheet), and `date` is the occurrence date. The date cannot be
    derived from the id — a materialized instance id is just `lessoninstance-<pk>` — and the
    calendar needs it to select the week the class falls in, which is often not the week it
    shows by default.
    """
    params = {"classId": str(e.get("id") or ""), "date": str(e.get("date") or "")}
    return f"/calendar?{urlencode(params)}"


def _to_list_item(e: Dict[str, Any], *, with_missing_badge: bool) -> Dict[str, Any]:
    count = _safe_int(e.get("participantCount"), 0)
    max_players = _safe_int(e.get("maxPlayers"), 0)
    missing = max_players - count

    return {
        "id": str(e.get("id") or ""),
        "title": e.get("title") or "",
        "dateLabel": _date_label(e.get("date")),
        "timeLabel": e.get("startTime") or "",
        "color": e.get("color"),
        "rightLabel": f"{count}/{max_players}" if max_players else None,
        "badge": f"Missing {missing}" if with_missing_badge and missing > 0 else None,
        "href": _calendar_href(e),
    }
