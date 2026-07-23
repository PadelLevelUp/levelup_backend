from datetime import datetime, date
from typing import Optional, Union
from padel_app.tools.calendar_tools import _format_date, _format_time

def _compute_status(
    start_dt: datetime,
    *,
    override_date: Optional[Union[str, datetime]] = None,
) -> str:
    if override_date:
        if isinstance(override_date, datetime):
            event_date = override_date.date()
        else:
            try:
                # ISO 8601 (e.g. 2026-01-23T13:00:00+00:00)
                event_date = datetime.fromisoformat(
                    override_date.replace("Z", "+00:00")
                ).date()
            except ValueError:
                # Fallback: YYYY-MM-DD
                event_date = datetime.strptime(
                    override_date, "%Y-%m-%d"
                ).date()
    else:
        event_date = start_dt.date()

    return "completed" if event_date < date.today() else "scheduled"

def serialize_calendar_event(obj, *, override_id: str | None = None, override_date: str | None = None) -> dict:
    """
    Serialize LessonInstance, Lesson or CalendarBlock into a CalendarEvent-compatible dict.
    """

    # --- Base fields shared by all events ---
    event = {
        "model": obj.model_name,
        "title": obj.title,
        "originalId": obj.id,
        "id": override_id or f"{obj.model_name.lower()}-{obj.id}",
        "date": override_date or _format_date(obj.start_datetime),
        "startTime": _format_time(obj.start_datetime),
        "endTime": _format_time(obj.end_datetime),
        "status": _compute_status(
            obj.start_datetime, override_date=override_date
        ),
    }

    # --- LessonInstance ---
    if obj.model_name == "LessonInstance":
        lesson = obj.lesson

        event.update(
            {
                "type": "class",
                "classType": lesson.type,
                # PAD-71: effective filled spots (enrolled minus declined), the
                # same value the class-detail "capacity" field shows. Declined
                # students free their spot and must not be counted here.
                "participantCount": obj.effective_filled_spots,
                "maxPlayers": obj.max_players,
                "color": lesson.color,
                "levelId": obj.level_id or lesson.default_level_id,
                "isRecurring": True if lesson.recurrence_rule else False
            }
        )

        return event

    # --- Lesson ---
    if obj.model_name == "Lesson":
        event.update(
            {
                "type": "class",
                "classType": obj.type,
                "maxPlayers": obj.max_players,
                # A Lesson template has no presences (nobody can have declined a
                # class that was never materialized), so enrolment IS the
                # effective filled count here. See LessonInstance.effective_filled_spots.
                "participantCount": len(obj.players_relations),
                "color": obj.color,
                "levelId": obj.default_level_id,
                "isRecurring": True if obj.recurrence_rule else False
            }
        )

        return event

    # --- CalendarBlock ---
    if obj.model_name == "CalendarBlock":
        event.update(
            {
                "type": "block",
                "blockType": obj.type,
                "isRecurring": True if obj.recurrence_rule else False
            }
        )

        return event

    # --- Safety net ---
    raise ValueError(f"Unsupported calendar model: {obj.model_name}")
