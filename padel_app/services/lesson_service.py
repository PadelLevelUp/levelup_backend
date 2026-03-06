from datetime import datetime, timedelta, time
import json

from padel_app.sql_db import db
from padel_app.models import (
    Lesson,
    LessonInstance,
    Presence,
    Association_CoachLesson,
    Association_PlayerLesson,
    Association_PlayerLessonInstance,
    Association_CoachLessonInstance,
)
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.tools.calendar_tools import build_datetime, _format_time
from padel_app.helpers.calendar_helpers import (
    load_lessons_for_coach,
    load_lesson_instances_for_coach,
    build_lesson_events,
)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def update_recurrence_weekday(lesson, old_date, new_date):
    if not lesson.recurrence_rule:
        return

    rule = json.loads(lesson.recurrence_rule)
    days = set(rule.get("daysOfWeek", []))

    old_wd = old_date.weekday() + 1
    new_wd = new_date.weekday() + 1

    if old_wd in days:
        days.remove(old_wd)

    days.add(new_wd)
    rule["daysOfWeek"] = sorted(days)
    return json.dumps(rule)


def transform_to_datetime(obj, data):
    date = data.get('date')
    start_time = data.get('start_time') if data.get('start_time') else _format_time(obj.start_datetime)
    end_time = data.get('end_time') if data.get('end_time') else _format_time(obj.end_datetime)

    data["start_datetime"] = build_datetime(date, start_time)
    data["end_datetime"] = build_datetime(date, end_time)
    return data


# ---------------------------------------------------------------------------
# Lesson instance helpers
# ---------------------------------------------------------------------------

def get_or_materialize_instance(lesson: Lesson, date):
    instance = LessonInstance.query.filter_by(
        lesson_id=lesson.id,
        start_datetime=datetime.combine(date, lesson.start_datetime.time()),
    ).first()

    if instance:
        return instance

    instance = LessonInstance(
        lesson_id=lesson.id,
        start_datetime=datetime.combine(date, lesson.start_datetime.time()),
        end_datetime=datetime.combine(date, lesson.end_datetime.time()),
        status="scheduled",
        max_players=lesson.max_players,
    )

    instance.add_to_session()
    instance.flush()

    for rel in lesson.players_relations:
        Presence(
            lesson_instance_id=instance.id,
            player_id=rel.player_id,
            invited=True,
            confirmed=False,
            validated=False,
        ).add_to_session()

    instance.save()
    return instance


def create_lesson_instance_helper(data, parent_lesson=None):
    if not parent_lesson and not data.get('lesson_id'):
        raise ValueError('Need connection to parent lesson')
    if not parent_lesson:
        parent_lesson = Lesson.query.get_or_404(data.get('lesson_id'))

    data = transform_to_datetime(parent_lesson, data)
    data['lesson'] = parent_lesson.id
    data['max_players'] = data['max_players'] or parent_lesson.max_players
    data['overwrite_title'] = data.get('title')

    lesson_instance = LessonInstance()
    form = lesson_instance.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    lesson_instance.update_with_dict(values)
    lesson_instance.create()

    add_ids = {
        int(pid)
        for pid in data.get('add_player_ids', [])
        if pid is not None
    }

    remove_ids = {
        int(pid)
        for pid in data.get('remove_player_ids', [])
        if pid is not None
    }

    existing_ids = [
        int(rel.player_id)
        for rel in parent_lesson.players_relations
        if rel.player_id is not None
    ]

    seen = set()
    player_ids = [
        pid for pid in existing_ids + list(add_ids)
        if pid not in remove_ids and not (pid in seen or seen.add(pid))
    ]

    for pid in player_ids:
        Association_PlayerLessonInstance(
            player_id=pid,
            lesson_instance_id=lesson_instance.id,
        ).create()

    for rel in parent_lesson.coaches_relations:
        Association_CoachLessonInstance(
            coach_id=rel.coach_id,
            lesson_instance_id=lesson_instance.id,
        ).create()

    return lesson_instance


def edit_lesson_instance_helper(data, lesson_instance=None):
    if not lesson_instance and not data.get("lesson_instance_id"):
        raise ValueError("Need lesson_instance or lesson_instance_id")

    if not lesson_instance:
        lesson_instance = LessonInstance.query.get_or_404(
            data.get("lesson_instance_id")
        )

    data = transform_to_datetime(lesson_instance, data)
    data['overwrite_title'] = data.get('title')

    form = lesson_instance.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    lesson_instance.update_with_dict(values)
    lesson_instance.save()

    for player_id in data.get("add_player_ids", []):
        Association_PlayerLessonInstance(
            player_id=player_id,
            lesson_instance_id=lesson_instance.id,
        ).create()

    for player_id in data.get("remove_player_ids", []):
        rel = Association_PlayerLessonInstance.query.filter_by(
            player_id=player_id,
            lesson_instance_id=lesson_instance.id,
        ).first()
        rel.delete()
        presence = Presence.query.filter_by(
            player_id=player_id,
            lesson_instance_id=lesson_instance.id,
        ).first()
        if presence:
            presence.delete()

    return lesson_instance


def add_presences(lesson_instance, payload):
    created_presences = []

    for item in payload:
        player_id = item.get('playerId')
        lesson_instance_id = lesson_instance.id
        data = {
            "status": item.get('status'),
            "justification": item.get('justification'),
            "invited": True,
            "confirmed": True,
            "validated": True,
        }

        existing = Presence.query.filter_by(
            lesson_instance_id=lesson_instance_id,
            player_id=player_id,
        ).first()

        if existing:
            presence_obj = existing
            form = presence_obj.get_edit_form()
        else:
            presence_obj = Presence(
                player_id=player_id,
                lesson_instance_id=lesson_instance_id,
            )
            form = presence_obj.get_create_form()

        fake_request = JsonRequestAdapter(data, form)
        values = form.set_values(fake_request)

        presence_obj.update_with_dict(values)

        if existing:
            presence_obj.save()
        else:
            presence_obj.create()

        created_presences.append(presence_obj)

    return created_presences


# ---------------------------------------------------------------------------
# Lesson helpers
# ---------------------------------------------------------------------------

def create_lesson_helper(data):
    lesson = Lesson()
    form = lesson.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    lesson.update_with_dict(values)
    lesson.create()

    if data.get("coach"):
        Association_CoachLesson(
            coach_id=data["coach"],
            lesson_id=lesson.id,
        ).create()

    if data.get("player_ids"):
        for player_id in data.get("player_ids"):
            Association_PlayerLesson(
                player_id=player_id,
                lesson_id=lesson.id,
            ).create()

    return lesson


def edit_lesson_helper(data, lesson=None):
    if not lesson and not data.get("lesson_id"):
        raise ValueError("Need lesson or lesson_id")

    if not lesson:
        lesson = Lesson.query.get_or_404(data.get("lesson_id"))

    data = transform_to_datetime(lesson, data)
    if data.get("event_date") and data.get("date"):
        new_date = datetime.strptime(data["date"], "%Y-%m-%d").date()

        recurrence_rule = update_recurrence_weekday(
            lesson,
            old_date=data["event_date"],
            new_date=new_date,
        )
        data['recurrence_rule'] = recurrence_rule

    form = lesson.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    lesson.update_with_dict(values)
    lesson.save()

    """ if "coach" in data:
        Association_CoachLesson.query.filter_by(
            lesson_id=lesson.id
        ).delete()

        if data["coach"]:
            Association_CoachLesson(
                coach_id=data["coach"],
                lesson_id=lesson.id,
            ).create() """

    for player_id in data.get("add_player_ids", []):
        Association_PlayerLesson(
            player_id=player_id,
            lesson_id=lesson.id,
        ).create()

    for player_id in data.get("remove_player_ids", []):
        Association_PlayerLesson.query.filter_by(
            player_id=player_id,
            lesson_id=lesson.id,
        ).delete()

    return lesson


def duplicate_lesson_helper(old_lesson):
    new_lesson = Lesson(
        title=old_lesson.title,
        type=old_lesson.type,
        status=old_lesson.status,
        color=old_lesson.color,
        max_players=old_lesson.max_players,
        default_level_id=old_lesson.default_level_id,
        is_recurring=old_lesson.is_recurring,
        recurrence_rule=old_lesson.recurrence_rule,
        recurrence_end=old_lesson.recurrence_end,
        start_datetime=old_lesson.start_datetime,
        end_datetime=old_lesson.end_datetime,
        club_id=old_lesson.club_id,
    )

    new_lesson.create()

    if old_lesson.coaches:
        for rel in old_lesson.coaches_relations:
            Association_CoachLesson(
                coach_id=rel.coach_id,
                lesson_id=new_lesson.id,
            ).create()

    if old_lesson.players_relations:
        for rel in old_lesson.players_relations:
            Association_PlayerLesson(
                player_id=rel.player_id,
                lesson_id=new_lesson.id,
            ).create()

    return new_lesson


def delete_future_instances(lesson, cutoff):
    instances = LessonInstance.query.filter(
        LessonInstance.lesson_id == lesson.id,
        LessonInstance.start_datetime >= cutoff,
    ).all()
    for instance in instances:
        instance.delete()
    return True


def split_lesson(lesson, date, remove_current_date=False):
    recurrence_start = date + timedelta(days=1) if remove_current_date else date
    original_recurrence_end = lesson.recurrence_end

    new_start = datetime.combine(recurrence_start, lesson.start_datetime.time())
    new_end = datetime.combine(recurrence_start, lesson.end_datetime.time())

    new_lesson = duplicate_lesson_helper(lesson)

    lesson.recurrence_end = date

    new_lesson.start_datetime = new_start
    new_lesson.end_datetime = new_end
    new_lesson.recurrence_end = original_recurrence_end

    instances_to_move = [
        inst for inst in lesson.instances
        if inst.start_datetime.date() >= recurrence_start
    ]

    for instance in instances_to_move:
        instance.lesson = new_lesson

    lesson.save()
    new_lesson.save()

    return lesson, new_lesson


# ---------------------------------------------------------------------------
# Route-level service functions
# ---------------------------------------------------------------------------

def edit_lesson_from_data(lesson, data):
    """Applies form-data edits to a Lesson, including datetime and recurrence fields."""
    form = lesson.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    lesson.update_with_dict(values)

    if "startDate" in data and "defaultStartTime" in data:
        lesson.start_datetime = datetime.fromisoformat(
            f'{data["startDate"]}T{data["defaultStartTime"]}'
        )

    if "startDate" in data and "defaultEndTime" in data:
        lesson.end_datetime = datetime.fromisoformat(
            f'{data["startDate"]}T{data["defaultEndTime"]}'
        )

    if "endDate" in data:
        lesson.recurrence_end = (
            datetime.fromisoformat(data["endDate"])
            if data["endDate"]
            else None
        )

    lesson.save()
    return lesson


def add_class_service(data, coach, club):
    """Builds a lesson payload from frontend add_class data and creates the lesson."""
    lesson_payload = {
        "title": data["name"],
        "type": data["classType"],
        "status": "active",
        "color": data.get("color"),
        "max_players": data["maxPlayers"],
        "level": data.get("levelId"),
        "is_recurring": data.get("isRecurring", False),
        "start_datetime": build_datetime(data["date"], data["startTime"]),
        "end_datetime": build_datetime(data["date"], data["endTime"]),
        "club": club.id,
        "coach": coach.id,
        "player_ids": data.get("playerIds", []),
    }

    if data.get("isRecurring"):
        lesson_payload["recurrence_rule"] = json.dumps(data.get("recurrenceRule"))
        lesson_payload["recurrence_end"] = data.get("endDate")

    return create_lesson_helper(lesson_payload)


def confirm_presences_service(class_instance_data, presences_data):
    """Materialises an instance if needed and records presences."""
    if 'parentClassId' in class_instance_data.keys():
        lesson = Lesson.query.get_or_404(class_instance_data.get('originalId'))
        payload = lesson.to_instance_data()
        payload['date'] = class_instance_data['date']
        payload['original_lesson_occurence_date'] = class_instance_data['date']
        instance = create_lesson_instance_helper(data=payload, parent_lesson=lesson)
    else:
        instance = LessonInstance.query.get_or_404(class_instance_data.get('originalId'))

    return add_presences(instance, presences_data)


def update_lesson_status_service(lesson_id, data):
    """Materialises an instance for the given date and sets its status."""
    lesson = Lesson.query.get_or_404(lesson_id)
    date = datetime.fromisoformat(data["date"]).date()
    instance = get_or_materialize_instance(lesson, date)
    instance.status = data["status"]  # canceled | completed
    instance.save()
    return instance


def get_lesson_instances_in_range(coach, range_start, range_end):
    """Returns serialised lesson events for a coach in the given date range."""
    lessons = load_lessons_for_coach(coach.id, range_start, range_end)
    instances_by_key = load_lesson_instances_for_coach(coach.id, range_start, range_end)
    return build_lesson_events(lessons, instances_by_key, range_start, range_end)


# ---------------------------------------------------------------------------
# edit_class internals
# ---------------------------------------------------------------------------

def _reassign_future_instances(*, old_lesson, new_lesson, boundary_dt):
    (
        LessonInstance.query
        .filter(LessonInstance.lesson_id == old_lesson.id)
        .filter(LessonInstance.start_datetime >= boundary_dt)
        .update({LessonInstance.lesson_id: new_lesson.id}, synchronize_session=False)
    )
    db.session.commit()


def _apply_future_edit_to_lesson(*, lesson, event_date, new_date, payload):
    from_date = new_date or event_date
    from_dt = datetime.combine(from_date, time.min)

    if event_date != lesson.start_datetime.date():
        lesson_to_edit = duplicate_lesson_helper(lesson)

        split_date = from_date - timedelta(days=1)
        lesson.recurrence_end = split_date
        lesson.save()

        _reassign_future_instances(
            old_lesson=lesson,
            new_lesson=lesson_to_edit,
            boundary_dt=from_dt,
        )
    else:
        lesson_to_edit = lesson

    payload_for_lesson = dict(payload)
    payload_for_lesson["event_date"] = event_date

    lesson_to_edit = edit_lesson_helper(data=payload_for_lesson, lesson=lesson_to_edit)
    lesson_to_edit.save()

    return lesson_to_edit, from_date


def _edit_future_instances_for_lesson(*, lesson, from_date, payload):
    from_dt = datetime.combine(from_date, time.min)
    instances = (
        LessonInstance.query
        .filter(LessonInstance.lesson_id == lesson.id)
        .filter(LessonInstance.start_datetime >= from_dt)
        .all()
    )

    for inst in instances:
        inst_payload = dict(payload)
        inst_payload["date"] = inst.start_datetime.date().strftime("%Y-%m-%d")
        edit_lesson_instance_helper(inst_payload, inst)


def _ensure_date(payload, date_obj):
    payload["date"] = payload.get("date") or date_obj.strftime("%Y-%m-%d")
    return payload


def edit_class_service(data):
    """Scope-aware class edit. Returns (result_dict, http_status_code)."""
    event = data.get("event")
    scope = data.get("scope")
    updates = data.get("updates", {})

    if not event or not scope:
        return {"error": "Invalid payload"}, 400

    event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
    date_str = updates.get("date")
    new_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None

    payload = {
        "title": updates.get("name", ""),
        "color": updates.get("color", ""),
        "max_players": updates.get("maxPlayers", None),
        "level": updates.get("levelId", None),
        "date": updates.get("date", None),
        "start_time": updates.get("startTime", None),
        "end_time": updates.get("endTime", None),
        "recurrence_end": updates.get("recurrenceEnd", None),
        "add_player_ids": updates.get("addPlayers", []),
        "remove_player_ids": updates.get("removePlayers", []),
    }

    model = event.get("model")
    original_id = event.get("originalId")

    if model == "LessonInstance":
        instance = LessonInstance.query.get_or_404(original_id)

        if scope == "single":
            _ensure_date(payload, event_date)
            edit_lesson_instance_helper(payload, instance)
            return {"id": instance.id}, 200

        if scope == "future":
            parent_lesson = instance.lesson
            _ensure_date(payload, event_date)
            lesson_to_edit, from_date = _apply_future_edit_to_lesson(
                lesson=parent_lesson,
                event_date=event_date,
                new_date=new_date,
                payload=payload,
            )
            _edit_future_instances_for_lesson(
                lesson=lesson_to_edit,
                from_date=from_date,
                payload=payload,
            )
            return {"id": lesson_to_edit.id}, 201

        return {"error": "Invalid scope"}, 400

    lesson = Lesson.query.get_or_404(original_id)

    if scope == "single":
        payload["original_lesson_occurence_date"] = event_date.strftime("%Y-%m-%d")
        _ensure_date(payload, event_date)
        instance = create_lesson_instance_helper(data=payload, parent_lesson=lesson)
        return {"id": instance.id}, 201

    if scope == "future":
        _ensure_date(payload, event_date)
        lesson_to_edit, _ = _apply_future_edit_to_lesson(
            lesson=lesson,
            event_date=event_date,
            new_date=new_date,
            payload=payload,
        )
        return {"id": lesson_to_edit.id}, 201

    return {"error": "Invalid scope"}, 400


# ---------------------------------------------------------------------------
# remove_class internals
# ---------------------------------------------------------------------------

def _truncate_lesson_future(*, lesson, from_date):
    lesson.recurrence_end = from_date - timedelta(days=1)
    lesson.save()
    delete_future_instances(lesson, from_date)


def _remove_single_occurrence_from_lesson(*, lesson, date):
    if not lesson.recurrence_rule:
        lesson.delete()
        return
    split_lesson(lesson, date, remove_current_date=True)


def remove_class_service(data):
    """Scope-aware class removal. Returns (result_dict, http_status_code)."""
    models_map = {
        "Lesson": Lesson,
        "LessonInstance": LessonInstance,
    }

    event = data.get("event", {}) or {}
    scope = data.get("scope")
    model_name = event.get("model")
    class_id = event.get("originalId")

    if not model_name or model_name not in models_map or not class_id:
        return {"error": "Invalid payload"}, 400

    if "date" not in event:
        return {"error": "Invalid payload"}, 400

    event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()

    obj = models_map[model_name].query.get_or_404(class_id)

    if model_name == "LessonInstance":
        if scope == "single" or not scope:
            obj.delete()
            return {"status": "deleted"}, 200

        if scope == "future":
            parent_lesson = obj.lesson
            obj.delete()
            delete_future_instances(parent_lesson, event_date)
            _truncate_lesson_future(lesson=parent_lesson, from_date=event_date)
            return {"status": "recurrence_truncated"}, 200

        return {"error": "Invalid request"}, 400

    if model_name == "Lesson":
        if scope == "future":
            _truncate_lesson_future(lesson=obj, from_date=event_date)
            return {"status": "recurrence_truncated"}, 200

        if scope == "single":
            _remove_single_occurrence_from_lesson(lesson=obj, date=event_date)
            return {"status": "single_removed"}, 200

    return {"error": "Invalid request"}, 400
