"""
Regression tests for two 500s found during an automated E2E test-health sweep:

1. `GET /api/app/exercises` and `GET /api/app/exercise-groups` threw
   `AttributeError: 'NoneType' object has no attribute 'id'` for any
   authenticated caller who is not a coach (e.g. a student), because
   `training_service.py` called `.filter_by(coach_id=coach.id)` without
   checking whether `coach` was `None`.

2. `GET /api/editor/tokenblocklist/schema` threw `AttributeError` because
   `TokenBlocklist` is a plain `db.Model` (no editor `Model` mixin) but was
   registered in `padel_app.models.MODELS`, so the generic editor's
   `model_cls().get_create_form()` call blew up.
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


def _make_student_user(app):
    from padel_app.models import User
    from padel_app.models.players import Player

    with app.app_context():
        user = User(name="Student", username="bug500_student", password="x")
        db.session.add(user)
        db.session.flush()

        player = Player(user_id=user.id)
        db.session.add(player)
        db.session.commit()
        return user.id


# ---------------------------------------------------------------------------
# Bug 1 — exercises / exercise-groups 500 for non-coach callers
# ---------------------------------------------------------------------------


def test_exercises_endpoint_does_not_500_for_student(app, client):
    student_user_id = _make_student_user(app)

    response = client.get(
        "/api/app/exercises", headers=_auth_header(app, student_user_id)
    )

    assert response.status_code == 403
    assert response.status_code != 500


def test_exercise_groups_endpoint_does_not_500_for_student(app, client):
    student_user_id = _make_student_user(app)

    response = client.get(
        "/api/app/exercise-groups", headers=_auth_header(app, student_user_id)
    )

    assert response.status_code == 403
    assert response.status_code != 500


def test_exercises_endpoint_still_works_for_coach(app, client):
    from padel_app.tests.helpers import make_coach
    from padel_app.models.coaches import Coach

    coach_id = make_coach(app)
    with app.app_context():
        coach_user_id = db.session.get(Coach, coach_id).user_id

    response = client.get(
        "/api/app/exercises", headers=_auth_header(app, coach_user_id)
    )

    assert response.status_code == 200
    assert response.get_json() == []


def test_exercise_groups_endpoint_still_works_for_coach(app, client):
    from padel_app.tests.helpers import make_coach
    from padel_app.models.coaches import Coach

    coach_id = make_coach(app)
    with app.app_context():
        coach_user_id = db.session.get(Coach, coach_id).user_id

    response = client.get(
        "/api/app/exercise-groups", headers=_auth_header(app, coach_user_id)
    )

    assert response.status_code == 200
    assert response.get_json() == []


def test_create_exercise_endpoint_does_not_500_for_student(app, client):
    """The write path (`create_exercise_service`) has the same `coach.id`
    dereference as the GET path — guard it the same way for consistency."""
    student_user_id = _make_student_user(app)

    response = client.post(
        "/api/app/exercises",
        json={"name": "Sneaky", "type": "drill"},
        headers=_auth_header(app, student_user_id),
    )

    assert response.status_code == 403
    assert response.status_code != 500


def test_create_exercise_group_endpoint_does_not_500_for_student(app, client):
    student_user_id = _make_student_user(app)

    response = client.post(
        "/api/app/exercise-groups",
        json={"name": "Sneaky Group"},
        headers=_auth_header(app, student_user_id),
    )

    assert response.status_code == 403
    assert response.status_code != 500


# ---------------------------------------------------------------------------
# Bug 2 — generic editor schema endpoint 500 for tokenblocklist
# ---------------------------------------------------------------------------


def _make_superadmin(app):
    from padel_app.models import User

    with app.app_context():
        user = User(
            name="Super Admin",
            username="bug500_superadmin",
            password="x",
            is_superadmin=True,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def test_tokenblocklist_excluded_from_generic_editor(app, client):
    """TokenBlocklist is internal auth plumbing and must not be admin-editable."""
    from padel_app.models import MODELS

    assert "tokenblocklist" not in MODELS


def test_tokenblocklist_schema_endpoint_returns_404_not_500(app, client):
    admin_id = _make_superadmin(app)

    response = client.get(
        "/api/editor/tokenblocklist/schema",
        headers=_auth_header(app, admin_id),
    )

    assert response.status_code == 404
    assert response.status_code != 500


def test_generic_editor_schema_still_works_for_registered_model(app, client):
    """Sanity check that the schema endpoint isn't broken for real editor models."""
    admin_id = _make_superadmin(app)

    response = client.get(
        "/api/editor/club/schema",
        headers=_auth_header(app, admin_id),
    )

    assert response.status_code == 200
