from datetime import datetime, timezone

from padel_app.utils.dates import utcnow_naive

from flask import abort
from sqlalchemy import func, nullslast, or_, and_

from padel_app.sql_db import db
from padel_app.models import (
    Message,
    MessageReaction,
    MessageReport,
    BlockedUser,
    Conversation,
    ConversationParticipant,
    User,
    Coach,
)
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.realtime import publish
from padel_app.serializers.message import serialize_message
from padel_app.utils.push_notifications import send_push_notification
from padel_app.utils.expo_push import send_expo_push_to_user


def _is_blocked_either_way(user_a_id, user_b_id):
    """True if either user has blocked the other."""
    return (
        db.session.query(BlockedUser.id)
        .filter(
            or_(
                and_(BlockedUser.blocker_id == user_a_id, BlockedUser.blocked_id == user_b_id),
                and_(BlockedUser.blocker_id == user_b_id, BlockedUser.blocked_id == user_a_id),
            )
        )
        .first()
        is not None
    )


def _messageable_target_ids_for(user):
    """The set of user ids `user` is allowed to START a new conversation with.

    Coach -> players belonging to any club the coach is in.
    Everyone else (student/player) -> any coach.
    """
    coach = getattr(user, "coach", None)
    if coach:
        return {
            player.user_id
            for club in coach.clubs
            for player in club.players
        }

    return {
        row.user_id
        for row in (
            Coach.query.join(User, Coach.user_id == User.id)
            .filter(User.status == "active")
            .all()
        )
    }


def _assert_messageable(user, target_id):
    if target_id not in _messageable_target_ids_for(user):
        abort(403, "You are not allowed to message this user")


def block_user_service(blocker_id, blocked_id):
    """Block a user. Idempotent."""
    if blocker_id == blocked_id:
        abort(400, "Cannot block yourself")
    existing = BlockedUser.query.filter_by(blocker_id=blocker_id, blocked_id=blocked_id).first()
    if not existing:
        BlockedUser(blocker_id=blocker_id, blocked_id=blocked_id).create()


def unblock_user_service(blocker_id, blocked_id):
    """Unblock a user. Idempotent."""
    existing = BlockedUser.query.filter_by(blocker_id=blocker_id, blocked_id=blocked_id).first()
    if existing:
        existing.delete()


def get_blocked_users_service(user_id):
    """Users that `user_id` has blocked."""
    rows = BlockedUser.query.filter_by(blocker_id=user_id).all()
    return [row.blocked for row in rows]


def get_messageable_users_service(user):
    """Users `user` may start a new conversation with, excluding blocks either way."""
    target_ids = _messageable_target_ids_for(user)
    target_ids.discard(user.id)
    if not target_ids:
        return []

    blocked_rows = BlockedUser.query.filter(
        or_(
            and_(BlockedUser.blocker_id == user.id, BlockedUser.blocked_id.in_(target_ids)),
            and_(BlockedUser.blocked_id == user.id, BlockedUser.blocker_id.in_(target_ids)),
        )
    ).all()
    blocked_ids = {
        row.blocked_id if row.blocker_id == user.id else row.blocker_id
        for row in blocked_rows
    }

    return (
        User.query.filter(User.id.in_(target_ids - blocked_ids), User.status == "active")
        .all()
    )


def report_message_service(reporter_id, message_id, reason=None):
    """Report a message. Only participants of that message's conversation may report it."""
    message = Message.query.get_or_404(message_id)
    is_participant = (
        ConversationParticipant.query.filter_by(
            conversation_id=message.conversation_id, user_id=reporter_id
        ).first()
        is not None
    )
    if not is_participant:
        abort(403, "You cannot report a message outside your conversations")

    report = MessageReport(reporter_id=reporter_id, message_id=message_id, reason=reason)
    report.create()
    return report


def get_unread_count(user_id):
    """Returns the number of unread messages for a user."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    CP = ConversationParticipant
    M = Message

    unread = (
        db.session.query(func.count(M.id))
        .join(CP, CP.conversation_id == M.conversation_id)
        .filter(CP.user_id == user_id)
        .filter(M.sender_id != user_id)
        .filter(M.is_deleted == False)
        .filter(M.sent_at > func.coalesce(CP.last_read_at, epoch))
        .scalar()
    )

    return int(unread or 0)


def create_message_service(data, user_id):
    """Creates a message and publishes a real-time event."""
    recipient_participants = ConversationParticipant.query.filter(
        ConversationParticipant.conversation_id == data["conversationId"],
        ConversationParticipant.user_id != user_id,
    ).all()

    for participant in recipient_participants:
        if _is_blocked_either_way(user_id, participant.user_id):
            abort(403, "Cannot message a blocked user")

    payload = {
        "text": data["text"],
        "conversation": data["conversationId"],
        "sender": user_id,
        # UTC, to match last_read_at (mark_conversation_read_service uses
        # utcnow_naive) and the unread query `sent_at > last_read_at`. Using
        # local time here stamped fresh messages ~1h in the future under a +1
        # offset, so a just-read message stayed "unread" until UTC caught up
        # (PAD-66).
        "sent_at": utcnow_naive().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    message = Message()
    form = message.get_create_form()

    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)

    message.update_with_dict(values)
    message.reply_to_id = data.get("replyToId")
    message.create()

    sender = User.query.get(user_id)

    sender_name = sender.name if sender else "Someone"
    for participant in recipient_participants:
        message_text = data.get("text", "")
        body = message_text[:100] if message_text else "Sent you a message"
        send_push_notification(
            participant.user_id,
            title=sender_name,
            body=body,
            url=f"/messages/{message.conversation_id}",
        )
        send_expo_push_to_user(
            participant.user_id,
            title=sender_name,
            body=body,
            data={"type": "message", "conversationId": message.conversation_id},
        )

    publish({
        "type": "message_created",
        "payload": serialize_message(message, None),
    })

    return message


def edit_message_service(message_id, new_text, user_id):
    """Edit a message. Only the sender may edit."""
    message = Message.query.get_or_404(message_id)
    if message.sender_id != user_id:
        abort(403, "Not your message")
    message.text   = new_text
    message.edited = True
    message.save()
    publish({
        "type": "message_edited",
        "payload": serialize_message(message, None),
    })
    return message


def delete_message_service(message_id, user_id):
    """Soft-delete a message. Only the sender may delete."""
    message = Message.query.get_or_404(message_id)
    if message.sender_id != user_id:
        abort(403, "Not your message")
    message.is_deleted = True
    message.save()
    publish({
        "type": "message_deleted",
        "payload": {
            "id": message_id,
            "conversationId": message.conversation_id,
        },
    })


def toggle_reaction_service(message_id, emoji, user_id):
    """Add or remove a reaction (toggle)."""
    existing = MessageReaction.query.filter_by(
        message_id=message_id, user_id=user_id, emoji=emoji
    ).first()
    if existing:
        existing.delete()
    else:
        MessageReaction(message_id=message_id, user_id=user_id, emoji=emoji).create()

    message = Message.query.get_or_404(message_id)
    publish({
        "type": "message_reaction",
        "payload": serialize_message(message, None),
    })


def get_user_conversations(user, page=1, limit=20):
    """Returns paginated conversations the user participates in, ordered by last message descending."""
    offset = (page - 1) * limit
    last_message_at = (
        db.session.query(func.max(Message.sent_at))
        .filter(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    query = (
        Conversation.query
        .join(ConversationParticipant)
        .filter(ConversationParticipant.user_id == user.id)
        .order_by(nullslast(last_message_at.desc()))
        .offset(offset)
    )
    convs = query.limit(limit + 1).all()
    has_more = len(convs) > limit
    return {"conversations": convs[:limit], "has_more": has_more}


def create_conversation_service(data, user):
    """Finds or creates a conversation for the given participants."""
    participants = [int(p) for p in data['otherParticipants']]
    participants.append(user.id)

    key = Conversation.build_participant_key(participants)

    conversation = Conversation.query.filter_by(participant_key=key).first()

    if not conversation:
        other_ids = [p for p in participants if p != user.id]
        for other_id in other_ids:
            if _is_blocked_either_way(user.id, other_id):
                abort(403, "Cannot start a conversation with a blocked user")
            _assert_messageable(user, other_id)

        payload = {
            "is_group": len(participants) >= 2 or False,
            "participant_ids": participants,
            "creator_id": user.id,
            "participant_key": key,
        }

        conversation = Conversation()
        form = conversation.get_create_form()

        fake_request = JsonRequestAdapter(payload, form)
        values = form.set_values(fake_request)

        conversation.update_with_dict(values)
        conversation.create()

        for participant_id in payload.get("participant_ids", []):
            ConversationParticipant(
                conversation_id=conversation.id,
                user_id=participant_id,
            ).create()

    return conversation, user.id


def mark_conversation_read_service(conversation_id, user):
    """Marks a conversation as read for the given user."""
    participation = (
        ConversationParticipant.query
        .filter_by(
            conversation_id=conversation_id,
            user_id=user.id,
        )
        .first_or_404()
    )

    participation.last_read_at = utcnow_naive()
    participation.save()
