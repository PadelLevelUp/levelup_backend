def serialize_presence(presence):
    return {
        "id": presence.id,
        "lessonInstanceId": presence.lesson_instance_id,
        "playerId": presence.player_id,
        "status": presence.status,
        "justification": presence.justification,
        "invited": presence.invited,
        "confirmed": presence.confirmed,
        "validated": presence.validated,
        "lateCancellation": presence.late_cancellation,
    }