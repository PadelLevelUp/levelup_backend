from __future__ import annotations

from typing import Any, Dict, List

from padel_app.tools.tools import _parse_range_or_default

from padel_app.helpers.dashboard.events import build_dashboard_event_lists
from padel_app.helpers.dashboard.kpis import compute_player_kpis


def build_player_dashboard_blocks(*, player) -> List[Dict[str, Any]]:
    """
    Build player-specific dashboard blocks:
      - KPI grid (attendance, missed, upcoming, invites)
      - Upcoming lessons list
      - Invites to confirm list (or empty depending on event data availability)
    """
    range_start, range_end = _parse_range_or_default()

    scheduled_count, upcoming_items, invites_items = build_dashboard_event_lists(
        player_id=player.id,
        range_start=range_start,
        range_end=range_end,
    )

    kpis = compute_player_kpis(player_id=player.id)

    return [
        {
            "id": "kpis",
            "type": "kpi_grid",
            "data": {
                "items": [
                    # NOTE (PAD-76): `href` is only set when a matching frontend
                    # route actually exists. There is no invites page
                    # (`/invites`) yet, so that KPI ships without a link and
                    # renders as an inert card instead of dropping the player on
                    # the 404 page.
                    #
                    # PAD-114 gave "Attended" a destination: `/attendance` is the
                    # student's attendance-history page and this KPI is its
                    # dashboard entry point (dashboard.navigation rule 11). It
                    # counts the same `status == "present"` presences the page
                    # charts, so the two can never disagree.
                    {
                        "label": "Attended",
                        "value": int(kpis.lessons_attended),
                        "icon": "check_circle",
                        "href": "/attendance",
                    },
                    # "Missed" stays inert: the attendance page shows attendance
                    # only and is explicitly NOT a missed-classes page, so PAD-76
                    # still applies here.
                    {
                        "label": "Missed",
                        "value": int(kpis.lessons_missed),
                        "icon": "x_circle",
                    },
                    {
                        "label": "Upcoming lessons",
                        "value": int(kpis.upcoming_lessons),
                        "icon": "calendar",
                        "href": "/calendar",
                    },
                    {
                        "label": "Invites",
                        "value": int(kpis.invites_to_confirm),
                        "icon": "mail",
                    },
                ]
            },
        },
        {
            "id": "lists",
            "type": "grid",
            "data": {
                "cols": {"base": 1, "lg": 2},
                "children": [
                    {
                        "id": "player_upcoming",
                        "type": "class_list",
                        "data": {
                            "title": "Your upcoming lessons",
                            "items": upcoming_items,
                        },
                    },
                    {
                        "id": "player_invites",
                        "type": "class_list",
                        "data": {
                            "title": "Invites to confirm",
                            "icon": "user_plus",
                            "emptyText": "No pending invites",
                            "items": invites_items,
                        },
                    },
                ],
            },
        },
    ]
