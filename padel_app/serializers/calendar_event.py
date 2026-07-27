from datetime import datetime
from typing import Optional, Union
from padel_app.tools.calendar_tools import _format_date, _format_time
from padel_app.utils.dates import utcnow_naive

def _compute_status(
    start_dt: datetime,
    end_dt: datetime,
    *,
    override_date: Optional[Union[str, datetime]] = None,
    now: Optional[datetime] = None,
) -> str:
    """An event is ``completed`` once its END datetime has passed, otherwise
    ``scheduled``.

    PAD-96: the comparison uses the real end datetime (the event's date combined
    with its end time-of-day), NOT just the date. Previously it compared the
    DATE against ``today()``, so a class that already ended earlier *today*
    stayed ``scheduled`` while previous days' classes correctly read
    ``completed``.

    ``now`` defaults to the same naive-UTC clock the scheduler uses to compare
    stored class datetimes (``utcnow_naive``); it is injectable for tests.
    """
    if now is None:
        now = utcnow_naive()

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

    # The effective end is the event's day combined with its end time-of-day —
    # exactly what the UI shows as ``endTime`` on the ``date`` day. ``.time()``
    # drops any tzinfo, keeping the comparison naive on both sides.
    effective_end = datetime.combine(event_date, end_dt.time())

    return "completed" if effective_end <= now else "scheduled"

def serialize_calendar_event(obj, *, override_id: str | None = None, override_date: str | None = None, now: Optional[datetime] = None) -> dict:
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
            obj.start_datetime, obj.end_datetime, override_date=override_date, now=now
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
