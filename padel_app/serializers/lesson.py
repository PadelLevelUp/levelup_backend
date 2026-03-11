import json
from padel_app.tools.tools import iso_date
from padel_app.serializers.player import serialize_player
from padel_app.serializers.presence import serialize_presence

def serialize_lesson(lesson):
    recurrence_rule = None
    if lesson.recurrence_rule:
        try:
            recurrence_rule = json.loads(lesson.recurrence_rule)
        except (TypeError, ValueError):
            recurrence_rule = None

    return {
        "id": lesson.id,
        "coachIds": [coach.id for coach in lesson.coaches],
        "type": lesson.type,
        "status": lesson.status,
        "color": lesson.color,
        "maxPlayers": lesson.max_players,
        "levelId": lesson.default_level_id,

        "name": lesson.title,
        "description": lesson.description,

        "isRecurring": lesson.is_recurring,
        "recurrenceRule": recurrence_rule,
        "recurrenceEnd": iso_date(lesson.recurrence_end),

        "startDate": lesson.start_datetime.date().isoformat(),
        "defaultStartTime": lesson.start_datetime.strftime("%H:%M"),
        "defaultEndTime": lesson.end_datetime.strftime("%H:%M"),
    }
    
def serialize_lesson_instance(instance):
    lesson = instance.lesson

    return {
        "id": instance.id,
        "lessonId": instance.lesson_id,

        "date": instance.start_datetime.date().isoformat(),
        "startTime": instance.start_datetime.strftime("%H:%M"),
        "endTime": instance.end_datetime.strftime("%H:%M"),

        "status": instance.status,
        "notes": instance.notes,
        "overriddenFields": instance.overridden_fields,

        "name": lesson.title if lesson else None,
        "color": lesson.color if lesson else None,
        "maxPlayers": instance.max_players,
    }

    
def serialize_class_instance(obj) -> dict:
    """
    Serialize Lesson or LessonInstance into ClassInstance-specific fields.
    Fields already provided by CalendarEvent are intentionally omitted.
    """

    is_instance = obj.model_name == "LessonInstance"
    lesson = obj.lesson if is_instance else obj

    coach_id = (
        lesson.coaches_relations[0].coach.id
        if lesson.coaches_relations
        else None
    )

    data = {
        "coachId": str(coach_id) if coach_id else None,
        "name": lesson.title,
        "levelId": (
            str(lesson.default_level_id)
            if lesson.default_level_id
            else None
        ),
        "participants": [
            serialize_player(rel.player)
            for rel in obj.players_relations
        ],
        "recurrenceEnd": lesson.recurrence_end.isoformat() if lesson.recurrence_end else None,
        "notificationsEnabled": obj.notifications_enabled if hasattr(obj, "notifications_enabled") else True,
    }

    if is_instance:
        data.update(
            {
                "parentClassId": str(lesson.id),
                "notes": obj.notes,
                "overriddenFields": (
                    json.loads(obj.overridden_fields)
                    if obj.overridden_fields
                    else []
                ),
                "presences": [
                    serialize_presence(p)
                    for p in getattr(obj, "presences", [])
                ],
            }
        )
        data["levelId"] = str(obj.level_id) if obj.level_id else data["levelId"]

    return data
