"""
Semi-automatic replacement approval service.

In semi-automatic mode (`NotificationConfig.invitation_mode == "semi_automatic"`)
the invitation engine asks the coach for approval before sending replacement
invitations. Each vacancy gets a ReplacementApprovalPrompt showing who declined
and the FULL ordered invite queue; prompts created in one shot (e.g. one
confirm-presences call) share a bundle_id and are delivered as ONE message in
the coach's Assistant conversation. The bundle is the unit of decision.

Public API
----------
- get_or_create_assistant_user()
- compute_full_invite_queue(vacancy, instance, coach_id, config)
- create_approval_prompts(vacancies, instance, coach_id, config, *, now=None)
- respond_to_approval(bundle_id, action, coach_id, *, now=None)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from padel_app.sql_db import db
from padel_app.realtime import publish
from padel_app.utils.dates import utcnow_naive
from padel_app.utils.push_notifications import send_push_notification

ASSISTANT_USERNAME = "levelup-assistant"
ASSISTANT_NAME = "LevelUp Assistant"

VALID_ACTIONS = ("yes_now", "yes_at_window", "dismiss")


# ---------------------------------------------------------------------------
# Assistant user / conversation helpers
# ---------------------------------------------------------------------------

def get_or_create_assistant_user():
    """Dedicated system user that owns the Assistant conversations.

    MUST stay status="disabled" so GET /api/app/users (which filters
    status="active") never lists it.
    """
    from padel_app.models import User

    user = User.query.filter_by(username=ASSISTANT_USERNAME).first()
    if user is None:
        user = User(
            name=ASSISTANT_NAME,
            username=ASSISTANT_USERNAME,
            status="disabled",
            password=None,
        )
        db.session.add(user)
        # flush (not commit) so the user is created in the SAME transaction as
        # the prompts/conversation/message — the caller owns the commit, keeping
        # the whole bundle atomic (no orphaned prompts if a later step fails).
        db.session.flush()
    return user


def _get_or_create_assistant_conversation(coach_user_id: int):
    """Return (conversation, assistant_user) for the coach's Assistant
    conversation, creating both atomically when missing."""
    from padel_app.models import Conversation, ConversationParticipant

    assistant = get_or_create_assistant_user()
    key = Conversation.build_participant_key([assistant.id, coach_user_id])
    conv = Conversation.query.filter_by(participant_key=key).first()
    if conv is None:
        conv = Conversation(participant_key=key, is_group=False)
        db.session.add(conv)
        db.session.flush()
        for uid in sorted({assistant.id, coach_user_id}):
            db.session.add(
                ConversationParticipant(conversation_id=conv.id, user_id=uid)
            )
        # flush only — the caller (create_approval_prompts) commits once at the
        # end so prompts + conversation + message persist atomically.
        db.session.flush()
    return conv, assistant


# ---------------------------------------------------------------------------
# Invite queue snapshot
# ---------------------------------------------------------------------------

def compute_full_invite_queue(vacancy, instance, coach_id: int, config) -> list[dict]:
    """
    Full ordered invite queue for a vacancy: all eligible candidates across
    ALL rounds/groups, in the exact order the engine would invite them,
    deduplicated by player (first occurrence wins).

    Exactness principle: this list is exactly the set of players who may
    receive invitations for this vacancy (eligibility is recomputed at send
    time with the same rules).
    """
    from padel_app.services.notification_service import (
        _get_eligible_students_for_group,
        _serialize_cp_for_group,
        get_eligible_students,
    )

    queue: list[dict] = []
    seen: set[int] = set()

    invitation_groups = config.get_invitation_groups()
    if invitation_groups:
        for idx in range(1, len(invitation_groups) + 1):
            for cp in _get_eligible_students_for_group(
                vacancy, instance, coach_id, config, idx
            ):
                if cp.player_id in seen:
                    continue
                seen.add(cp.player_id)
                queue.append({**_serialize_cp_for_group(cp), "roundNumber": idx})
    else:
        for round_cfg in config.get_rounds():
            round_number = round_cfg["id"]
            for cp in get_eligible_students(
                vacancy, instance, coach_id, config, round_number
            ):
                if cp.player_id in seen:
                    continue
                seen.add(cp.player_id)
                queue.append({**_serialize_cp_for_group(cp), "roundNumber": round_number})

    return queue


# ---------------------------------------------------------------------------
# Prompt creation
# ---------------------------------------------------------------------------

def _player_name(player_id: int | None) -> str | None:
    if not player_id:
        return None
    from padel_app.models import Player

    player = Player.query.get(player_id)
    return player.user.name if player and player.user else None


def _build_prompt_text(vacancies_payload: list[dict]) -> str:
    parts = []
    for v in vacancies_payload:
        declined = v.get("declinedPlayerName") or "A player"
        queue_names = ", ".join(e["name"] for e in v.get("queue", []))
        part = f"{declined} dropped out."
        if queue_names:
            part += f" Invite queue: {queue_names}."
        else:
            part += " No eligible replacements found."
        if v.get("waitingListPlayerName"):
            part += (
                f" {v['waitingListPlayerName']} from the waiting list"
                " will be added directly to the class."
            )
        parts.append(part)
    parts.append("Send replacement invitations?")
    return " ".join(parts)


def create_approval_prompts(
    vacancies: list,
    instance,
    coach_id: int,
    config,
    *,
    now: datetime | None = None,
) -> dict | None:
    """
    Create one ReplacementApprovalPrompt per vacancy (idempotent — vacancies
    that already have a prompt are skipped) sharing a single bundle_id, and
    persist the bundle as ONE message in the coach's Assistant conversation.

    Returns the serialized bundle dict (same shape as the message
    msg_metadata), or the existing bundle when nothing new was created.
    """
    from padel_app.models import Coach, Message
    from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
    from padel_app.scheduler import _compute_invite_start_dt
    from padel_app.serializers.message import serialize_message
    from padel_app.services.notification_service import _check_waiting_list

    existing_prompts = []
    new_vacancies = []
    for vacancy in vacancies:
        prompt = ReplacementApprovalPrompt.query.filter_by(
            vacancy_id=vacancy.id
        ).first()
        if prompt is not None:
            existing_prompts.append(prompt)
        else:
            new_vacancies.append(vacancy)

    if not new_vacancies:
        # Idempotent: nothing new to ask — return the existing bundle.
        for prompt in existing_prompts:
            if prompt.message_id:
                msg = Message.query.get(prompt.message_id)
                if msg and msg.msg_metadata:
                    return msg.msg_metadata
        return None

    coach = Coach.query.get(coach_id)
    coach_user_id = coach.user_id if coach else None

    window_open_dt = _compute_invite_start_dt(
        instance, config.get_invitation_start_timing()
    )

    bundle_id = str(uuid.uuid4())
    prompts = []
    vacancies_payload = []
    for vacancy in new_vacancies:
        queue = compute_full_invite_queue(vacancy, instance, coach_id, config)

        wl_entry = _check_waiting_list(
            vacancy, instance, coach_id, config, vacancy.current_round_number
        )
        wl_player_id = wl_entry.player_id if wl_entry else None

        prompt = ReplacementApprovalPrompt(
            coach_id=coach_id,
            vacancy_id=vacancy.id,
            bundle_id=bundle_id,
            declined_player_id=vacancy.original_player_id,
            queue_snapshot=queue,
            waiting_list_player_id=wl_player_id,
            status="pending",
        )
        db.session.add(prompt)
        prompts.append(prompt)

        vacancies_payload.append({
            "vacancyId": vacancy.id,
            "declinedPlayerId": vacancy.original_player_id,
            "declinedPlayerName": _player_name(vacancy.original_player_id),
            "queue": queue,
            "waitingListPlayerId": wl_player_id,
            "waitingListPlayerName": _player_name(wl_player_id),
        })

    db.session.flush()

    bundle = {
        "bundleId": bundle_id,
        "lessonInstanceId": instance.id,
        "windowOpenAt": window_open_dt.isoformat() if window_open_dt else None,
        "responded": False,
        "vacancies": vacancies_payload,
    }

    if coach_user_id:
        conv, assistant = _get_or_create_assistant_conversation(coach_user_id)
        text = _build_prompt_text(vacancies_payload)
        msg = Message(
            text=text,
            sender_id=assistant.id,
            conversation_id=conv.id,
            message_type="replacement_approval",
            msg_metadata=bundle,
        )
        db.session.add(msg)
        db.session.flush()
        for prompt in prompts:
            prompt.message_id = msg.id
        db.session.commit()

        publish({
            "type": "message_created",
            "payload": serialize_message(msg, None),
        })
        # Push to the COACH (the recipient of the approval request)
        send_push_notification(
            user_id=coach_user_id,
            title="Replacement approval needed",
            body=text[:100],
            url=f"/messages/{conv.id}",
        )
    else:
        db.session.commit()

    return bundle


# ---------------------------------------------------------------------------
# Coach decision
# ---------------------------------------------------------------------------

def respond_to_approval(
    bundle_id: str,
    action: str,
    coach_id: int,
    *,
    now: datetime | None = None,
) -> dict:
    """
    Apply the coach's decision to every prompt in a bundle.

    action: "yes_now" | "yes_at_window" | "dismiss"

    Per-vacancy results: "approved_now" | "approved_at_window" | "dismissed"
    | "stale" (vacancy filled/expired or prompt already decided — no-op).
    """
    from flask import abort

    from padel_app.models import Message
    from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
    from padel_app.scheduler import _compute_invite_start_dt
    from padel_app.serializers.message import serialize_message
    from padel_app.services.notification_service import (
        get_or_create_config,
        trigger_invitations,
    )

    if action not in VALID_ACTIONS:
        abort(400, "action must be one of: yes_now, yes_at_window, dismiss")

    prompts = ReplacementApprovalPrompt.query.filter_by(bundle_id=bundle_id).all()
    if not prompts:
        abort(404, "Approval bundle not found")
    if any(p.coach_id != coach_id for p in prompts):
        abort(403, "Not authorized")

    _now = now or utcnow_naive()
    config = get_or_create_config(coach_id)

    results = []
    instances_to_trigger = {}
    for prompt in prompts:
        vacancy = prompt.vacancy
        instance = vacancy.lesson_instance if vacancy else None

        is_stale = (
            prompt.status != "pending"
            or vacancy is None
            or vacancy.status != "open"
            or instance is None
            or instance.start_datetime <= _now
        )
        if is_stale:
            # No-op decision; mark still-pending prompts whose vacancy closed.
            if prompt.status == "pending":
                prompt.status = "stale"
                prompt.decided_at = _now
            results.append({"vacancyId": prompt.vacancy_id, "result": "stale"})
            continue

        if action == "dismiss":
            # Terminal: the engine never sends for this vacancy, but the
            # vacancy REMAINS OPEN for the manual flow.
            vacancy.approval_status = "dismissed"
            prompt.status = "dismissed"
            prompt.decided_at = _now
            results.append({"vacancyId": vacancy.id, "result": "dismissed"})
            continue

        window_open_dt = _compute_invite_start_dt(
            instance, config.get_invitation_start_timing()
        )
        window_already_open = window_open_dt is None or _now >= window_open_dt

        if action == "yes_now" or (action == "yes_at_window" and window_already_open):
            # "Yes, at window" with the window already open executes as yes_now.
            vacancy.approval_status = "approved"
            prompt.status = "approved"
            prompt.decided_at = _now
            instances_to_trigger[instance.id] = instance
            results.append({"vacancyId": vacancy.id, "result": "approved_now"})
        else:  # yes_at_window, window not open yet
            vacancy.approval_status = "approved"
            vacancy.invite_not_before = window_open_dt
            prompt.status = "approved"
            prompt.decided_at = _now
            results.append({"vacancyId": vacancy.id, "result": "approved_at_window"})

    db.session.commit()

    # Mark the persisted assistant message as responded
    message_ids = {p.message_id for p in prompts if p.message_id}
    for message_id in message_ids:
        msg = Message.query.get(message_id)
        if msg and msg.msg_metadata is not None:
            msg.msg_metadata = {
                **msg.msg_metadata,
                "responded": True,
                "response": action,
                "decidedAt": _now.isoformat(),
            }
            msg.save()
            publish({
                "type": "message_edited",
                "payload": serialize_message(msg, None),
            })

    # Send invitations once per instance (not per vacancy)
    for instance in instances_to_trigger.values():
        trigger_invitations(instance, coach_id, now=now)

    return {"action": action, "vacancies": results}
