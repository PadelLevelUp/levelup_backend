from datetime import datetime, timezone

from sqlalchemy import func

from padel_app.sql_db import db
from padel_app.models import (
    Message,
    Conversation,
    ConversationParticipant,
    User,
)
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.realtime import publish
from padel_app.serializers.message import serialize_message
from padel_app.utils.push_notifications import send_push_notification


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
        .filter(M.sent_at > func.coalesce(CP.last_read_at, epoch))
        .scalar()
    )

    return int(unread or 0)


def create_message_service(data, user_id):
    """Creates a message and publishes a real-time event."""
    payload = {
        "text": data["text"],
        "conversation": data["conversationId"],
        "sender": user_id,
        "sent_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    message = Message()
    form = message.get_create_form()

    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)

    message.update_with_dict(values)
    message.create()

    sender = User.query.get(user_id)
    recipient_participants = ConversationParticipant.query.filter(
        ConversationParticipant.conversation_id == message.conversation_id,
        ConversationParticipant.user_id != user_id,
    ).all()

    # TODO: SSE currently tracks global queue subscribers only (not user presence),
    # so this fallback sends push unconditionally to recipients. Add user-scoped
    # SSE tracking and skip push for online recipients.
    sender_name = sender.name if sender else "Someone"
    for participant in recipient_participants:
        message_text = data.get("text", "")
        body = f"{sender_name}: {message_text[:100]}" if message_text else f"{sender_name} sent you a message"
        send_push_notification(
            participant.user_id,
            title=sender_name,
            body=body,
            url="/messages",
        )

    publish({
        "type": "message_created",
        "payload": serialize_message(message, None),
    })

    return message


def get_user_conversations(user):
    """Returns all conversations the user participates in."""
    return (
        Conversation.query
        .join(ConversationParticipant)
        .filter(ConversationParticipant.user_id == user.id)
        .all()
    )


def create_conversation_service(data, user):
    """Finds or creates a conversation for the given participants."""
    participants = data['otherParticipants']
    participants.append(user.id)

    key = Conversation.build_participant_key(participants)

    conversation = Conversation.query.filter_by(participant_key=key).first()

    if not conversation:
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

    participation.last_read_at = datetime.utcnow()
    participation.save()
