"""
PAD-116 — the coach-only Training and Players routes must reject a caller with
no coach profile with a deliberate 403, resolved on the route.

Follow-up to PAD-103, which converted the coach-only *Settings* endpoints from
the nullable ``current_coach()`` to ``require_coach()``. The Training and
Players routes were left out to keep that ticket scoped to Settings.

WHAT WAS ACTUALLY BROKEN — measured, not assumed. The ticket lists 13 routes and
says all of them 500. Probing them against ``main`` with a student token before
any change showed that is only half true:

    /exercises            GET/POST/PUT/DELETE  -> 403   (already correct)
    /exercises/<id>       GET                  -> 403   (already correct)
    /exercise-groups      GET/POST/PUT/DELETE  -> 403   (already correct)
    /players              GET                  -> 500   <-- real defect
    /coach_players        GET                  -> 500   <-- real defect
    /coach_players_paginated GET               -> 500   <-- real defect
    /player_profile/<id>  GET                  -> 500   <-- real defect

The nine Training routes already 403 because every service behind them opens
with its own ``if coach is None: abort(403, "Only a coach can ...")``. That
contract therefore holds only for as long as each service remembers to check —
the route itself hands a ``None`` coach straight through. The four Players
routes have no such service guard and dereference the ``None`` (``coach.id``,
and ``current_club()`` for ``/players``), raising an unhandled AttributeError.

So this file does two different jobs:
  * for the four Players routes it pins a real fix (500 -> 403);
  * for the nine Training routes it pins a contract that currently holds by
    luck, so a later service refactor cannot silently regress it into a 500.

Statuses are asserted with ``==``, never ``!= 200``: the failure mode being
fixed here IS a 500, so a loose assertion would pass against unfixed code.

Specs: players.list rule 7, players.profile rule 7, training.exercises rule 9,
training.groups rule 6.
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
def roles(app):
    """A coach with a club and a roster, and a student with neither."""
    from padel_app.models import (
        User,
        Player,
        Association_CoachPlayer,
        Exercise,
        ExerciseGroup,
    )
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachExercise import Association_CoachExercise
    from padel_app.models.Association_CoachExerciseGroup import (
        Association_CoachExerciseGroup,
    )

    with app.app_context():
        club = Club(name="Role Authz Club")
        db.session.add(club)

        coach_user = User(name="Role Coach", username="role_coach", password="x")
        # A user with a player profile and NO coach profile — the caller whose
        # current_coach() is None. This is the shape that 500'd.
        student_user = User(name="Role Student", username="role_student", password="x")
        db.session.add_all([coach_user, student_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        student_player = Player(user_id=student_user.id)
        db.session.add_all([coach, student_player])
        db.session.flush()

        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))
        coach.current_club_id = club.id

        # A player on the coach's roster, so the coach-side happy paths return
        # real data rather than passing vacuously on an empty roster.
        roster_user = User(name="Rostered", username="role_rostered", password="x")
        db.session.add(roster_user)
        db.session.flush()
        rostered = Player(user_id=roster_user.id)
        db.session.add(rostered)
        db.session.flush()
        db.session.add(
            Association_CoachPlayer(coach_id=coach.id, player_id=rostered.id)
        )

        exercise = Exercise(
            name="Role drill", type="attack", difficulty=3, owner_coach_id=coach.id
        )
        group = ExerciseGroup(name="Role group", owner_coach_id=coach.id)
        db.session.add_all([exercise, group])
        db.session.flush()
        db.session.add_all([
            Association_CoachExercise(
                coach_id=coach.id, exercise_id=exercise.id, role="owner"),
            Association_CoachExerciseGroup(
                coach_id=coach.id, exercise_group_id=group.id, role="owner"),
        ])

        db.session.commit()

        return {
            "coach_user": coach_user.id,
            "student_user": student_user.id,
            "coach": coach.id,
            "player": rostered.id,
            "exercise": exercise.id,
            "group": group.id,
        }


def _routes(r):
    """(label, method, path, body) for every route PAD-116 converts."""
    return [
        # --- Training: exercises ---
        ("exercises.list", "get", "/api/app/exercises", None),
        ("exercises.detail", "get", f"/api/app/exercises/{r['exercise']}", None),
        ("exercises.create", "post", "/api/app/exercises",
         {"name": "x", "type": "attack", "difficulty": 3}),
        ("exercises.update", "put", f"/api/app/exercises/{r['exercise']}",
         {"name": "y"}),
        ("exercises.delete", "delete", f"/api/app/exercises/{r['exercise']}", None),
        # --- Training: exercise groups ---
        ("groups.list", "get", "/api/app/exercise-groups", None),
        ("groups.create", "post", "/api/app/exercise-groups", {"name": "g"}),
        ("groups.update", "put", f"/api/app/exercise-groups/{r['group']}",
         {"name": "g2"}),
        ("groups.delete", "delete", f"/api/app/exercise-groups/{r['group']}", None),
        # --- Players ---
        ("players.list", "get", "/api/app/players", None),
        ("players.coach_players", "get", "/api/app/coach_players", None),
        ("players.paginated", "get", "/api/app/coach_players_paginated", None),
        ("players.profile", "get", f"/api/app/player_profile/{r['player']}", None),
    ]


def test_every_converted_route_rejects_a_student_with_403(app, client, roles):
    """The whole matrix in one assertion, so a miss names the offending route."""
    offenders = {}
    for label, method, path, body in _routes(roles):
        res = getattr(client, method)(
            path, json=body, headers=_auth_header(app, roles["student_user"])
        )
        if res.status_code != 403:
            offenders[label] = res.status_code

    assert offenders == {}, (
        f"expected exactly 403 for a student caller, got: {offenders}"
    )


def test_no_converted_route_500s_for_a_student(app, client, roles):
    """Stated separately from the 403 check because it is the regression that
    actually shipped: a 500 here is an unhandled AttributeError, not a decision.
    """
    five_hundreds = {}
    for label, method, path, body in _routes(roles):
        res = getattr(client, method)(
            path, json=body, headers=_auth_header(app, roles["student_user"])
        )
        if res.status_code >= 500:
            five_hundreds[label] = res.status_code

    assert five_hundreds == {}, f"server errors for a student caller: {five_hundreds}"


@pytest.mark.parametrize(
    "label",
    ["players.list", "players.coach_players", "players.paginated", "players.profile"],
)
def test_the_four_players_routes_are_the_real_fix(app, client, roles, label):
    """Pinned individually: these are the four that genuinely 500'd on main.

    Kept apart from the matrix so that if the Training half is ever reorganised
    the actual defect this ticket fixes still has dedicated coverage.
    """
    route = {r[0]: r for r in _routes(roles)}[label]
    _, method, path, body = route

    res = getattr(client, method)(
        path, json=body, headers=_auth_header(app, roles["student_user"])
    )
    assert res.status_code == 403


def test_anonymous_callers_are_still_rejected(app, client, roles):
    """The role check must not displace the auth check (PAD-92 contract)."""
    offenders = {}
    for label, method, path, body in _routes(roles):
        res = getattr(client, method)(path, json=body)
        if res.status_code != 401:
            offenders[label] = res.status_code

    assert offenders == {}, f"expected 401 without a JWT, got: {offenders}"


# ---------------------------------------------------------------------------
# Coach-side happy paths — the swap must not cost a coach any access.
# ---------------------------------------------------------------------------


def test_coach_still_reads_the_roster(app, client, roles):
    headers = _auth_header(app, roles["coach_user"])

    for path in (
        "/api/app/players",
        "/api/app/coach_players",
        "/api/app/coach_players_paginated",
        f"/api/app/player_profile/{roles['player']}",
    ):
        res = client.get(path, headers=headers)
        assert res.status_code == 200, f"{path} -> {res.status_code}"


def test_coach_still_reads_exercises_and_groups(app, client, roles):
    headers = _auth_header(app, roles["coach_user"])

    listing = client.get("/api/app/exercises", headers=headers)
    assert listing.status_code == 200
    assert [e["id"] for e in listing.get_json()] == [str(roles["exercise"])]

    detail = client.get(
        f"/api/app/exercises/{roles['exercise']}", headers=headers
    )
    assert detail.status_code == 200

    groups = client.get("/api/app/exercise-groups", headers=headers)
    assert groups.status_code == 200
    assert [g["id"] for g in groups.get_json()] == [str(roles["group"])]


def test_coach_still_writes_exercises_and_groups(app, client, roles):
    headers = _auth_header(app, roles["coach_user"])

    created = client.post(
        "/api/app/exercises",
        json={"name": "New drill", "type": "volley", "difficulty": 2},
        headers=headers,
    )
    assert created.status_code == 201

    updated = client.put(
        f"/api/app/exercises/{roles['exercise']}",
        json={"name": "Renamed drill"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "Renamed drill"

    new_group = client.post(
        "/api/app/exercise-groups", json={"name": "New group"}, headers=headers
    )
    assert new_group.status_code == 201

    removed = client.delete(
        f"/api/app/exercises/{roles['exercise']}", headers=headers
    )
    assert removed.status_code == 204


# ---------------------------------------------------------------------------
# The routes that must NOT be converted.
# ---------------------------------------------------------------------------
# These branch on `current_coach() is None` to serve students, so hardening them
# with require_coach() would lock students out of their own calendar. There is a
# comment listing them next to require_coach() in frontend_api.py; this test is
# the executable half of it, so "do not convert" survives the next sweep.


def test_student_facing_routes_are_not_hardened(app, client, roles):
    headers = _auth_header(app, roles["student_user"])

    for path in ("/api/app/calendar", "/api/app/dashboard", "/api/app/availability_blockers"):
        res = client.get(path, headers=headers)
        assert res.status_code != 403, (
            f"{path} must keep serving students, got 403 — it was hardened by mistake"
        )
        assert res.status_code < 500, f"{path} -> {res.status_code}"
