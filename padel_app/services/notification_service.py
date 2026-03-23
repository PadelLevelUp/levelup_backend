"""
Notification engine service.

Handles reminders, vacancy-based invitations, waiting list, and manual notifications:

  Config helpers
  - get_or_create_config / get_config_dict / update_config

  Reminder flow
  - send_class_reminders(instance_id)           called by APScheduler at reminder time
  - respond_to_reminder(...)                     player presses Yes/No on reminder

  Invitation flow
  - trigger_invitations(instance, coach_id)      main trigger (called by scheduler or manually)
  - process_invitation_batches()                 recurring APScheduler job (every 2 min)
  - respond_to_notification(...)                 player presses Yes/No on invite
  - coach_respond_to_notification(...)           coach manually records a response

  Manual notifications
  - send_manual_notifications(...)               coach hand-picks players

  Waiting list
  - respond_to_waiting_list(...)                 player responds to waiting list offer
  - get_waiting_list(instance_id, coach_id)      list active waiting list entries

  Notification groups (manual modal)
  - get_notification_groups(...)

  Activity feed
  - get_notification_activity(coach_id)
"""

from __future__ import annotations

from datetime import datetime, timedelta

from padel_app.sql_db import db
from padel_app.models import (
    Association_CoachLessonInstance,
    Association_CoachPlayer,
    Association_PlayerLessonInstance,
    LessonInstance,
    NotificationConfig,
    NotificationEvent,
    Presence,
    Vacancy,
    WaitingListEntry,
)
from padel_app.models.standing_waiting_list_entry import StandingWaitingListEntry
from padel_app.models.notification_config import (
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_NOTIFICATION_GROUPS,
    DEFAULT_PRIORITY_CRITERIA,
    DEFAULT_RESTRICTIONS,
    DEFAULT_ROUNDS,
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
        "reminderTiming": config.reminder_timing,
        "invitationStartTiming": config.get_invitation_start_timing(),
        "invitationGroups": config.get_invitation_groups(),
        "tiebreakers": config.get_tiebreakers(),
    }


def update_config(coach_id: int, data: dict) -> NotificationConfig:
    config = get_or_create_config(coach_id)

    timing_changed = False

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
    if "reminderTiming" in data:
        config.reminder_timing = data["reminderTiming"]
        timing_changed = True
    if "invitationStartTiming" in data:
        config.invitation_start_timing = data["invitationStartTiming"]
        timing_changed = True
    if "invitationGroups" in data:
        config.invitation_groups = data["invitationGroups"]
    if "tiebreakers" in data:
        config.tiebreakers = data["tiebreakers"]

    config.save()

    if timing_changed:
        try:
            from padel_app.scheduler import reschedule_all_future_jobs
            reschedule_all_future_jobs(coach_id)
        except Exception:
            pass  # scheduler may not be running (tests, etc.)

    return config


# ---------------------------------------------------------------------------
# Student ranking helpers
# ---------------------------------------------------------------------------

def _level_sort_key(coach_player: Association_CoachPlayer) -> int:
    if coach_player.level:
        return coach_player.level.display_order
    return 9999


def _attendance_stats(player_id: int) -> tuple[float, float]:
    presences = Presence.query.filter_by(player_id=player_id).all()
    if not presences:
        return 0.0, 0.0
    total = len(presences)
    present = sum(1 for p in presences if p.status == "present")
    justified = sum(1 for p in presences if p.status == "absent" and p.justification == "justified")
    return present / total, justified / total


def _build_sort_key(criteria: list[dict], player_stats: dict):
    enabled = [c["id"] for c in criteria if c.get("enabled")]

    def key(cp: Association_CoachPlayer):
        parts = []
        stats = player_stats.get(cp.player_id, {})
        for criterion in enabled:
            if criterion == "level":
                parts.append(_level_sort_key(cp))
            elif criterion == "justified_misses":
                parts.append(-stats.get("justified_miss_rate", 0.0))
            elif criterion == "attendance":
                parts.append(-stats.get("attendance_rate", 0.0))
            elif criterion == "playing_side":
                parts.append(0 if cp.side == "left" else 1)
            elif criterion == "subscription_status":
                parts.append(0 if getattr(cp, "player", None) and cp.player.user.status == "active" else 1)
        return tuple(parts)

    return key


def _unjustified_absence_count(player_id: int, coach_id: int) -> int:
    """Count unjustified absences for a player across all of this coach's class instances."""
    coach_instance_ids = {
        rel.lesson_instance_id
        for rel in Association_CoachLessonInstance.query.filter_by(coach_id=coach_id).all()
    }
    if not coach_instance_ids:
        return 0
    return Presence.query.filter(
        Presence.player_id == player_id,
        Presence.lesson_instance_id.in_(coach_instance_ids),
        Presence.justification == "unjustified",
    ).count()


# ---------------------------------------------------------------------------
# Invitation group helpers
# ---------------------------------------------------------------------------

def _has_makeups(player_id: int, coach_id: int) -> bool:
    """True when a player has more justified absences than accepted invitations for this coach."""
    coach_instance_ids = {
        rel.lesson_instance_id
        for rel in Association_CoachLessonInstance.query.filter_by(coach_id=coach_id).all()
    }
    if not coach_instance_ids:
        return False
    justified = Presence.query.filter(
        Presence.player_id == player_id,
        Presence.lesson_instance_id.in_(coach_instance_ids),
        Presence.justification == "justified",
    ).count()
    accepted = NotificationEvent.query.filter_by(
        player_id=player_id, coach_id=coach_id, status="confirmed"
    ).count()
    return justified > accepted


def _level_ids_one_above(vacancy_level, coach_id: int) -> set:
    """Return the set of level IDs that are one step above vacancy_level (closest higher level)."""
    from padel_app.models.coach_levels import CoachLevel
    levels_above = [
        lv for lv in CoachLevel.query.filter_by(coach_id=coach_id).all()
        if lv.display_order < vacancy_level.display_order
    ]
    if not levels_above:
        return set()
    max_order = max(lv.display_order for lv in levels_above)
    return {lv.id for lv in levels_above if lv.display_order == max_order}


def _level_ids_one_below(vacancy_level, coach_id: int) -> set:
    """Return the set of level IDs that are one step below vacancy_level (closest lower level)."""
    from padel_app.models.coach_levels import CoachLevel
    levels_below = [
        lv for lv in CoachLevel.query.filter_by(coach_id=coach_id).all()
        if lv.display_order > vacancy_level.display_order
    ]
    if not levels_below:
        return set()
    min_order = min(lv.display_order for lv in levels_below)
    return {lv.id for lv in levels_below if lv.display_order == min_order}


def _compare(value, op: str, threshold) -> bool:
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return True
    if op == "less_than":               return value < threshold
    if op == "less_than_or_equal":      return value <= threshold
    if op == "equals":                  return value == threshold
    if op == "greater_than":            return value > threshold
    if op == "greater_than_or_equal":   return value >= threshold
    return True


def _passes_group_rules(rules: list, cp: Association_CoachPlayer, vacancy: Vacancy, coach_id: int) -> bool:
    """Apply all rules in an invitation group with AND logic."""
    for rule in rules:
        attr = rule.get("attribute")
        op = rule.get("operation")
        val = rule.get("value")

        if attr == "level":
            if vacancy.level_id is None or vacancy.level is None:
                continue  # No level on vacancy → skip this filter
            if cp.level is None:
                return False
            vd = vacancy.level.display_order
            cd = cp.level.display_order
            if op == "same_as_vacancy":
                if cp.level_id != vacancy.level_id:
                    return False
            elif op == "one_above_vacancy":
                if cp.level_id not in _level_ids_one_above(vacancy.level, coach_id):
                    return False
            elif op == "one_below_vacancy":
                if cp.level_id not in _level_ids_one_below(vacancy.level, coach_id):
                    return False
            elif op == "all_above_vacancy":
                if cd >= vd:
                    return False
            elif op == "all_below_vacancy":
                if cd <= vd:
                    return False

        elif attr == "side":
            if vacancy.side is None:
                continue
            if op == "same_as_vacancy" and cp.side != vacancy.side:
                return False

        elif attr == "has_makeups":
            if op == "is_true" and not _has_makeups(cp.player_id, coach_id):
                return False

        elif attr == "unjustified_absences":
            count = _unjustified_absence_count(cp.player_id, coach_id)
            if not _compare(count, op, val):
                return False

        elif attr == "justified_absences":
            _, just_rate = _attendance_stats(cp.player_id)
            total_presences = Presence.query.filter_by(player_id=cp.player_id).count()
            just_count = round(just_rate * total_presences)
            if not _compare(just_count, op, val):
                return False

        elif attr == "attendance_rate":
            att_rate, _ = _attendance_stats(cp.player_id)
            if not _compare(att_rate * 100, op, val):
                return False

        elif attr == "subscription_status":
            status = cp.player.user.status if cp.player and cp.player.user else None
            if op == "equals" and status != val:
                return False

    return True


def _get_eligible_students_for_group(
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
    group_index: int,
) -> list[Association_CoachPlayer]:
    """Like get_eligible_students but uses invitation group rules instead of round criteria."""
    invitation_groups = config.get_invitation_groups()
    idx = group_index - 1  # 1-indexed → 0-indexed
    if idx < 0 or idx >= len(invitation_groups):
        return []
    group = invitation_groups[idx]
    rules = group.get("rules", [])

    enrolled_ids = {rel.player_id for rel in instance.players_relations}
    active_invite_ids = {
        e.player_id
        for e in NotificationEvent.query.filter(
            NotificationEvent.vacancy_id == vacancy.id,
            NotificationEvent.status.in_(["sent", "queued", "confirmed"]),
        ).all()
    }
    excluded_ids = enrolled_ids | active_invite_ids

    coach_players = [
        cp for cp in Association_CoachPlayer.query.filter_by(coach_id=coach_id).all()
        if cp.player_id not in excluded_ids
        and _passes_group_rules(rules, cp, vacancy, coach_id)
    ]

    player_stats = {}
    for cp in coach_players:
        att_rate, just_rate = _attendance_stats(cp.player_id)
        player_stats[cp.player_id] = {"attendance_rate": att_rate, "justified_miss_rate": just_rate}

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats)
    return sorted(coach_players, key=sort_key)


# ---------------------------------------------------------------------------
# Eligible students — new criteria-based version
# ---------------------------------------------------------------------------

def get_eligible_students(
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
    round_number: int,
) -> list[Association_CoachPlayer]:
    """
    Returns coach_player relations for students eligible for the given vacancy and round,
    ranked by the configured priority criteria.
    """
    # Players already enrolled in this instance
    enrolled_ids = {rel.player_id for rel in instance.players_relations}

    # Players with an active (non-expired) invitation for THIS vacancy
    active_invite_ids = {
        e.player_id
        for e in NotificationEvent.query.filter(
            NotificationEvent.vacancy_id == vacancy.id,
            NotificationEvent.status.in_(["sent", "queued", "confirmed"]),
        ).all()
    }

    excluded_ids = enrolled_ids | active_invite_ids

    coach_players = [
        cp for cp in Association_CoachPlayer.query.filter_by(coach_id=coach_id).all()
        if cp.player_id not in excluded_ids
    ]

    # Apply round criteria filters
    rounds = config.get_rounds()
    round_cfg = next((r for r in rounds if r["id"] == round_number), None)
    if round_cfg is None:
        return []

    criteria = round_cfg.get("criteria", [])
    criteria_values = round_cfg.get("criteria_values", {})

    for criterion in criteria:
        if criterion == "same_level":
            if vacancy.level_id is not None:
                coach_players = [cp for cp in coach_players if cp.level_id == vacancy.level_id]

        elif criterion == "same_side":
            if vacancy.side is not None:
                coach_players = [cp for cp in coach_players if cp.side == vacancy.side]

        elif criterion == "max_unjustified_absences":
            max_abs = criteria_values.get("max_unjustified_absences", 0)
            coach_players = [
                cp for cp in coach_players
                if _unjustified_absence_count(cp.player_id, coach_id) <= max_abs
            ]

    # Build stats and rank
    player_stats = {}
    for cp in coach_players:
        att_rate, just_rate = _attendance_stats(cp.player_id)
        player_stats[cp.player_id] = {
            "attendance_rate": att_rate,
            "justified_miss_rate": just_rate,
        }

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats)
    return sorted(coach_players, key=sort_key)


# ---------------------------------------------------------------------------
# Restriction checks
# ---------------------------------------------------------------------------

def _check_restrictions(
    instance: LessonInstance,
    coach_id: int,
    restrictions: dict,
    *,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.utcnow()

    if restrictions.get("quietHours", {}).get("enabled"):
        hour = now.hour
        if hour >= 22 or hour < 7:
            return False

    min_time = restrictions.get("minTimeBeforeClass", {})
    if min_time.get("enabled"):
        minutes_until = (instance.start_datetime - now).total_seconds() / 60
        if minutes_until < min_time["value"]:
            return False

    max_total = restrictions.get("maxTotal", {})
    if max_total.get("enabled"):
        already_sent = NotificationEvent.query.filter_by(
            lesson_instance_id=instance.id,
        ).filter(NotificationEvent.status.in_(["sent", "queued", "confirmed"])).count()
        if already_sent >= max_total["value"]:
            return False

    return True


def _check_per_student_daily_limit(
    player_id: int,
    coach_id: int,
    restrictions: dict,
    *,
    now: datetime | None = None,
) -> bool:
    limit = restrictions.get("maxInvitesPerStudentPerDay", {})
    if not limit.get("enabled"):
        return True
    _now = now or datetime.utcnow()
    today_start = _now.replace(hour=0, minute=0, second=0, microsecond=0)
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


def _user_id_for_player(player_id: int) -> int | None:
    from padel_app.models import Player
    player = Player.query.get(player_id)
    return player.user_id if player else None


def _effective_filled_spots(instance: LessonInstance) -> int:
    enrolled = len(instance.players_relations)
    absent = sum(1 for p in instance.presences if p.status == "absent")
    return max(0, enrolled - absent)


def _add_player_to_instance(player_id: int, instance: LessonInstance) -> None:
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
    vacancy_id: int | None = None,
) -> None:
    """
    Mark all other 'sent' events as expired, update their invite messages,
    and send the spot-filled message. Scoped to vacancy_id when provided.
    """
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    spot_filled_text = templates.get("spot_filled", DEFAULT_MESSAGE_TEMPLATES["spot_filled"])

    query = NotificationEvent.query.filter(
        NotificationEvent.status == "sent",
        NotificationEvent.id != confirmed_event_id,
    )
    if vacancy_id is not None:
        query = query.filter(NotificationEvent.vacancy_id == vacancy_id)
    else:
        query = query.filter(NotificationEvent.lesson_instance_id == instance.id)

    pending_events = query.all()

    for other_event in pending_events:
        other_player_user_id = _user_id_for_player(other_event.player_id)
        if not other_player_user_id:
            continue

        if other_event.message_id:
            invite_msg = Message.query.get(other_event.message_id)
            if invite_msg and invite_msg.msg_metadata is not None:
                invite_msg.msg_metadata = {
                    **invite_msg.msg_metadata,
                    "responded": True,
                    "response": "spot_filled",
                }
                invite_msg.save()
                publish({"type": "message_edited", "payload": serialize_message(invite_msg, None)})

        _send_system_message(coach_user_id, other_player_user_id, spot_filled_text)
        other_event.status = "expired"
        other_event.save()
        publish({
            "type": "notification_responded",
            "payload": {
                "lessonInstanceId": instance.id,
                "notificationEventId": other_event.id,
                "response": "spot_filled",
            },
        })


# ---------------------------------------------------------------------------
# Vacancy helpers
# ---------------------------------------------------------------------------

def _create_vacancy_for_absent_player(
    instance: LessonInstance,
    coach_id: int,
    absent_player_id: int,
) -> Vacancy:
    cp = Association_CoachPlayer.query.filter_by(
        coach_id=coach_id, player_id=absent_player_id
    ).first()
    side = cp.side if cp else None
    level_id = cp.level_id if cp else None

    vacancy = Vacancy(
        lesson_instance_id=instance.id,
        coach_id=coach_id,
        original_player_id=absent_player_id,
        side=side,
        level_id=level_id,
        status="open",
    )
    vacancy.create()
    return vacancy


def _create_structural_vacancies(instance: LessonInstance, coach_id: int) -> list[Vacancy]:
    """
    Create Vacancy records for spots that are open because the class was never
    fully enrolled (no 'departing' player to snapshot from).
    """
    existing_count = Vacancy.query.filter_by(
        lesson_instance_id=instance.id,
    ).filter(Vacancy.status.in_(["open", "filled"])).count()

    open_spots = instance.max_players - _effective_filled_spots(instance)
    spots_to_create = max(0, open_spots - existing_count)

    vacancies = []
    for _ in range(spots_to_create):
        v = Vacancy(
            lesson_instance_id=instance.id,
            coach_id=coach_id,
            original_player_id=None,
            side=None,
            level_id=instance.level_id,
            status="open",
        )
        v.create()
        vacancies.append(v)
    return vacancies


# ---------------------------------------------------------------------------
# Reminder flow
# ---------------------------------------------------------------------------

def send_class_reminders(instance_id: int, *, now: datetime | None = None) -> None:
    """
    Send 'Are you coming?' messages to all enrolled players.
    Called by APScheduler at the configured reminder time.

    Pass ``now`` in tests to control the current time without waiting for real time to pass.
    """
    from padel_app.models import Coach

    from flask import current_app, has_app_context
    _log = current_app.logger if has_app_context() else None

    _now = now or datetime.utcnow()

    instance = LessonInstance.query.get(instance_id)
    if not instance:
        if _log:
            _log.warning("send_class_reminders: instance %s not found — skipping", instance_id)
        return
    if instance.status in ("canceled", "completed"):
        if _log:
            _log.info("send_class_reminders: instance %s status=%s — skipping", instance_id, instance.status)
        return
    if instance.start_datetime <= _now:
        if _log:
            _log.info("send_class_reminders: instance %s start_datetime in the past — skipping", instance_id)
        return

    player_count = len(list(instance.players_relations))
    if _log:
        _log.info(
            "send_class_reminders: instance=%s start=%s players=%d — sending",
            instance_id, instance.start_datetime, player_count,
        )

    if player_count == 0:
        if _log:
            _log.info("send_class_reminders: instance %s has no enrolled players — nothing to send", instance_id)
        return

    # Find the coach for this instance
    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=instance_id
    ).first()
    if not coach_rel:
        if _log:
            _log.warning("send_class_reminders: instance %s has no coach association — skipping", instance_id)
        return

    coach = Coach.query.get(coach_rel.coach_id)
    if not coach:
        return

    coach_user_id = coach.user_id
    config = get_or_create_config(coach.id)
    templates = config.get_message_templates()

    level_code = instance.level.code if getattr(instance, "level", None) else "this"
    weekday = instance.start_datetime.strftime("%A") if instance.start_datetime else ""
    time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

    for rel in instance.players_relations:
        player_id = rel.player_id
        player_user_id = _user_id_for_player(player_id)
        if not player_user_id or not coach_user_id:
            continue

        # Ensure a Presence record exists for this player
        existing_presence = Presence.query.filter_by(
            player_id=player_id,
            lesson_instance_id=instance_id,
        ).first()
        if not existing_presence:
            Presence(
                lesson_instance_id=instance_id,
                player_id=player_id,
                invited=True,
                confirmed=False,
            ).create()

        from padel_app.models import Player
        player = Player.query.get(player_id)
        player_name = (player.user.name if player and player.user else "there").split()[0]

        text = _format_template(
            templates.get("reminder", DEFAULT_MESSAGE_TEMPLATES["reminder"]),
            name=player_name,
            level=level_code,
            weekday=weekday,
            time=time_str,
        )

        _send_system_message(
            coach_user_id=coach_user_id,
            player_user_id=player_user_id,
            text=text,
            message_type="notification_reminder",
            msg_metadata={
                "lessonInstanceId": instance_id,
                "responded": False,
            },
        )


def respond_to_reminder(
    lesson_instance_id: int,
    action: str,
    acting_user_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    """
    Called when a player presses Yes or No on a reminder message.
    action: "yes" | "no"
    """
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(lesson_instance_id)

    player = Player.query.filter_by(user_id=acting_user_id).first()
    if not player:
        from flask import abort
        abort(403)

    presence = Presence.query.filter_by(
        player_id=player.id,
        lesson_instance_id=lesson_instance_id,
    ).first()

    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=lesson_instance_id
    ).first()
    coach = Coach.query.get(coach_rel.coach_id) if coach_rel else None
    coach_user_id = coach.user_id if coach else None

    config = get_or_create_config(coach.id) if coach else None
    templates = config.get_message_templates() if config else DEFAULT_MESSAGE_TEMPLATES

    # Mark the reminder message as responded so the frontend shows the badge on reload
    if coach_user_id:
        from padel_app.models import Message
        from padel_app.serializers.message import serialize_message
        conv = _get_or_create_direct_conversation(coach_user_id, acting_user_id)
        recent_reminders = Message.query.filter_by(
            conversation_id=conv.id,
            message_type="notification_reminder",
        ).order_by(Message.id.desc()).all()
        reminder_msg = next(
            (m for m in recent_reminders
             if m.msg_metadata
             and m.msg_metadata.get("lessonInstanceId") == lesson_instance_id
             and not m.msg_metadata.get("responded")),
            None,
        )
        if reminder_msg:
            reminder_msg.msg_metadata = {
                **reminder_msg.msg_metadata,
                "responded": True,
                "response": action,
            }
            reminder_msg.save()
            publish({"type": "message_edited", "payload": serialize_message(reminder_msg, None)})

    if action == "yes":
        if presence:
            presence.confirmed = True
            # status intentionally not set — only the coach marks someone as present
            presence.save()
        if coach_user_id:
            _send_system_message(
                coach_user_id,
                acting_user_id,
                templates.get("reminder_confirm", DEFAULT_MESSAGE_TEMPLATES["reminder_confirm"]),
            )
        return {"action": "confirmed"}

    elif action == "no":
        if presence:
            presence.confirmed = True
            presence.status = "absent"
            presence.justification = "justified"
            presence.save()
        if coach_user_id:
            _send_system_message(
                coach_user_id,
                acting_user_id,
                templates.get("reminder_decline", DEFAULT_MESSAGE_TEMPLATES["reminder_decline"]),
            )

        # Always pre-create vacancy so the invite_start job finds it when window opens.
        # If the invitation window is already open, trigger invitations immediately.
        if coach and config:
            from padel_app.scheduler import _compute_invite_start_dt
            _ensure_vacancy_for_player(instance, coach.id, player.id)
            invite_start_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
            _now = now or datetime.utcnow()
            if invite_start_dt is None or _now >= invite_start_dt:
                trigger_invitations(instance, coach.id)

        return {"action": "declined"}

    return {"action": "unknown"}


def _trigger_vacancy_for_player(
    instance: LessonInstance,
    coach_id: int,
    player_id: int,
) -> None:
    """Create a vacancy for a player who declined and immediately trigger invitations."""
    # Avoid duplicate vacancies for the same departing player
    existing = Vacancy.query.filter_by(
        lesson_instance_id=instance.id,
        original_player_id=player_id,
        status="open",
    ).first()
    if not existing:
        _create_vacancy_for_absent_player(instance, coach_id, player_id)
    trigger_invitations(instance, coach_id)


def _ensure_vacancy_for_player(
    instance: LessonInstance,
    coach_id: int,
    player_id: int,
) -> None:
    """Create a vacancy for an absent player without triggering invitations.
    Used when the invitation window hasn't opened yet — the invite_start scheduler
    job will call trigger_invitations when the window opens."""
    existing = Vacancy.query.filter_by(
        lesson_instance_id=instance.id,
        original_player_id=player_id,
        status="open",
    ).first()
    if not existing:
        _create_vacancy_for_absent_player(instance, coach_id, player_id)


# ---------------------------------------------------------------------------
# Invitation batch helpers
# ---------------------------------------------------------------------------

def _send_invitation_batch(
    vacancy: Vacancy,
    instance: LessonInstance,
    config: NotificationConfig,
    coach_id: int,
    max_sim_override: int | None = None,
) -> list[dict]:
    """
    Send the next batch of invitations for this vacancy.
    Returns list of {id, name} for players notified.
    """
    from padel_app.models import Coach, Player

    # Check waiting list before doing a fresh invite round
    wl_entry = _check_waiting_list(vacancy, instance, coach_id, config, vacancy.current_round_number)
    if wl_entry:
        _fill_from_waiting_list(wl_entry, vacancy, instance, coach_id, config)
        return [{"id": str(wl_entry.player_id), "name": "waiting_list"}]

    invitation_groups = config.get_invitation_groups()
    if invitation_groups:
        eligible = _get_eligible_students_for_group(vacancy, instance, coach_id, config, vacancy.current_round_number)
    else:
        eligible = get_eligible_students(vacancy, instance, coach_id, config, vacancy.current_round_number)

    if not eligible:
        _advance_round(vacancy, instance, coach_id, config)
        return []

    restrictions = config.get_restrictions()

    # Determine batch size
    if max_sim_override is not None:
        batch_size = max_sim_override
    else:
        max_sim = restrictions.get("maxSimultaneous", {})
        batch_size = max_sim["value"] if max_sim.get("enabled") else len(eligible)

    # Respect maxTotal across ALL vacancies for this instance
    max_total = restrictions.get("maxTotal", {})
    if max_total.get("enabled"):
        already_sent = NotificationEvent.query.filter(
            NotificationEvent.lesson_instance_id == instance.id,
            NotificationEvent.status.in_(["sent", "queued", "confirmed"]),
        ).count()
        remaining_budget = max_total["value"] - already_sent
        if remaining_budget <= 0:
            return []
        batch_size = min(batch_size, remaining_budget)

    coach_obj = Coach.query.get(coach_id)
    coach_user_id = coach_obj.user_id if coach_obj else None
    templates = config.get_message_templates()
    level_code = instance.level.code if getattr(instance, "level", None) else "this"
    weekday = instance.start_datetime.strftime("%A") if instance.start_datetime else ""
    time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

    notified = []
    for cp in eligible[:batch_size]:
        if not _check_per_student_daily_limit(cp.player_id, coach_id, restrictions):
            continue
        player_user_id = _user_id_for_player(cp.player_id)
        if not coach_user_id or not player_user_id:
            continue

        player = Player.query.get(cp.player_id)
        player_name = (player.user.name if player and player.user else "Player").split()[0]

        event = NotificationEvent(
            coach_id=coach_id,
            lesson_instance_id=instance.id,
            player_id=cp.player_id,
            vacancy_id=vacancy.id,
            type="auto",
            round_number=vacancy.current_round_number,
            status="sent",
        )
        event.create()

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
                "lessonInstanceId": instance.id,
                "vacancyId": vacancy.id,
                "responded": False,
            },
        )
        event.message_id = msg.id
        event.save()

        notified.append({"id": str(cp.player_id), "name": player_name})

    vacancy.last_activity_at = datetime.utcnow()
    vacancy.current_batch_number += 1
    vacancy.save()

    return notified


def _advance_round(
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
) -> None:
    """Move vacancy to next round or expire it."""
    vacancy.current_round_number += 1
    vacancy.save()

    invitation_groups = config.get_invitation_groups()
    max_count = len(invitation_groups) if invitation_groups else len(config.get_rounds())
    if vacancy.current_round_number > max_count:
        vacancy.status = "expired"
        vacancy.save()
        return

    # Check waiting list for new round, then send fresh batch
    wl_entry = _check_waiting_list(vacancy, instance, coach_id, config, vacancy.current_round_number)
    if wl_entry:
        _fill_from_waiting_list(wl_entry, vacancy, instance, coach_id, config)
    else:
        _send_invitation_batch(vacancy, instance, config, coach_id)


def _send_next_on_decline(
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
) -> None:
    """After a decline, immediately invite the next single eligible player."""
    _send_invitation_batch(vacancy, instance, config, coach_id, max_sim_override=1)


# ---------------------------------------------------------------------------
# Main invitation trigger
# ---------------------------------------------------------------------------

def trigger_invitations(
    instance: LessonInstance,
    coach_id: int,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """
    Main entry point to start filling open spots.
    Finds or creates vacancies and sends the first invitation batch for each.
    Returns list of {id, name} for players notified in round 1.

    Pass ``now`` in tests to control the current time without waiting for real time to pass.
    """
    config = get_or_create_config(coach_id)

    if not config.auto_notify_enabled:
        return []
    if not instance.notifications_enabled:
        return []

    restrictions = config.get_restrictions()
    if not _check_restrictions(instance, coach_id, restrictions, now=now):
        return []

    # Find existing open vacancies or create new ones
    open_vacancies = Vacancy.query.filter_by(
        lesson_instance_id=instance.id,
        status="open",
    ).all()

    if not open_vacancies:
        # Create vacancies from absent presences
        absent_ids = {
            p.player_id for p in instance.presences if p.status == "absent"
        }
        # Avoid duplicates — check which absent players already have vacancies
        existing_vacancy_player_ids = {
            v.original_player_id
            for v in Vacancy.query.filter_by(lesson_instance_id=instance.id).all()
            if v.original_player_id is not None
        }
        for player_id in absent_ids - existing_vacancy_player_ids:
            open_vacancies.append(_create_vacancy_for_absent_player(instance, coach_id, player_id))

        # Also create structural vacancies (spots never filled)
        open_vacancies.extend(_create_structural_vacancies(instance, coach_id))

    if not open_vacancies:
        return []

    all_notified: list[dict] = []
    for vacancy in open_vacancies:
        notified = _send_invitation_batch(vacancy, instance, config, coach_id)
        all_notified.extend(notified)

    if all_notified:
        publish({
            "type": "notify_sent",
            "payload": {
                "lessonInstanceId": instance.id,
                "count": len(all_notified),
                "type": "auto",
            },
        })

    return all_notified


# ---------------------------------------------------------------------------
# Recurring batch processor (called by APScheduler every 2 minutes)
# ---------------------------------------------------------------------------

def process_invitation_batches(*, now: datetime | None = None) -> int:
    """
    For each open vacancy, check if enough time has passed since last activity.
    If so, send the next invitation batch.
    Returns count of vacancies where a batch was sent.

    Pass ``now`` in tests to control the current time without waiting for real time to pass.
    """
    _now = now or datetime.utcnow()
    open_vacancies = Vacancy.query.filter_by(status="open").all()
    processed = 0

    for vacancy in open_vacancies:
        instance = vacancy.lesson_instance

        # Skip past or canceled classes
        if instance.start_datetime <= _now:
            vacancy.status = "expired"
            vacancy.save()
            continue
        if instance.status in ("canceled", "completed"):
            vacancy.status = "expired"
            vacancy.save()
            continue

        config = get_or_create_config(vacancy.coach_id)
        restrictions = config.get_restrictions()

        last = vacancy.last_activity_at

        # Fresh vacancy (no batch sent yet) — trigger immediately
        if last is None:
            _send_invitation_batch(vacancy, instance, config, vacancy.coach_id)
            processed += 1
            continue

        # Check inactivity timer
        max_inactive = restrictions.get("maxInactiveTime", {})
        if max_inactive.get("enabled"):
            threshold = timedelta(minutes=max_inactive["value"])
            if _now - last >= threshold:
                _send_invitation_batch(vacancy, instance, config, vacancy.coach_id)
                processed += 1

    return processed


# ---------------------------------------------------------------------------
# Respond to notification (player presses Yes / No on invite)
# ---------------------------------------------------------------------------

def respond_to_notification(notification_event_id: int, action: str, acting_user_id: int) -> dict:
    from flask import abort
    from padel_app.models import Coach, Message, Player
    from padel_app.serializers.message import serialize_message

    event = NotificationEvent.query.get_or_404(notification_event_id)

    player = Player.query.get(event.player_id)
    if not player or player.user_id != acting_user_id:
        abort(403, "Not authorized to respond to this notification")

    config = get_or_create_config(event.coach_id)
    templates = config.get_message_templates()

    coach = Coach.query.get(event.coach_id)
    coach_user_id = coach.user_id if coach else None
    player_user_id = acting_user_id

    # Mark original invite message as responded
    if event.message_id:
        invite_msg = Message.query.get(event.message_id)
        if invite_msg and invite_msg.msg_metadata is not None:
            invite_msg.msg_metadata = {
                **invite_msg.msg_metadata,
                "responded": True,
                "response": action,
            }
            invite_msg.save()
            publish({"type": "message_edited", "payload": serialize_message(invite_msg, None)})

    instance = event.lesson_instance
    vacancy = event.vacancy

    if action == "no":
        event.status = "expired"
        event.save()

        if vacancy:
            vacancy.last_activity_at = datetime.utcnow()
            vacancy.save()
            # Immediately invite the next player without waiting for inactivity timer
            _send_next_on_decline(vacancy, instance, event.coach_id, config)

        if coach_user_id:
            _send_system_message(
                coach_user_id,
                player_user_id,
                templates.get("decline", DEFAULT_MESSAGE_TEMPLATES["decline"]),
            )

        publish({
            "type": "notification_responded",
            "payload": {
                "lessonInstanceId": instance.id,
                "notificationEventId": event.id,
                "response": "no",
            },
        })
        return {"action": "declined"}

    elif action == "yes":
        # Check vacancy status first
        if vacancy and vacancy.status != "open":
            event.status = "expired"
            event.save()
            if coach_user_id:
                _send_system_message(
                    coach_user_id,
                    player_user_id,
                    templates.get("spot_filled", DEFAULT_MESSAGE_TEMPLATES["spot_filled"]),
                )
            _offer_waiting_list(event.player_id, instance, event.coach_id, templates)
            publish({
                "type": "notification_responded",
                "payload": {
                    "lessonInstanceId": instance.id,
                    "notificationEventId": event.id,
                    "response": "spot_filled",
                },
            })
            return {"action": "spot_filled_waiting_list_offered"}

        # Re-check capacity
        if _effective_filled_spots(instance) >= instance.max_players:
            event.status = "expired"
            event.save()
            if coach_user_id:
                _send_system_message(
                    coach_user_id,
                    player_user_id,
                    templates.get("spot_filled", DEFAULT_MESSAGE_TEMPLATES["spot_filled"]),
                )
            _offer_waiting_list(event.player_id, instance, event.coach_id, templates)
            publish({
                "type": "notification_responded",
                "payload": {
                    "lessonInstanceId": instance.id,
                    "notificationEventId": event.id,
                    "response": "spot_filled",
                },
            })
            return {"action": "spot_filled_waiting_list_offered"}

        # Fill the spot
        _add_player_to_instance(event.player_id, instance)
        event.status = "confirmed"
        event.save()

        if vacancy:
            vacancy.status = "filled"
            vacancy.filled_by_player_id = event.player_id
            vacancy.filled_at = datetime.utcnow()
            vacancy.save()

        if coach_user_id:
            _send_system_message(
                coach_user_id,
                player_user_id,
                templates.get("confirm", DEFAULT_MESSAGE_TEMPLATES["confirm"]),
            )
            _broadcast_spot_filled(
                instance,
                event.id,
                coach_user_id,
                templates,
                vacancy_id=vacancy.id if vacancy else None,
            )

        publish({
            "type": "notification_responded",
            "payload": {
                "lessonInstanceId": instance.id,
                "notificationEventId": event.id,
                "response": "yes",
            },
        })
        return {"action": "confirmed"}

    return {"action": "unknown"}


def coach_respond_to_notification(notification_event_id: int, action: str, coach_id: int) -> dict:
    from flask import abort

    event = NotificationEvent.query.get_or_404(notification_event_id)
    if event.coach_id != coach_id:
        abort(403, "Not authorized")

    instance = event.lesson_instance
    vacancy = event.vacancy

    if action == "no":
        event.status = "expired"
        event.save()
        if vacancy:
            vacancy.last_activity_at = datetime.utcnow()
            vacancy.save()
        return {"action": "declined"}

    elif action == "yes":
        if vacancy and vacancy.status != "open":
            event.status = "expired"
            event.save()
            return {"action": "spot_filled"}

        if _effective_filled_spots(instance) >= instance.max_players:
            event.status = "expired"
            event.save()
            return {"action": "spot_filled"}

        _add_player_to_instance(event.player_id, instance)
        event.status = "confirmed"
        event.save()

        if vacancy:
            vacancy.status = "filled"
            vacancy.filled_by_player_id = event.player_id
            vacancy.filled_at = datetime.utcnow()
            vacancy.save()

        # Expire other pending invitations for this vacancy
        other_events = NotificationEvent.query.filter(
            NotificationEvent.vacancy_id == vacancy.id if vacancy else
            NotificationEvent.lesson_instance_id == instance.id,
            NotificationEvent.status == "sent",
            NotificationEvent.id != event.id,
        ).all()
        for other in other_events:
            other.status = "expired"
            other.save()

        return {"action": "confirmed"}

    return {"action": "unknown"}


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

        event = NotificationEvent(
            coach_id=coach_id,
            lesson_instance_id=instance_id,
            player_id=player_id,
            type="manual",
            round_number=1,
            status="sent",
        )
        event.create()

        if coach_user_id and player_user_id:
            player = Player.query.get(player_id)
            player_name = (player.user.name if player and player.user else "there").split()[0]
            level_code = instance.level.code if getattr(instance, "level", None) else "this"
            weekday = instance.start_datetime.strftime("%A") if instance.start_datetime else ""
            time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

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
# Waiting list
# ---------------------------------------------------------------------------

def _offer_waiting_list(
    player_id: int,
    instance: LessonInstance,
    coach_id: int,
    templates: dict,
) -> None:
    """Send a waiting-list offer message to the player."""
    from padel_app.models import Coach
    coach = Coach.query.get(coach_id)
    if not coach:
        return
    player_user_id = _user_id_for_player(player_id)
    if not player_user_id:
        return

    text = templates.get("waiting_list_offer", DEFAULT_MESSAGE_TEMPLATES["waiting_list_offer"])
    _send_system_message(
        coach_user_id=coach.user_id,
        player_user_id=player_user_id,
        text=text,
        message_type="waiting_list_offer",
        msg_metadata={
            "lessonInstanceId": instance.id,
            "responded": False,
        },
    )


def respond_to_waiting_list(
    lesson_instance_id: int,
    action: str,
    acting_user_id: int,
) -> dict:
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(lesson_instance_id)

    player = Player.query.filter_by(user_id=acting_user_id).first()
    if not player:
        from flask import abort
        abort(403)

    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=lesson_instance_id
    ).first()
    coach = Coach.query.get(coach_rel.coach_id) if coach_rel else None
    if not coach:
        return {"action": "unknown"}

    config = get_or_create_config(coach.id)
    templates = config.get_message_templates()

    if action == "yes":
        # Upsert waiting list entry
        existing = WaitingListEntry.query.filter_by(
            lesson_instance_id=lesson_instance_id,
            player_id=player.id,
        ).first()
        if existing:
            existing.is_active = True
            existing.save()
        else:
            WaitingListEntry(
                lesson_instance_id=lesson_instance_id,
                player_id=player.id,
                coach_id=coach.id,
            ).create()

        if coach.user_id:
            _send_system_message(
                coach_user_id=coach.user_id,
                player_user_id=acting_user_id,
                text=templates.get("waiting_list_confirm", DEFAULT_MESSAGE_TEMPLATES["waiting_list_confirm"]),
            )
        return {"action": "added_to_waiting_list"}

    elif action == "no":
        return {"action": "declined"}

    return {"action": "unknown"}


def get_waiting_list(instance_id: int, coach_id: int) -> list[dict]:
    entries = WaitingListEntry.query.filter_by(
        lesson_instance_id=instance_id,
        coach_id=coach_id,
        is_active=True,
    ).all()
    result = []
    for e in entries:
        player = e.player
        user = player.user if player else None
        result.append({
            "id": e.id,
            "playerId": e.player_id,
            "playerName": user.name if user else None,
            "joinedAt": e.joined_at.isoformat() if e.joined_at else None,
        })
    return result


def _check_waiting_list(
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
    round_number: int,
) -> WaitingListEntry | None:
    """
    Return the highest-priority waiting list entry that meets the current round's criteria,
    or None if the waiting list is empty / no match.
    """
    entries = WaitingListEntry.query.filter_by(
        lesson_instance_id=instance.id,
        coach_id=coach_id,
        is_active=True,
    ).all()
    if not entries:
        return None

    invitation_groups = config.get_invitation_groups()

    # Filter entries — when invitation groups are configured, skip round-criteria filtering
    eligible_entries = []
    for entry in entries:
        # Check if linked standing entry is still valid
        if entry.standing_entry_id:
            standing = StandingWaitingListEntry.query.get(entry.standing_entry_id)
            if standing and (not standing.is_active or standing.expires_at < datetime.utcnow()):
                _deactivate_standing_entry(standing)
                continue

        cp = Association_CoachPlayer.query.filter_by(
            coach_id=coach_id, player_id=entry.player_id
        ).first()
        if not cp:
            continue

        if invitation_groups:
            # All active waiting list entries compete; no group-criteria filter
            eligible_entries.append((entry, cp))
        else:
            # Legacy rounds-based filter
            rounds = config.get_rounds()
            round_cfg = next((r for r in rounds if r["id"] == round_number), None)
            if round_cfg is None:
                continue
            criteria = round_cfg.get("criteria", [])
            criteria_values = round_cfg.get("criteria_values", {})
            passes = True
            for criterion in criteria:
                if criterion == "same_level":
                    if vacancy.level_id is not None and cp.level_id != vacancy.level_id:
                        passes = False
                        break
                elif criterion == "same_side":
                    if vacancy.side is not None and cp.side != vacancy.side:
                        passes = False
                        break
                elif criterion == "max_unjustified_absences":
                    max_abs = criteria_values.get("max_unjustified_absences", 0)
                    if _unjustified_absence_count(entry.player_id, coach_id) > max_abs:
                        passes = False
                        break
            if passes:
                eligible_entries.append((entry, cp))

    if not eligible_entries:
        return None

    # Rank by priority ordering
    player_stats = {}
    for entry, cp in eligible_entries:
        att_rate, just_rate = _attendance_stats(entry.player_id)
        player_stats[entry.player_id] = {
            "attendance_rate": att_rate,
            "justified_miss_rate": just_rate,
        }

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats)
    eligible_entries.sort(key=lambda pair: sort_key(pair[1]))

    return eligible_entries[0][0]


def _fill_from_waiting_list(
    entry: WaitingListEntry,
    vacancy: Vacancy,
    instance: LessonInstance,
    coach_id: int,
    config: NotificationConfig,
) -> None:
    from padel_app.models import Coach

    _add_player_to_instance(entry.player_id, instance)

    vacancy.status = "filled"
    vacancy.filled_by_player_id = entry.player_id
    vacancy.filled_at = datetime.utcnow()
    vacancy.save()

    entry.is_active = False
    entry.save()

    # Credit the standing entry, deactivate when cap reached
    if entry.standing_entry_id:
        standing = StandingWaitingListEntry.query.get(entry.standing_entry_id)
        if standing and standing.is_active:
            standing.credits_used += 1
            standing.save()
            if standing.credits_used >= standing.credits_total:
                _deactivate_standing_entry(standing)

    coach = Coach.query.get(coach_id)
    if not coach:
        return

    player_user_id = _user_id_for_player(entry.player_id)
    if not player_user_id:
        return

    templates = config.get_message_templates()
    level_code = instance.level.code if getattr(instance, "level", None) else "this"
    weekday = instance.start_datetime.strftime("%A") if instance.start_datetime else ""
    time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

    text = _format_template(
        templates.get("waiting_list_placed", DEFAULT_MESSAGE_TEMPLATES["waiting_list_placed"]),
        level=level_code,
        weekday=weekday,
        time=time_str,
    )
    _send_system_message(
        coach_user_id=coach.user_id,
        player_user_id=player_user_id,
        text=text,
        message_type="waiting_list_placed",
    )

    publish({
        "type": "notification_responded",
        "payload": {
            "lessonInstanceId": instance.id,
            "vacancyId": vacancy.id,
            "response": "waiting_list_filled",
        },
    })


# ---------------------------------------------------------------------------
# Notification groups (manual notify modal)
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
    config = get_or_create_config(coach_id)
    groups_config = config.get_notification_groups()
    enabled_groups = [g for g in groups_config if g.get("enabled")]

    already_notified_ids: set[int] = set()
    if model.lower() == "lessoninstance":
        obj = LessonInstance.query.get(original_id)
        if obj is None:
            return []
        level_id = obj.level_id or (obj.lesson.default_level_id if obj.lesson else None)
        enrolled_ids = {rel.player_id for rel in obj.players_relations}
        already_notified_ids = {
            e.player_id
            for e in NotificationEvent.query.filter(
                NotificationEvent.lesson_instance_id == obj.id,
                NotificationEvent.status.in_(["sent", "queued", "confirmed"]),
            ).all()
        }
    else:
        from padel_app.models import Lesson
        obj = Lesson.query.get(original_id)
        if obj is None:
            return []
        level_id = obj.default_level_id
        enrolled_ids = {rel.player_id for rel in obj.players_relations}

    all_coach_players = [
        cp for cp in Association_CoachPlayer.query.filter_by(coach_id=coach_id).all()
        if cp.player_id not in enrolled_ids and cp.player_id not in already_notified_ids
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
# Standing waiting list
# ---------------------------------------------------------------------------

def _deactivate_standing_entry(entry: StandingWaitingListEntry) -> None:
    """Deactivate a standing entry and all its linked per-class WaitingListEntry rows."""
    entry.is_active = False
    entry.save()
    linked = WaitingListEntry.query.filter_by(
        standing_entry_id=entry.id, is_active=True
    ).all()
    for wle in linked:
        wle.is_active = False
        wle.save()


def _fan_out_standing_entry(entry: StandingWaitingListEntry) -> None:
    """Create per-class WaitingListEntry rows for all upcoming instances for this coach."""
    now = datetime.utcnow()
    coach_instance_ids = {
        rel.lesson_instance_id
        for rel in Association_CoachLessonInstance.query.filter_by(coach_id=entry.coach_id).all()
    }
    for instance_id in coach_instance_ids:
        instance = LessonInstance.query.get(instance_id)
        if not instance:
            continue
        if instance.start_datetime <= now:
            continue
        if instance.status in ("canceled", "completed"):
            continue
        existing = WaitingListEntry.query.filter_by(
            lesson_instance_id=instance_id,
            player_id=entry.player_id,
            is_active=True,
        ).first()
        if existing:
            continue
        WaitingListEntry(
            lesson_instance_id=instance_id,
            player_id=entry.player_id,
            coach_id=entry.coach_id,
            standing_entry_id=entry.id,
        ).create()


def add_standing_waiting_list_entry(
    coach_id: int, player_id: int, credits_total: int, duration_days: int
) -> StandingWaitingListEntry:
    """Add (or replace) a standing waiting list entry for a player."""
    # Deactivate any existing active entry for this coach/player pair
    existing = StandingWaitingListEntry.query.filter_by(
        coach_id=coach_id, player_id=player_id, is_active=True
    ).first()
    if existing:
        _deactivate_standing_entry(existing)

    entry = StandingWaitingListEntry(
        coach_id=coach_id,
        player_id=player_id,
        credits_total=credits_total,
        credits_used=0,
        expires_at=datetime.utcnow() + timedelta(days=duration_days),
        is_active=True,
    )
    entry.create()
    _fan_out_standing_entry(entry)
    return entry


def remove_standing_waiting_list_entry(entry_id: int, coach_id: int) -> None:
    """Remove a standing waiting list entry and deactivate all linked per-class entries."""
    from flask import abort
    entry = StandingWaitingListEntry.query.get_or_404(entry_id)
    if entry.coach_id != coach_id:
        abort(403, "Not authorized")
    _deactivate_standing_entry(entry)


def get_standing_waiting_list(coach_id: int) -> list[dict]:
    """Return all active standing waiting list entries for this coach."""
    entries = StandingWaitingListEntry.query.filter_by(
        coach_id=coach_id, is_active=True
    ).all()
    result = []
    for e in entries:
        player = e.player
        user = player.user if player else None
        active_class_count = WaitingListEntry.query.filter_by(
            standing_entry_id=e.id, is_active=True
        ).count()
        result.append({
            "id": e.id,
            "playerId": e.player_id,
            "playerName": user.name if user else None,
            "creditsUsed": e.credits_used,
            "creditsTotal": e.credits_total,
            "expiresAt": e.expires_at.isoformat() if e.expires_at else None,
            "createdAt": e.created_at.isoformat() if e.created_at else None,
            "activeClassCount": active_class_count,
        })
    return result


def _sync_standing_entries_for_new_instance(instance: LessonInstance, coach_id: int) -> None:
    """Called when a new instance is created — add it to all active standing entries."""
    active_entries = StandingWaitingListEntry.query.filter_by(
        coach_id=coach_id, is_active=True
    ).all()
    for entry in active_entries:
        existing = WaitingListEntry.query.filter_by(
            lesson_instance_id=instance.id,
            player_id=entry.player_id,
            is_active=True,
        ).first()
        if existing:
            continue
        WaitingListEntry(
            lesson_instance_id=instance.id,
            player_id=entry.player_id,
            coach_id=coach_id,
            standing_entry_id=entry.id,
        ).create()


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
            "vacancyId": e.vacancy_id,
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
