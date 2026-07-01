from padel_app.utils.dates import to_utc_iso


def serialize_message(message, last_read_at):
    if message.is_deleted:
        return {
            "id": message.id,
            "senderId": message.sender_id,
            "content": None,
            "timestamp": to_utc_iso(message.sent_at),
            "conversationId": message.conversation_id,
            "isRead": True,
            "status": "read",
            "replyTo": None,
            "edited": False,
            "isDeleted": True,
            "reactions": [],
        }

    is_read = bool(last_read_at and message.sent_at <= last_read_at)

    return {
        "id": message.id,
        "senderId": message.sender_id,
        "content": message.text,
        "timestamp": to_utc_iso(message.sent_at),
        "conversationId": message.conversation_id,
        "isRead": is_read,
        "status": "read" if is_read else "delivered",
        "replyTo": message.reply_to_id,
        "edited": message.edited,
        "isDeleted": False,
        "reactions": [
            {"emoji": r.emoji, "userId": r.user_id}
            for r in (message.reactions or [])
        ],
        "messageType": getattr(message, "message_type", "text") or "text",
        "metadata": getattr(message, "msg_metadata", None),
    }
