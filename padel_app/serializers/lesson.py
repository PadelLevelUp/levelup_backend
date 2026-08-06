import json
from padel_app.tools.tools import iso_date
from padel_app.serializers.player import serialize_player
from padel_app.serializers.presence import serialize_presence


# How "meaningful" each invitation status is when a student ends up with more
# than one NotificationEvent for the same class instance (PAD-72). An actual
# response always beats a still-pending invite:
#   confirmed  — the student said yes
#   expired    — the student said no / the invite lapsed (rendered "declined")
#   sent       — delivered, awaiting an answer
#   queued     — not delivered yet (semi-automatic mode, batching)
_INVITATION_STATUS_RANK = {
    "confirmed": 3,
    "expired": 2,
    "sent": 1,
    "queued": 0,
}


def _invitation_precedence(event):
    """Sort key for picking the surviving NotificationEvent of a student.

    Highest wins: most meaningful status first, then the most recent record
    (later round, then later row).
    """
    return (
        _INVITATION_STATUS_RANK.get(event.status, -1),
        event.round_number or 0,
        event.id or 0,
    )


def dedupe_invitation_events(events):
    """Collapse NotificationEvents to at most one per student (PAD-72).

    The invitation engine legitimately writes one row per invite SENT — a
    student can be invited across several rounds, receive a manual invite on
    top of an automatic one, or be re-invited after declining. The coach's
    guest list is keyed by STUDENT, so only the most meaningful record per
    player survives (see ``_invitation_precedence``); its own ``id`` is kept so
    coach response actions still target a real NotificationEvent.

    Order is stable: students keep the position of their FIRST invite record,
    so the list still reads chronologically.
    """
    winners = {}
    order = []
    for event in events:
        player_id = event.player_id
        if player_id not in winners:
            winners[player_id] = event
            order.append(player_id)
        elif _invitation_precedence(event) > _invitation_precedence(winners[player_id]):
            winners[player_id] = event
    return [winners[player_id] for player_id in order]


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
        from datetime import timedelta
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.lesson_instance_training import LessonInstanceTraining
        from padel_app.models.notification_config import (
            NotificationConfig,
            DEFAULT_CANCELLATION_DEADLINE_HOURS,
        )

        notification_query = NotificationEvent.query.filter_by(
            lesson_instance_id=obj.id
        )
        if is_student:
            notification_query = notification_query.filter_by(
                player_id=viewer_player_id
            )
        # One row per STUDENT, not per invite record (PAD-72).
        notification_events = dedupe_invitation_events(
            notification_query.order_by(NotificationEvent.id).all()
        )

        training_rows = LessonInstanceTraining.query.filter_by(
            lesson_instance_id=obj.id
        ).all()

        presences = [
            serialize_presence(p)
            for p in getattr(obj, "presences", [])
            if not is_student or p.player_id == viewer_player_id
        ]

        # Effective cancellation deadline for this instance (PAD-43) so the
        # frontend can render deadline UX. Falls back to the default when the
        # coach has no config.
        deadline_hours = DEFAULT_CANCELLATION_DEADLINE_HOURS
        if coach_id is not None:
            config = NotificationConfig.query.filter_by(coach_id=coach_id).first()
            if config is not None:
                deadline_hours = config.get_cancellation_deadline_hours()
        cancellation_deadline = None
        if obj.start_datetime is not None:
            cancellation_deadline = (
                obj.start_datetime - timedelta(hours=deadline_hours)
            ).isoformat()

        # PAD-73: the proactive-decline window. Computed by the SAME server
        # helper that `cancel_attendance` uses to classify a decline, so the
        # frontend can never offer the proactive action at a moment the server
        # would refuse to treat as proactive. Imported lazily because
        # notification_service imports serializers — a module-level import here
        # would create a cycle.
        from padel_app.services.notification_service import (
            proactive_decline_deadline,
            proactive_decline_window_is_open,
        )

        proactive_config = None
        if coach_id is not None:
            proactive_config = NotificationConfig.query.filter_by(
                coach_id=coach_id
            ).first()
        proactive_deadline_dt = proactive_decline_deadline(obj, proactive_config)
        can_decline_proactively = proactive_decline_window_is_open(
            obj, proactive_config
        )

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
                "cancellationDeadlineHours": deadline_hours,
                "cancellationDeadline": cancellation_deadline,
                "proactiveDeclineDeadline": (
                    proactive_deadline_dt.isoformat()
                    if proactive_deadline_dt is not None
                    else None
                ),
                "canDeclineProactively": can_decline_proactively,
            }
        )
        data["levelId"] = str(obj.level_id) if obj.level_id else data["levelId"]

    return data
