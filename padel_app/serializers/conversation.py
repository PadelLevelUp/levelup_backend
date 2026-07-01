from padel_app.serializers.message import serialize_message
from padel_app.utils.dates import to_utc_iso

def serialize_conversation(conversation, user_id):
    messages = sorted(conversation.messages, key=lambda m: m.sent_at)
    last_message = messages[-1] if messages else None

    conversation_participation = next(
        p for p in conversation.participants
        if p.user_id != user_id
    )
    
    conversation_participation_own = next(
        p for p in conversation.participants
        if p.user_id == user_id
    )

    participant = conversation_participation.user

    last_read_at = conversation_participation_own.last_read_at

    unread_count = sum(
        1 for m in messages
        if m.sender_id != user_id
        and (not last_read_at or m.sent_at > last_read_at)
    )

    return {
        "id": conversation.id,
        "participantId": participant.id,
        "participantName": participant.name,
        "participantAvatar": getattr(participant, "avatar_url", None),
        "isAssistant": participant.username == "levelup-assistant",

        "lastMessage": last_message.text if last_message else None,
        "lastMessageAt": (
            to_utc_iso(last_message.sent_at)
            if last_message
            else None
        ),

        "unreadCount": unread_count,
    }

def serialize_conversation_detail(conversation, user_id):
    last_read_at = conversation.last_read_by(user_id)

    return {
        **serialize_conversation(conversation, user_id),
        "messages": [
            serialize_message(m, last_read_at)
            for m in sorted(conversation.messages, key=lambda m: m.sent_at)
        ],
    }
