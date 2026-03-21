from datetime import datetime

from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from padel_app.models import Lesson, LessonInstance, User
from padel_app.services.lesson_service import get_or_materialize_instance
from padel_app.services.notification_service import (
    get_config_dict,
    update_config,
    send_manual_notifications,
    trigger_invitations,
    process_invitation_batches,
    get_notification_activity,
    get_notification_groups,
    get_waiting_list,
    respond_to_notification,
    respond_to_reminder,
    respond_to_waiting_list,
    coach_respond_to_notification,
    get_standing_waiting_list,
    add_standing_waiting_list_entry,
    remove_standing_waiting_list_entry,
)

bp = Blueprint("notification_engine_api", __name__, url_prefix="/api/app/notify")


def _current_coach():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    if not user.coach:
        abort(403)
    return user.coach


def _resolve_instance(model: str, original_id: int, date_str: str | None) -> LessonInstance:
    if model.lower() == "lessoninstance":
        return LessonInstance.query.get_or_404(original_id)
    lesson = Lesson.query.get_or_404(original_id)
    if not date_str:
        abort(400, "date is required for Lesson events")
    date = datetime.strptime(date_str, "%Y-%m-%d").date()
    return get_or_materialize_instance(lesson, date)


@bp.get("/config")
@jwt_required()
def get_config():
    coach = _current_coach()
    return jsonify(get_config_dict(coach.id))


@bp.post("/config")
@jwt_required()
def save_config():
    coach = _current_coach()
    data = request.get_json() or {}
    update_config(coach.id, data)
    return jsonify(get_config_dict(coach.id))


@bp.post("/toggle_class")
@jwt_required()
def toggle_class_notifications():
    _current_coach()
    data = request.get_json() or {}
    model = data.get("model", "LessonInstance")
    original_id = int(data.get("originalId"))
    if model.lower() == "lessoninstance":
        obj = LessonInstance.query.get_or_404(original_id)
    else:
        obj = Lesson.query.get_or_404(original_id)
    obj.notifications_enabled = not obj.notifications_enabled
    obj.save()
    return jsonify({"notificationsEnabled": obj.notifications_enabled})


@bp.post("/manual")
@jwt_required()
def manual_notify():
    coach = _current_coach()
    data = request.get_json() or {}
    model = data.get("model", "LessonInstance")
    original_id = int(data.get("originalId"))
    date_str = data.get("date")
    player_ids = [int(pid) for pid in data.get("playerIds", [])]
    if not player_ids:
        return jsonify({"error": "No player IDs provided"}), 400
    instance = _resolve_instance(model, original_id, date_str)
    events = send_manual_notifications(instance.id, player_ids, coach.id)
    return jsonify({"sent": len(events)})


@bp.get("/groups")
@jwt_required()
def student_groups():
    coach = _current_coach()
    model = request.args.get("model", "LessonInstance")
    original_id = int(request.args.get("originalId"))
    date_str = request.args.get("date")
    groups = get_notification_groups(model, original_id, date_str, coach.id)
    return jsonify(groups)


@bp.get("/activity")
@jwt_required()
def activity():
    coach = _current_coach()
    items = get_notification_activity(coach.id)
    return jsonify(items)


@bp.post("/respond")
@jwt_required()
def respond():
    """Called by the player when they press Yes or No on a notification invite message."""
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    notification_event_id = int(data.get("notificationEventId"))
    action = data.get("action")  # "yes" | "no"
    result = respond_to_notification(notification_event_id, action, user_id)
    return jsonify(result)


@bp.post("/respond_reminder")
@jwt_required()
def respond_reminder_endpoint():
    """Called by the player when they press Yes or No on a reminder message."""
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    lesson_instance_id = int(data.get("lessonInstanceId"))
    action = data.get("action")  # "yes" | "no"
    result = respond_to_reminder(lesson_instance_id, action, user_id)
    return jsonify(result)


@bp.post("/respond_waiting_list")
@jwt_required()
def respond_waiting_list_endpoint():
    """Called by the player when they press Yes or No on a waiting list offer."""
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    lesson_instance_id = int(data.get("lessonInstanceId"))
    action = data.get("action")  # "yes" | "no"
    result = respond_to_waiting_list(lesson_instance_id, action, user_id)
    return jsonify(result)


@bp.get("/waiting_list/<int:instance_id>")
@jwt_required()
def waiting_list(instance_id: int):
    """Get active waiting list entries for a class instance."""
    coach = _current_coach()
    entries = get_waiting_list(instance_id, coach.id)
    return jsonify(entries)


@bp.post("/coach_respond")
@jwt_required()
def coach_respond():
    """Coach manually records a player's Yes/No response to an invitation."""
    coach = _current_coach()
    data = request.get_json() or {}
    notification_event_id = int(data.get("notificationEventId"))
    action = data.get("action")  # "yes" | "no"
    result = coach_respond_to_notification(notification_event_id, action, coach.id)
    return jsonify(result)


@bp.post("/process_rounds")
@jwt_required()
def process_rounds():
    """Intended for cron job / periodic polling. Processes invitation batches."""
    processed = process_invitation_batches()
    return jsonify({"processed": processed})


@bp.get("/standing_waiting_list")
@jwt_required()
def standing_waiting_list_get():
    """Get all active standing waiting list entries for this coach."""
    coach = _current_coach()
    entries = get_standing_waiting_list(coach.id)
    return jsonify(entries)


@bp.post("/standing_waiting_list")
@jwt_required()
def standing_waiting_list_add():
    """Add a player to the standing waiting list."""
    coach = _current_coach()
    data = request.get_json() or {}
    player_id = int(data.get("playerId"))
    credits_total = int(data.get("credits", 3))
    duration_days = int(data.get("durationDays", 30))
    entry = add_standing_waiting_list_entry(coach.id, player_id, credits_total, duration_days)
    entries = get_standing_waiting_list(coach.id)
    added = next((e for e in entries if e["id"] == entry.id), None)
    return jsonify(added), 201


@bp.delete("/standing_waiting_list/<int:entry_id>")
@jwt_required()
def standing_waiting_list_remove(entry_id: int):
    """Remove a player from the standing waiting list."""
    coach = _current_coach()
    remove_standing_waiting_list_entry(entry_id, coach.id)
    return jsonify({"removed": True})
