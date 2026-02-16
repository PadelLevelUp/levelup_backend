from flask import Blueprint, jsonify, request, abort, g, Response
from datetime import datetime, timezone, time, timedelta
from dateutil import parser
import json
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, or_

from padel_app.sql_db import db
from padel_app.models import *
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.tools.calendar_tools import build_datetime

from padel_app.serializers.calendar_event import serialize_calendar_event
from padel_app.serializers.lesson import (
    serialize_lesson,
    serialize_lesson_instance,
    serialize_class_instance
)
from padel_app.serializers.user import serialize_user
from padel_app.serializers.presence import serialize_presence
from padel_app.serializers.calendar import serialize_calendar_block
from padel_app.serializers.message import serialize_message
from padel_app.serializers.conversation import serialize_conversation_detail, serialize_conversation
from padel_app.serializers.coach_level import serialize_coach_level
from padel_app.helpers.calendar_helpers import (
    load_lessons_for_coach, 
    load_lesson_instances_for_coach,
    load_lessons_for_player,
    load_lesson_instances_for_player,
    build_lesson_events, 
    load_calendar_blocks_for_user, 
    build_block_events
)

from padel_app.helpers.lesson_services import (
    create_lesson_helper, 
    duplicate_lesson_helper, 
    delete_future_instances, 
    split_lesson, 
    edit_lesson_instance_helper, 
    create_lesson_instance_helper,
    edit_lesson_helper,
    add_presences
)
from padel_app.helpers.dashboard_services import build_dashboard_payload
from padel_app.helpers.player_services import create_player_helper, edit_player_helper
from padel_app.realtime import publish, subscribe, unsubscribe

bp = Blueprint("frontend_api", __name__, url_prefix="/api/app")

@bp.route("/events")
@jwt_required(locations=["query_string"])
def events():
    def stream():
        q = subscribe()
        try:
            while True:
                event = q.get()
                yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:
            unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream")


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def current_user():
    if 'current_user' not in g:
        user_id = get_jwt_identity()
        if user_id is None:
            abort(401, "Missing or invalid JWT")

        g.current_user = (
            User.query.get_or_404(int(user_id))
        )
    return g.current_user

def current_coach():
    if 'current_coach' not in g:
        user = current_user()
        if not user.coach:
            g.current_coach = None
        g.current_coach = user.coach
    return g.current_coach

def current_player():
    if 'current_player' not in g:
        user = current_user()
        if not user.player:
            g.current_player = None
        g.current_player = user.player
    return g.current_player

def current_club():
    coach = current_coach()
    return coach.current_club


def get_or_materialize_instance(lesson: Lesson, date: datetime.date):
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
        club_id=lesson.club_id,
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

# -------------------------------------------------------------------
# READ
# -------------------------------------------------------------------

@bp.get("/messages/unread_count")
@jwt_required()
def unread_total():

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    user_id = int(get_jwt_identity())

    CP = ConversationParticipant
    M = Message

    unread = (
        db.session.query(func.count(M.id))
        .join(CP, CP.conversation_id == M.conversation_id)
        .filter(CP.user_id == user_id)
        .filter(M.sender_id != user_id)
        .filter(M.sent_at > func.coalesce(CP.last_read_at, epoch))
        .scalar()
    )

    return jsonify({"unreadCount": int(unread or 0)})

@bp.get("/calendar")
@jwt_required()
def calendar():
    start = request.args.get("from")
    end = request.args.get("to")

    if not start or not end:
        abort(400, "from and to are required")

    user = current_user()
    coach = current_coach()
    player = current_player()

    range_start = parser.isoparse(start).astimezone(timezone.utc)
    range_end = parser.isoparse(end).astimezone(timezone.utc)

    if coach is not None:
        lessons = load_lessons_for_coach(coach.id, range_start, range_end)
        instances_by_key = load_lesson_instances_for_coach(coach.id, range_start, range_end)
    elif player is not None:
        lessons = load_lessons_for_player(player.id, range_start, range_end)
        instances_by_key = load_lesson_instances_for_player(player.id, range_start, range_end)
    else:
        abort(403, "User has no coach or player profile")

    lesson_events = build_lesson_events(
        lessons,
        instances_by_key,
        range_start,
        range_end,
    )

    blocks = load_calendar_blocks_for_user(user.id, range_start, range_end)
    block_events = build_block_events(blocks, range_start, range_end)

    return jsonify(lesson_events + block_events)

@bp.get("/lesson_instance/<int:instance_id>")
def lesson_instance_detail(instance_id):
    instance = LessonInstance.query.get_or_404(instance_id)

    presences = Presence.query.filter_by(
        lesson_instance_id=instance.id
    ).all()

    return jsonify({
        "lessonInstance": serialize_lesson_instance(instance),
        "presences": [serialize_presence(p) for p in presences],
    })

@bp.get("/register/user/<user_id>")
def get_user_for_registration(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify(serialize_user(user))

@bp.post("/activate/user/<user_id>")
def activate_user(user_id):

    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}

    data['status'] = 'active'
    
    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()

    return jsonify(success=True)

@bp.get("/dashboard")
@jwt_required()
def dashboard():
    user = current_user()
    coach = current_coach()
    player = current_player()

    payload = build_dashboard_payload(user=user, coach=coach, player=player)
    return jsonify(payload)
    
@bp.get("/conversations")
@jwt_required()
def conversations():
    user = current_user()
    if not user.id:
        abort(400, "user_id is required")

    conversations = (
        Conversation.query
        .join(ConversationParticipant)
        .filter(ConversationParticipant.user_id == user.id)
        .all()
    )

    return jsonify([
        serialize_conversation(c, user.id)
        for c in conversations
    ])
    
@bp.get("/conversation/<int:conversation_id>")
@jwt_required()
def conversation_detail(conversation_id):
    user = current_user()
    conversation = Conversation.query.get_or_404(conversation_id)
    return jsonify(
        serialize_conversation_detail(conversation, user.id)
    )
    
@bp.post("/conversation/<int:conversation_id>/read")
@jwt_required()
def mark_conversation_read(conversation_id):
    user = current_user()

    participation = (
        ConversationParticipant.query
        .filter_by(
            conversation_id=conversation_id,
            user_id=user.id
        )
        .first_or_404()
    )

    participation.last_read_at = datetime.utcnow()
    participation.save()

    return "", 204
    
@bp.get("/coach")
@jwt_required()
def coach_detail():
    coach = current_coach()
    return jsonify({
        "id": coach.id,
        "user": serialize_user(coach.user),
    })
    
@bp.get("/players")
@jwt_required()
def players():
    coach = current_coach()
    club = current_club()

    if coach:
        players = coach.players

    elif club:
        players = club.players

    else:
        players = Player.query.all()

    return jsonify([
        {
            "id": p.id,
            "userId": p.user_id,
            "name": p.user.name,
            "email": p.user.email,
            "phone": p.user.phone,
        }
        for p in players
    ])
    
@bp.get("/users")
@jwt_required()
def users():

    users = User.query.filter_by(
        status="active"
    ).all()

    return jsonify([
        serialize_user(u)
        for u in users
    ])
    
@bp.get("/coach_players")
@jwt_required()
def coach_players():
    coach = current_coach()
    
    if coach:
        coach = Coach.query.get_or_404(coach.id)
        players = coach.players

    return jsonify([
        p.coach_player_info(coach.id)
        for p in players
    ])
    
@bp.get("/coach_levels")
@jwt_required()
def coach_levels():
    coach = current_coach()
    return jsonify(
        [
            serialize_coach_level(l) 
            for l in coach.levels
            ]
        )
    
@bp.get("/lessons")
def lessons():
    return jsonify([
        serialize_lesson(lesson)
        for lesson in Lesson.query.all()
    ])
    
@bp.get("/calendar_block")
def calendar_block():
    return jsonify([
        serialize_calendar_block(calendar_block)
        for calendar_block in CalendarBlock.query.all()
    ])

@bp.get("/lesson_instances")
@jwt_required()
def lesson_instances():
    start = request.args.get("from")
    end = request.args.get("to")
    coach = current_coach()

    if not coach:
        abort(403, "User is not a coach")

    if not start or not end:
        abort(400, "from and to are required")

    range_start = parser.isoparse(start).astimezone(timezone.utc)
    range_end = parser.isoparse(end).astimezone(timezone.utc)

    lessons = load_lessons_for_coach(coach.id, range_start, range_end)
    instances_by_key = load_lesson_instances_for_coach(
        coach.id, range_start, range_end
    )

    lesson_events = build_lesson_events(
        lessons,
        instances_by_key,
        range_start,
        range_end,
    )
    
    return lesson_events
    
@bp.get("/lesson_instance/<int:instance_id>/presences")
def lesson_instance_presences(instance_id):
    presences = Presence.query.filter_by(
        lesson_instance_id=instance_id
    ).all()

    return jsonify([
        serialize_presence(p) for p in presences
    ])

@bp.get("/calendar_event")
def calendar_event():
    event_types = {
        "lesson": Lesson,
        "lesson_instance": LessonInstance,
        "calendar_block": CalendarBlock
    }
    model = request.args.get("model")
    id = request.args.get("original_id")
    
    if not model:
        abort(400, "model is required")

    current_event = event_types[model].query.get_or_404(id)

    return jsonify(serialize_calendar_event(current_event))

@bp.post("/class_instance")
@jwt_required()
def class_instance():
    event_types = {
        "lesson": Lesson,
        "lessoninstance": LessonInstance
    }
    model = request.args.get("model").lower()
    id = request.args.get("id")
    
    if not model:
        abort(400, "model is required")

    current_class = event_types[model].query.get_or_404(id)

    return jsonify(serialize_class_instance(current_class))
# -------------------------------------------------------------------
# CREATE
# -------------------------------------------------------------------

@bp.post("/club")
def create_club():
    data = request.get_json() or {}

    club = Club()
    form = club.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.create()

    return jsonify({"id": club.id}), 201


@bp.post("/user")
def create_user():
    data = request.get_json() or {}

    user = User()
    form = user.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.create()

    return jsonify({"id": user.id}), 201

@bp.post("/player")
def create_player():
    data = request.get_json() or {}

    player = Player()
    form = player.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    player.update_with_dict(values)
    player.create()
    
    if data.get("coach"):
        Association_CoachPlayer(
            coach_id=data["coach"],
            player_id=player.id,
        ).create()

    return jsonify({"id": player.id}), 201

@bp.post("/coach")
def create_coach():
    data = request.get_json() or {}

    coach = Coach()
    form = coach.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach.update_with_dict(values)
    coach.create()

    return jsonify({"id": coach.id}), 201

@bp.post("/coach_level")
def create_coach_level():
    data = request.get_json() or {}

    coach_level = CoachLevel()
    form = coach_level.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach_level.update_with_dict(values)
    coach_level.create()

    return jsonify({"id": coach_level.id}), 201


@bp.post("/lesson")
def create_lesson():
    data = request.get_json() or {}
    
    lesson = create_lesson_helper(data)

    return jsonify(serialize_lesson(lesson)), 201


@bp.post("/calendar_block")
def create_calendar_block():
    data = request.get_json() or {}

    block = CalendarBlock()
    form = block.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    block.update_with_dict(values)

    block.create()
    return jsonify(serialize_calendar_block(block)), 201


@bp.post("/message")
@jwt_required()
def create_message():
    data = request.get_json() or {}
    
    payload = {
        "text": data["text"],
        "conversation": data["conversationId"],
        "sender": current_user().id,
        "sent_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    message = Message()
    form = message.get_create_form()

    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)

    message.update_with_dict(values)

    message.create()
    publish({
        "type": "message_created",
        "payload": serialize_message(message, None)
    })
    return jsonify(serialize_message(message, None)), 201

@bp.post("/conversation")
@jwt_required()
def create_conversation():
    data = request.get_json() or {}
    
    user = current_user()
    participants = data['otherParticipants']
    participants.append(user.id)
    
    
    key = Conversation.build_participant_key(participants)

    conversation = Conversation.query.filter_by(
        participant_key=key
    ).first()
    
    if not conversation:
        
        payload = {
            "is_group": len(participants) >= 2 or False,
            "participant_ids": participants,
            "creator_id": user.id,
            "participant_key": key
        }
        
        conversation = Conversation()
        form = conversation.get_create_form()

        fake_request = JsonRequestAdapter(payload, form)
        values = form.set_values(fake_request)

        conversation.update_with_dict(values)

        conversation.create()

        if payload.get("participant_ids"):
            for participant_id in payload.get("participant_ids"):
                conversation_participant = ConversationParticipant(
                    conversation_id=conversation.id,
                    user_id=participant_id,
                )
                conversation_participant.create()

    return jsonify(serialize_conversation_detail(conversation, user_id=payload.get("creator_id"))), 201

@bp.post("/add_class")
@jwt_required()
def add_class():
    data = request.get_json() or {}
    coach = current_coach()
    club = current_club()
    
    lesson_payload = {
        "title": data["name"],
        "type": data["classType"],
        "status": "active",
        "color": data.get("color"),
        "max_players": data["maxPlayers"],
        "level": data.get("levelId"),
        "is_recurring": data.get("isRecurring", False),
        "start_datetime": build_datetime(
            data["date"], data["startTime"]
        ),
        "end_datetime": build_datetime(
            data["date"], data["endTime"]
        ),
        "club" : club.id,
        "coach": coach.id,
        "player_ids": data.get("playerIds", []),
    }

    # Recurrence
    if data.get("isRecurring"):
        lesson_payload["recurrence_rule"] = json.dumps(
            data.get("recurrenceRule")
        )
        lesson_payload["recurrence_end"] = data.get("endDate")
        
    lesson = create_lesson_helper(lesson_payload)

    return jsonify(serialize_calendar_event(lesson))

@bp.post("/add_coach_level")
@jwt_required()
def add_coach_level():
    data = request.get_json() or {}
    coach = current_coach()
    
    def _edit_or_create(form, payload, element):
        fake_request = JsonRequestAdapter(payload, form)
        values = form.set_values(fake_request)
        
        coach_level.update_with_dict(values)
        return element
    
    for entry in data:
        level_payload = {
            "code": entry.get("code"),
            "label": entry.get("label"),
            "coach": coach.id,
            "display_order": entry.get("displayOrder"),
        }
        coach_level = (CoachLevel.query
            .filter(CoachLevel.coach_id == coach.id)
            .filter(CoachLevel.code == level_payload["code"])
            .first()
        )
        if coach_level:
            form = coach_level.get_edit_form()
            coach_level = _edit_or_create(form, level_payload, coach_level)
            coach_level.save()
        else:
            coach_level = CoachLevel()
            form = coach_level.get_create_form()
            coach_level = _edit_or_create(form, level_payload, coach_level)
            coach_level.create()

    return jsonify(data)

# -------------------------------------------------------------------
# EDIT / DOMAIN ACTIONS
# -------------------------------------------------------------------

@bp.post("/user/<int:user_id>")
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}

    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()

    return jsonify(success=True)

@bp.post("/club/<int:club_id>")
def edit_club(club_id):
    club = User.query.get_or_404(club_id)
    data = request.get_json() or {}

    form = club.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.save()

    return jsonify(success=True)

@bp.post("/lesson/<int:lesson_id>")
def edit_lesson(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    data = request.get_json() or {}

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
    return jsonify(serialize_lesson(lesson))

@bp.post("/calendar_block/<int:block_id>")
def edit_calendar_block(block_id):
    block = CalendarBlock.query.get_or_404(block_id)
    data = request.get_json() or {}

    form = block.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    block.update_with_dict(values)
    block.save()

    return jsonify(serialize_calendar_block(block))

@bp.post("/class_instance/presences/confirm")
def confirm_presences():
    data = request.get_json()

    class_instance = data['classInstance']
    presences = data['presences']
    
    if class_instance['model'] == 'Lesson':
        
        lesson = Lesson.query.get_or_404(class_instance.get('originalId'))
        payload = lesson.to_instance_data()
        payload['date'] = class_instance['date']
        payload['original_lesson_occurence_date'] = class_instance['date']
        
        instance = create_lesson_instance_helper(
            data=payload,
            parent_lesson=lesson
        )
        
    elif class_instance['model'] == 'LessonInstance':
        instance = LessonInstance.query.get_or_404(class_instance.get('originalId'))
    
    presences = add_presences(instance, presences)
    return jsonify([serialize_presence(p) for p in presences])


@bp.post("/lesson/<int:lesson_id>/status")
def update_lesson_status(lesson_id):
    data = request.get_json()

    lesson = Lesson.query.get_or_404(lesson_id)
    date = datetime.fromisoformat(data["date"]).date()

    instance = get_or_materialize_instance(lesson, date)
    instance.status = data["status"]  # canceled | completed
    instance.save()

    return jsonify(serialize_lesson_instance(instance))

@bp.post("/edit_class")
def edit_class():
    from datetime import datetime, timedelta

    data = request.get_json() or {}

    event = data.get("event")
    scope = data.get("scope")
    updates = data.get("updates", {})

    if not event or not scope:
        return jsonify({"error": "Invalid payload"}), 400

    def _apply_future_edit_to_lesson(*, lesson, event_date, new_date, payload):
        """
        Applies 'future' semantics to a lesson series:
        - Split series if editing from the middle
        - Reconnect existing future instances to the new lesson
        - Edit the (possibly duplicated) series lesson with edit_lesson_helper
        Returns: (lesson_to_edit, from_date)
        """
        from datetime import datetime, timedelta, time

        from_date = new_date or event_date  # boundary where "future" starts
        from_dt = datetime.combine(from_date, time.min)

        def _reassign_future_instances(*, old_lesson, new_lesson, boundary_dt):
            """
            Move existing materialized instances from old_lesson to new_lesson
            for all instances occurring on/after boundary_dt.
            """
            (
                LessonInstance.query
                .filter(LessonInstance.lesson_id == old_lesson.id)
                .filter(LessonInstance.start_datetime >= boundary_dt)
                .update({LessonInstance.lesson_id: new_lesson.id}, synchronize_session=False)
            )
            db.session.commit()

        if event_date != lesson.start_datetime.date():
            lesson_to_edit = duplicate_lesson_helper(lesson)

            # Truncate old lesson up to the day before the future boundary
            split_date = from_date - timedelta(days=1)
            lesson.recurrence_end = split_date
            lesson.save()  # likely commits old lesson changes

            # IMPORTANT: reconnect existing instances after the split to the new lesson
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

    def _edit_future_instances(*, lesson, from_date, payload):
        """
        Edit all existing instances for a lesson from 'from_date' onward
        using edit_lesson_instance_helper.
        """
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
        """Ensure payload['date'] is always present as YYYY-MM-DD for helpers that expect it."""
        payload["date"] = payload.get("date") or date_obj.strftime("%Y-%m-%d")
        return payload

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
            return jsonify({"id": instance.id}), 200

        if scope == "future":
            parent_lesson = instance.lesson

            _ensure_date(payload, event_date)
            lesson_to_edit, from_date = _apply_future_edit_to_lesson(
                lesson=parent_lesson,
                event_date=event_date,
                new_date=new_date,
                payload=payload,
            )

            _edit_future_instances(
                lesson=lesson_to_edit,
                from_date=from_date,
                payload=payload,
            )

            return jsonify({"id": lesson_to_edit.id}), 201

        return jsonify({"error": "Invalid scope"}), 400

    lesson = Lesson.query.get_or_404(original_id)

    if scope == "single":
        payload["original_lesson_occurence_date"] = event_date.strftime("%Y-%m-%d")
        _ensure_date(payload, event_date)
        instance = create_lesson_instance_helper(data=payload, parent_lesson=lesson)
        return jsonify({"id": instance.id}), 201

    if scope == "future":
        _ensure_date(payload, event_date)
        lesson_to_edit, _ = _apply_future_edit_to_lesson(
            lesson=lesson,
            event_date=event_date,
            new_date=new_date,
            payload=payload,
        )
        return jsonify({"id": lesson_to_edit.id}), 201

    return jsonify({"error": "Invalid scope"}), 400

@bp.post("/remove_class")
def remove_class():
    from datetime import datetime, timedelta

    data = request.get_json() or {}

    models = {
        "Lesson": Lesson,
        "LessonInstance": LessonInstance,
    }

    event = data.get("event", {}) or {}
    scope = data.get("scope")
    model_name = event.get("model")
    class_id = event.get("originalId")

    if not model_name or model_name not in models or not class_id:
        return jsonify({"error": "Invalid payload"}), 400

    if "date" not in event:
        return jsonify({"error": "Invalid payload"}), 400

    event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()

    def _truncate_lesson_future(*, lesson, from_date):
        """
        Implements 'future' removal semantics on a Lesson:
        - truncate recurrence_end to the day before from_date
        - delete future materialized instances from from_date onward
        """
        lesson.recurrence_end = from_date - timedelta(days=1)
        lesson.save()
        delete_future_instances(lesson, from_date)

    def _remove_single_occurrence_from_lesson(*, lesson, date):
        """
        Implements 'single' removal semantics on a Lesson:
        - if non-recurring: delete the lesson
        - if recurring: split lesson around that date and remove current date
        """
        if not lesson.recurrence_rule:
            lesson.delete()
            return
        split_lesson(lesson, date, remove_current_date=True)

    obj = models[model_name].query.get_or_404(class_id)

    if model_name == "LessonInstance":
        if scope == "single" or not scope:
            obj.delete()
            return jsonify({"status": "deleted"}), 200

        if scope == "future":
            parent_lesson = obj.lesson
            obj.delete()
            delete_future_instances(parent_lesson, event_date)
            _truncate_lesson_future(lesson=parent_lesson, from_date=event_date)

            return jsonify({"status": "recurrence_truncated"}), 200

        return jsonify({"error": "Invalid request"}), 400

    if model_name == "Lesson":
        if scope == "future":
            _truncate_lesson_future(lesson=obj, from_date=event_date)
            return jsonify({"status": "recurrence_truncated"}), 200

        if scope == "single":
            _remove_single_occurrence_from_lesson(lesson=obj, date=event_date)
            return jsonify({"status": "single_removed"}), 200

    return jsonify({"error": "Invalid request"}), 400

@bp.post("/add_player")
def add_player():
    data = request.get_json() or {}
    
    payload = {
        'coach': int(data['coachId']) if data['coachId'] else None,
        'level': int(data['levelId']) if data.get('levelId', None) else None,
        'side': data.get('side', None),
        'notes': data.get('notes', None),
        'user': {
            'name': data.get('name', None),
            'username': data.get('username', None),
            'email': data.get('email', None),
            'phone': data.get('phone', None)
        },
    }
    
    coach_player_info = create_player_helper(payload)

    return jsonify(coach_player_info)

@bp.post("/edit_player")
def edit_player():
    data = request.get_json() or {}
    
    updates = data['updates']
    player_info = data['player']

    changes = {k: v for k, v in updates.items() if v != player_info.get(k)}
    
    payload = {
        'coach': player_info['coachId'],
        'relation': {           
            'level': int(changes['levelId']) if changes.get('levelId', None) else None,
            'side': changes.get('side', None),
            'notes': changes.get('notes', None),
        },
        'user': {
            'name': changes.get('name', None),
            'username': changes.get('username', None),
            'email': changes.get('email', None),
            'phone': changes.get('phone', None)
        },
    }
    
    player = Player.query.get_or_404(player_info['playerId'])
    rel = Association_CoachPlayer.query.filter_by(
        coach_id=player_info['coachId'],
        player_id=player_info["playerId"],
    ).first_or_404()
    coach_player_info = edit_player_helper(player, rel, payload)

    return jsonify(coach_player_info)

@bp.post("/remove_player")
def remove_player():
    data = request.get_json() or {}

    coach_id = data.get("coachId", None)
    player_id = data.get("playerId", None)
    
    player = Player.query.get_or_404(player_id)
    user = player.user
    
    if user.status == "active":
        rel = Association_CoachPlayer.query.filter_by(
            coach_id=coach_id,
            player_id=player_id,
        ).first_or_404()
        rel.delete()
        return jsonify({"status": "Removed active user"}), 200
    else:
        player.delete()
        user.delete()
        return jsonify({"status": "Delete inactive user"}), 200