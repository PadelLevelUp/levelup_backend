"""
PAD-92 — `/api/app` (modules/frontend_api.py) must not expose unauthenticated
routes, and an authenticated coach must not be able to reach another coach's
data.

Before PAD-92 roughly 25 routes on this blueprint carried no auth decorator at
all, and the services behind them read `coachId` / `playerId` / `id` straight
from the request body. `delete/coach_level`, `delete/evaluation_category` and
`delete/coach_note` were the sharpest: a bare `id` plus `.first_or_404().delete()`
meant anonymous deletion of any coach's row.

Contract pinned here, per route:
    anonymous            -> 401, nothing written
    other coach          -> 403, nothing written
    owning coach         -> 2xx

Plus: the legacy no-caller routes are gone, and the notification debug endpoint
fails closed.
"""
import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(app):
    """Two coaches. Coach A owns a player, a level, a category, a note and a class."""
    from padel_app.models import (
        User,
        Player,
        Association_CoachPlayer,
        CoachLevel,
        EvaluationCategory,
        CoachPlayerNote,
        Lesson,
        LessonInstance,
    )
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from datetime import datetime, timedelta

    with app.app_context():
        club = Club(name="Authz Club")
        db.session.add(club)

        user_a = User(name="Coach A", username="coach_a", password="x")
        user_b = User(name="Coach B", username="coach_b", password="x")
        player_user = User(name="Player P", username="player_p", password="x")
        db.session.add_all([user_a, user_b, player_user])
        db.session.flush()

        coach_a = Coach(user_id=user_a.id)
        coach_b = Coach(user_id=user_b.id)
        player = Player(user_id=player_user.id)
        db.session.add_all([coach_a, coach_b, player])
        db.session.flush()

        rel = Association_CoachPlayer(coach_id=coach_a.id, player_id=player.id)
        level = CoachLevel(coach_id=coach_a.id, label="Beginner", code="B1")
        category = EvaluationCategory(coach_id=coach_a.id, name="Volley")
        db.session.add_all([rel, level, category])
        db.session.flush()

        note = CoachPlayerNote(
            coach_player_id=rel.id, type="strength", text="Great serve"
        )
        db.session.add(note)

        start = datetime.utcnow() + timedelta(days=3)
        lesson = Lesson(
            title="Authz Class",
            type="academy",
            max_players=4,
            club_id=club.id,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
        )
        db.session.add(lesson)
        db.session.flush()
        db.session.add(
            Association_CoachLesson(coach_id=coach_a.id, lesson_id=lesson.id)
        )

        instance = LessonInstance(
            lesson_id=lesson.id,
            max_players=4,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            original_lesson_occurence_date=start.date(),
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(
            Association_CoachLessonInstance(
                coach_id=coach_a.id, lesson_instance_id=instance.id
            )
        )

        db.session.commit()

        return {
            "user_a": user_a.id,
            "user_b": user_b.id,
            "coach_a": coach_a.id,
            "coach_b": coach_b.id,
            "player": player.id,
            # The player's *user* id — the JWT identity for a student caller.
            "player_user": player_user.id,
            "level": level.id,
            "category": category.id,
            "note": note.id,
            "lesson": lesson.id,
            "instance": instance.id,
            "instance_date": start.date().isoformat(),
        }


def _count(app, model_cls):
    with app.app_context():
        return model_cls.query.count()


# ---------------------------------------------------------------------------
# 1. Anonymous callers
# ---------------------------------------------------------------------------

# (method, path, json-body). Bodies are minimal on purpose: the guard must fire
# before any payload validation, so a 400 here would be as much of a failure as
# a 200.
ANONYMOUS_ROUTES = [
    ("post", "/api/app/add_player", {"coachId": 1, "name": "Anon"}),
    (
        "post",
        "/api/app/edit_player",
        {"player": {"playerId": 1, "coachId": 1}, "updates": {"name": "Anon"}},
    ),
    ("post", "/api/app/remove_player", {"coachId": 1, "playerId": 1}),
    (
        "post",
        "/api/app/edit_class",
        {
            "event": {"model": "LessonInstance", "originalId": 1, "date": "2030-01-01"},
            "scope": "single",
            "updates": {},
        },
    ),
    (
        "post",
        "/api/app/remove_class",
        {
            "event": {"model": "LessonInstance", "originalId": 1, "date": "2030-01-01"},
            "scope": "single",
        },
    ),
    ("post", "/api/app/delete/coach_note", {"id": 1}),
    ("post", "/api/app/delete/coach_level", {"id": 1}),
    ("post", "/api/app/delete/evaluation_category", {"id": 1}),
    (
        "post",
        "/api/app/check_field_available",
        {"model": "user", "field": "username", "value": "coach_a"},
    ),
    ("post", "/api/app/incomplete_player", {"coachId": 1, "name": "Anon"}),
    ("get", "/api/app/calendar_event?model=lesson_instance&original_id=1", None),
]


@pytest.mark.parametrize("method,path,body", ANONYMOUS_ROUTES)
def test_anonymous_caller_is_rejected(app, client, world, method, path, body):
    res = getattr(client, method)(path, json=body)
    assert res.status_code == 401, f"{method.upper()} {path} -> {res.status_code}"


def test_anonymous_delete_routes_do_not_mutate(app, client, world):
    """The three bare-id delete routes must not touch a row when unauthenticated."""
    from padel_app.models import CoachLevel, EvaluationCategory, CoachPlayerNote

    before = (
        _count(app, CoachLevel),
        _count(app, EvaluationCategory),
        _count(app, CoachPlayerNote),
    )

    client.post("/api/app/delete/coach_level", json={"id": world["level"]})
    client.post("/api/app/delete/evaluation_category", json={"id": world["category"]})
    client.post("/api/app/delete/coach_note", json={"id": world["note"]})

    after = (
        _count(app, CoachLevel),
        _count(app, EvaluationCategory),
        _count(app, CoachPlayerNote),
    )
    assert after == before


# ---------------------------------------------------------------------------
# 2. Authenticated, but not the owner (IDOR)
# ---------------------------------------------------------------------------


def test_other_coach_cannot_delete_level(app, client, world):
    from padel_app.models import CoachLevel

    res = client.post(
        "/api/app/delete/coach_level",
        json={"id": world["level"]},
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403
    assert _count(app, CoachLevel) == 1


def test_other_coach_cannot_delete_evaluation_category(app, client, world):
    from padel_app.models import EvaluationCategory

    res = client.post(
        "/api/app/delete/evaluation_category",
        json={"id": world["category"]},
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403
    assert _count(app, EvaluationCategory) == 1


def test_other_coach_cannot_delete_coach_note(app, client, world):
    from padel_app.models import CoachPlayerNote

    res = client.post(
        "/api/app/delete/coach_note",
        json={"id": world["note"]},
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403
    assert _count(app, CoachPlayerNote) == 1


def test_other_coach_cannot_remove_player(app, client, world):
    from padel_app.models import Association_CoachPlayer

    res = client.post(
        "/api/app/remove_player",
        json={"playerId": world["player"]},
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403
    assert _count(app, Association_CoachPlayer) == 1


def test_other_coach_cannot_edit_player(app, client, world):
    res = client.post(
        "/api/app/edit_player",
        json={
            "player": {"playerId": world["player"]},
            "updates": {"notes": "pwned"},
        },
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403


def test_body_coach_id_cannot_impersonate_another_coach(app, client, world):
    """Passing someone else's `coachId` is a 403, not a silent override."""
    res = client.post(
        "/api/app/add_player",
        json={"coachId": world["coach_a"], "name": "Injected"},
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403


def test_other_coach_cannot_remove_class(app, client, world):
    from padel_app.models import LessonInstance

    res = client.post(
        "/api/app/remove_class",
        json={
            "event": {
                "model": "LessonInstance",
                "originalId": world["instance"],
                "date": world["instance_date"],
            },
            "scope": "single",
        },
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403
    assert _count(app, LessonInstance) == 1


def test_other_coach_cannot_edit_class(app, client, world):
    res = client.post(
        "/api/app/edit_class",
        json={
            "event": {
                "model": "LessonInstance",
                "originalId": world["instance"],
                "date": world["instance_date"],
            },
            "scope": "single",
            "updates": {"name": "pwned"},
        },
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403

    from padel_app.models import LessonInstance

    with app.app_context():
        assert LessonInstance.query.get(world["instance"]).title != "pwned"


def test_other_coach_cannot_read_calendar_event(app, client, world):
    res = client.get(
        f"/api/app/calendar_event?model=lesson_instance&original_id={world['instance']}",
        headers=_auth_header(app, world["user_b"]),
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# 3. The owner still gets through
# ---------------------------------------------------------------------------


def test_owner_can_delete_own_level(app, client, world):
    from padel_app.models import CoachLevel

    res = client.post(
        "/api/app/delete/coach_level",
        json={"id": world["level"]},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200
    assert _count(app, CoachLevel) == 0


def test_owner_can_delete_own_evaluation_category(app, client, world):
    from padel_app.models import EvaluationCategory

    res = client.post(
        "/api/app/delete/evaluation_category",
        json={"id": world["category"]},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200
    assert _count(app, EvaluationCategory) == 0


def test_owner_can_delete_own_coach_note(app, client, world):
    from padel_app.models import CoachPlayerNote

    res = client.post(
        "/api/app/delete/coach_note",
        json={"id": world["note"]},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200
    assert _count(app, CoachPlayerNote) == 0


def test_owner_can_read_own_calendar_event(app, client, world):
    res = client.get(
        f"/api/app/calendar_event?model=lesson_instance&original_id={world['instance']}",
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200


def test_owner_check_field_available_uses_jwt_scope(app, client, world):
    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "username", "value": "totally-free"},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_owner_can_remove_own_player(app, client, world):
    from padel_app.models import Association_CoachPlayer

    res = client.post(
        "/api/app/remove_player",
        json={"coachId": world["coach_a"], "playerId": world["player"]},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 200
    assert _count(app, Association_CoachPlayer) == 0


# ---------------------------------------------------------------------------
# 4. Legacy no-caller routes are gone
# ---------------------------------------------------------------------------

DELETED_ROUTES = [
    ("post", "/api/app/club"),
    ("post", "/api/app/user"),
    ("post", "/api/app/player"),
    ("post", "/api/app/coach"),
    ("post", "/api/app/coach_level"),
    ("post", "/api/app/lesson"),
    ("post", "/api/app/calendar_block"),
    ("post", "/api/app/user/1"),
    ("post", "/api/app/club/1"),
    ("post", "/api/app/lesson/1"),
    ("post", "/api/app/calendar_block/1"),
    ("post", "/api/app/lesson/1/status"),
    ("get", "/api/app/lessons"),
    ("get", "/api/app/calendar_block"),
]


@pytest.mark.parametrize("method,path", DELETED_ROUTES)
def test_legacy_routes_are_removed(app, client, method, path):
    res = getattr(client, method)(path, json={})
    assert res.status_code in (404, 405), f"{method.upper()} {path} -> {res.status_code}"


def test_url_map_has_no_unauthenticated_write_routes(app):
    """A blanket check so a future route cannot silently reintroduce the hole.

    Every `/api/app` rule that writes (POST/PUT/DELETE) must either be on the
    public allowlist below or have a JWT-verifying view.
    """
    public_writes = {
        "/api/app/activate/user/<user_id>",
        "/api/app/coach-invitations/<token>/accept",
        "/api/app/player-invitations/<token>/accept",
    }

    offenders = []
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api/app"):
            continue
        if not ({"POST", "PUT", "DELETE"} & rule.methods):
            continue
        if path in public_writes:
            continue

        view = app.view_functions[rule.endpoint]
        # flask-jwt-extended wraps the view; the wrapper closes over the
        # decorator's args, so the original function is reachable via __wrapped__.
        if not hasattr(view, "__wrapped__"):
            offenders.append(path)

    assert offenders == [], f"unauthenticated write routes on /api/app: {offenders}"


# ---------------------------------------------------------------------------
# 5. The notification debug endpoint fails closed
# ---------------------------------------------------------------------------


def test_debug_reminder_endpoint_requires_jwt(app, client, world):
    app.config["E2E_DEBUG_ENDPOINTS"] = "true"
    res = client.post(
        "/api/app/notify/debug/schedule_reminder_test",
        json={"secondsUntilReminderFires": 3600},
    )
    assert res.status_code == 401


def test_debug_reminder_endpoint_404s_when_flag_off(app, client, world):
    app.config["E2E_DEBUG_ENDPOINTS"] = None
    res = client.post(
        "/api/app/notify/debug/schedule_reminder_test",
        json={"secondsUntilReminderFires": 3600},
        headers=_auth_header(app, world["user_a"]),
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# 6. PAD-115 — POST /class_instance/training/confirm
# ---------------------------------------------------------------------------
# This route was missed by PAD-92 (anonymous / other-coach) and PAD-103
# (wrong-role) because it never dereferenced a coach at all: it read
# `classInstance` and `exerciseIds` straight out of the body and wrote. It
# therefore neither 401'd nor crashed — it silently succeeded, which is a live
# IDOR rather than a wrong-status-code bug.
#
# Statuses are asserted with `==`, never `!= 200`: the pre-fix behaviour was a
# 200, and the neighbouring failure mode on this blueprint is a 500, so a loose
# assertion would pass against unfixed code.
#
# Spec: specs/training/spec.md -> training.lesson-planning, rules 5-8.


@pytest.fixture
def training_world(app, world):
    """Adds exercises and a class of coach B's own on top of ``world``.

    Exercise access is modelled the way `get_exercises_for_coach` reads it — an
    `Association_CoachExercise` row in either role — so the fixture can express
    "owned", "followed" and "no relation at all" separately.
    """
    from padel_app.models import Exercise, Lesson, LessonInstance
    from padel_app.models.Association_CoachExercise import Association_CoachExercise
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.lesson_instance_training import LessonInstanceTraining
    from padel_app.models.clubs import Club
    from datetime import datetime, timedelta

    with app.app_context():
        club_id = Club.query.first().id

        ex_a = Exercise(name="A's drill", type="attack", difficulty=3,
                        owner_coach_id=world["coach_a"])
        ex_b = Exercise(name="B's drill", type="defense", difficulty=3,
                        owner_coach_id=world["coach_b"])
        ex_shared = Exercise(name="A's shared drill", type="serve", difficulty=3,
                             owner_coach_id=world["coach_a"])
        db.session.add_all([ex_a, ex_b, ex_shared])
        db.session.flush()

        db.session.add_all([
            Association_CoachExercise(
                coach_id=world["coach_a"], exercise_id=ex_a.id, role="owner"),
            Association_CoachExercise(
                coach_id=world["coach_b"], exercise_id=ex_b.id, role="owner"),
            Association_CoachExercise(
                coach_id=world["coach_a"], exercise_id=ex_shared.id, role="owner"),
            # Coach B may *read* A's shared drill, so B may also plan it.
            Association_CoachExercise(
                coach_id=world["coach_b"], exercise_id=ex_shared.id, role="follower"),
        ])

        # A plan already on coach A's instance, so "other coach is rejected" can
        # assert the existing plan survives rather than only that nothing new
        # appeared.
        db.session.add(LessonInstanceTraining(
            lesson_instance_id=world["instance"], exercise_id=ex_a.id))

        # A class of coach B's own: needed to isolate the exercise-ownership
        # check from the class-ownership check.
        start_b = datetime.utcnow() + timedelta(days=4)
        lesson_b = Lesson(
            title="B's Class", type="academy", max_players=4, club_id=club_id,
            start_datetime=start_b, end_datetime=start_b + timedelta(hours=1),
        )
        db.session.add(lesson_b)
        db.session.flush()
        db.session.add(
            Association_CoachLesson(coach_id=world["coach_b"], lesson_id=lesson_b.id))

        instance_b = LessonInstance(
            lesson_id=lesson_b.id, max_players=4,
            start_datetime=start_b, end_datetime=start_b + timedelta(hours=1),
            original_lesson_occurence_date=start_b.date(),
        )
        db.session.add(instance_b)
        db.session.flush()
        db.session.add(Association_CoachLessonInstance(
            coach_id=world["coach_b"], lesson_instance_id=instance_b.id))

        # A recurring lesson of coach A's with NO instance row for `rec_date`.
        # Posting the Lesson-shaped body against it is what would lazily
        # materialize an instance, so it pins that a rejection materializes
        # nothing (RULES.md #1).
        start_rec = datetime.utcnow() + timedelta(days=5)
        lesson_rec = Lesson(
            title="A's Recurring Class", type="academy", max_players=4,
            club_id=club_id, start_datetime=start_rec,
            end_datetime=start_rec + timedelta(hours=1),
            is_recurring=True, recurrence_rule="FREQ=WEEKLY",
        )
        db.session.add(lesson_rec)
        db.session.flush()
        db.session.add(Association_CoachLesson(
            coach_id=world["coach_a"], lesson_id=lesson_rec.id))

        db.session.commit()

        return {
            **world,
            "ex_a": ex_a.id,
            "ex_b": ex_b.id,
            "ex_shared": ex_shared.id,
            "lesson_b": lesson_b.id,
            "instance_b": instance_b.id,
            "lesson_rec": lesson_rec.id,
            "rec_date": (start_rec + timedelta(days=7)).date().isoformat(),
        }


def _instance_body(w, instance_key, exercise_ids, lesson_key="lesson"):
    """Body shape the calendar sends for an already-materialized occurrence."""
    return {
        "classInstance": {
            "model": "LessonInstance",
            "originalId": str(w[instance_key]),
            "parentClassId": str(w[lesson_key]),
        },
        "exerciseIds": [str(e) for e in exercise_ids],
    }


def _planned_exercise_ids(app, instance_id):
    from padel_app.models.lesson_instance_training import LessonInstanceTraining

    with app.app_context():
        rows = LessonInstanceTraining.query.filter_by(
            lesson_instance_id=instance_id).all()
        return sorted(r.exercise_id for r in rows)


def test_confirm_training_rejects_anonymous(app, client, training_world):
    w = training_world
    res = client.post("/api/app/class_instance/training/confirm",
                      json=_instance_body(w, "instance", [w["ex_a"]]))

    assert res.status_code == 401
    assert _planned_exercise_ids(app, w["instance"]) == [w["ex_a"]]


def test_confirm_training_rejects_student(app, client, training_world):
    """A user with a player profile and no coach profile: 403, not 500."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", [w["ex_a"]]),
        headers=_auth_header(app, w["player_user"]),
    )

    assert res.status_code == 403
    assert _planned_exercise_ids(app, w["instance"]) == [w["ex_a"]]


def test_confirm_training_rejects_other_coach_and_writes_nothing(
    app, client, training_world
):
    """The IDOR itself: coach B planning training onto coach A's class."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", [w["ex_b"]]),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 403
    # Coach A's own plan is intact — not replaced, not emptied. The pre-fix code
    # deleted every row for the instance before inserting, so a successful
    # attack wiped A's plan even when it wrote nothing of its own.
    assert _planned_exercise_ids(app, w["instance"]) == [w["ex_a"]]


def test_confirm_training_rejects_exercise_the_coach_cannot_access(
    app, client, training_world
):
    """Own class, but an exercise coach B neither owns nor follows."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance_b", [w["ex_a"]], lesson_key="lesson_b"),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 403
    assert _planned_exercise_ids(app, w["instance_b"]) == []


def test_confirm_training_rejects_the_whole_request_atomically(
    app, client, training_world
):
    """One inaccessible id poisons the request — no partial plan is written."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(
            w, "instance_b", [w["ex_shared"], w["ex_a"]], lesson_key="lesson_b"),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 403
    assert _planned_exercise_ids(app, w["instance_b"]) == []


def test_confirm_training_allows_a_followed_exercise(app, client, training_world):
    """`follower` is read access (training.exercises rule 7), so B may plan it."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance_b", [w["ex_shared"]], lesson_key="lesson_b"),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 200
    assert _planned_exercise_ids(app, w["instance_b"]) == [w["ex_shared"]]


def test_confirm_training_allows_an_exercise_owned_without_an_association(
    app, client, training_world
):
    """`owner_coach_id` with no association row — the admin-CRUD / legacy shape.

    `create_exercise_service` always writes the owner association, so the two
    agree for anything made through the app. The generic admin CRUD does not,
    and 403-ing a coach on their own drill would be a regression this guard has
    no business causing.
    """
    from padel_app.models import Exercise

    w = training_world
    with app.app_context():
        orphan = Exercise(name="Admin-made drill", type="volley", difficulty=2,
                          owner_coach_id=w["coach_b"])
        db.session.add(orphan)
        db.session.commit()
        orphan_id = orphan.id

    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance_b", [orphan_id], lesson_key="lesson_b"),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 200
    assert _planned_exercise_ids(app, w["instance_b"]) == [orphan_id]


def test_confirm_training_rejects_a_foreign_orphan_exercise(
    app, client, training_world
):
    """The same shape, but owned by the *other* coach: still 403."""
    from padel_app.models import Exercise

    w = training_world
    with app.app_context():
        orphan = Exercise(name="A's admin-made drill", type="volley", difficulty=2,
                          owner_coach_id=w["coach_a"])
        db.session.add(orphan)
        db.session.commit()
        orphan_id = orphan.id

    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance_b", [orphan_id], lesson_key="lesson_b"),
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 403
    assert _planned_exercise_ids(app, w["instance_b"]) == []


def test_confirm_training_allows_clearing_the_plan(app, client, training_world):
    """An empty selection is a legitimate save, not an authorization failure."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", []),
        headers=_auth_header(app, w["user_a"]),
    )

    assert res.status_code == 200
    assert _planned_exercise_ids(app, w["instance"]) == []


def test_confirm_training_collapses_duplicate_exercise_ids(
    app, client, training_world
):
    """A repeated id used to hit the composite PK as a duplicate insert (500)."""
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", [w["ex_a"], w["ex_a"]]),
        headers=_auth_header(app, w["user_a"]),
    )

    assert res.status_code == 200
    assert _planned_exercise_ids(app, w["instance"]) == [w["ex_a"]]


def test_confirm_training_rejects_a_malformed_body(app, client, training_world):
    """A missing/garbage payload is a 400, never an unhandled 500."""
    w = training_world
    headers = _auth_header(app, w["user_a"])

    no_class = client.post("/api/app/class_instance/training/confirm",
                           json={"exerciseIds": []}, headers=headers)
    assert no_class.status_code == 400

    bad_ids = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", ["not-an-id"]),
        headers=headers,
    )
    assert bad_ids.status_code == 400


def test_confirm_training_allows_the_owning_coach(app, client, training_world):
    w = training_world
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=_instance_body(w, "instance", [w["ex_a"], w["ex_shared"]]),
        headers=_auth_header(app, w["user_a"]),
    )

    assert res.status_code == 200
    assert res.get_json()["plannedExerciseIds"] is not None
    assert _planned_exercise_ids(app, w["instance"]) == sorted(
        [w["ex_a"], w["ex_shared"]])


def test_confirm_training_rejection_does_not_materialize_an_instance(
    app, client, training_world
):
    """The Lesson-shaped body materializes on demand — a 403 must not.

    Without the guard, `confirm_training_service` calls
    `get_or_materialize_instance()` before anything else, so a rejected caller
    would still leave a LessonInstance row behind for another coach's lesson.
    """
    from padel_app.models import LessonInstance

    w = training_world
    body = {
        "classInstance": {
            "model": "Lesson",
            "originalId": str(w["lesson_rec"]),
            "date": w["rec_date"],
        },
        "exerciseIds": [str(w["ex_b"])],
    }
    res = client.post(
        "/api/app/class_instance/training/confirm",
        json=body,
        headers=_auth_header(app, w["user_b"]),
    )

    assert res.status_code == 403
    with app.app_context():
        assert LessonInstance.query.filter_by(lesson_id=w["lesson_rec"]).count() == 0
