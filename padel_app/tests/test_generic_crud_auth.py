"""
PAD-88 — the generic model-CRUD blueprint (`padel_app/modules/api.py`) must not
be reachable without administrator credentials.

Before PAD-88 the blueprint carried no `before_request` guard and no
`jwt_required` on any route, so an anonymous caller could create, edit, delete,
dump and CSV-export every entry of `padel_app.models.MODELS`.

These tests pin the contract:
  * no credentials                -> 401, and nothing is written;
  * valid credentials, not admin  -> 403, and nothing is written;
  * admin (JWT or Flask-Login session) -> the guard lets the request through.
"""
import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db
from padel_app.tests.helpers import make_coach


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _make_user(app, username, *, is_admin=False, is_superadmin=False):
    from padel_app.models import User

    with app.app_context():
        user = User(
            name=username,
            username=username,
            password="x",
            is_admin=is_admin,
            is_superadmin=is_superadmin,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def _season_count(app):
    from padel_app.models.seasons import Season

    with app.app_context():
        return Season.query.count()


# Every route the blueprint exposes, as (method, path). None of them may be
# served to an anonymous caller.
ALL_ROUTES = [
    ("post", "/api/create/season"),
    ("post", "/api/edit/season/1"),
    ("get", "/api/delete/season/1"),
    ("post", "/api/delete/season/1"),
    ("get", "/api/query/user"),
    ("post", "/api/query/user"),
    ("get", "/api/remove_relationship"),
    ("post", "/api/remove_relationship"),
    ("get", "/api/modal_create_page/season"),
    ("post", "/api/modal_create_page/season"),
    ("get", "/api/download_csv/user"),
    ("post", "/api/download_csv/user"),
    ("get", "/api/upload_csv_to_db/user"),
    ("post", "/api/upload_csv_to_db/user"),
    ("get", "/api/image/1"),
]


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_every_generic_crud_route_rejects_anonymous_callers(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401, (
        f"{method.upper()} {path} returned {response.status_code}, expected 401"
    )


def test_anonymous_create_is_rejected_and_writes_nothing(app, client):
    coach_id = make_coach(app)
    before = _season_count(app)

    response = client.post(
        "/api/create/season",
        json={
            "values": {
                "coach_id": coach_id,
                "name": "Bypass A",
                "start_date": "2026-03-01",
                "end_date": "2026-05-31",
            }
        },
    )

    assert response.status_code == 401
    assert _season_count(app) == before


def test_anonymous_edit_is_rejected_and_writes_nothing(app, client):
    from padel_app.models.seasons import Season
    from datetime import date

    coach_id = make_coach(app)
    with app.app_context():
        season = Season(
            coach_id=coach_id,
            name="Legit",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 5, 31),
        )
        db.session.add(season)
        db.session.commit()
        season_id = season.id

    response = client.post(
        f"/api/edit/season/{season_id}",
        json={"values": {"name": "Tampered"}},
    )

    assert response.status_code == 401
    with app.app_context():
        assert db.session.get(Season, season_id).name == "Legit"


def test_anonymous_query_does_not_leak_rows(app, client):
    _make_user(app, "leak_probe")

    response = client.get("/api/query/user")

    assert response.status_code == 401
    assert b"leak_probe" not in response.data


def test_authenticated_non_admin_is_forbidden(app, client):
    """A valid login is not enough — the caller must be an administrator."""
    coach_id = make_coach(app)
    user_id = _make_user(app, "plain_user")
    headers = _auth_header(app, user_id)
    before = _season_count(app)

    create = client.post(
        "/api/create/season",
        json={
            "values": {
                "coach_id": coach_id,
                "name": "Bypass B",
                "start_date": "2026-03-01",
                "end_date": "2026-05-31",
            }
        },
        headers=headers,
    )
    assert create.status_code == 403
    assert _season_count(app) == before

    assert client.get("/api/query/user", headers=headers).status_code == 403


def test_admin_jwt_passes_the_guard(app, client):
    from padel_app.models.clubs import Club

    admin_id = _make_user(app, "admin_user", is_admin=True)

    response = client.post(
        "/api/create/club",
        json={
            "values": {
                "name": "Admin created club",
                "description": "created through the generic editor API",
                "location": "Lisbon",
            }
        },
        headers=_auth_header(app, admin_id),
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    with app.app_context():
        assert Club.query.filter_by(name="Admin created club").count() == 1


def test_superadmin_jwt_passes_the_guard(app, client):
    admin_id = _make_user(app, "super_user", is_superadmin=True)

    response = client.get("/api/query/user", headers=_auth_header(app, admin_id))

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Flask-Login session path (the legacy Jinja editor at /editor)
# ---------------------------------------------------------------------------
# The shared `app` fixture builds an app with no SECRET_KEY and no configured
# server-side session backend, so cookie sessions cannot be opened there. These
# two tests therefore build their own app with sessions enabled.


@pytest.fixture
def session_app():
    import os
    import tempfile

    from padel_app import create_app
    from padel_app.sql_db import init_db

    db_fd, db_path = tempfile.mkstemp()
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SECRET_KEY": "test-secret-key",
            "SESSION_TYPE": "filesystem",
        }
    )
    with app.app_context():
        init_db(app)
        db.create_all()

    yield app

    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture
def session_client(session_app):
    return session_app.test_client()


def _login_session(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_admin_flask_login_session_passes_the_guard(session_app, session_client):
    """The legacy Jinja editor authenticates with a session, not a JWT."""
    admin_id = _make_user(session_app, "session_admin", is_admin=True)
    _login_session(session_client, admin_id)

    assert session_client.get("/api/query/user").status_code == 200


def test_non_admin_flask_login_session_is_forbidden(session_app, session_client):
    user_id = _make_user(session_app, "session_user")
    _login_session(session_client, user_id)

    assert session_client.get("/api/query/user").status_code == 403
