import json
from padel_app.tools.tools import iso_date

def serialize_calendar_block(block):
    recurrence_rule = None
    if block.recurrence_rule:
        try:
            recurrence_rule = json.loads(block.recurrence_rule)
        except (TypeError, ValueError):
            recurrence_rule = None

    return {
        "id": block.id,
        "userId": block.user_id,
        "type": block.type,

        "title": block.title,
        "description": block.description,

        "isRecurring": block.is_recurring,
        "recurrenceRule": recurrence_rule,
        "recurrenceEnd": iso_date(block.recurrence_end),

        "blocksAutoInvitations": bool(getattr(block, "blocks_auto_invitations", False)),

        "date": (
            block.start_datetime.date().isoformat()
            if block.start_datetime
            else None
        ),
        "startTime": (
            block.start_datetime.strftime("%H:%M")
            if block.start_datetime
            else None
        ),
        "endTime": (
            block.end_datetime.strftime("%H:%M")
            if block.end_datetime
            else None
        ),
    }
