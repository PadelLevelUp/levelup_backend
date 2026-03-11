"""
Notification engine service.

Handles auto/manual student notification when spots open in a class:
  - get_or_create_config  / get_config_dict / update_config
  - get_eligible_students  (priority-ranked list)
  - send_manual_notifications
  - trigger_auto_notifications  (Round 1 fires immediately; later rounds stored as queued)
  - process_queued_rounds       (call periodically via cron / /notify/process_rounds)
  - get_notification_activity
"""

from __future__ import annotations

from datetime import datetime, timedelta

from padel_app.sql_db import db
from padel_app.models import (
    Association_CoachPlayer,
    Association_PlayerLessonInstance,
    LessonInstance,
    NotificationConfig,
    NotificationEvent,
    Presence,
)
from padel_app.models.notification_config import (
    DEFAULT_PRIORITY_CRITERIA,
    DEFAULT_RESTRICTIONS,
    DEFAULT_ROUNDS,
    DEFAULT_NOTIFICATION_GROUPS,
    DEFAULT_MESSAGE_TEMPLATES,
)
from padel_app.realtime import publish
from padel_app.utils.push_notifications import send_push_notification


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_or_create_config(coach_id: int) -> NotificationConfig:
    config = NotificationConfig.query.filter_by(coach_id=coach_id).first()
    if config is None:
        config = NotificationConfig(
            coach_id=coach_id,
            auto_notify_enabled=False,
            priority_criteria=DEFAULT_PRIORITY_CRITERIA,
            restrictions=DEFAULT_RESTRICTIONS,
            rounds=DEFAULT_ROUNDS,
        )
        config.create()
    return config


def get_config_dict(coach_id: int) -> dict:
    config = get_or_create_config(coach_id)
    return {
        "autoNotifyEnabled": config.auto_notify_enabled,
        "priorityCriteria": config.get_priority_criteria(),
        "restrictions": config.get_restrictions(),
        "rounds": config.get_rounds(),
        "notificationGroups": config.get_notification_groups(),
        "messageTemplates": config.get_message_templates(),
    }


def update_config(coach_id: int, data: dict) -> NotificationConfig:
    config = get_or_create_config(coach_id)

    if "autoNotifyEnabled" in data:
        config.auto_notify_enabled = bool(data["autoNotifyEnabled"])
    if "priorityCriteria" in data:
        config.priority_criteria = data["priorityCriteria"]
    if "restrictions" in data:
        config.restrictions = data["restrictions"]
    if "rounds" in data:
        config.rounds = data["rounds"]
    if "notificationGroups" in data:
        config.notification_groups = data["notificationGroups"]
    if "messageTemplates" in data:
        config.message_templates = data["messageTemplates"]

    config.save()
    return config


# ---------------------------------------------------------------------------
# Student ranking
# ---------------------------------------------------------------------------

def _level_sort_key(coach_player: Association_CoachPlayer) -> int:
    """Lower display_order = higher level = better (sort ascending)."""
    if coach_player.level:
        return coach_player.level.display_order
    return 9999


def _attendance_stats(player_id: int) -> tuple[float, float]:
    """Returns (attendance_rate, justified_miss_rate) for a player across all classes."""
    presences = Presence.query.filter_by(player_id=player_id).all()
    if not presences:
        return 0.0, 0.0
    total = len(presences)
    present = sum(1 for p in presences if p.status == "present")
    justified = sum(1 for p in presences if p.status == "absent" and p.justification == "justified")
    return present / total, justified / total


def _build_sort_key(criteria: list[dict], player_stats: dict):
    """
    Returns a function(coach_player) -> tuple that can be used as sort key.
    Lower tuple value = higher priority.
    """
    enabled = [c["id"] for c in criteria if c.get("enabled")]

    def key(cp: Association_CoachPlayer):
        parts = []
        stats = player_stats.get(cp.player_id, {})
        for criterion in enabled:
            if criterion == "level":
                parts.append(_level_sort_key(cp))
            elif criterion == "justified_misses":
                # Fewer justified misses → better (lower ratio = better)
                parts.append(-stats.get("justified_miss_rate", 0.0))
            elif criterion == "attendance":
                # Higher attendance → better (negate so lower = better after sort)
                parts.append(-stats.get("attendance_rate", 0.0))
            elif criterion == "playing_side":
                parts.append(0 if cp.side == "left" else 1)
            elif criterion == "subscription_status":
                parts.append(0 if getattr(cp, "player", None) and cp.player.user.status == "active" else 1)
        return tuple(parts)

    return key


def get_eligible_students(
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
) -> list[Association_CoachPlayer]:
    """
    Returns coach_player relations for students eligible to fill an open spot,
    ranked by the configured priority criteria.
    """
    restrictions = config.get_restrictions()

    # IDs already in this class
    enrolled_ids = {
        rel.player_id for rel in instance.players_relations
    }

    # Coach's players not enrolled
    coach_players = [
        cp for cp in Association_CoachPlayer.query.filter_by(coach_id=coach_id).all()
        if cp.player_id not in enrolled_ids
    ]

    # Apply level deviation filter
    if restrictions.get("maxLevelDeviation", {}).get("enabled") and instance.level_id:
        max_dev = restrictions["maxLevelDeviation"]["value"]
        instance_order = instance.level.display_order if instance.level else None
        if instance_order is not None:
            coach_players = [
                cp for cp in coach_players
                if cp.level and abs(cp.level.display_order - instance_order) <= max_dev
            ]

    # Build per-player stats for sorting
    player_stats = {}
    for cp in coach_players:
        att_rate, just_rate = _attendance_stats(cp.player_id)
        player_stats[cp.player_id] = {
            "attendance_rate": att_rate,
            "justified_miss_rate": just_rate,
        }

    # Rank by configured priority criteria
    criteria = config.get_priority_criteria()
    sort_key = _build_sort_key(criteria, player_stats)
    return sorted(coach_players, key=sort_key)


# ---------------------------------------------------------------------------
# Restriction checks
# ---------------------------------------------------------------------------

def _check_restrictions(instance: LessonInstance, coach_id: int, restrictions: dict) -> bool:
    """
    Returns True if notifications are allowed given the current restrictions.
    """
    now = datetime.utcnow()

    # Quiet hours (22:00 – 07:00)
    if restrictions.get("quietHours", {}).get("enabled"):
        hour = now.hour
        if hour >= 22 or hour < 7:
            return False

    # Min time before class
    min_time = restrictions.get("minTimeBeforeClass", {})
    if min_time.get("enabled"):
        minutes_until = (instance.start_datetime - now).total_seconds() / 60
        if minutes_until < min_time["value"]:
            return False

    # Max total notifications already sent for this class
    max_total = restrictions.get("maxTotal", {})
    if max_total.get("enabled"):
        already_sent = NotificationEvent.query.filter_by(
            lesson_instance_id=instance.id,
        ).filter(NotificationEvent.status.in_(["sent", "queued", "confirmed"])).count()
        if already_sent >= max_total["value"]:
            return False

    return True


def _check_per_student_daily_limit(player_id: int, coach_id: int, restrictions: dict) -> bool:
    """Returns True if the student hasn't hit the daily invite limit."""
    limit = restrictions.get("maxInvitesPerStudentPerDay", {})
    if not limit.get("enabled"):
        return True
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    count = NotificationEvent.query.filter(
        NotificationEvent.player_id == player_id,
        NotificationEvent.coach_id == coach_id,
        NotificationEvent.created_at >= today_start,
    ).count()
    return count < limit["value"]


# ---------------------------------------------------------------------------
# Conversation / message helpers
# ---------------------------------------------------------------------------

def _format_template(template: str, **variables) -> str:
    for key, val in variables.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def _get_or_create_direct_conversation(coach_user_id: int, player_user_id: int):
    """Find or create a 1:1 conversation between the coach and player user."""
    from padel_app.models import Conversation, ConversationParticipant

    key = Conversation.build_participant_key([coach_user_id, player_user_id])
    conv = Conversation.query.filter_by(participant_key=key).first()
    if conv is None:
        conv = Conversation(participant_key=key, is_group=False)
        conv.create()
        for uid in sorted(set([coach_user_id, player_user_id])):
            ConversationParticipant(conversation_id=conv.id, user_id=uid).create()
    return conv


def _send_system_message(
    coach_user_id: int,
    player_user_id: int,
    text: str,
    message_type: str = "text",
    msg_metadata: dict | None = None,
):
    """Create a message in the coach<->player conversation and publish it via SSE."""
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    conv = _get_or_create_direct_conversation(coach_user_id, player_user_id)
    msg = Message(
        text=text,
        sender_id=coach_user_id,
        conversation_id=conv.id,
        message_type=message_type,
        msg_metadata=msg_metadata or {},
    )
    msg.create()

    publish({
        "type": "message_created",
        "payload": serialize_message(msg, None),
    })

    send_push_notification(
        user_id=player_user_id,
        title="New message",
        body=text[:100],
        url=f"/messages/{conv.id}",
    )

    return msg


# ---------------------------------------------------------------------------
# Manual notifications
# ---------------------------------------------------------------------------

def send_manual_notifications(
    instance_id: int, player_ids: list[int], coach_id: int
) -> list[NotificationEvent]:
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(instance_id)
    if not instance.notifications_enabled:
        return []
    config = get_or_create_config(coach_id)
    templates = config.get_message_templates()

    coach = Coach.query.get(coach_id)
    coach_user_id = coach.user_id if coach else None

    events = []
    for player_id in player_ids:
        player_user_id = _user_id_for_player(player_id)

        # Create event record first (we need its ID for message metadata)
        event = NotificationEvent(
            coach_id=coach_id,
            lesson_instance_id=instance_id,
            player_id=player_id,
            type="manual",
            round_number=1,
            status="sent",
        )
        event.create()

        # Send as a conversation message if we can resolve both user IDs
        if coach_user_id and player_user_id:
            player = Player.query.get(player_id)
            player_name = (player.user.name if player and player.user else "there").split()[0]
            level_code = instance.level.code if getattr(instance, "level", None) else "this"
            if instance.start_datetime:
                weekday = instance.start_datetime.strftime("%A")
                time_str = instance.start_datetime.strftime("%H:%M")
            else:
                weekday, time_str = "", ""

            text = _format_template(
                templates.get("invite", DEFAULT_MESSAGE_TEMPLATES["invite"]),
                name=player_name,
                level=level_code,
                weekday=weekday,
                time=time_str,
            )

            msg = _send_system_message(
                coach_user_id=coach_user_id,
                player_user_id=player_user_id,
                text=text,
                message_type="notification_invite",
                msg_metadata={
                    "notificationEventId": event.id,
                    "lessonInstanceId": instance_id,
                    "responded": False,
                },
            )
            # Link the message back to the event
            event.message_id = msg.id
            event.save()

        events.append(event)

    publish({
        "type": "notify_sent",
        "payload": {
            "lessonInstanceId": instance_id,
            "count": len(events),
            "type": "manual",
        },
    })

    return events


# ---------------------------------------------------------------------------
# Auto-trigger
# ---------------------------------------------------------------------------

def trigger_auto_notifications(instance: LessonInstance, coach_id: int) -> None:
    """
    Called after a presence is confirmed absent. Fires Round 1 immediately;
    queues subsequent rounds for processing by process_queued_rounds().
    """
    config = get_or_create_config(coach_id)

    if not config.auto_notify_enabled:
        return
    if not instance.notifications_enabled:
        return

    # Count open spots
    attending = sum(
        1 for p in instance.presences if p.status == "present"
    )
    open_spots = instance.max_players - attending
    if open_spots <= 0:
        return

    restrictions = config.get_restrictions()
    if not _check_restrictions(instance, coach_id, restrictions):
        return

    eligible = get_eligible_students(instance, coach_id, config)
    if not eligible:
        return

    rounds = config.get_rounds()
    max_sim = restrictions.get("maxSimultaneous", {}).get("value", 3)

    # Already-notified players for this instance (don't double-notify)
    already_notified = {
        e.player_id
        for e in NotificationEvent.query.filter_by(lesson_instance_id=instance.id).all()
    }

    # Split eligible students into round buckets
    remaining = [cp for cp in eligible if cp.player_id not in already_notified]

    for round_idx, round_cfg in enumerate(rounds):
        bucket = remaining[:max_sim]
        remaining = remaining[max_sim:]
        round_number = round_idx + 1

        if round_number == 1:
            # Fire immediately
            for cp in bucket:
                if not _check_per_student_daily_limit(cp.player_id, coach_id, restrictions):
                    continue
                send_push_notification(
                    user_id=_user_id_for_player(cp.player_id),
                    title="Spot available!",
                    body=f"A spot opened in {instance.title}. Tap to see details.",
                    url="/calendar",
                )
                NotificationEvent(
                    coach_id=coach_id,
                    lesson_instance_id=instance.id,
                    player_id=cp.player_id,
                    type="auto",
                    round_number=1,
                    status="sent",
                ).create()
        else:
            # Queue for later rounds
            for cp in bucket:
                NotificationEvent(
                    coach_id=coach_id,
                    lesson_instance_id=instance.id,
                    player_id=cp.player_id,
                    type="auto",
                    round_number=round_number,
                    status="queued",
                ).create()

        if not remaining:
            break

    publish({
        "type": "notify_sent",
        "payload": {
            "lessonInstanceId": instance.id,
            "count": len([cp for cp in eligible if cp.player_id not in already_notified]),
            "type": "auto",
        },
    })


# ---------------------------------------------------------------------------
# Process queued rounds (call from cron / /notify/process_rounds endpoint)
# ---------------------------------------------------------------------------

def process_queued_rounds() -> int:
    """
    Sends notifications for queued rounds whose wait time has elapsed.
    Returns the number of notifications sent.
    """
    queued = NotificationEvent.query.filter_by(status="queued").all()
    sent_count = 0

    for event in queued:
        config = get_or_create_config(event.coach_id)
        rounds = config.get_rounds()

        # Sum of durations for rounds before this one
        wait_minutes = sum(
            r["duration"] for r in rounds if r["id"] < event.round_number
        )

        due_at = event.created_at + timedelta(minutes=wait_minutes)
        if datetime.utcnow() < due_at:
            continue

        # Check if spot is still open
        instance = event.lesson_instance
        attending = sum(1 for p in instance.presences if p.status == "present")
        if attending >= instance.max_players:
            event.status = "expired"
            event.save()
            continue

        restrictions = config.get_restrictions()
        if not _check_per_student_daily_limit(event.player_id, event.coach_id, restrictions):
            event.status = "expired"
            event.save()
            continue

        send_push_notification(
            user_id=_user_id_for_player(event.player_id),
            title="Spot still available!",
            body=f"A spot is still open in {instance.title}. Tap to see details.",
            url="/calendar",
        )
        event.status = "sent"
        event.save()
        sent_count += 1

    return sent_count


# ---------------------------------------------------------------------------
# Notification groups (for manual notify modal)
# ---------------------------------------------------------------------------

def _students_with_recent_absences(coach_players: list, lookback: int = 8) -> list:
    result = []
    for cp in coach_players:
        recent = (
            Presence.query
            .filter_by(player_id=cp.player_id)
            .order_by(Presence.created_at.desc())
            .limit(lookback)
            .all()
        )
        if any(p.status == "absent" for p in recent):
            result.append(cp)
    return result


def _students_with_justified_absences(coach_players: list) -> list:
    result = []
    for cp in coach_players:
        has_justified = Presence.query.filter_by(
            player_id=cp.player_id, justification="justified"
        ).first()
        if has_justified:
            result.append(cp)
    return result


def _serialize_cp_for_group(cp: Association_CoachPlayer) -> dict:
    player = cp.player
    user = player.user if player else None
    return {
        "id": str(cp.player_id),
        "name": user.name if user else "Unknown",
        "levelCode": cp.level.code if cp.level else None,
        "levelId": str(cp.level_id) if cp.level_id else None,
    }


def get_notification_groups(
    model: str, original_id: int, date_str: str | None, coach_id: int
) -> list[dict]:
    """
    Returns pre-computed student groups for the notify modal.
    Groups are based on the coach's notificationGroups config.
    Students already enrolled in the class are excluded.
    """
    config = get_or_create_config(coach_id)
    groups_config = config.get_notification_groups()
    enabled_groups = [g for g in groups_config if g.get("enabled")]

    # Resolve level_id and enrolled_ids from the event
    if model.lower() == "lessoninstance":
        obj = LessonInstance.query.get(original_id)
        if obj is None:
            return []
        level_id = obj.level_id or (obj.lesson.default_level_id if obj.lesson else None)
        enrolled_ids = {rel.player_id for rel in obj.players_relations}
    else:
        from padel_app.models import Lesson
        obj = Lesson.query.get(original_id)
        if obj is None:
            return []
        level_id = obj.default_level_id
        enrolled_ids = {rel.player_id for rel in obj.players_relations}

    # All coach players not already enrolled
    all_coach_players = [
        cp for cp in Association_CoachPlayer.query.filter_by(coach_id=coach_id).all()
        if cp.player_id not in enrolled_ids
    ]

    result = []
    for group_config in enabled_groups:
        gid = group_config["id"]
        label = group_config["label"]

        if gid == "same_level":
            if not level_id:
                continue
            players = [cp for cp in all_coach_players if cp.level_id == level_id]
        elif gid == "recent_absences":
            players = _students_with_recent_absences(all_coach_players)
        elif gid == "justified_absences":
            players = _students_with_justified_absences(all_coach_players)
        elif gid == "all_students":
            players = all_coach_players
        else:
            continue

        if not players:
            continue

        result.append({
            "id": gid,
            "label": label,
            "players": [_serialize_cp_for_group(cp) for cp in players],
        })

    return result


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------

def get_notification_activity(coach_id: int, limit: int = 20) -> list[dict]:
    events = (
        NotificationEvent.query
        .filter_by(coach_id=coach_id)
        .order_by(NotificationEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for e in events:
        result.append({
            "id": e.id,
            "type": e.type,
            "roundNumber": e.round_number,
            "status": e.status,
            "createdAt": e.created_at.isoformat() if e.created_at else None,
            "lessonInstance": {
                "id": e.lesson_instance_id,
                "title": e.lesson_instance.title if e.lesson_instance else None,
                "startDatetime": e.lesson_instance.start_datetime.isoformat() if e.lesson_instance else None,
            },
            "player": {
                "id": e.player_id,
                "name": e.player.user.name if e.player and e.player.user else None,
            },
        })
    return result


# ---------------------------------------------------------------------------
# Respond to notification (player presses Yes / No)
# ---------------------------------------------------------------------------

def respond_to_notification(notification_event_id: int, action: str, acting_user_id: int) -> dict:
    """
    Called when a player presses Yes or No on a notification invite message.

    action: "yes" | "no"
    acting_user_id: the player's user_id (from JWT).
    Returns a dict with {"action": "confirmed" | "declined" | "spot_filled"}.
    """
    from flask import abort
    from padel_app.models import Coach, Message, Player
    from padel_app.serializers.message import serialize_message

    event = NotificationEvent.query.get_or_404(notification_event_id)

    # Security: verify the acting user is the player for this event
    player = Player.query.get(event.player_id)
    if not player or player.user_id != acting_user_id:
        abort(403, "Not authorized to respond to this notification")

    config = get_or_create_config(event.coach_id)
    templates = config.get_message_templates()

    coach = Coach.query.get(event.coach_id)
    coach_user_id = coach.user_id if coach else None
    player_user_id = acting_user_id

    # Mark the original invite message as responded
    if event.message_id:
        invite_msg = Message.query.get(event.message_id)
        if invite_msg and invite_msg.msg_metadata is not None:
            invite_msg.msg_metadata = {**invite_msg.msg_metadata, "responded": True}
            invite_msg.save()
            publish({"type": "message_edited", "payload": serialize_message(invite_msg, None)})

    instance = event.lesson_instance

    if action == "no":
        event.status = "expired"
        event.save()

        if coach_user_id:
            decline_text = templates.get("decline", DEFAULT_MESSAGE_TEMPLATES["decline"])
            _send_system_message(coach_user_id, player_user_id, decline_text)

        return {"action": "declined"}

    elif action == "yes":
        # Check capacity using enrolled count
        enrolled_count = Association_PlayerLessonInstance.query.filter_by(
            lesson_instance_id=instance.id
        ).count()

        if enrolled_count >= instance.max_players:
            # Spot already filled
            event.status = "expired"
            event.save()

            if coach_user_id:
                spot_filled_text = templates.get("spot_filled", DEFAULT_MESSAGE_TEMPLATES["spot_filled"])
                _send_system_message(coach_user_id, player_user_id, spot_filled_text)

            return {"action": "spot_filled"}

        # Add the player to the lesson instance
        _add_player_to_instance(event.player_id, instance)
        event.status = "confirmed"
        event.save()

        # Notify all other pending invitees that the spot is filled
        if coach_user_id:
            _broadcast_spot_filled(instance, event.id, coach_user_id, templates)

        return {"action": "confirmed"}

    return {"action": "unknown"}


def _add_player_to_instance(player_id: int, instance: LessonInstance) -> None:
    """Add a player to a lesson instance roster and create an accepted presence record."""
    existing_assoc = Association_PlayerLessonInstance.query.filter_by(
        player_id=player_id,
        lesson_instance_id=instance.id,
    ).first()
    if not existing_assoc:
        Association_PlayerLessonInstance(
            player_id=player_id,
            lesson_instance_id=instance.id,
        ).create()

    existing_presence = Presence.query.filter_by(
        player_id=player_id,
        lesson_instance_id=instance.id,
    ).first()
    if not existing_presence:
        Presence(
            lesson_instance_id=instance.id,
            player_id=player_id,
            invited=True,
            confirmed=True,
        ).create()


def _broadcast_spot_filled(
    instance: LessonInstance,
    confirmed_event_id: int,
    coach_user_id: int,
    templates: dict,
) -> None:
    """
    Mark all other 'sent' notification events for this instance as expired,
    update their invite messages to responded=True, and send the spot-filled message.
    """
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    spot_filled_text = templates.get("spot_filled", DEFAULT_MESSAGE_TEMPLATES["spot_filled"])

    pending_events = NotificationEvent.query.filter(
        NotificationEvent.lesson_instance_id == instance.id,
        NotificationEvent.status == "sent",
        NotificationEvent.id != confirmed_event_id,
    ).all()

    for other_event in pending_events:
        other_player_user_id = _user_id_for_player(other_event.player_id)
        if not other_player_user_id:
            continue

        # Mark invite message as responded
        if other_event.message_id:
            invite_msg = Message.query.get(other_event.message_id)
            if invite_msg and invite_msg.msg_metadata is not None:
                invite_msg.msg_metadata = {**invite_msg.msg_metadata, "responded": True}
                invite_msg.save()
                publish({"type": "message_edited", "payload": serialize_message(invite_msg, None)})

        _send_system_message(coach_user_id, other_player_user_id, spot_filled_text)

        other_event.status = "expired"
        other_event.save()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_id_for_player(player_id: int) -> int | None:
    from padel_app.models import Player  # avoid circular import at module level
    player = Player.query.get(player_id)
    return player.user_id if player else None
