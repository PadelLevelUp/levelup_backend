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

    
def serialize_class_instance(obj, viewer_player_id=None) -> dict:
    """
    Serialize Lesson or LessonInstance into ClassInstance-specific fields.
    Fields already provided by CalendarEvent are intentionally omitted.

    Role-based visibility (PAD-36):
    - Coaches (``viewer_player_id`` is None) get the full payload: the complete
      participant list, everyone's presences, and the full notification
      (invitation) log.
    - Students (``viewer_player_id`` set to the requesting player's id) get a
      restricted payload that only ever exposes their OWN data: no other
      students appear in ``participants``, ``presences`` or ``invitations``.
    """

    is_student = viewer_player_id is not None

    is_instance = obj.model_name == "LessonInstance"
    lesson = obj.lesson if is_instance else obj

    coach_id = (
        lesson.coaches_relations[0].coach.id
        if lesson.coaches_relations
        else None
    )

    participants = [
        serialize_player(rel.player)
        for rel in obj.players_relations
        if not is_student or rel.player_id == viewer_player_id
    ]

    data = {
        "coachId": str(coach_id) if coach_id else None,
        "name": obj.title,
        "levelId": (
            str(lesson.default_level_id)
            if lesson.default_level_id
            else None
        ),
        "participants": participants,
        "recurrenceEnd": lesson.recurrence_end.isoformat() if lesson.recurrence_end else None,
        "notificationsEnabled": obj.notifications_enabled if hasattr(obj, "notifications_enabled") else True,
    }

    if is_instance:
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.lesson_instance_training import LessonInstanceTraining

        notification_query = NotificationEvent.query.filter_by(
            lesson_instance_id=obj.id
        )
        if is_student:
            notification_query = notification_query.filter_by(
                player_id=viewer_player_id
            )
        notification_events = notification_query.all()

        training_rows = LessonInstanceTraining.query.filter_by(
            lesson_instance_id=obj.id
        ).all()

        presences = [
            serialize_presence(p)
            for p in getattr(obj, "presences", [])
            if not is_student or p.player_id == viewer_player_id
        ]

        data.update(
            {
                "parentClassId": str(lesson.id),
                "notes": obj.notes,
                "overriddenFields": (
                    json.loads(obj.overridden_fields)
                    if obj.overridden_fields
                    else []
                ),
                "presences": presences,
                "invitations": [
                    {
                        "id": ev.id,
                        "playerId": str(ev.player_id),
                        "playerName": (
                            ev.player.user.name
                            if ev.player and ev.player.user
                            else "Unknown"
                        ),
                        "status": ev.status,
                    }
                    for ev in notification_events
                ],
                "plannedExerciseIds": [str(t.exercise_id) for t in training_rows],
            }
        )
        data["levelId"] = str(obj.level_id) if obj.level_id else data["levelId"]

    return data
