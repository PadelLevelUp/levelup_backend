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
  - expire_stale_invitations()                   retire pending invites for classes that are over

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

import re
from datetime import datetime, timedelta

from padel_app.sql_db import db
from padel_app.utils.dates import utcnow_naive
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
    DEFAULT_NOTIFICATION_GROUPS,
    DEFAULT_PRIORITY_CRITERIA,
    DEFAULT_RESTRICTIONS,
    DEFAULT_ROUNDS,
    default_templates_for_locale,
    resolve_message_template,
)
from padel_app.realtime import publish
from padel_app.services.level_ladder import (
    get_level_ladder,
    ladder_index,
    ladder_index_map,
)
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
    from padel_app.models import Coach

    config = get_or_create_config(coach_id)
    locale = _resolve_locale(Coach.query.get(coach_id))
    return {
        "autoNotifyEnabled": config.auto_notify_enabled,
        "invitationMode": config.get_invitation_mode(),
        "priorityCriteria": config.get_priority_criteria(),
        "restrictions": config.get_restrictions(),
        "rounds": config.get_rounds(),
        "notificationGroups": config.get_notification_groups(),
        "messageTemplates": config.get_message_templates(locale),
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
    if "invitationMode" in data:
        mode = data["invitationMode"]
        if mode not in ("automatic", "semi_automatic"):
            from flask import abort
            abort(400, "invitationMode must be 'automatic' or 'semi_automatic'")
        config.invitation_mode = mode
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


def _is_semi_auto(config: NotificationConfig) -> bool:
    """True when the coach requires approval before invitations are sent."""
    return bool(config.auto_notify_enabled) and config.get_invitation_mode() == "semi_automatic"


# ---------------------------------------------------------------------------
# Student ranking helpers
# ---------------------------------------------------------------------------

def _level_sort_key(coach_player: Association_CoachPlayer, ladder: dict | None = None) -> int:
    """Rank a candidate by their position in the coach's ladder (0 = strongest).

    ``ladder`` is a ``{level_id: position}`` map (see level_ladder.py). Without
    one — the only caller that has no vacancy to derive the coach from — this
    falls back to the raw ``display_order``. Players with no level always sort
    last.
    """
    if not coach_player.level:
        return 9999
    if ladder is not None:
        return ladder.get(coach_player.level_id, 9999)
    return coach_player.level.display_order or 9999


# ---------------------------------------------------------------------------
# Side (court side) matching helpers — PAD-15
# ---------------------------------------------------------------------------
# A player's side may be "left", "right", "both", or None.
# "both" players are eligible for open spots of ANY side, and a "both"-side
# vacancy (its departing player was "both") accepts players of any side.
# Eligibility is therefore inclusive/symmetric; exact-side is only PREFERRED,
# not required, via the playing-side tiebreaker below.

def _side_eligible(player_side, vacancy_side) -> bool:
    """True when a player is eligible for a vacancy under a 'same side' criterion.

    Inclusive rule: eligible when the vacancy has no side, the sides match, the
    player plays "both", or the vacancy side is "both". Only a strict
    left-vs-right mismatch (with neither being "both") is ineligible.
    """
    if vacancy_side is None:
        return True
    if player_side is None:
        # Player has no recorded side preference — treat as ineligible for a
        # side-specific vacancy (unchanged from prior left/right behaviour where
        # None != "left"/"right").
        return False
    if player_side == vacancy_side:
        return True
    if player_side == "both" or vacancy_side == "both":
        return True
    return False


def _side_preference_rank(player_side, vacancy_side) -> int:
    """Rank for the playing-side tiebreaker: lower is preferred.

    0 = exact side match (or vacancy has no side constraint),
    1 = "both" fallback (player or vacancy is "both"),
    2 = anything else (wrong side; only reachable in looser rounds).
    """
    if vacancy_side is None or player_side == vacancy_side:
        return 0
    if player_side == "both" or vacancy_side == "both":
        return 1
    return 2


def _attendance_stats(player_id: int) -> tuple[float, float]:
    presences = Presence.query.filter_by(player_id=player_id).all()
    if not presences:
        return 0.0, 0.0
    total = len(presences)
    present = sum(1 for p in presences if p.status == "present")
    justified = sum(1 for p in presences if p.status == "absent" and p.justification == "justified")
    return present / total, justified / total


def _build_sort_key(criteria: list[dict], player_stats: dict, vacancy: Vacancy = None):
    enabled = [c["id"] for c in criteria if c.get("enabled")]
    vacancy_side = getattr(vacancy, "side", None)
    # PAD-70: rank by ladder position, not by the raw display_order integer.
    vacancy_coach_id = getattr(vacancy, "coach_id", None)
    ladder = ladder_index_map(vacancy_coach_id) if vacancy_coach_id else None

    def key(cp: Association_CoachPlayer):
        parts = []
        stats = player_stats.get(cp.player_id, {})
        for criterion in enabled:
            if criterion == "level":
                parts.append(_level_sort_key(cp, ladder))
            elif criterion == "justified_misses":
                parts.append(-stats.get("justified_miss_rate", 0.0))
            elif criterion == "attendance":
                parts.append(-stats.get("attendance_rate", 0.0))
            elif criterion == "playing_side":
                # Prefer an exact-side match first, then "both" players, then any
                # remaining. When the vacancy has no side, fall back to the legacy
                # "left first" ordering so behaviour is unchanged for that case.
                if vacancy_side is not None:
                    parts.append(_side_preference_rank(cp.side, vacancy_side))
                else:
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
    """Level IDs sitting exactly one step ABOVE (stronger than) ``vacancy_level``.

    PAD-70: adjacency is a question about the coach's ladder POSITION, not about
    the ``display_order`` integers. Comparing the raw values treats a level with
    an unset order (``NULL`` / the column default ``0``) as the coach's
    strongest level and collapses duplicated orders into a single step, which is
    how a "5-" student got invited as if they were one level above a "4"
    vacancy. See ``padel_app/services/level_ladder.py``.

    Returns an empty set when the vacancy sits at the top of the ladder, or when
    its level does not belong to this coach.
    """
    ladder = get_level_ladder(coach_id)
    index = ladder_index(ladder, getattr(vacancy_level, "id", None))
    if index is None or index == 0:
        return set()
    return {ladder[index - 1].id}


def _level_ids_one_below(vacancy_level, coach_id: int) -> set:
    """Level IDs sitting exactly one step BELOW (weaker than) ``vacancy_level``.

    Positional, for the same reasons as :func:`_level_ids_one_above`. Empty when
    the vacancy sits at the bottom of the ladder.
    """
    ladder = get_level_ladder(coach_id)
    index = ladder_index(ladder, getattr(vacancy_level, "id", None))
    if index is None or index >= len(ladder) - 1:
        return set()
    return {ladder[index + 1].id}


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
            if op == "same_as_vacancy":
                if cp.level_id != vacancy.level_id:
                    return False
            elif op == "one_above_vacancy":
                if cp.level_id not in _level_ids_one_above(vacancy.level, coach_id):
                    return False
            elif op == "one_below_vacancy":
                if cp.level_id not in _level_ids_one_below(vacancy.level, coach_id):
                    return False
            elif op in ("all_above_vacancy", "all_below_vacancy"):
                # PAD-70: compare ladder POSITIONS (0 = strongest), never the raw
                # display_order values — see level_ladder.py.
                ladder = get_level_ladder(coach_id)
                vd = ladder_index(ladder, vacancy.level_id)
                cd = ladder_index(ladder, cp.level_id)
                if vd is None or cd is None:
                    return False
                if op == "all_above_vacancy" and cd >= vd:
                    return False
                if op == "all_below_vacancy" and cd <= vd:
                    return False

        elif attr == "side":
            if vacancy.side is None:
                continue
            # Inclusive of "both": a "both" player (or a "both" vacancy) is eligible
            # for any side. Exact-side is preferred via the sort key, not required.
            if op == "same_as_vacancy" and not _side_eligible(cp.side, vacancy.side):
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

    restrictions = config.get_restrictions()
    if restrictions["excludedPlayers"]["enabled"]:
        excluded_player_ids = set(restrictions["excludedPlayers"]["playerIds"])
        coach_players = [cp for cp in coach_players if str(cp.player_id) not in excluded_player_ids]
    if restrictions["excludeUnpaidSubscription"]["enabled"]:
        coach_players = [
            cp for cp in coach_players
            if cp.player and cp.player.user and cp.player.user.status == "active"
        ]

    # PAD-28: drop students who have set an availability blocker overlapping
    # this class window (AUTO invitations only — manual add is unaffected).
    from padel_app.services.student_availability_service import filter_blocked_coach_players
    coach_players = filter_blocked_coach_players(coach_players, instance)

    player_stats = {}
    for cp in coach_players:
        att_rate, just_rate = _attendance_stats(cp.player_id)
        player_stats[cp.player_id] = {"attendance_rate": att_rate, "justified_miss_rate": just_rate}

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats, vacancy)
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

    restrictions = config.get_restrictions()
    if restrictions["excludedPlayers"]["enabled"]:
        excluded_player_ids = set(restrictions["excludedPlayers"]["playerIds"])
        coach_players = [cp for cp in coach_players if str(cp.player_id) not in excluded_player_ids]
    if restrictions["excludeUnpaidSubscription"]["enabled"]:
        coach_players = [
            cp for cp in coach_players
            if cp.player and cp.player.user and cp.player.user.status == "active"
        ]

    # Apply round criteria filters
    rounds = config.get_rounds()
    round_cfg = next((r for r in rounds if r["id"] == round_number), None)
    if round_cfg is None:
        return []

    # PAD-28: drop students who have set an availability blocker overlapping
    # this class window (AUTO invitations only — manual add is unaffected).
    from padel_app.services.student_availability_service import filter_blocked_coach_players
    coach_players = filter_blocked_coach_players(coach_players, instance)

    criteria = round_cfg.get("criteria", [])
    criteria_values = round_cfg.get("criteria_values", {})

    for criterion in criteria:
        if criterion == "same_level":
            if vacancy.level_id is not None:
                coach_players = [cp for cp in coach_players if cp.level_id == vacancy.level_id]

        elif criterion == "same_side":
            if vacancy.side is not None:
                # "both" players (and any player for a "both" vacancy) stay eligible;
                # exact-side is preferred later by the playing-side tiebreaker.
                coach_players = [
                    cp for cp in coach_players if _side_eligible(cp.side, vacancy.side)
                ]

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

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats, vacancy)
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
    now = now or utcnow_naive()

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
    _now = now or utcnow_naive()
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
    # An empty placeholder (e.g. a level-less class -> empty {level}) can leave a
    # double space or a space before punctuation; collapse those so the rendered
    # message stays grammatical.
    template = re.sub(r"\s{2,}", " ", template)
    template = re.sub(r"\s+([,.!?;:])", r"\1", template)
    return template.strip()


# Portuguese weekday names, indexed by ``datetime.weekday()`` (Monday == 0).
# Tactical localization only — full locale-driven i18n is tracked in PAD-39.
_PT_WEEKDAYS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def _weekday_pt(start_datetime) -> str:
    """Portuguese weekday name for a datetime, or "" when missing.

    Avoids ``strftime("%A")`` which returns the English weekday under the
    server's default (en) locale — the source of the "esta Wednesday" leak.
    """
    if not start_datetime:
        return ""
    return _PT_WEEKDAYS[start_datetime.weekday()]


def _level_label(instance) -> str:
    """Class-name / modality for the ``{level}`` placeholder.

    Returns the level code when the instance has a level, otherwise an empty
    string. The previous ``"this"`` fallback was an English filler word that
    leaked into pt templates as "aula de this".
    """
    level = getattr(instance, "level", None)
    return level.code if level else ""


def _resolve_locale(coach):
    """Resolve the coach's preferred locale, falling back to Portuguese."""
    try:
        lang = getattr(coach.user, "language", None) if coach and coach.user else None
    except Exception:
        lang = None
    return "pt" if not lang else ("pt" if lang.startswith("pt") else "en")


def _format_weekday(dt, locale):
    """Locale-aware full weekday name via Babel (e.g. pt -> 'quarta-feira')."""
    if not dt:
        return ""
    try:
        from babel.dates import format_date
        return format_date(dt, format="EEEE", locale=locale)
    except Exception:
        return dt.strftime("%A")


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
    class_instance_id: int | None = None,
):
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message
    from padel_app.utils.expo_push import send_expo_push_to_user

    # PAD-67 backstop: never deliver an empty message. Template resolution
    # (``resolve_message_template``) already substitutes a built-in default for a
    # missing/blank template, so reaching here with blank text means the rendered
    # body genuinely has nothing to say — sending it would only produce the empty
    # chat bubbles + empty push notifications reported in the ticket.
    if not (text or "").strip():
        from flask import current_app, has_app_context
        if has_app_context():
            current_app.logger.warning(
                "_send_system_message: refusing to send an empty %s message to user %s",
                message_type, player_user_id,
            )
        return None

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

    # Native (Expo) push — additive, best-effort. Every _send_system_message call
    # in this module is a class/notification-engine event (reminder, invite,
    # spot-filled, waiting list, etc.), so it always carries a lesson instance id
    # for mobile tap-routing to class/[id]. Falls back to msg_metadata's
    # lessonInstanceId/instanceId when the caller didn't pass it explicitly.
    resolved_instance_id = class_instance_id
    if resolved_instance_id is None and msg_metadata:
        resolved_instance_id = msg_metadata.get("lessonInstanceId") or msg_metadata.get("instanceId")
    if resolved_instance_id is not None:
        send_expo_push_to_user(
            player_user_id,
            title="New message",
            body=text[:100],
            data={"type": "class", "classInstanceId": resolved_instance_id},
        )

    return msg


def _notify_coach_of_cancellation(
    coach_user_id: int,
    player_user_id: int,
    instance: LessonInstance,
    player,
    *,
    is_late: bool,
    locale: str = "en",
) -> "object | None":
    """Create a single COACH-facing notification when a student cancels.

    Unlike ``_send_system_message`` (which pushes to the *player*), this reuses the
    same coach↔player direct conversation but sends the message *from the student*
    (``sender_id=player_user_id``) and directs the push notification at the
    ``coach_user_id`` — so the coach is the one who actually gets notified.

    Emitted from ``cancel_attendance`` only (the single path that computes
    lateness), so it fires exactly once per cancellation. The human-readable text
    reflects lateness, and ``msg_metadata`` carries a machine-readable
    ``lateCancellation`` marker plus the ``lessonInstanceId``.
    """
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    if not coach_user_id or not player_user_id:
        return None

    player_name = player.user.name if player and player.user else "A player"
    class_title = instance.title or "the class"
    when = _format_class_when(instance, locale)

    if is_late:
        text = (
            f"{player_name} cancelled (LATE) for {class_title}{when}."
        )
    else:
        text = f"{player_name} cancelled for {class_title}{when}."

    conv = _get_or_create_direct_conversation(coach_user_id, player_user_id)
    msg = Message(
        text=text,
        sender_id=player_user_id,
        conversation_id=conv.id,
        message_type="text",
        msg_metadata={
            "cancellation": True,
            "lateCancellation": bool(is_late),
            "lessonInstanceId": instance.id,
        },
    )
    msg.create()

    publish({
        "type": "message_created",
        "payload": serialize_message(msg, None),
    })

    send_push_notification(
        user_id=coach_user_id,
        title="Late cancellation" if is_late else "Cancellation",
        body=text[:100],
        url=f"/messages/{conv.id}",
    )

    from padel_app.utils.expo_push import send_expo_push_to_user
    send_expo_push_to_user(
        coach_user_id,
        title="Late cancellation" if is_late else "Cancellation",
        body=text[:100],
        data={"type": "class", "classInstanceId": instance.id},
    )

    return msg


def _format_class_when(instance: LessonInstance, locale: str = "en") -> str:
    """Human-readable ' on <weekday> at <time>' suffix for a class instance."""
    if instance.start_datetime is None:
        return ""
    weekday = _format_weekday(instance.start_datetime, locale)
    time_str = instance.start_datetime.strftime("%H:%M")
    if weekday:
        return f" on {weekday} at {time_str}"
    return f" at {time_str}"


def collect_cancellation_recipients(source) -> list[dict]:
    """Snapshot what's needed to tell each enrolled student a class was cancelled.

    PAD-75: when a coach cancels/removes a scheduled class, every enrolled student
    should be told. This resolves the coach, the coach's locale + message
    templates, and each enrolled student's user id and personalised message text
    into plain dicts, so the caller can send the notifications AFTER the class (and
    its relations) has been removed.

    Works uniformly for a ``Lesson`` or a ``LessonInstance`` — both expose
    ``coaches_relations``, ``players_relations``, ``title``, ``start_datetime`` and
    ``level``. MUST be called BEFORE removal, because removal cascade-deletes those
    relations. Returns ``[]`` when there is no coach or no enrolled student, so the
    caller sends nothing.
    """
    from padel_app.models import Coach, LessonInstance, Player

    coach_rels = list(getattr(source, "coaches_relations", []) or [])
    coach = None
    if coach_rels:
        coach = getattr(coach_rels[0], "coach", None) or Coach.query.get(
            coach_rels[0].coach_id
        )
    coach_user_id = coach.user_id if coach else None
    if not coach_user_id:
        return []

    locale = _resolve_locale(coach)
    config = get_or_create_config(coach.id)
    templates = config.get_message_templates(locale)

    level = getattr(source, "level", None)
    level_code = level.code if level else ""
    start_dt = getattr(source, "start_datetime", None)
    weekday = _format_weekday(start_dt, locale)
    time_str = start_dt.strftime("%H:%M") if start_dt else ""

    # Only a materialized LessonInstance carries an id the mobile app can route to.
    instance_id = source.id if isinstance(source, LessonInstance) else None

    recipients: list[dict] = []
    for rel in list(getattr(source, "players_relations", []) or []):
        player = getattr(rel, "player", None) or Player.query.get(rel.player_id)
        player_user_id = player.user_id if player else None
        if not player_user_id:
            continue
        first_name = (
            (player.user.name or "").split()[0]
            if player and player.user and player.user.name
            else ""
        )
        text = _format_template(
            resolve_message_template(templates, "class_cancelled", locale),
            name=first_name,
            level=level_code,
            weekday=weekday,
            time=time_str,
        )
        recipients.append(
            {
                "coach_user_id": coach_user_id,
                "player_user_id": player_user_id,
                "text": text,
                "instance_id": instance_id,
            }
        )
    return recipients


def notify_students_of_cancellation(recipients: list[dict]) -> int:
    """Send each pre-computed cancellation notification (PAD-75).

    Reuses ``_send_system_message`` — the same coach→student conversation channel
    (in-app message + push) used by every other class notification. Returns the
    number of messages actually sent.
    """
    sent = 0
    for r in recipients or []:
        metadata = {"classCancellation": True}
        if r.get("instance_id") is not None:
            metadata["lessonInstanceId"] = r["instance_id"]
        msg = _send_system_message(
            coach_user_id=r["coach_user_id"],
            player_user_id=r["player_user_id"],
            text=r["text"],
            message_type="text",
            msg_metadata=metadata,
            class_instance_id=r.get("instance_id"),
        )
        if msg is not None:
            sent += 1
    return sent


def _user_id_for_player(player_id: int) -> int | None:
    from padel_app.models import Player
    player = Player.query.get(player_id)
    return player.user_id if player else None


def _instance_is_over(instance: LessonInstance, now: datetime | None = None) -> bool:
    """True when a class can no longer accept attendance changes or invitations.

    PAD-68: a class that has already started (or was canceled/completed) is
    "closed" — nothing about its roster can usefully change any more. Every
    notification path that could send a message or move a player must consult
    this before acting, so a late response to a stale reminder/invite can never
    resurrect the invitation engine for a class that already happened.
    """
    if instance is None:
        return True
    if instance.status in ("canceled", "completed"):
        return True
    _now = now or utcnow_naive()
    return instance.start_datetime is not None and instance.start_datetime <= _now


def _effective_filled_spots(instance: LessonInstance) -> int:
    # Delegates to the single source of truth on the model (PAD-71) so the
    # invitation engine, the calendar payload and the class-detail capacity
    # field can never drift apart.
    return instance.effective_filled_spots


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
    locale: str | None = None,
) -> None:
    """
    Mark all other 'sent' events as expired, update their invite messages,
    and send the spot-filled message. Scoped to vacancy_id when provided.
    """
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    spot_filled_text = resolve_message_template(templates, "spot_filled", locale)

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

        _send_system_message(
            coach_user_id, other_player_user_id, spot_filled_text,
            class_instance_id=instance.id,
        )
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

    config = get_or_create_config(coach_id)

    vacancy = Vacancy(
        lesson_instance_id=instance.id,
        coach_id=coach_id,
        original_player_id=absent_player_id,
        side=side,
        level_id=level_id,
        status="open",
        approval_status="pending" if _is_semi_auto(config) else "not_required",
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

    config = get_or_create_config(coach_id)
    approval_status = "pending" if _is_semi_auto(config) else "not_required"

    vacancies = []
    for _ in range(spots_to_create):
        v = Vacancy(
            lesson_instance_id=instance.id,
            coach_id=coach_id,
            original_player_id=None,
            side=None,
            level_id=instance.level_id,
            status="open",
            approval_status=approval_status,
        )
        v.create()
        vacancies.append(v)
    return vacancies


# ---------------------------------------------------------------------------
# Reminder flow
# ---------------------------------------------------------------------------

def send_class_reminders(instance_id: int, *, now: datetime | None = None) -> dict:
    """
    Send 'Are you coming?' messages to all enrolled players.
    Called by APScheduler at the configured reminder time.

    Sends up to ``reminderCount`` reminders per student (spaced
    ``hoursBetweenReminders`` apart — the spacing is enforced by the scheduler
    re-arming this function). A student is skipped once they have responded
    (confirmed or declined) or once they have already received the configured
    number of reminders.

    Returns ``{"sent": <int>, "more_due": <bool>}`` where ``more_due`` is True
    iff at least one student still has NOT responded AND has received fewer than
    ``reminderCount`` reminders after this round (i.e. the scheduler should
    re-arm another reminder pass).

    Pass ``now`` in tests to control the current time without waiting for real time to pass.
    """
    from padel_app.models import Coach

    from flask import current_app, has_app_context
    _log = current_app.logger if has_app_context() else None

    _now = now or utcnow_naive()
    _no_send = {"sent": 0, "more_due": False}

    instance = LessonInstance.query.get(instance_id)
    if not instance:
        if _log:
            _log.warning("send_class_reminders: instance %s not found — skipping", instance_id)
        return _no_send
    if instance.status in ("canceled", "completed"):
        if _log:
            _log.info("send_class_reminders: instance %s status=%s — skipping", instance_id, instance.status)
        return _no_send
    if instance.start_datetime <= _now:
        if _log:
            _log.info("send_class_reminders: instance %s start_datetime in the past — skipping", instance_id)
        return _no_send

    player_count = len(list(instance.players_relations))
    if _log:
        _log.info(
            "send_class_reminders: instance=%s start=%s players=%d — sending",
            instance_id, instance.start_datetime, player_count,
        )

    if player_count == 0:
        if _log:
            _log.info("send_class_reminders: instance %s has no enrolled players — nothing to send", instance_id)
        return _no_send

    # Find the coach for this instance
    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=instance_id
    ).first()
    if not coach_rel:
        if _log:
            _log.warning("send_class_reminders: instance %s has no coach association — skipping", instance_id)
        return _no_send

    coach = Coach.query.get(coach_rel.coach_id)
    if not coach:
        return _no_send

    coach_user_id = coach.user_id
    config = get_or_create_config(coach.id)
    locale = _resolve_locale(coach)
    templates = config.get_message_templates(locale)
    reminder_count = config.get_reminder_count()

    level_code = instance.level.code if getattr(instance, "level", None) else ""
    weekday = _format_weekday(instance.start_datetime, locale)
    time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

    from padel_app.models import Message, Player

    sent_this_round = 0
    more_due = False

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
            existing_presence = Presence(
                lesson_instance_id=instance_id,
                player_id=player_id,
                invited=True,
                confirmed=False,
            )
            existing_presence.create()

        # Stop reminding a student as soon as they have responded.
        # Both "yes" and "no" responses set ``confirmed`` (see respond_to_reminder).
        if existing_presence.confirmed:
            continue

        # Count reminders already sent to THIS player for THIS instance.
        # DB-portable: load this player's reminder Messages and filter in Python
        # on msg_metadata (no JSON-column SQL, so SQLite tests and Postgres prod
        # behave identically).
        conv = _get_or_create_direct_conversation(coach_user_id, player_user_id)
        prior_reminders = Message.query.filter_by(
            conversation_id=conv.id,
            message_type="notification_reminder",
        ).all()
        sent_count = sum(
            1 for m in prior_reminders
            if m.msg_metadata and m.msg_metadata.get("instanceId") == instance_id
        )

        if sent_count >= reminder_count:
            continue

        player = Player.query.get(player_id)
        player_name = (player.user.name if player and player.user else "there").split()[0]

        template_key = "reminder_followup" if sent_count > 0 else "reminder"
        text = _format_template(
            resolve_message_template(templates, template_key, locale),
            name=player_name,
            level=level_code,
            weekday=weekday,
            time=time_str,
        )

        # PAD-49: Supersede older un-actioned reminders for THIS (player, instance)
        # before sending the new one. Only the latest reminder should stay
        # actionable; older reminders the student never responded to are marked
        # superseded so the frontend renders them disabled/"expired". Reminders the
        # player already responded to are left untouched (they keep their badge).
        from padel_app.serializers.message import serialize_message
        for m in prior_reminders:
            if (
                m.msg_metadata
                and m.msg_metadata.get("instanceId") == instance_id
                and not m.msg_metadata.get("responded")
                and not m.msg_metadata.get("superseded")
            ):
                m.msg_metadata = {**m.msg_metadata, "superseded": True}
                m.save()
                publish({"type": "message_edited", "payload": serialize_message(m, None)})

        _send_system_message(
            coach_user_id=coach_user_id,
            player_user_id=player_user_id,
            text=text,
            message_type="notification_reminder",
            msg_metadata={
                "lessonInstanceId": instance_id,
                "instanceId": instance_id,
                "reminderNumber": sent_count + 1,
                "responded": False,
                # ISO start time so the student UI can offer "Cancel attendance"
                # only before the class starts (PAD-35).
                "startsAt": (
                    instance.start_datetime.isoformat()
                    if instance.start_datetime is not None
                    else None
                ),
            },
        )
        sent_this_round += 1

        # If this student still has reminders remaining, the scheduler must re-arm.
        if (sent_count + 1) < reminder_count:
            more_due = True

    return {"sent": sent_this_round, "more_due": more_due}


def _expire_stale_reminders(instance: LessonInstance, player_user_id: int) -> None:
    """Flag every un-actioned reminder for (player, instance) as expired.

    PAD-68: PAD-49 only supersedes older reminders when a *newer* one is sent, so
    the last reminder of a series stays actionable forever. Once the class is
    over there will never be a newer reminder, so nothing ever retires it. This
    retires them explicitly — reusing the existing ``superseded`` flag the UI
    already renders as "reminder expired", so no client change is required for
    the flag to take effect.
    """
    from padel_app.models import Coach, Message
    from padel_app.serializers.message import serialize_message

    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=instance.id
    ).first()
    coach = Coach.query.get(coach_rel.coach_id) if coach_rel else None
    if not coach or not coach.user_id:
        return

    conv = _get_or_create_direct_conversation(coach.user_id, player_user_id)
    reminders = Message.query.filter_by(
        conversation_id=conv.id,
        message_type="notification_reminder",
    ).all()
    for m in reminders:
        if (
            m.msg_metadata
            and m.msg_metadata.get("lessonInstanceId") == instance.id
            and not m.msg_metadata.get("responded")
            and not m.msg_metadata.get("superseded")
        ):
            m.msg_metadata = {**m.msg_metadata, "superseded": True, "expired": True}
            m.save()
            publish({"type": "message_edited", "payload": serialize_message(m, None)})


def _retire_invite_message(event: NotificationEvent) -> None:
    """Flag the conversation message that delivered ``event`` as no longer live.

    PAD-68: reuses the ``responded`` flag the invite bubble already keys off, so
    the Yes/No buttons stop rendering on both web and mobile with no client
    change. ``response`` is set to ``"expired"`` — neither "yes" nor "no" — which
    both clients already fall through to a neutral non-actionable badge.
    """
    from padel_app.models import Message
    from padel_app.serializers.message import serialize_message

    if not event.message_id:
        return
    msg = Message.query.get(event.message_id)
    if msg is None or msg.msg_metadata is None:
        return
    if msg.msg_metadata.get("responded"):
        return
    msg.msg_metadata = {**msg.msg_metadata, "responded": True, "response": "expired"}
    msg.save()
    publish({"type": "message_edited", "payload": serialize_message(msg, None)})


def _expire_stale_invitations(instance: LessonInstance) -> int:
    """Retire every un-actioned invitation for a class that is already over.

    PAD-68 follow-up: the first pass retired stale *reminders* but pending
    *invitations* stayed live — a student could still tap "yes" on an invite for
    a class that already happened. This moves every NotificationEvent still in
    ``sent``/``queued`` to the existing terminal ``expired`` status, flags the
    invite message so the client stops offering buttons, and closes any vacancy
    that is still open (nothing can fill a class that already happened).

    Idempotent: a second call finds nothing in ``sent``/``queued`` and nothing
    un-``responded``, so repeated taps are no-ops.

    Callers must have already established that the class is over via
    ``_instance_is_over`` — there is deliberately only one staleness rule.
    """
    pending = NotificationEvent.query.filter(
        NotificationEvent.lesson_instance_id == instance.id,
        NotificationEvent.status.in_(("sent", "queued")),
    ).all()

    for event in pending:
        event.status = "expired"
        event.save()
        _retire_invite_message(event)

    open_vacancies = Vacancy.query.filter_by(
        lesson_instance_id=instance.id, status="open"
    ).all()
    for vacancy in open_vacancies:
        vacancy.status = "expired"
        vacancy.save()

    return len(pending)


def expire_stale_invitations(*, now: datetime | None = None) -> int:
    """Sweep every class that is over and retire its still-pending invitations.

    PAD-68 follow-up: the lazy guards only fire when *someone responds*. An
    invitation nobody ever answers — the common case for a class that quietly
    passed — would stay live forever. This sweep runs from the existing
    two-minute ``process_batches`` APScheduler job so stale invites are retired
    without requiring any user action.

    Returns the number of NotificationEvent rows retired.
    """
    _now = now or utcnow_naive()

    instance_ids = [
        row[0]
        for row in NotificationEvent.query
        .with_entities(NotificationEvent.lesson_instance_id)
        .filter(NotificationEvent.status.in_(("sent", "queued")))
        .distinct()
        .all()
    ]

    retired = 0
    for instance_id in instance_ids:
        instance = LessonInstance.query.get(instance_id)
        if instance is None:
            continue
        if not _instance_is_over(instance, _now):
            continue
        retired += _expire_stale_invitations(instance)
    return retired


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

    PAD-68: a reminder for a class that has already started (or was
    canceled/completed) is *expired*. Responding to it is a no-op: the answer is
    not recorded against attendance and it never creates a vacancy or fans out
    replacement invitations for a class that already happened. The stale
    reminder message is flagged so the UI stops offering Yes/No.
    """
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(lesson_instance_id)

    player = Player.query.filter_by(user_id=acting_user_id).first()
    if not player:
        from flask import abort
        abort(403)

    _now = now or utcnow_naive()
    if _instance_is_over(instance, _now):
        _expire_stale_reminders(instance, acting_user_id)
        # The class is over for everyone, not just this student: retire any
        # invitation still offering a spot in it.
        _expire_stale_invitations(instance)
        return {"action": "expired"}

    presence = Presence.query.filter_by(
        player_id=player.id,
        lesson_instance_id=lesson_instance_id,
    ).first()
    if presence is None:
        # PAD-69: a response must always be durably recorded. Without a Presence
        # row the answer is silently dropped and the next reminder pass sees the
        # student as "never responded" and re-reminds them.
        presence = Presence(
            lesson_instance_id=lesson_instance_id,
            player_id=player.id,
            invited=True,
            confirmed=False,
        )
        presence.create()

    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=lesson_instance_id
    ).first()
    coach = Coach.query.get(coach_rel.coach_id) if coach_rel else None
    coach_user_id = coach.user_id if coach else None

    config = get_or_create_config(coach.id) if coach else None
    locale = _resolve_locale(coach)
    templates = (
        config.get_message_templates(locale)
        if config
        else dict(default_templates_for_locale(locale))
    )

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
             and not m.msg_metadata.get("responded")
             and not m.msg_metadata.get("superseded")),
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
                resolve_message_template(templates, "reminder_confirmed", locale),
                class_instance_id=instance.id,
            )
        return {"action": "confirmed"}

    elif action == "no":
        _free_spot_for_declining_player(
            instance,
            presence,
            player,
            coach,
            coach_user_id,
            acting_user_id,
            config,
            templates,
            locale=locale,
            now=now,
        )
        return {"action": "declined"}

    return {"action": "unknown"}


def _free_spot_for_declining_player(
    instance: LessonInstance,
    presence: "Presence | None",
    player,
    coach,
    coach_user_id: int | None,
    acting_user_id: int,
    config,
    templates: dict,
    *,
    locale: str | None = None,
    now: datetime | None = None,
) -> None:
    """Revert a player to "not attending" and free their spot.

    This is the single shared path used both when a player declines a reminder
    (``respond_to_reminder`` with action ``no``) and when a player cancels a
    previously-confirmed attendance (``cancel_attendance``). It reuses the exact
    same vacancy-creation and invitation-engine logic — cancellation is NOT a
    separate fork.
    """
    if presence:
        presence.confirmed = True
        presence.status = "absent"
        presence.justification = "justified"
        presence.save()
    if coach_user_id:
        _send_system_message(
            coach_user_id,
            acting_user_id,
            resolve_message_template(templates, "reminder_declined", locale),
            class_instance_id=instance.id,
        )

    # Always pre-create vacancy so the invite_start job finds it when window opens.
    # If the invitation window is already open, trigger invitations immediately.
    if coach and config:
        from padel_app.scheduler import _compute_invite_start_dt
        vacancy = _ensure_vacancy_for_player(instance, coach.id, player.id)
        if _is_semi_auto(config):
            # Semi-automatic: ask the coach for approval instead of sending.
            # No invitations are sent until the coach approves the prompt.
            if vacancy is not None and vacancy.approval_status == "pending":
                from padel_app.services.replacement_approval_service import (
                    create_approval_prompts,
                )
                create_approval_prompts(
                    [vacancy], instance, coach.id, config, now=now
                )
        else:
            invite_start_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
            _now = now or utcnow_naive()
            if invite_start_dt is None or _now >= invite_start_dt:
                trigger_invitations(instance, coach.id)


def cancel_attendance(
    lesson_instance_id: int,
    acting_user_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    """Cancel a previously-confirmed attendance for the acting player.

    Allowed only BEFORE the class start time. Reverts the player to
    "not attending" and frees the spot, reusing the exact same engine path as a
    reminder decline. After the class has started, raises 409.

    Cancellations at or after the coach's configured cancellation deadline
    (``cancellationDeadlineHours`` before start, default 24) are still allowed
    and still free the spot, but flag the Presence with ``late_cancellation``.
    """
    from flask import abort
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(lesson_instance_id)

    _now = now or utcnow_naive()
    if instance.start_datetime is not None and _now >= instance.start_datetime:
        abort(409, description="Class has already started; attendance can no longer be cancelled.")

    player = Player.query.filter_by(user_id=acting_user_id).first()
    if not player:
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
    locale = _resolve_locale(coach)
    templates = (
        config.get_message_templates(locale)
        if config
        else dict(default_templates_for_locale(locale))
    )

    # Flag late cancellations: at/after the deadline (start - cancellationDeadlineHours)
    # but still before start. The spot is freed either way.
    is_late = False
    if presence is not None:
        from padel_app.models.notification_config import (
            DEFAULT_CANCELLATION_DEADLINE_HOURS,
        )
        deadline_hours = (
            config.get_cancellation_deadline_hours()
            if config
            else DEFAULT_CANCELLATION_DEADLINE_HOURS
        )
        if instance.start_datetime is not None:
            deadline = instance.start_datetime - timedelta(hours=deadline_hours)
            is_late = _now >= deadline
        presence.late_cancellation = is_late

    # PAD-44: notify the COACH of the cancellation exactly once, flagging late
    # cancellations. This is emitted HERE (not in the shared
    # _free_spot_for_declining_player) because cancel_attendance is the only path
    # that computes lateness — keeping a single emission point avoids duplicates and
    # a false "late" flag on plain reminder declines.
    if coach_user_id:
        _notify_coach_of_cancellation(
            coach_user_id,
            acting_user_id,
            instance,
            player,
            is_late=is_late,
            locale=locale,
        )

    # Mark the most recent reminder message as responded ("no") so the UI reflects
    # the cancellation on reload, mirroring respond_to_reminder.
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
             and m.msg_metadata.get("lessonInstanceId") == lesson_instance_id),
            None,
        )
        if reminder_msg:
            reminder_msg.msg_metadata = {
                **reminder_msg.msg_metadata,
                "responded": True,
                "response": "no",
            }
            reminder_msg.save()
            publish({"type": "message_edited", "payload": serialize_message(reminder_msg, None)})

    _free_spot_for_declining_player(
        instance,
        presence,
        player,
        coach,
        coach_user_id,
        acting_user_id,
        config,
        templates,
        locale=locale,
        now=now,
    )
    return {"action": "declined"}


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
) -> Vacancy:
    """Create a vacancy for an absent player without triggering invitations.
    Used when the invitation window hasn't opened yet — the invite_start scheduler
    job will call trigger_invitations when the window opens.
    Returns the existing or newly created vacancy."""
    existing = Vacancy.query.filter_by(
        lesson_instance_id=instance.id,
        original_player_id=player_id,
        status="open",
    ).first()
    if existing:
        return existing
    return _create_vacancy_for_absent_player(instance, coach_id, player_id)


# ---------------------------------------------------------------------------
# Invitation batch helpers
# ---------------------------------------------------------------------------

def _send_invitation_batch(
    vacancy: Vacancy,
    instance: LessonInstance,
    config: NotificationConfig,
    coach_id: int,
    max_sim_override: int | None = None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """
    Send the next batch of invitations for this vacancy.
    Returns list of {id, name} for players notified.

    PAD-68: this is the single chokepoint every automatic invitation message
    flows through. A class that has already started can never be filled, so the
    vacancy is expired here instead of inviting anyone — this backstops every
    caller (trigger_invitations, process_invitation_batches, _advance_round,
    _send_next_on_decline) including late responses to stale messages.
    """
    from padel_app.models import Coach, Player

    if _instance_is_over(instance, now):
        if vacancy.status == "open":
            vacancy.status = "expired"
            vacancy.save()
        return []

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
    locale = _resolve_locale(coach_obj)
    templates = config.get_message_templates(locale)
    level_code = instance.level.code if getattr(instance, "level", None) else ""
    weekday = _format_weekday(instance.start_datetime, locale)
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
            resolve_message_template(templates, "invite", locale),
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
        # _send_system_message returns None only if the body came out empty
        # (PAD-67 backstop); the event still exists, just without a chat message.
        if msg is not None:
            event.message_id = msg.id
            event.save()

        notified.append({"id": str(cp.player_id), "name": player_name})

    vacancy.last_activity_at = utcnow_naive()
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

def _find_or_create_open_vacancies(instance: LessonInstance, coach_id: int) -> list[Vacancy]:
    """Find existing open vacancies for an instance or create new ones
    (from absent presences + structural open spots)."""
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

    return open_vacancies


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

    In semi-automatic mode, vacancies pending coach approval produce a
    replacement approval prompt instead of invitations; only "not_required"
    and "approved" vacancies are sent.

    Pass ``now`` in tests to control the current time without waiting for real time to pass.
    """
    config = get_or_create_config(coach_id)

    if not config.auto_notify_enabled:
        return []
    if not instance.notifications_enabled:
        return []
    # PAD-68: never open/refresh vacancies for a class that already happened.
    if _instance_is_over(instance, now):
        return []

    semi_auto = _is_semi_auto(config)
    open_vacancies: list[Vacancy] | None = None

    if semi_auto:
        # Restrictions gate SENDING, not asking — create the approval prompts
        # before the restrictions check so the coach is always asked exactly
        # once (the invite_start DateTrigger fires only once).
        open_vacancies = _find_or_create_open_vacancies(instance, coach_id)
        pending = [v for v in open_vacancies if v.approval_status == "pending"]
        if pending:
            from padel_app.services.replacement_approval_service import (
                create_approval_prompts,
            )
            create_approval_prompts(pending, instance, coach_id, config, now=now)

    restrictions = config.get_restrictions()
    if not _check_restrictions(instance, coach_id, restrictions, now=now):
        return []

    if open_vacancies is None:
        open_vacancies = _find_or_create_open_vacancies(instance, coach_id)

    if not open_vacancies:
        return []

    _now = now or utcnow_naive()
    sendable = [
        v for v in open_vacancies
        if v.approval_status in ("not_required", "approved")
        and (v.invite_not_before is None or _now >= v.invite_not_before)
    ]

    all_notified: list[dict] = []
    for vacancy in sendable:
        notified = _send_invitation_batch(vacancy, instance, config, coach_id, now=_now)
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

    PAD-68 follow-up: this pass also retires invitations that are still pending
    for classes that already happened. It is hooked here rather than on a new
    APScheduler job because this is already the periodic notification-engine
    tick (every 2 minutes, ``process_batches``), so no new job registration or
    jobstore entry is needed, and the sweep must run whether or not the class
    still has an open vacancy.
    """
    _now = now or utcnow_naive()
    expire_stale_invitations(now=_now)
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

        # Semi-automatic gating: never send (or waiting-list fill) vacancies
        # awaiting coach approval or dismissed by the coach.
        if vacancy.approval_status in ("pending", "dismissed"):
            continue
        # Approved "at the invitation window": hold until the window opens.
        if vacancy.invite_not_before is not None and _now < vacancy.invite_not_before:
            continue

        config = get_or_create_config(vacancy.coach_id)
        restrictions = config.get_restrictions()

        last = vacancy.last_activity_at

        # Fresh vacancy (no batch sent yet) — trigger immediately
        if last is None:
            _send_invitation_batch(vacancy, instance, config, vacancy.coach_id, now=_now)
            processed += 1
            continue

        # Check inactivity timer
        max_inactive = restrictions.get("maxInactiveTime", {})
        if max_inactive.get("enabled"):
            threshold = timedelta(minutes=max_inactive["value"])
            if _now - last >= threshold:
                _send_invitation_batch(vacancy, instance, config, vacancy.coach_id, now=_now)
                processed += 1

    return processed


# ---------------------------------------------------------------------------
# Respond to notification (player presses Yes / No on invite)
# ---------------------------------------------------------------------------

def respond_to_notification(
    notification_event_id: int,
    action: str,
    acting_user_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    from flask import abort
    from padel_app.models import Coach, Message, Player
    from padel_app.serializers.message import serialize_message

    event = NotificationEvent.query.get_or_404(notification_event_id)

    player = Player.query.get(event.player_id)
    if not player or player.user_id != acting_user_id:
        abort(403, "Not authorized to respond to this notification")

    # PAD-68: a late response to an invitation for a class that already happened
    # must not enrol anyone, free anyone, or trigger the next invitation round.
    # Every pending invite for the class is retired here, not just this one — the
    # class is over for everyone who was offered the spot.
    if _instance_is_over(event.lesson_instance, now):
        _expire_stale_invitations(event.lesson_instance)
        # The event may already have been out of sent/queued (so the sweep above
        # skipped it) while its message was still showing live buttons.
        _retire_invite_message(event)
        return {"action": "expired"}

    config = get_or_create_config(event.coach_id)

    coach = Coach.query.get(event.coach_id)
    locale = _resolve_locale(coach)
    templates = config.get_message_templates(locale)
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
            vacancy.last_activity_at = utcnow_naive()
            vacancy.save()
            # Immediately invite the next player without waiting for inactivity timer
            _send_next_on_decline(vacancy, instance, event.coach_id, config)

        if coach_user_id:
            _send_system_message(
                coach_user_id,
                player_user_id,
                resolve_message_template(templates, "decline", locale),
                class_instance_id=instance.id,
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
                    resolve_message_template(templates, "spot_filled", locale),
                    class_instance_id=instance.id,
                )
            _offer_waiting_list(event.player_id, instance, event.coach_id, templates, locale)
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
                    resolve_message_template(templates, "spot_filled", locale),
                    class_instance_id=instance.id,
                )
            _offer_waiting_list(event.player_id, instance, event.coach_id, templates, locale)
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
            vacancy.filled_at = utcnow_naive()
            vacancy.save()

        if coach_user_id:
            _send_system_message(
                coach_user_id,
                player_user_id,
                resolve_message_template(templates, "confirm", locale),
                class_instance_id=instance.id,
            )
            _broadcast_spot_filled(
                instance,
                event.id,
                coach_user_id,
                templates,
                vacancy_id=vacancy.id if vacancy else None,
                locale=locale,
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


def coach_respond_to_notification(
    notification_event_id: int,
    action: str,
    coach_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    from flask import abort

    event = NotificationEvent.query.get_or_404(notification_event_id)
    if event.coach_id != coach_id:
        abort(403, "Not authorized")

    # PAD-68: the coach recording a late answer must not enrol anyone into a
    # class that already happened either — same staleness rule as the player path.
    if _instance_is_over(event.lesson_instance, now):
        _expire_stale_invitations(event.lesson_instance)
        _retire_invite_message(event)
        return {"action": "expired"}

    instance = event.lesson_instance
    vacancy = event.vacancy

    if action == "no":
        event.status = "expired"
        event.save()
        if vacancy:
            vacancy.last_activity_at = utcnow_naive()
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
            vacancy.filled_at = utcnow_naive()
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

    coach = Coach.query.get(coach_id)
    coach_user_id = coach.user_id if coach else None
    locale = _resolve_locale(coach)
    templates = config.get_message_templates(locale)

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
            level_code = instance.level.code if getattr(instance, "level", None) else ""
            weekday = _format_weekday(instance.start_datetime, locale)
            time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

            text = _format_template(
                resolve_message_template(templates, "invite", locale),
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
            if msg is not None:
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
    locale: str | None = None,
) -> None:
    """Send a waiting-list offer message to the player."""
    from padel_app.models import Coach
    coach = Coach.query.get(coach_id)
    if not coach:
        return
    player_user_id = _user_id_for_player(player_id)
    if not player_user_id:
        return

    text = resolve_message_template(templates, "waiting_list_offer", locale)
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
    *,
    now: datetime | None = None,
) -> dict:
    """
    Called when a player presses Yes or No on a waiting-list offer.

    PAD-68: joining the waiting list for a class that already happened is
    meaningless — the entry could never be filled — so a late answer is a no-op
    and any invitation still pending for that class is retired.
    """
    from padel_app.models import Coach, Player

    instance = LessonInstance.query.get_or_404(lesson_instance_id)

    player = Player.query.filter_by(user_id=acting_user_id).first()
    if not player:
        from flask import abort
        abort(403)

    if _instance_is_over(instance, now):
        _expire_stale_invitations(instance)
        return {"action": "expired"}

    coach_rel = Association_CoachLessonInstance.query.filter_by(
        lesson_instance_id=lesson_instance_id
    ).first()
    coach = Coach.query.get(coach_rel.coach_id) if coach_rel else None
    if not coach:
        return {"action": "unknown"}

    config = get_or_create_config(coach.id)
    locale = _resolve_locale(coach)
    templates = config.get_message_templates(locale)

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
                text=resolve_message_template(templates, "waiting_list_confirm", locale),
                class_instance_id=instance.id,
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
            if standing and (not standing.is_active or standing.expires_at < utcnow_naive()):
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
                    if vacancy.side is not None and not _side_eligible(cp.side, vacancy.side):
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

    sort_key = _build_sort_key(config.get_priority_criteria(), player_stats, vacancy)
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
    vacancy.filled_at = utcnow_naive()
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

    from padel_app.models import Player

    locale = _resolve_locale(coach)
    templates = config.get_message_templates(locale)
    player = Player.query.get(entry.player_id)
    player_name = (player.user.name if player and player.user else "there").split()[0]
    level_code = instance.level.code if getattr(instance, "level", None) else ""
    weekday = _format_weekday(instance.start_datetime, locale)
    time_str = instance.start_datetime.strftime("%H:%M") if instance.start_datetime else ""

    text = _format_template(
        resolve_message_template(templates, "waiting_list_placed", locale),
        name=player_name,
        level=level_code,
        weekday=weekday,
        time=time_str,
    )
    _send_system_message(
        coach_user_id=coach.user_id,
        player_user_id=player_user_id,
        text=text,
        message_type="waiting_list_placed",
        class_instance_id=instance.id,
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
    now = utcnow_naive()
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
        expires_at=utcnow_naive() + timedelta(days=duration_days),
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
