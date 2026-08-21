from __future__ import annotations

from typing import Any, Dict, List

from padel_app.helpers.dashboard.coach_home import (
    build_needs_you_block,
    build_next_class_block,
    build_schedule_block,
    build_week_pulse_block,
)


def build_coach_dashboard_blocks(*, coach, user_id: int) -> List[Dict[str, Any]]:
    """
    Build coach-specific dashboard blocks, in the order the screen renders them:
      - Next class hero (omitted entirely when nothing is scheduled)
      - "Needs you" queue
      - Next 7 days
      - This week

    The old ``kpi_grid`` / ``pending_confirmations`` / ``notification_activity``
    blocks are gone. Players and Upcoming-classes were bare counts with nothing
    to act on and now live in ``week_pulse`` with a denominator; pending
    validation became a queue item that carries its own button and can reach
    zero. See helpers/dashboard/coach_home.py for the reasoning.
    """
    blocks: List[Dict[str, Any]] = []

    # Omitted rather than emptied: an empty hero would be the biggest element on
    # the screen saying nothing.
    hero = build_next_class_block(coach_id=coach.id)
    if hero is not None:
        blocks.append(hero)

    blocks.append(build_needs_you_block(coach_id=coach.id, user_id=user_id))
    blocks.append(build_schedule_block(coach_id=coach.id))
    blocks.append(build_week_pulse_block(coach_id=coach.id))

    return blocks
