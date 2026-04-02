from flask import Blueprint, jsonify, request, abort, g, Response
from datetime import timezone
from dateutil import parser
import json
from flask_jwt_extended import jwt_required, get_jwt_identity

from padel_app.models import *
from padel_app.realtime import subscribe, unsubscribe
from padel_app.serializers.calendar_event import serialize_calendar_event
from padel_app.serializers.lesson import (
    serialize_lesson,
    serialize_lesson_instance,
    serialize_class_instance,
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
    build_block_events,
)
from padel_app.helpers.dashboard_services import build_dashboard_payload

from padel_app.services.lesson_service import (
    get_or_materialize_instance,
    create_lesson_helper,
    add_class_service,
    edit_lesson_from_data,
    confirm_presences_service,
    update_lesson_status_service,
    get_lesson_instances_in_range,
    edit_class_service,
    remove_class_service,
)
from padel_app.services.player_service import (
    create_player_service,
    get_players_list,
    get_player_profile,
    add_player_service,
    edit_player_service,
    remove_player_service,
    get_coach_players_list,
    get_coach_players_paginated,
)
from padel_app.services.user_service import (
    create_user_service,
    edit_user_service,
    activate_user_service,
)
from padel_app.services.club_service import create_club_service, edit_club_service
from padel_app.services.coach_service import (
    create_coach_service,
    create_coach_level_service,
    upsert_coach_levels,
    upsert_evaluation_categories,
    add_coach_note_service,
    add_evaluation_entry_service,
)
from padel_app.services.messaging_service import (
    get_unread_count,
    create_message_service,
    edit_message_service,
    delete_message_service,
    toggle_reaction_service,
    get_user_conversations,
    create_conversation_service,
    mark_conversation_read_service,
)
from padel_app.services.calendar_service import (
    create_calendar_block_service,
    edit_calendar_block_service,
    add_event_service,
    edit_event_service,
    reschedule_block_service,
    remove_block_service,
)
from padel_app.services.ai_service import stream_import_analysis
from padel_app.services.import_service import (
    bulk_create_coach_levels,
    bulk_create_evaluation_categories,
    bulk_create_players,
    bulk_create_lessons,
    bulk_create_player_lesson_associations,
    bulk_create_presences,
    bulk_create_evaluation_entries,
    bulk_create_coach_notes,
)

bp = Blueprint("frontend_api", __name__, url_prefix="/api/app")


# -------------------------------------------------------------------
# Request context helpers
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


# -------------------------------------------------------------------
# SSE
# -------------------------------------------------------------------

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
# READ
# -------------------------------------------------------------------

@bp.get("/messages/unread_count")
@jwt_required()
def unread_total():
    user_id = int(get_jwt_identity())
    return jsonify({"unreadCount": get_unread_count(user_id)})


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

    lesson_events = build_lesson_events(lessons, instances_by_key, range_start, range_end)
    blocks = load_calendar_blocks_for_user(user.id, range_start, range_end)
    block_events = build_block_events(blocks, range_start, range_end)

    return jsonify(lesson_events + block_events)


@bp.get("/lesson_instance/<int:instance_id>")
def lesson_instance_detail(instance_id):
    instance = LessonInstance.query.get_or_404(instance_id)
    presences = Presence.query.filter_by(lesson_instance_id=instance.id).all()

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
    data = request.get_json() or {}
    activate_user_service(user_id, data)
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
def get_conversations():
    user = current_user()
    if not user.id:
        abort(400, "user_id is required")

    convs = get_user_conversations(user)
    return jsonify([serialize_conversation(c, user.id) for c in convs])


@bp.get("/conversation/<int:conversation_id>")
@jwt_required()
def conversation_detail(conversation_id):
    user = current_user()
    conversation = Conversation.query.get_or_404(conversation_id)
    return jsonify(serialize_conversation_detail(conversation, user.id))


@bp.post("/conversation/<int:conversation_id>/read")
@jwt_required()
def mark_conversation_read(conversation_id):
    user = current_user()
    mark_conversation_read_service(conversation_id, user)
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
def get_players():
    coach = current_coach()
    club = current_club()
    player_list = get_players_list(coach, club)

    return jsonify([
        {
            "id": p.id,
            "userId": p.user_id,
            "name": p.user.name,
            "email": p.user.email,
            "phone": p.user.phone,
        }
        for p in player_list
    ])


@bp.get("/users")
@jwt_required()
def get_users():
    users = User.query.filter_by(status="active").all()
    return jsonify([serialize_user(u) for u in users])


@bp.get("/coach_players")
@jwt_required()
def coach_players():
    coach = current_coach()
    coach = Coach.query.get_or_404(coach.id)
    return jsonify(get_coach_players_list(coach))


@bp.get("/coach_players_paginated")
@jwt_required()
def coach_players_paginated():
    coach = current_coach()
    coach = Coach.query.get_or_404(coach.id)

    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=25, type=int)
    page = max(1, page or 1)
    per_page = max(1, min(100, per_page or 25))

    result = get_coach_players_paginated(coach, page=page, per_page=per_page)
    return jsonify(result)


@bp.get("/coach_levels")
@jwt_required()
def get_coach_levels():
    coach = current_coach()
    return jsonify([serialize_coach_level(l) for l in coach.levels])


@bp.get("/lessons")
def get_lessons():
    return jsonify([serialize_lesson(lesson) for lesson in Lesson.query.all()])


@bp.get("/calendar_block")
def calendar_block():
    return jsonify([
        serialize_calendar_block(b) for b in CalendarBlock.query.all()
    ])


@bp.get("/evaluation_categories")
@jwt_required()
def evaluation_categories():
    coach = current_coach()
    return jsonify([ec.frontend_dict() for ec in coach.evaluation_categories])


@bp.get("/lesson_instances")
@jwt_required()
def get_lesson_instances():
    start = request.args.get("from")
    end = request.args.get("to")
    coach = current_coach()

    if not coach:
        abort(403, "User is not a coach")

    if not start or not end:
        abort(400, "from and to are required")

    range_start = parser.isoparse(start).astimezone(timezone.utc)
    range_end = parser.isoparse(end).astimezone(timezone.utc)

    return get_lesson_instances_in_range(coach, range_start, range_end)


@bp.get("/lesson_instance/<int:instance_id>/presences")
def lesson_instance_presences(instance_id):
    presences = Presence.query.filter_by(lesson_instance_id=instance_id).all()
    return jsonify([serialize_presence(p) for p in presences])


@bp.get("/calendar_event")
def calendar_event():
    event_types = {
        "lesson": Lesson,
        "lesson_instance": LessonInstance,
        "calendar_block": CalendarBlock,
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
        "lessoninstance": LessonInstance,
    }
    model = request.args.get("model").lower()
    id = request.args.get("id")

    if not model:
        abort(400, "model is required")

    current_class = event_types[model].query.get_or_404(id)

    if model == "lesson":
        date_str = request.args.get("date")
        if date_str:
            try:
                event_date = parser.isoparse(date_str).date()
            except (TypeError, ValueError):
                abort(400, "date must be an ISO date")

            instance = (
                LessonInstance.query
                .filter_by(
                    lesson_id=current_class.id,
                    original_lesson_occurence_date=event_date,
                )
                .first()
            )
            if instance is not None:
                current_class = instance

    return jsonify(serialize_class_instance(current_class))


@bp.get("/player_profile/<int:player_id>")
@jwt_required()
def player_profile(player_id):
    coach = current_coach()
    return jsonify(get_player_profile(coach, player_id))


# -------------------------------------------------------------------
# CREATE
# -------------------------------------------------------------------

@bp.post("/club")
def create_club():
    data = request.get_json() or {}
    club = create_club_service(data)
    return jsonify({"id": club.id}), 201


@bp.post("/user")
def create_user():
    data = request.get_json() or {}
    user = create_user_service(data)
    return jsonify({"id": user.id}), 201


@bp.post("/player")
def create_player():
    data = request.get_json() or {}
    player = create_player_service(data)
    return jsonify({"id": player.id}), 201


@bp.post("/coach")
def create_coach():
    data = request.get_json() or {}
    coach = create_coach_service(data)
    return jsonify({"id": coach.id}), 201


@bp.post("/coach_level")
def create_coach_level():
    data = request.get_json() or {}
    coach_level = create_coach_level_service(data)
    return jsonify({"id": coach_level.id}), 201


@bp.post("/lesson")
def create_lesson():
    data = request.get_json() or {}
    lesson = create_lesson_helper(data)
    return jsonify(serialize_lesson(lesson)), 201


@bp.post("/calendar_block")
def create_calendar_block():
    data = request.get_json() or {}
    block = create_calendar_block_service(data)
    return jsonify(serialize_calendar_block(block)), 201


@bp.post("/message")
@jwt_required()
def create_message():
    data = request.get_json() or {}
    message = create_message_service(data, current_user().id)
    return jsonify(serialize_message(message, None)), 201


@bp.put("/message/<int:message_id>")
@jwt_required()
def edit_message(message_id):
    data = request.get_json() or {}
    edit_message_service(message_id, data["text"], current_user().id)
    return jsonify({"ok": True})


@bp.delete("/message/<int:message_id>")
@jwt_required()
def delete_message(message_id):
    delete_message_service(message_id, current_user().id)
    return jsonify({"ok": True})


@bp.post("/message/<int:message_id>/reaction")
@jwt_required()
def toggle_reaction(message_id):
    data = request.get_json() or {}
    toggle_reaction_service(message_id, data["emoji"], current_user().id)
    return jsonify({"ok": True})


@bp.post("/conversation")
@jwt_required()
def create_conversation():
    data = request.get_json() or {}
    conversation, creator_id = create_conversation_service(data, current_user())
    return jsonify(serialize_conversation_detail(conversation, user_id=creator_id)), 201


@bp.post("/add_class")
@jwt_required()
def add_class():
    data = request.get_json() or {}
    lesson = add_class_service(data, current_coach(), current_club())
    return jsonify(serialize_calendar_event(lesson))


@bp.post("/add_event")
@jwt_required()
def add_event():
    data = request.get_json() or {}
    block = add_event_service(current_user().id, data)
    return jsonify(serialize_calendar_block(block)), 201


@bp.get("/calendar_block/<int:block_id>")
@jwt_required()
def get_calendar_block(block_id):
    block = CalendarBlock.query.filter_by(id=block_id, user_id=current_user().id).first_or_404()
    return jsonify(serialize_calendar_block(block))


@bp.put("/calendar_block/<int:block_id>")
@jwt_required()
def put_calendar_block(block_id):
    data = request.get_json() or {}
    block = edit_event_service(block_id, current_user().id, data)
    return jsonify(serialize_calendar_block(block))


@bp.delete("/calendar_block/<int:block_id>")
@jwt_required()
def delete_calendar_block(block_id):
    data = request.get_json() or {}
    remove_block_service(block_id, current_user().id, data.get('occDate'), data.get('scope', 'all'))
    return "", 204


@bp.post("/reschedule_block/<int:block_id>")
@jwt_required()
def reschedule_block(block_id):
    data = request.get_json() or {}
    reschedule_block_service(block_id, current_user().id, data)
    return "", 204


@bp.post("/add_coach_level")
@jwt_required()
def add_coach_level():
    data = request.get_json() or {}
    upsert_coach_levels(current_coach(), data)
    return jsonify(data)


@bp.post("/add_evaluation_categories")
@jwt_required()
def add_evaluation_categories():
    data = request.get_json() or {}
    upsert_evaluation_categories(current_coach(), data)
    return jsonify(data)


@bp.post("/add_coach_note")
@jwt_required()
def add_coach_note():
    data = request.get_json() or {}
    result, status = add_coach_note_service(current_coach(), data)
    return jsonify(result), status


@bp.post("/add_evaluation_entry")
@jwt_required()
def add_evaluation_entry():
    data = request.get_json() or {}
    result = add_evaluation_entry_service(current_coach(), data)
    return jsonify(result)


# -------------------------------------------------------------------
# EDIT / DOMAIN ACTIONS
# -------------------------------------------------------------------

@bp.post("/user/<int:user_id>")
def edit_user(user_id):
    data = request.get_json() or {}
    edit_user_service(user_id, data)
    return jsonify(success=True)


@bp.post("/club/<int:club_id>")
def edit_club(club_id):
    data = request.get_json() or {}
    edit_club_service(club_id, data)
    return jsonify(success=True)


@bp.post("/lesson/<int:lesson_id>")
def edit_lesson(lesson_id):
    lesson = Lesson.query.get_or_404(lesson_id)
    data = request.get_json() or {}
    lesson = edit_lesson_from_data(lesson, data)
    return jsonify(serialize_lesson(lesson))


@bp.post("/calendar_block/<int:block_id>")
def edit_calendar_block(block_id):
    data = request.get_json() or {}
    block = edit_calendar_block_service(block_id, data)
    return jsonify(serialize_calendar_block(block))


@bp.post("/class_instance/presences/confirm")
@jwt_required()
def confirm_presences():
    from datetime import datetime
    from padel_app.scheduler import _compute_invite_start_dt
    from padel_app.services.notification_service import get_or_create_config, trigger_invitations

    data = request.get_json()
    presences = confirm_presences_service(data['classInstance'], data['presences'])

    notified_players = []
    has_absences = any(p.status == "absent" for p in presences)
    if has_absences and presences:
        coach = current_coach()
        instance = presences[0].lesson_instance
        if instance and instance.start_datetime > datetime.utcnow():
            config = get_or_create_config(coach.id)
            invite_start_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
            # Only send invitations if the invitation window has opened.
            # If not yet open, the invite_start scheduler job will call trigger_invitations
            # at the configured time, which will find the absent presences and invite.
            if invite_start_dt is None or datetime.utcnow() >= invite_start_dt:
                try:
                    notified_players = trigger_invitations(instance, coach.id) or []
                except Exception:
                    from padel_app.sql_db import db
                    db.session.rollback()

    return jsonify({
        "presences": [serialize_presence(p) for p in presences],
        "notifiedPlayers": notified_players,
    })


@bp.post("/lesson/<int:lesson_id>/status")
def update_lesson_status(lesson_id):
    data = request.get_json()
    instance = update_lesson_status_service(lesson_id, data)
    return jsonify(serialize_lesson_instance(instance))


@bp.post("/edit_class")
def edit_class():
    data = request.get_json() or {}
    result, status = edit_class_service(data)
    return jsonify(result), status


@bp.post("/remove_class")
def remove_class():
    data = request.get_json() or {}
    result, status = remove_class_service(data)
    return jsonify(result), status


@bp.post("/add_player")
def add_player():
    data = request.get_json() or {}
    coach_player_info = add_player_service(data)
    return jsonify(coach_player_info)


@bp.post("/edit_player")
def edit_player():
    data = request.get_json() or {}
    coach_player_info = edit_player_service(data)
    return jsonify(coach_player_info)


@bp.post("/remove_player")
def remove_player():
    data = request.get_json() or {}
    result, status = remove_player_service(data)
    return jsonify(result), status


@bp.post("/delete/coach_level")
def delete_coach_level():
    data = request.get_json() or {}
    rel = CoachLevel.query.filter_by(id=int(data['id'])).first_or_404()
    rel.delete()
    return jsonify({"status": "Removed coach levels"}), 200


@bp.post("/delete/evaluation_category")
def delete_evaluation_category():
    data = request.get_json() or {}
    rel = EvaluationCategory.query.filter_by(id=int(data['id'])).first_or_404()
    rel.delete()
    return jsonify({"status": "Removed evaluation categories"}), 200


@bp.post("/delete/coach_note")
def delete_coach_note():
    data = request.get_json() or {}
    rel = CoachPlayerNote.query.filter_by(id=int(data['id'])).first_or_404()
    rel.delete()
    return jsonify({"status": "Removed coach note"}), 200


# -------------------------------------------------------------------
# Import
# -------------------------------------------------------------------
# Maps AI table display names -> (bulk_fn, needs_club).
# Order defines the dependency-safe import sequence.
_TABLE_MAP = [
    # Inferred reference data — must come before anything that depends on them.
    ("Coach Levels",          bulk_create_coach_levels,                                          False),
    ("Evaluation Categories", bulk_create_evaluation_categories,                                 False),
    # Main data — in dependency order.
    ("Players",               bulk_create_players,                                               True),
    ("Classes",               bulk_create_lessons,                                               True),
    ("Players in Classes",    bulk_create_player_lesson_associations,                            False),
    ("Presences",             bulk_create_presences,                                             False),
    ("Evaluations",           bulk_create_evaluation_entries,                                    False),
    ("Strengths",             lambda rows, coach: bulk_create_coach_notes(rows, coach, "strength"), False),
    ("Weaknesses",            lambda rows, coach: bulk_create_coach_notes(rows, coach, "weakness"), False),
]
_TABLE_NAMES = {entry[0] for entry in _TABLE_MAP}


@bp.post("/import/analyze")
@jwt_required()
def import_analyze():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    coach = current_coach()
    if not coach:
        return jsonify({"error": "Coach not found"}), 404

    file_bytes = file.read()

    # Optional: user can select which tables to import via query param or form field.
    # e.g. ?tables=Players,Classes,Presences  or  form field "tables"
    # If not provided, defaults to all tables.
    tables_param = request.form.get("tables") or request.args.get("tables")
    requested_tables = None
    if tables_param:
        requested_tables = [t.strip() for t in tables_param.split(",") if t.strip() in _TABLE_NAMES]
        if not requested_tables:
            requested_tables = None  # fall back to all

    return Response(
        stream_import_analysis(
            file_bytes,
            coach_id=coach.id,
            requested_tables=requested_tables,
        ),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@bp.post("/import/confirm")
@jwt_required()
def import_confirm():
    import json
    from padel_app.sql_db import db
    from padel_app.models.bulk_import import BulkImport

    coach = current_coach()
    club = current_club()
    data = request.get_json() or {}
    results = {}
    all_created_ids = {}
    summary = {}

    for table_name, fn, needs_club in _TABLE_MAP:
        rows = data.get(table_name)
        if not rows:
            continue
        print(f"Importing {len(rows)} to {table_name}")
        result = fn(rows, coach, club) if needs_club else fn(rows, coach)
        results[table_name] = result

        # Collect created IDs for tracking
        if result.get("created_ids"):
            for key, ids in result["created_ids"].items():
                all_created_ids.setdefault(key, []).extend(ids)

        # Build summary of imported counts
        if result.get("imported", 0) > 0:
            summary[table_name] = result["imported"]

    # Create a BulkImport record if anything was imported
    if summary:
        bulk_import = BulkImport(
            coach_id=coach.id,
            filename=data.get("_filename"),
            status="active",
            summary=json.dumps(summary),
            record_ids=json.dumps(all_created_ids),
        )
        db.session.add(bulk_import)
        db.session.commit()

    return jsonify(results)


@bp.get("/import/history")
@jwt_required()
def import_history():
    from padel_app.services.import_service import get_import_history
    coach = current_coach()
    if not coach:
        return jsonify({"error": "Coach not found"}), 404
    return jsonify(get_import_history(coach))


@bp.post("/import/<int:import_id>/revert")
@jwt_required()
def import_revert(import_id):
    from padel_app.services.import_service import revert_import
    coach = current_coach()
    if not coach:
        return jsonify({"error": "Coach not found"}), 404

    result = revert_import(import_id, coach)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)


# -------------------------------------------------------------------
# Training – Exercises
# -------------------------------------------------------------------

from padel_app.serializers.training import serialize_exercise, serialize_exercise_group
from padel_app.services.training_service import (
    get_exercises_for_coach,
    get_exercise_for_coach,
    create_exercise_service,
    update_exercise_service,
    delete_exercise_service,
    get_exercise_groups_for_coach,
    create_exercise_group_service,
    update_exercise_group_service,
    delete_exercise_group_service,
    confirm_training_service,
)


@bp.get("/exercises")
@jwt_required()
def exercises():
    coach = current_coach()
    return jsonify([serialize_exercise(ex) for ex in get_exercises_for_coach(coach)])


@bp.get("/exercises/<int:exercise_id>")
@jwt_required()
def exercise_detail(exercise_id):
    coach = current_coach()
    return jsonify(serialize_exercise(get_exercise_for_coach(coach, exercise_id)))


@bp.post("/exercises")
@jwt_required()
def create_exercise():
    coach = current_coach()
    data = request.get_json() or {}
    exercise = create_exercise_service(coach, data)
    return jsonify(serialize_exercise(exercise)), 201


@bp.put("/exercises/<int:exercise_id>")
@jwt_required()
def update_exercise(exercise_id):
    coach = current_coach()
    data = request.get_json() or {}
    exercise = update_exercise_service(exercise_id, coach, data)
    return jsonify(serialize_exercise(exercise))


@bp.delete("/exercises/<int:exercise_id>")
@jwt_required()
def delete_exercise(exercise_id):
    coach = current_coach()
    delete_exercise_service(exercise_id, coach)
    return "", 204


# -------------------------------------------------------------------
# Training – Exercise Groups
# -------------------------------------------------------------------

@bp.get("/exercise-groups")
@jwt_required()
def exercise_groups():
    coach = current_coach()
    return jsonify([serialize_exercise_group(g) for g in get_exercise_groups_for_coach(coach)])


@bp.post("/exercise-groups")
@jwt_required()
def create_exercise_group():
    coach = current_coach()
    data = request.get_json() or {}
    group = create_exercise_group_service(coach, data)
    return jsonify(serialize_exercise_group(group)), 201


@bp.put("/exercise-groups/<int:group_id>")
@jwt_required()
def update_exercise_group(group_id):
    coach = current_coach()
    data = request.get_json() or {}
    group = update_exercise_group_service(group_id, coach, data)
    return jsonify(serialize_exercise_group(group))


@bp.delete("/exercise-groups/<int:group_id>")
@jwt_required()
def delete_exercise_group(group_id):
    coach = current_coach()
    delete_exercise_group_service(group_id, coach)
    return "", 204


# -------------------------------------------------------------------
# Training – Lesson Instance Training
# -------------------------------------------------------------------

@bp.post("/class_instance/training/confirm")
@jwt_required()
def confirm_training():
    data = request.get_json()
    training = confirm_training_service(data['classInstance'], data['exerciseIds'])
    return jsonify({
        "plannedExerciseIds": [str(t.exercise_id) for t in training],
    })
