from __future__ import annotations

from padel_app.helpers.dashboard.messages import compute_message_overview
from padel_app.helpers.dashboard.coach import build_coach_dashboard_blocks
from padel_app.helpers.dashboard.player import build_player_dashboard_blocks


def build_dashboard_payload(*, user, coach: Optional[object], player: Optional[object]) -> Dict[str, Any]:
    """
    Orchestrator: returns either coach or player dashboard.

    Args:
        user: Current user
        coach: Coach model or None
        player: Player model or None

    Returns:
        Dashboard payload dict
    """
    unread_messages, conversations_to_reply, latest = compute_message_overview(user_id=user.id)

    base_blocks = [
        {
            "id": "messages",
            "type": "messages_overview",
            "data": {
                "unreadMessages": unread_messages,
                "conversationsToReply": conversations_to_reply,
                "latest": latest,
                "href": "/messages",
            },
        }
    ]

    if coach is not None:
        role_blocks = build_coach_dashboard_blocks(coach=coach, user_id=user.id)
        dashboard_id = "coach_default_v1"
        title = "Dashboard"
    else:
        role_blocks = build_player_dashboard_blocks(player=player)
        dashboard_id = "player_default_v1"
        title = "Dashboard"

    return {
        "id": dashboard_id,
        "title": title,
        "blocks": base_blocks + role_blocks,
    }