from flask import Blueprint, jsonify, request, abort, g, Response
from datetime import timezone
from dateutil import parser
import json
import queue
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token


from padel_app.models import *
from padel_app.realtime import subscribe, unsubscribe
from padel_app.serializers.calendar_event import serialize_calendar_event
from padel_app.serializers.lesson import (
    serialize_lesson_instance,
    serialize_class_instance,
)
from padel_app.serializers.user import serialize_user
from padel_app.tools.username_tools import is_placeholder_username
from padel_app.serializers.presence import serialize_presence
from padel_app.serializers.calendar import serialize_calendar_block
from padel_app.serializers.message import serialize_message
from padel_app.serializers.conversation import serialize_conversation_detail, serialize_conversation
from padel_app.serializers.coach_level import serialize_coach_level
from padel_app.serializers.season import serialize_season
from padel_app.services.season_service import (
    list_seasons,
    upsert_seasons,
    delete_season,
    regenerate_future_instances_for_season,
)

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
    add_class_service,
    confirm_presences_service,
    get_lesson_instances_in_range,
    edit_class_service,
    remove_class_service,
)
from padel_app.services.player_service import (
    get_players_list,
    get_player_profile,
    add_player_service,
    edit_player_service,
    remove_player_service,
    get_coach_players_list,
    get_coach_players_paginated,
)
from padel_app.services.user_service import (
    activate_user_service,
)
from padel_app.services.attendance_history_service import (
    build_attendance_history,
    default_range as default_attendance_range,
)
from padel_app.services.club_service import (
    create_coach_invitation_service,
    get_coach_invitation_service,
    accept_coach_invitation_service,
    revoke_coach_invitation_service,
    list_coach_invitations_service,
)
from padel_app.services.player_invitation_service import (
    create_incomplete_player_service,
    get_player_invitation_service,
    accept_player_invitation_service,
    revoke_player_invitation_service,
)
from padel_app.services.coach_service import (
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
    block_user_service,
    unblock_user_service,
    get_blocked_users_service,
    get_messageable_users_service,
    report_message_service,
)
from padel_app.services.calendar_service import (
    add_event_service,
    edit_event_service,
    reschedule_block_service,
    remove_block_service,
)
from padel_app.services.student_availability_service import (
    list_student_blockers,
    create_student_blocker,
    update_student_blocker,
    delete_student_blocker,
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
# Health check
# -------------------------------------------------------------------

@bp.get("/healthz")
def healthz():
    """Liveness + readiness check. 200 when DB is reachable and (in TEST_MODE)
    the scheduler has initialised. Used by Playwright's webServer readiness."""
    from sqlalchemy import text
    from padel_app.sql_db import db
    from padel_app.scheduler import ensure_scheduler_ready
    import os as _os

    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        return jsonify({"status": "db_unreachable", "error": str(exc)}), 503

    if _os.environ.get("TEST_MODE", "").lower() == "true":
        try:
            ensure_scheduler_ready()
        except Exception as exc:
            return jsonify({"status": "scheduler_not_ready", "error": str(exc)}), 503

    return jsonify({"status": "ok"})


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
    # PAD-103: a club is always reached *through* a coach. Resolving it with the
    # nullable `current_coach()` turned every student caller into an
    # `AttributeError` -> 500; `require_coach()` makes it the 403 it always
    # should have been. `require_coach` is defined below — Python resolves it at
    # call time, so the ordering is fine.
    coach = require_coach()
    return coach.current_club


# -------------------------------------------------------------------
# Authorization helpers (PAD-92)
# -------------------------------------------------------------------
# Until PAD-92 a large part of this blueprint carried no auth decorator at all,
# and the services behind it read `coachId` / `playerId` / `id` straight out of
# the request body. Adding `@jwt_required()` on its own would only have turned an
# anonymous hole into an IDOR: any logged-in user could still edit or delete
# another coach's players, classes, levels and notes.
#
# The rule these helpers enforce, matching RULES.md #2 ("coach-scoped data"):
#   * the acting coach is always derived from the JWT, never from the body;
#   * a body-supplied owner id is only ever accepted when it matches the JWT's;
#   * touching a row owned by somebody else is 403, not 404, and never mutates.
#
# PAD-103 closed the other half of the same hole. PAD-92 only applied
# `require_coach()` to the `delete/*` routes; every other coach-only route still
# started from the nullable `current_coach()` and dereferenced it, so a caller
# with a *player* profile and no coach profile — a student — produced an
# unhandled `AttributeError` (500) instead of a 403. That is the same class of
# bug: the caller's role was never actually checked, it just happened to crash.
# Every coach-only route now goes through `require_coach()`; the routes that
# legitimately serve students (`/calendar`, `/dashboard`, `/class_instance`,
# `/calendar_event`, `/lesson_instance/<id>/presences`, `/availability_blockers`)
# keep their explicit `current_coach() is None` student branch and must NOT be
# converted.


def require_coach():
    """Return the calling ``Coach``, or 403 when the caller has no coach profile."""
    coach = current_coach()
    if coach is None:
        abort(403, "User is not a coach")
    return coach


def assert_acting_coach(coach, claimed_coach_id):
    """Reject a request whose body claims to act as a *different* coach.

    The body value is legacy payload shape — the apps still send `coachId`. We
    keep accepting it, but only as an assertion: it must agree with the JWT.
    """
    if claimed_coach_id in (None, ""):
        return
    try:
        claimed = int(claimed_coach_id)
    except (TypeError, ValueError):
        abort(400, "coachId must be an integer")
    if claimed != coach.id:
        abort(403, "Not authorized to act on behalf of another coach")


def _required_int_id(data, key="id"):
    """Read a required integer id out of a JSON body (400 when absent/invalid)."""
    raw = (data or {}).get(key)
    if raw in (None, ""):
        abort(400, f"{key} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort(400, f"{key} must be an integer")


def coach_owns_lesson(coach, lesson):
    if lesson is None:
        return False
    return any(rel.coach_id == coach.id for rel in lesson.coaches_relations)


def coach_owns_instance(coach, instance):
    """A coach owns an instance directly, or through its parent lesson."""
    if instance is None:
        return False
    if any(rel.coach_id == coach.id for rel in instance.coaches_relations):
        return True
    return coach_owns_lesson(coach, instance.lesson)


def require_owned_class(coach, model_name, class_id):
    """Load a Lesson/LessonInstance and assert the calling coach owns it."""
    normalized = (model_name or "").strip().lower()
    if normalized == "lesson":
        obj = Lesson.query.get_or_404(class_id)
        owned = coach_owns_lesson(coach, obj)
    elif normalized in ("lessoninstance", "lesson_instance"):
        obj = LessonInstance.query.get_or_404(class_id)
        owned = coach_owns_instance(coach, obj)
    else:
        abort(400, "Unsupported model")

    if not owned:
        abort(403, "Not authorized to modify this class")
    return obj


def require_own_roster_relation(coach, player_id):
    """Load the caller's Association_CoachPlayer row for ``player_id`` (403 if none)."""
    if player_id in (None, ""):
        abort(400, "playerId is required")
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        abort(400, "playerId must be an integer")

    rel = Association_CoachPlayer.query.filter_by(
        coach_id=coach.id, player_id=pid
    ).first()
    if rel is None:
        abort(403, "Not authorized to modify this player")
    return rel


# -------------------------------------------------------------------
# SSE
# -------------------------------------------------------------------

@bp.route("/events")
@jwt_required(locations=["query_string"])
def events():
    def stream():
        # Each connected client pins one gunicorn thread for the lifetime of
        # this generator. A disconnect is only detected when a write fails, so
        # q.get() must time out and emit a keep-alive: otherwise a closed tab
        # whose queue never receives an event leaks its thread forever and the
        # worker pool eventually starves (prod outage 2026-06-10/11).
        q = subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


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
@jwt_required()
def lesson_instance_detail(instance_id):
    instance = LessonInstance.query.get_or_404(instance_id)

    presence_query = Presence.query.filter_by(lesson_instance_id=instance.id)
    # Role-based visibility (PAD-36): a student only sees their own presence.
    if current_coach() is None:
        player = current_player()
        if player is None:
            abort(403, "Not authorized to view this class")
        presence_query = presence_query.filter_by(player_id=player.id)
    presences = presence_query.all()

    return jsonify({
        "lessonInstance": serialize_lesson_instance(instance),
        "presences": [serialize_presence(p) for p in presences],
    })


@bp.get("/register/user/<user_id>")
def get_user_for_registration(user_id):
    user = User.query.get_or_404(user_id)
    payload = serialize_user(user)
    # PAD-105: a coach-created account carries a generated `pending-…`
    # placeholder username. This form is precisely where the user picks their
    # own, so hand back an empty field rather than the placeholder — prefilling
    # it leaks an internal detail and nudges the user into keeping a
    # machine-generated login.
    if is_placeholder_username(payload.get("username")):
        payload["username"] = None
    return jsonify(payload)


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

    page = request.args.get("page", default=1, type=int)
    limit = min(request.args.get("limit", default=20, type=int), 50)
    result = get_user_conversations(user, page=page, limit=limit)
    return jsonify({
        "conversations": [serialize_conversation(c, user.id) for c in result["conversations"]],
        "hasMore": result["has_more"],
    })


@bp.get("/conversation/<int:conversation_id>")
@jwt_required()
def conversation_detail(conversation_id):
    user = current_user()
    conversation = Conversation.query.get_or_404(conversation_id)
    is_participant = any(p.user_id == user.id for p in conversation.participants)
    if not is_participant:
        abort(403, "Not a participant of this conversation")
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
    coach = require_coach()
    club = coach.current_club
    return jsonify({
        "id": coach.id,
        "user": serialize_user(coach.user),
        "club": {"id": club.id, "name": club.name} if club else None,
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


@bp.get("/messageable-users")
@jwt_required()
def get_messageable_users():
    user = current_user()
    users = get_messageable_users_service(user)
    return jsonify([serialize_user(u) for u in users])


@bp.post("/users/<int:user_id>/block")
@jwt_required()
def block_user(user_id):
    user = current_user()
    block_user_service(user.id, user_id)
    return jsonify({"ok": True})


@bp.delete("/users/<int:user_id>/block")
@jwt_required()
def unblock_user(user_id):
    user = current_user()
    unblock_user_service(user.id, user_id)
    return jsonify({"ok": True})


@bp.get("/blocked-users")
@jwt_required()
def get_blocked_users():
    user = current_user()
    blocked = get_blocked_users_service(user.id)
    return jsonify([{"id": u.id, "name": u.name} for u in blocked])


@bp.post("/messages/<int:message_id>/report")
@jwt_required()
def report_message(message_id):
    user = current_user()
    data = request.get_json(silent=True) or {}
    report_message_service(user.id, message_id, data.get("reason"))
    return jsonify({"ok": True}), 201


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
    search = request.args.get("search", default=None, type=str)
    sort_by = request.args.get("sort_by", default="name", type=str)
    sort_dir = request.args.get("sort_dir", default="asc", type=str)
    missing_level = request.args.get("missing_level", default="", type=str) == "true"
    missing_side = request.args.get("missing_side", default="", type=str) == "true"

    page = max(1, page or 1)
    per_page = max(1, min(100, per_page or 25))
    if search:
        search = search.strip() or None
    if sort_by not in ("name", "level"):
        sort_by = "name"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    result = get_coach_players_paginated(
        coach, page=page, per_page=per_page, search=search,
        sort_by=sort_by, sort_dir=sort_dir,
        missing_level=missing_level, missing_side=missing_side,
    )
    return jsonify(result)


@bp.get("/coach_levels")
@jwt_required()
def get_coach_levels():
    coach = require_coach()
    return jsonify([serialize_coach_level(l) for l in coach.levels])


@bp.get("/seasons")
@jwt_required()
def get_seasons():
    coach = require_coach()
    return jsonify([serialize_season(s) for s in list_seasons(coach)])


# PAD-92: `GET /lessons` and `GET /calendar_block` used to dump EVERY lesson and
# EVERY calendar block in the database to an anonymous caller. Nothing in the web
# app, the mobile app, the issue bot, the Playwright specs or the seed scripts
# referenced them, so they are removed rather than guarded — the scoped
# equivalents (`/calendar`, `/lesson_instances`, `/calendar_block/<id>`) already
# cover every real use case.


@bp.get("/evaluation_categories")
@jwt_required()
def evaluation_categories():
    coach = require_coach()
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
@jwt_required()
def lesson_instance_presences(instance_id):
    presence_query = Presence.query.filter_by(lesson_instance_id=instance_id)
    # Role-based visibility (PAD-36): a student only sees their own presence.
    if current_coach() is None:
        player = current_player()
        if player is None:
            abort(403, "Not authorized to view this class")
        presence_query = presence_query.filter_by(player_id=player.id)
    presences = presence_query.all()
    return jsonify([serialize_presence(p) for p in presences])


@bp.get("/calendar_event")
@jwt_required()
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
    if model not in event_types:
        abort(400, "Unsupported model")

    current_event = event_types[model].query.get_or_404(id)

    # PAD-92: this route used to hand any calendar row to any caller, by id.
    # A coach may read their own lessons/instances; a student may read the ones
    # they are enrolled in; calendar blocks belong to a single user.
    coach = current_coach()
    player = current_player()

    if model == "calendar_block":
        if current_event.user_id != current_user().id:
            abort(403, "Not authorized to view this calendar event")
    elif model == "lesson":
        allowed = coach_owns_lesson(coach, current_event) if coach else False
        if not allowed and player is not None:
            allowed = any(
                rel.player_id == player.id for rel in current_event.players_relations
            )
        if not allowed:
            abort(403, "Not authorized to view this calendar event")
    else:  # lesson_instance
        allowed = coach_owns_instance(coach, current_event) if coach else False
        if not allowed and player is not None:
            allowed = any(
                rel.player_id == player.id for rel in current_event.players_relations
            )
        if not allowed:
            abort(403, "Not authorized to view this calendar event")

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

    # Role-based visibility (PAD-36): coaches get the full payload; students
    # only ever see their own participation, presence and notifications.
    viewer_player_id = None
    if current_coach() is None:
        player = current_player()
        if player is None:
            abort(403, "Not authorized to view this class")
        viewer_player_id = player.id

    return jsonify(
        serialize_class_instance(current_class, viewer_player_id=viewer_player_id)
    )


@bp.get("/player_profile/<int:player_id>")
@jwt_required()
def player_profile(player_id):
    coach = current_coach()
    return jsonify(get_player_profile(coach, player_id))


def _resolve_attendance_subject(raw_player_id):
    """Authorize `/attendance_history` and return the player whose data to read.

    Spec `attendance.history` rule 3. The page has two entry points — a student
    reading their own history and a coach reading a roster player's — so the
    guard lives HERE, on the data endpoint, not on the frontend route. PAD-88 and
    PAD-115 are the precedent: a `RoleRoute` in the SPA is UX, not authorization.

    Resolution order matters. The SELF case is checked first so a user who holds
    both a player and a coach profile is never 403'd on their own data — the
    coach-first ordering used elsewhere in this blueprint would do exactly that.
    """
    player = current_player()

    if raw_player_id in (None, ""):
        if player is None:
            abort(403, "Not authorized to view this attendance history")
        return player

    try:
        target_id = int(raw_player_id)
    except (TypeError, ValueError):
        abort(400, "playerId must be an integer")

    # 1. Own data.
    if player is not None and player.id == target_id:
        return player

    # 2. A coach may read any player on their own roster, and nobody else's.
    coach = current_coach()
    if coach is not None:
        require_own_roster_relation(coach, target_id)
        return Player.query.get_or_404(target_id)

    abort(403, "Not authorized to view this attendance history")


def _parse_attendance_bound(raw, *, end_of_day):
    """Parse a `from`/`to` query param; a bare date means the whole day."""
    parsed = parser.isoparse(raw)
    if len(raw.strip()) <= 10 and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


@bp.get("/attendance_history")
@jwt_required()
def attendance_history():
    """Attended-class history for one player (PAD-114).

    Query params: `playerId` (defaults to the caller), `from`/`to` (ISO-8601,
    default = current month) and an optional `granularity` pin. The response
    always echoes the granularity actually used so the chart labels its axis from
    the payload rather than re-deriving the rule client-side.
    """
    subject = _resolve_attendance_subject(request.args.get("playerId"))

    raw_from = request.args.get("from")
    raw_to = request.args.get("to")
    if raw_from and raw_to:
        try:
            range_start = _parse_attendance_bound(raw_from, end_of_day=False)
            range_end = _parse_attendance_bound(raw_to, end_of_day=True)
        except (ValueError, OverflowError):
            abort(400, "from/to must be ISO-8601 datetimes")
    else:
        range_start, range_end = default_attendance_range()

    payload = build_attendance_history(
        player_id=subject.id,
        range_start=range_start,
        range_end=range_end,
        granularity=request.args.get("granularity"),
    )
    payload["playerName"] = subject.user.name if subject.user else None
    return jsonify(payload)


# -------------------------------------------------------------------
# CREATE
# -------------------------------------------------------------------

# PAD-92: the raw entity-creation routes below this line were removed.
#
#   POST /club, /user, /player, /coach, /coach_level, /lesson, /calendar_block
#
# All seven were unauthenticated thin wrappers around a create-service, letting
# an anonymous caller mint clubs, users, coaches and lessons at will. None of
# them had a caller in `apps/web`, `apps/mobile`, `packages/api`,
# `levelup_issue_bot`, the Playwright specs or `e2e/scripts/seed.py`; the real
# clients use the scoped routes (`/add_player`, `/incomplete_player`,
# `/add_coach_level`, `/add_class`, `/add_event`), which derive the owning coach
# from the JWT. Deleting them removes the attack surface entirely rather than
# guarding a route nobody uses.


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
    from padel_app.services.lesson_service import NoSeasonCoversDateError

    data = request.get_json() or {}
    try:
        lesson = add_class_service(data, current_coach(), current_club())
    except NoSeasonCoversDateError as e:
        # PAD-90: "recurs until season end" with no covering season is rejected
        # rather than creating an unbounded recurring class.
        return jsonify({"error": str(e), "code": e.code}), 400
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


# -------------------------------------------------------------------
# Student availability blockers (PAD-28)
# Suppress AUTOMATIC class invitations during unavailable windows.
# Student-scoped: only users with a player profile may manage these.
# -------------------------------------------------------------------

def _require_student():
    user = current_user()
    if not user.player:
        abort(403, "Availability blockers are only available to students")
    return user


@bp.get("/availability_blockers")
@jwt_required()
def get_availability_blockers():
    user = _require_student()
    blocks = list_student_blockers(user.id)
    return jsonify([serialize_calendar_block(b) for b in blocks])


@bp.post("/availability_blockers")
@jwt_required()
def create_availability_blocker():
    user = _require_student()
    data = request.get_json() or {}
    block = create_student_blocker(user.id, data)
    return jsonify(serialize_calendar_block(block)), 201


@bp.put("/availability_blockers/<int:block_id>")
@jwt_required()
def update_availability_blocker(block_id):
    user = _require_student()
    data = request.get_json() or {}
    block = update_student_blocker(block_id, user.id, data)
    return jsonify(serialize_calendar_block(block))


@bp.delete("/availability_blockers/<int:block_id>")
@jwt_required()
def delete_availability_blocker(block_id):
    user = _require_student()
    data = request.get_json() or {}
    delete_student_blocker(
        block_id, user.id, data.get("occDate"), data.get("scope", "all")
    )
    return "", 204


@bp.post("/add_coach_level")
@jwt_required()
def add_coach_level():
    data = request.get_json() or {}
    upsert_coach_levels(require_coach(), data)
    return jsonify(data)


@bp.post("/add_seasons")
@jwt_required()
def add_seasons():
    data = request.get_json() or []
    coach = require_coach()
    try:
        upsert_seasons(coach, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    seasons = list_seasons(coach)
    for season in seasons:
        regenerate_future_instances_for_season(season)

    return jsonify([serialize_season(s) for s in list_seasons(coach)])


@bp.post("/delete/season")
@jwt_required()
def delete_season_route():
    data = request.get_json() or {}
    delete_season(require_coach(), data["id"])
    return jsonify({"status": "Removed season"}), 200


@bp.post("/add_evaluation_categories")
@jwt_required()
def add_evaluation_categories():
    data = request.get_json() or {}
    upsert_evaluation_categories(require_coach(), data)
    return jsonify(data)


@bp.post("/add_coach_note")
@jwt_required()
def add_coach_note():
    data = request.get_json() or {}
    result, status = add_coach_note_service(require_coach(), data)
    return jsonify(result), status


@bp.post("/add_evaluation_entry")
@jwt_required()
def add_evaluation_entry():
    data = request.get_json() or {}
    result = add_evaluation_entry_service(require_coach(), data)
    return jsonify(result)


# -------------------------------------------------------------------
# EDIT / DOMAIN ACTIONS
# -------------------------------------------------------------------

# PAD-92: `POST /user/<id>` and `POST /club/<id>` were unauthenticated arbitrary
# edits of any user or club row, by id — an anonymous caller could rewrite any
# account. Neither had a caller anywhere in the repos; profile edits go through
# `/api/account/profile` and club edits through the admin editor. Removed.


# -------------------------------------------------------------------
# Coach invitations (clubs.coach-invitation)
# -------------------------------------------------------------------

@bp.post("/club/<int:club_id>/coach-invitations")
@jwt_required()
def create_coach_invitation(club_id):
    data = request.get_json(silent=True) or {}
    coach = require_coach()
    invitation = create_coach_invitation_service(
        club_id, coach, email=data.get("email")
    )
    return jsonify({
        "token": invitation.token,
        "inviteLink": f"/invite/coach/{invitation.token}",
        "expiresAt": invitation.expires_at.isoformat(),
    }), 201


@bp.get("/club/<int:club_id>/coach-invitations")
@jwt_required()
def list_coach_invitations(club_id):
    coach = require_coach()
    invitations = list_coach_invitations_service(club_id, coach)
    return jsonify([
        {
            "token": inv.token,
            "email": inv.email,
            "expiresAt": inv.expires_at.isoformat(),
            "createdAt": inv.created_at.isoformat() if inv.created_at else None,
        }
        for inv in invitations
    ])


@bp.get("/coach-invitations/<token>")
def get_coach_invitation(token):
    invitation = get_coach_invitation_service(token)
    return jsonify({
        "clubName": invitation.club.name,
        "status": invitation.status,
    })


@bp.post("/coach-invitations/<token>/accept")
@jwt_required(optional=True)
def accept_coach_invitation(token):
    data = request.get_json(silent=True) or {}

    coach = None
    user_id = get_jwt_identity()
    if user_id is not None:
        user = User.query.get(int(user_id))
        coach = user.coach if user else None

    if coach is not None:
        accept_coach_invitation_service(token, coach=coach)
        return jsonify({"success": True})

    user = accept_coach_invitation_service(token, data=data)
    return jsonify({
        "accessToken": create_access_token(identity=str(user.id)),
    })


@bp.post("/coach-invitations/<token>/revoke")
@jwt_required()
def revoke_coach_invitation(token):
    coach = require_coach()
    revoke_coach_invitation_service(token, coach)
    return jsonify({"success": True})


# -------------------------------------------------------------------
# Player invitations (players.invite-completion)
# -------------------------------------------------------------------

@bp.post("/incomplete_player")
@jwt_required()
def create_incomplete_player():
    data = request.get_json() or {}
    # PAD-92: the invitation is always issued by the CALLING coach. The body's
    # legacy `coachId` is kept for payload compatibility but only as an
    # assertion — it may not name a different coach.
    coach = require_coach()
    assert_acting_coach(coach, data.get("coachId"))
    data["coachId"] = coach.id
    invitation = create_incomplete_player_service(data)
    return jsonify({
        "token": invitation.token,
        "inviteLink": f"/invite/player/{invitation.token}",
        "expiresAt": invitation.expires_at.isoformat(),
    }), 201


@bp.get("/player-invitations/<token>")
def get_player_invitation(token):
    invitation = get_player_invitation_service(token)
    return jsonify({
        "playerName": invitation.player.user.name,
        "status": invitation.status,
    })


@bp.post("/player-invitations/<token>/accept")
def accept_player_invitation(token):
    data = request.get_json(silent=True) or {}
    user = accept_player_invitation_service(token, data=data)
    return jsonify({
        "accessToken": create_access_token(identity=str(user.id)),
    })


@bp.post("/player-invitations/<token>/revoke")
@jwt_required()
def revoke_player_invitation(token):
    coach = require_coach()
    revoke_player_invitation_service(token, coach)
    return jsonify({"success": True})


# PAD-92: `POST /lesson/<id>` and `POST /calendar_block/<id>` were
# unauthenticated edits of any lesson or block by id. Both are dead legacy
# duplicates — the apps edit classes through `/edit_class` and blocks through the
# `@jwt_required()` `PUT /calendar_block/<id>`. Removed.


@bp.post("/class_instance/presences/confirm")
@jwt_required()
def confirm_presences():
    from padel_app.scheduler import _compute_invite_start_dt
    from padel_app.services.notification_service import (
        _ensure_vacancy_for_player,
        _is_semi_auto,
        get_or_create_config,
        trigger_invitations,
    )
    from padel_app.utils.dates import utcnow_naive

    data = request.get_json()
    presences = confirm_presences_service(data['classInstance'], data['presences'])

    notified_players = []
    approval_bundle = None
    has_absences = any(p.status == "absent" for p in presences)
    if has_absences and presences:
        coach = current_coach()
        instance = presences[0].lesson_instance
        if instance and instance.start_datetime > utcnow_naive():
            config = get_or_create_config(coach.id)
            if _is_semi_auto(config):
                # Semi-automatic: create pending vacancies for the absent
                # players and bundle ONE approval prompt instead of sending.
                from padel_app.services.replacement_approval_service import (
                    create_approval_prompts,
                )
                pending_vacancies = []
                for p in presences:
                    if p.status != "absent":
                        continue
                    vacancy = _ensure_vacancy_for_player(instance, coach.id, p.player_id)
                    if vacancy is not None and vacancy.approval_status == "pending":
                        pending_vacancies.append(vacancy)
                if pending_vacancies:
                    approval_bundle = create_approval_prompts(
                        pending_vacancies, instance, coach.id, config
                    )
            else:
                invite_start_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
                # Only send invitations if the invitation window has opened.
                # If not yet open, the invite_start scheduler job will call trigger_invitations
                # at the configured time, which will find the absent presences and invite.
                if invite_start_dt is None or utcnow_naive() >= invite_start_dt:
                    try:
                        notified_players = trigger_invitations(instance, coach.id) or []
                    except Exception:
                        from padel_app.sql_db import db
                        db.session.rollback()

    return jsonify({
        "presences": [serialize_presence(p) for p in presences],
        "notifiedPlayers": notified_players,
        "approvalBundle": approval_bundle,
    })


# PAD-92: `POST /lesson/<id>/status` was an unauthenticated status mutation on
# any lesson by id, with no caller in any repo. Removed.


def _assert_owns_class_payload(coach, data):
    """Shared guard for /edit_class and /remove_class (PAD-92).

    Both take ``{"event": {"model": ..., "originalId": ...}, "scope": ...}`` and
    hand it straight to a service that resolves the row by id. Resolve and
    ownership-check the target here, BEFORE the service mutates anything.
    """
    event = data.get("event") or {}
    model = event.get("model")
    original_id = event.get("originalId")

    # Payload-shape errors stay 400 — the services already return that, and we
    # must not turn a malformed body into a misleading 403.
    if not model or original_id in (None, ""):
        return None
    return require_owned_class(coach, model, original_id)


@bp.post("/edit_class")
@jwt_required()
def edit_class():
    data = request.get_json() or {}
    _assert_owns_class_payload(require_coach(), data)
    result, status = edit_class_service(data)
    return jsonify(result), status


@bp.post("/remove_class")
@jwt_required()
def remove_class():
    data = request.get_json() or {}
    _assert_owns_class_payload(require_coach(), data)
    result, status = remove_class_service(data)
    return jsonify(result), status


UNIQUE_FIELD_CHECKS = {
    ("user", "username"),
    ("user", "email"),
}

# Fields that are NOT DB-unique but where we still want to WARN about duplicates
# (PAD-17). Matching is exact but case-insensitive. These never block the caller —
# the frontend surfaces them as a non-blocking warning.
#
# PAD-17 fix: the name warn-check is scoped to the REQUESTING coach's own roster
# (Association_CoachPlayer). A globally-unscoped match warned coaches about
# identically-named players at *other* clubs — a false positive. The warn now
# only fires when the duplicate name belongs to a player in the caller's roster,
# which the message reflects.
WARN_DUPLICATE_CHECKS = {
    ("user", "name"): "You already have a player with this name",
}


@bp.post("/check_field_available")
@jwt_required()
def check_field_available():
    """Check whether a field value is available for any whitelisted model.

    Two kinds of checks are supported:
      * UNIQUE_FIELD_CHECKS  — hard uniqueness (username, email). Exact match,
        matched GLOBALLY across the table (these are true DB uniqueness rules).
      * WARN_DUPLICATE_CHECKS — non-unique fields (name) where we only WARN about
        duplicates. Matched case-insensitively and scoped to the requesting
        coach's own roster (see below).

    In both cases a duplicate returns HTTP 409 with a human-readable ``message``.
    The frontend decides whether a 409 blocks submission (unique fields) or is a
    soft warning (duplicate fields).

    Scoping (name warn-check): the caller must pass a ``scope`` (or ``coach``)
    field carrying the requesting coach's id. Only players in that coach's
    ``Association_CoachPlayer`` roster are considered. If no scope is supplied we
    deliberately skip the warn (return ``available: true``) rather than fall back
    to a global match, which would resurface the cross-club false positive.
    """
    data = request.get_json() or {}
    model_key = (data.get("model") or "").strip().lower()
    field = (data.get("field") or "").strip()
    value = (data.get("value") or "").strip()

    is_unique = (model_key, field) in UNIQUE_FIELD_CHECKS
    warn_message = WARN_DUPLICATE_CHECKS.get((model_key, field))

    if not is_unique and warn_message is None:
        abort(400, "Check not allowed")
    if not value:
        return jsonify({"available": True})

    ModelClass = MODELS[model_key]
    column = getattr(ModelClass, field)

    if is_unique:
        exists = ModelClass.query.filter_by(**{field: value}).first() is not None
        message = f"This {field} is already taken"
    else:
        # Warn-only duplicate field (name): case-insensitive exact match, scoped
        # to the requesting coach's roster.
        from sqlalchemy import func

        # PAD-92: the roster scope is the CALLER's own coach id, taken from the
        # JWT. Previously it came from the request body, which let an anonymous
        # caller probe any coach's roster for the names on it. The legacy body
        # field is still accepted, but only as an assertion.
        claimed_scope = data.get("scope", data.get("coach"))
        caller_coach = current_coach()
        if caller_coach is None:
            # A student has no roster to check against — skip the warn rather
            # than falling back to a global match.
            return jsonify({"available": True})
        assert_acting_coach(caller_coach, claimed_scope)
        coach_id = caller_coach.id

        exists = (
            ModelClass.query
            .join(Player, Player.user_id == User.id)
            .join(
                Association_CoachPlayer,
                Association_CoachPlayer.player_id == Player.id,
            )
            .filter(Association_CoachPlayer.coach_id == coach_id)
            .filter(func.lower(column) == value.lower())
            .first()
        ) is not None
        message = warn_message

    if exists:
        return jsonify({"available": False, "message": message}), 409
    return jsonify({"available": True})


@bp.post("/add_player")
@jwt_required()
def add_player():
    data = request.get_json() or {}
    # PAD-92: the new player joins the CALLING coach's roster.
    coach = require_coach()
    assert_acting_coach(coach, data.get("coachId"))
    data["coachId"] = coach.id
    coach_player_info = add_player_service(data)
    return jsonify(coach_player_info)


@bp.post("/edit_player")
@jwt_required()
def edit_player():
    data = request.get_json() or {}
    # PAD-92: `player.coachId` used to select which coach-player relation to
    # edit, straight from the body — so any caller could rewrite any coach's
    # notes/level/side for any player. Pin it to the JWT and require the player
    # to actually be on the caller's roster.
    coach = require_coach()
    player_info = data.get("player") or {}
    assert_acting_coach(coach, player_info.get("coachId"))
    require_own_roster_relation(coach, player_info.get("playerId"))
    player_info["coachId"] = coach.id
    data["player"] = player_info
    coach_player_info = edit_player_service(data)
    return jsonify(coach_player_info)


@bp.post("/remove_player")
@jwt_required()
def remove_player():
    data = request.get_json() or {}
    # PAD-92: a coach may only remove a player from their OWN roster.
    coach = require_coach()
    assert_acting_coach(coach, data.get("coachId"))
    require_own_roster_relation(coach, data.get("playerId"))
    data["coachId"] = coach.id
    result, status = remove_player_service(data)
    return jsonify(result), status


@bp.post("/delete/coach_level")
@jwt_required()
def delete_coach_level():
    """PAD-92: previously an anonymous `id`-only delete of any coach's level."""
    data = request.get_json() or {}
    coach = require_coach()
    rel = CoachLevel.query.filter_by(id=_required_int_id(data)).first_or_404()
    if rel.coach_id != coach.id:
        abort(403, "Not authorized to delete this level")
    rel.delete()
    return jsonify({"status": "Removed coach levels"}), 200


@bp.post("/delete/evaluation_category")
@jwt_required()
def delete_evaluation_category():
    """PAD-92: previously an anonymous `id`-only delete of any coach's category."""
    data = request.get_json() or {}
    coach = require_coach()
    rel = EvaluationCategory.query.filter_by(id=_required_int_id(data)).first_or_404()
    if rel.coach_id != coach.id:
        abort(403, "Not authorized to delete this evaluation category")
    rel.delete()
    return jsonify({"status": "Removed evaluation categories"}), 200


@bp.post("/delete/coach_note")
@jwt_required()
def delete_coach_note():
    """PAD-92: previously an anonymous `id`-only delete of any coach's note.

    A note hangs off an ``Association_CoachPlayer`` row, so ownership is that
    relation's ``coach_id``.
    """
    data = request.get_json() or {}
    coach = require_coach()
    rel = CoachPlayerNote.query.filter_by(id=_required_int_id(data)).first_or_404()
    if rel.coach_player is None or rel.coach_player.coach_id != coach.id:
        abort(403, "Not authorized to delete this note")
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

    coach = require_coach()

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


def _run_import_confirm(data, coach, club):
    """Shared bulk-import worker.

    Runs the per-table import loop and yields SSE-shaped progress events so a
    streaming response can keep the connection alive (avoiding a false gateway
    504 on large uploads). The final ``done`` event carries the same results
    dict the JSON endpoint returns.

    Yielded event shapes::

        {"type": "progress", "table": <str|None>, "done": <int>, "total": <int>}
        {"type": "done", "results": {TableName: {imported, errors, created_ids}}}
        {"type": "error", "message": <str>}

    Individual ``bulk_create_*`` services already commit per row and collect
    their own per-row errors, so already-imported rows persist even if a later
    table fails — matching the previous behaviour.
    """
    import json
    from padel_app.sql_db import db
    from padel_app.models.bulk_import import BulkImport

    results = {}
    all_created_ids = {}
    summary = {}

    tables_to_run = [
        (name, fn, needs_club)
        for (name, fn, needs_club) in _TABLE_MAP
        if data.get(name)
    ]
    total = len(tables_to_run)

    try:
        for idx, (table_name, fn, needs_club) in enumerate(tables_to_run):
            rows = data.get(table_name)
            # Emit progress before each table so bytes flow to the client and
            # the gateway never idle-times-out mid-import.
            yield {"type": "progress", "table": table_name, "done": idx, "total": total}
            print(f"Importing {len(rows)} to {table_name}")

            if needs_club:
                # Players is the reported slow path — stream row-level progress
                # from it so a single large player list can't exceed the gateway
                # timeout on its own.
                if getattr(fn, "supports_progress", False):
                    result = None
                    for step in fn(rows, coach, club, stream=True):
                        if isinstance(step, dict) and step.get("_progress"):
                            yield {
                                "type": "progress",
                                "table": table_name,
                                "done": idx,
                                "total": total,
                                "rows_done": step["done"],
                                "rows_total": step["total"],
                            }
                        else:
                            result = step
                else:
                    result = fn(rows, coach, club)
            else:
                result = fn(rows, coach)

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

        yield {"type": "progress", "table": None, "done": total, "total": total}
        yield {"type": "done", "results": results}

    except Exception as exc:  # infrastructure-level safety net
        db.session.rollback()
        yield {"type": "error", "message": str(exc)}


@bp.post("/import/confirm")
@jwt_required()
def import_confirm():
    """JSON import endpoint (kept for API/back-compat).

    Drains the shared worker and returns the same results dict as before.
    """
    coach = require_coach()
    club = current_club()
    data = request.get_json() or {}

    final = {}
    for ev in _run_import_confirm(data, coach, club):
        if ev["type"] == "done":
            final = ev["results"]
        elif ev["type"] == "error":
            return jsonify({"error": ev["message"]}), 500

    return jsonify(final)


@bp.post("/import/confirm/stream")
@jwt_required()
def import_confirm_stream():
    """Streaming import endpoint used by the frontend.

    Emits SSE progress events while importing so the connection stays alive and
    the front gateway never returns a false 504 on large uploads. The terminal
    ``done`` event carries the same results dict as ``/import/confirm``.
    """
    from flask import stream_with_context

    coach = require_coach()
    club = current_club()
    data = request.get_json() or {}

    def gen():
        # Initial keepalive comment flushes headers immediately.
        yield ": keepalive\n\n"
        for ev in _run_import_confirm(data, coach, club):
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@bp.get("/import/history")
@jwt_required()
def import_history():
    from padel_app.services.import_service import get_import_history
    coach = require_coach()
    return jsonify(get_import_history(coach))


@bp.post("/import/<int:import_id>/revert")
@jwt_required()
def import_revert(import_id):
    from padel_app.services.import_service import revert_import
    coach = require_coach()

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


# -------------------------------------------------------------------
# Dashboard – manual notification for pending confirmations (PAD-78)
# -------------------------------------------------------------------
# NOTE: added at the end of the file (frontend_api.py is also touched by the
# open PR #54); import is local to keep the top-of-file import block untouched
# and minimise merge conflicts.

@bp.post("/dashboard/pending-confirmations/notify")
@jwt_required()
def notify_pending_confirmations_route():
    from padel_app.helpers.dashboard.pending import notify_pending_confirmations

    coach = current_coach()
    if coach is None:
        abort(403, "Only coaches can send confirmation reminders")

    result = notify_pending_confirmations(coach_id=coach.id)
    return jsonify(result)
