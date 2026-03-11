from __future__ import annotations

from typing import Any, Dict, List

from padel_app.tools.tools import _parse_range_or_default

from padel_app.helpers.dashboard.events import build_dashboard_event_lists
from padel_app.helpers.dashboard.kpis import compute_coach_kpis
from padel_app.services.notification_service import get_notification_activity


def build_coach_dashboard_blocks(*, coach) -> List[Dict[str, Any]]:
    """
    Build coach-specific dashboard blocks:
      - KPI grid
      - Upcoming classes list
      - Needs players list
    """
    range_start, range_end = _parse_range_or_default()

    scheduled_count, upcoming_items, needs_items = build_dashboard_event_lists(
        coach_id=coach.id,
        range_start=range_start,
        range_end=range_end,
    )

    kpis = compute_coach_kpis(coach_id=coach.id, scheduled_count=scheduled_count)

    return [
        {
            "id": "kpis",
            "type": "kpi_grid",
            "data": {
                "items": [
                    {
                        "label": "Players",
                        "value": int(kpis.total_players),
                        "icon": "users",
                        "href": "/players",
                    },
                    {
                        "label": "Upcoming classes",
                        "value": int(kpis.scheduled_count),
                        "icon": "calendar",
                        "href": "/calendar",
                    },
                    {
                        "label": "Pending validation",
                        "value": int(kpis.pending_validations),
                        "icon": "clipboard_check",
                        "href": "/validations",
                    },
                    {
                        "label": "Revenue (est.)",
                        "value": int(kpis.monthly_revenue),
                        "prefix": "€",
                        "icon": "trending_up",
                        "href": "/revenue",
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
                        "id": "upcoming_classes",
                        "type": "class_list",
                        "data": {
                            "title": "Upcoming classes",
                            "items": upcoming_items,
                        },
                    },
                    {
                        "id": "needs_players",
                        "type": "class_list",
                        "data": {
                            "title": "Needs players",
                            "icon": "user_plus",
                            "emptyText": "All scheduled classes are full",
                            "items": needs_items,
                        },
                    },
                ],
            },
        },
        {
            "id": "notification_activity",
            "type": "notification_activity",
            "data": {
                "title": "Notification activity",
                "items": get_notification_activity(coach_id=coach.id, limit=8),
            },
        },
    ]
