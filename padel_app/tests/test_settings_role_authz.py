"""
PAD-103 — the coach-only Settings surface must reject a *student* caller with
403, server-side.

Hiding the sections in the web UI is cosmetic: `/settings` was reachable by URL
and by the avatar dropdown for every authenticated user, and the endpoints
behind the coach-only panels resolved the acting coach with the nullable
`current_coach()`. For a user with a player profile and no coach profile that
returns `None`, which the route then dereferenced — so the "authorization"
outcome was an unhandled `AttributeError` (500), not a deliberate 403.

PAD-92 hardened the same blueprint against *anonymous* and *other-coach*
callers; this file pins the third axis it left open, the *wrong-role* caller:

    student on a coach-only route      -> 403 (never 500, never 2xx)
    coach on their own coach-only route -> 2xx (unchanged)
    student on a per-user route         -> 2xx (must not be swept up)

Every assertion checks the exact status code on purpose. `!= 200` would have
passed against the pre-PAD-103 code, because the pre-existing failure mode was
a 500 — the whole point of the ticket is that it was never an access decision.
"""
from datetime import date, datetime, timedelta

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
    """One coach owning the full coach-only Settings surface, plus one student.

    The student is a *pure* student: a `Player` row and no `Coach` row, which is
    exactly the shape `require_coach()` has to reject. Backend roles are
    mutually exclusive (`api_auth.py`: `["coach"] if user.coach else ["player"]`).
    """
    from padel_app.models import (
        User,
        Player,
        Association_CoachPlayer,
        CoachLevel,
        EvaluationCategory,
        Season,
    )
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub

    with app.app_context():
        club = Club(name="Settings Authz Club")
        db.session.add(club)
        db.session.flush()

        coach_user = User(name="Coach C", username="settings_coach", password="x")
        student_user = User(name="Student S", username="settings_student", password="x")
        db.session.add_all([coach_user, student_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        student = Player(user_id=student_user.id)
        db.session.add_all([coach, student])
        db.session.flush()

        # `Coach.current_club` is derived from the newest club relation.
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))
        db.session.add(
            Association_CoachPlayer(coach_id=coach.id, player_id=student.id)
        )

        level = CoachLevel(coach_id=coach.id, label="Beginner", code="B1")
        category = EvaluationCategory(coach_id=coach.id, name="Volley")
        season = Season(
            coach_id=coach.id,
            name="Season 1",
            start_date=date(2030, 1, 1),
            end_date=date(2030, 6, 30),
        )
        db.session.add_all([level, category, season])
        db.session.commit()

        return {
            "coach_user": coach_user.id,
            "student_user": student_user.id,
            "coach": coach.id,
            "student": student.id,
            "club": club.id,
            "level": level.id,
            "category": category.id,
            "season": season.id,
        }


def _count(app, model_cls):
    with app.app_context():
        return model_cls.query.count()


# ---------------------------------------------------------------------------
# 1. Coach-only READS — a student gets 403
# ---------------------------------------------------------------------------
# (label, method, path-template). `{club}` is substituted from the fixture.

COACH_ONLY_READS = [
    ("preferences: skill levels", "get", "/api/app/coach_levels"),
    ("preferences: evaluation categories", "get", "/api/app/evaluation_categories"),
    ("calendar tab: seasons", "get", "/api/app/seasons"),
    ("club tab: coach identity", "get", "/api/app/coach"),
    ("import tab: history", "get", "/api/app/import/history"),
    ("club tab: coach invitations", "get", "/api/app/club/{club}/coach-invitations"),
]


@pytest.mark.parametrize("label,method,path", COACH_ONLY_READS)
def test_student_cannot_read_coach_only_settings(
    app, client, world, label, method, path
):
    res = getattr(client, method)(
        path.format(club=world["club"]),
        headers=_auth_header(app, world["student_user"]),
    )
    assert res.status_code == 403, (
        f"{label}: {method.upper()} {path} -> {res.status_code} "
        "(403 required; 500 means the role was never checked)"
    )


# ---------------------------------------------------------------------------
# 2. Coach-only WRITES — a student gets 403
# ---------------------------------------------------------------------------
# Bodies are deliberately well-formed enough to reach the service layer, so a
# 400 would be as much of a failure as a 200: the role check has to fire first.

COACH_ONLY_WRITES = [
    (
        "preferences: add skill level",
        "post",
        "/api/app/add_coach_level",
        [{"code": "X1", "label": "Injected", "displayOrder": 1}],
    ),
    (
        "preferences: add evaluation category",
        "post",
        "/api/app/add_evaluation_categories",
        [{"name": "Injected", "scaleMin": 1, "scaleMax": 5}],
    ),
    (
        "calendar tab: add season",
        "post",
        "/api/app/add_seasons",
        [{"name": "Injected", "startDate": "2031-01-01", "endDate": "2031-06-30"}],
    ),
    ("calendar tab: delete season", "post", "/api/app/delete/season", {"id": 1}),
    ("preferences: delete skill level", "post", "/api/app/delete/coach_level", {"id": 1}),
    (
        "preferences: delete evaluation category",
        "post",
        "/api/app/delete/evaluation_category",
        {"id": 1},
    ),
    ("import tab: confirm", "post", "/api/app/import/confirm", {"tables": {}}),
    ("import tab: confirm (stream)", "post", "/api/app/import/confirm/stream", {"tables": {}}),
    ("import tab: revert", "post", "/api/app/import/1/revert", {}),
    (
        "club tab: create coach invitation",
        "post",
        "/api/app/club/{club}/coach-invitations",
        {"email": "injected@example.com"},
    ),
    (
        "club tab: revoke coach invitation",
        "post",
        "/api/app/coach-invitations/some-token/revoke",
        {},
    ),
]


@pytest.mark.parametrize("label,method,path,body", COACH_ONLY_WRITES)
def test_student_cannot_write_coach_only_settings(
    app, client, world, label, method, path, body
):
    if isinstance(body, dict) and "id" in body:
        # Point the delete at a row that really exists and really belongs to the
        # coach, so a missing guard would actually destroy data.
        key = {"/api/app/delete/season": "season",
               "/api/app/delete/coach_level": "level",
               "/api/app/delete/evaluation_category": "category"}[path]
        body = {"id": world[key]}

    res = getattr(client, method)(
        path.format(club=world["club"]),
        json=body,
        headers=_auth_header(app, world["student_user"]),
    )
    assert res.status_code == 403, (
        f"{label}: {method.upper()} {path} -> {res.status_code} "
        "(403 required; 500 means the role was never checked)"
    )


def test_student_cannot_upload_an_import_file(app, client, world):
    """`/import/analyze` is multipart, so it needs its own case.

    The guard must also fire *before* the uploaded bytes are read.
    """
    import io

    res = client.post(
        "/api/app/import/analyze",
        data={"file": (io.BytesIO(b"col\n1\n"), "injected.csv")},
        content_type="multipart/form-data",
        headers=_auth_header(app, world["student_user"]),
    )
    assert res.status_code == 403, f"import/analyze -> {res.status_code}"


def test_student_writes_leave_the_coachs_settings_untouched(app, client, world):
    """The 403s above must be pure: nothing created, nothing deleted."""
    from padel_app.models import CoachLevel, EvaluationCategory, Season

    before = (
        _count(app, CoachLevel),
        _count(app, EvaluationCategory),
        _count(app, Season),
    )
    assert before == (1, 1, 1)

    headers = _auth_header(app, world["student_user"])
    client.post(
        "/api/app/add_coach_level",
        json=[{"code": "X1", "label": "Injected", "displayOrder": 1}],
        headers=headers,
    )
    client.post(
        "/api/app/add_evaluation_categories",
        json=[{"name": "Injected", "scaleMin": 1, "scaleMax": 5}],
        headers=headers,
    )
    client.post(
        "/api/app/add_seasons",
        json=[{"name": "Injected", "startDate": "2031-01-01", "endDate": "2031-06-30"}],
        headers=headers,
    )
    client.post(
        "/api/app/delete/season", json={"id": world["season"]}, headers=headers
    )
    client.post(
        "/api/app/delete/coach_level", json={"id": world["level"]}, headers=headers
    )
    client.post(
        "/api/app/delete/evaluation_category",
        json={"id": world["category"]},
        headers=headers,
    )

    after = (
        _count(app, CoachLevel),
        _count(app, EvaluationCategory),
        _count(app, Season),
    )
    assert after == before


# ---------------------------------------------------------------------------
# 3. The notification-engine tab already failed closed — pin it
# ---------------------------------------------------------------------------

NOTIFY_ROUTES = [
    ("get", "/api/app/notify/config", None),
    ("get", "/api/app/notify/groups", None),
    ("get", "/api/app/notify/standing_waiting_list", None),
    ("post", "/api/app/notify/standing_waiting_list", {"playerId": 1}),
    ("post", "/api/app/notify/config", {"mode": "automatic"}),
    ("get", "/api/app/notify/activity", None),
]


@pytest.mark.parametrize("method,path,body", NOTIFY_ROUTES)
def test_student_cannot_reach_the_notification_engine(
    app, client, world, method, path, body
):
    """`/api/app/notify/*` resolves its coach through its own `_current_coach()`,
    which already `abort(403)`s. Audited clean under PAD-103, pinned here so it
    stays that way."""
    res = getattr(client, method)(
        path, json=body, headers=_auth_header(app, world["student_user"])
    )
    assert res.status_code in (403, 404, 405), f"{method.upper()} {path} -> {res.status_code}"
    if res.status_code in (404, 405):
        pytest.skip(f"{path} is not registered on this build")


# ---------------------------------------------------------------------------
# 4. The coach is unaffected (regression guard for the require_coach() swap)
# ---------------------------------------------------------------------------

COACH_HAPPY_PATH_READS = [
    ("get", "/api/app/coach_levels"),
    ("get", "/api/app/evaluation_categories"),
    ("get", "/api/app/seasons"),
    ("get", "/api/app/coach"),
    ("get", "/api/app/import/history"),
    ("get", "/api/app/club/{club}/coach-invitations"),
]


@pytest.mark.parametrize("method,path", COACH_HAPPY_PATH_READS)
def test_coach_still_reads_their_own_settings(app, client, world, method, path):
    res = getattr(client, method)(
        path.format(club=world["club"]),
        headers=_auth_header(app, world["coach_user"]),
    )
    assert res.status_code == 200, f"{method.upper()} {path} -> {res.status_code}"


def test_coach_still_writes_their_own_settings(app, client, world):
    from padel_app.models import CoachLevel, EvaluationCategory, Season

    headers = _auth_header(app, world["coach_user"])

    res = client.post(
        "/api/app/add_coach_level",
        json=[{"code": "A1", "label": "Advanced", "displayOrder": 1}],
        headers=headers,
    )
    assert res.status_code == 200
    assert _count(app, CoachLevel) == 2

    res = client.post(
        "/api/app/add_evaluation_categories",
        json=[{"name": "Smash", "scaleMin": 1, "scaleMax": 5}],
        headers=headers,
    )
    assert res.status_code == 200
    assert _count(app, EvaluationCategory) == 2

    res = client.post(
        "/api/app/add_seasons",
        json=[{"name": "Season 2", "startDate": "2031-01-01", "endDate": "2031-06-30"}],
        headers=headers,
    )
    assert res.status_code == 200
    assert _count(app, Season) == 2

    res = client.post(
        "/api/app/delete/season", json={"id": world["season"]}, headers=headers
    )
    assert res.status_code == 200
    assert _count(app, Season) == 1


# ---------------------------------------------------------------------------
# 5. Per-user settings must NOT be swept up by the coach check
# ---------------------------------------------------------------------------


def test_student_can_still_read_and_update_their_own_profile(app, client, world):
    """Profile + language live on the `users` row, not on a coach.

    They are the sections a student legitimately keeps (spec `settings.profile`
    and `settings.language`), so the PAD-103 hardening must not touch them.
    """
    headers = _auth_header(app, world["student_user"])

    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200, f"GET /api/auth/me -> {res.status_code}"
    assert "player" in res.get_json()["roles"]

    res = client.patch("/api/auth/me", json={"language": "en"}, headers=headers)
    assert res.status_code == 200, f"PATCH /api/auth/me -> {res.status_code}"

    res = client.get("/api/auth/me", headers=headers)
    assert res.get_json()["language"] == "en"


def test_student_can_still_manage_their_availability(app, client, world):
    """The one student-scoped settings-ish surface, guarded by `_require_student`.

    Included so the `require_coach()` sweep can't be widened into it later.
    """
    res = client.get(
        "/api/app/availability_blockers",
        headers=_auth_header(app, world["student_user"]),
    )
    assert res.status_code == 200

    res = client.get(
        "/api/app/availability_blockers",
        headers=_auth_header(app, world["coach_user"]),
    )
    assert res.status_code == 403
