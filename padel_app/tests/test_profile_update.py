"""
PAD-81 — self-service profile editing.

`PATCH /api/auth/me` used to accept only `language`; every other profile field
the Settings screen presented (name, abbreviation, email, phone) was silently
dropped, which is why the UI could report a successful save that never
persisted. These tests pin the contract: partial updates, normalisation,
validation (400) and email conflicts (409).
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
def profile_user(app):
    from padel_app.models import User

    with app.app_context():
        user = User(
            name="Ana Beatriz Costa",
            username="profile_owner",
            email="ana@example.com",
            phone="555-0000",
            password="x",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        return user.id


@pytest.fixture
def other_user(app):
    from padel_app.models import User

    with app.app_context():
        user = User(
            name="Other Person",
            username="profile_other",
            email="taken@example.com",
            password="x",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def test_get_me_exposes_profile_fields(client, app, profile_user):
    resp = client.get("/api/auth/me", headers=_auth_header(app, profile_user))

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Ana Beatriz Costa"
    assert body["email"] == "ana@example.com"
    assert body["phone"] == "555-0000"
    # No stored abbreviation yet -> derived from the first two words of the name.
    assert body["abbreviation"] == "AB"


def test_patch_me_persists_all_profile_fields(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={
            "name": "Ana Costa",
            "abbreviation": "ac",
            "email": "New.Ana@Example.com",
            "phone": "+351912345678",
        },
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Ana Costa"
    assert body["abbreviation"] == "AC"  # uppercased
    assert body["email"] == "new.ana@example.com"  # trimmed + lowercased
    assert body["phone"] == "+351912345678"

    with app.app_context():
        user = User.query.get(profile_user)
        assert user.name == "Ana Costa"
        assert user.abbreviation == "AC"
        assert user.email == "new.ana@example.com"
        assert user.phone == "+351912345678"


def test_patch_me_is_partial(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"phone": "555-9999"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    with app.app_context():
        user = User.query.get(profile_user)
        assert user.phone == "555-9999"
        assert user.name == "Ana Beatriz Costa"  # untouched
        assert user.email == "ana@example.com"  # untouched


def test_patch_me_still_updates_language(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"language": "en"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    assert resp.get_json()["language"] == "en"
    with app.app_context():
        assert User.query.get(profile_user).language == "en"


def test_patch_me_rejects_unsupported_language(client, app, profile_user):
    resp = client.patch(
        "/api/auth/me",
        json={"language": "fr"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 400


def test_patch_me_rejects_blank_name(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"name": "   "},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 400
    with app.app_context():
        assert User.query.get(profile_user).name == "Ana Beatriz Costa"


def test_patch_me_rejects_invalid_email(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"email": "not-an-email"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 400
    with app.app_context():
        assert User.query.get(profile_user).email == "ana@example.com"


def test_patch_me_rejects_duplicate_email(client, app, profile_user, other_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"email": "taken@example.com"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 409
    with app.app_context():
        assert User.query.get(profile_user).email == "ana@example.com"


def test_patch_me_allows_resaving_own_email(client, app, profile_user):
    resp = client.patch(
        "/api/auth/me",
        json={"email": "ana@example.com", "name": "Ana B. Costa"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    assert resp.get_json()["email"] == "ana@example.com"


def test_patch_me_clears_optional_fields_with_empty_strings(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"email": "", "phone": "", "abbreviation": ""},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["email"] is None
    assert body["phone"] is None
    # Cleared abbreviation falls back to the derived initials.
    assert body["abbreviation"] == "AB"

    with app.app_context():
        user = User.query.get(profile_user)
        assert user.email is None
        assert user.phone is None
        assert user.abbreviation is None


def test_patch_me_truncates_long_abbreviation(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"abbreviation": "abcdefgh"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    assert resp.get_json()["abbreviation"] == "ABCD"
    with app.app_context():
        assert User.query.get(profile_user).abbreviation == "ABCD"


def test_patch_me_ignores_fields_outside_the_allowlist(client, app, profile_user):
    from padel_app.models import User

    resp = client.patch(
        "/api/auth/me",
        json={"is_superadmin": True, "username": "hacker", "name": "Ana C"},
        headers=_auth_header(app, profile_user),
    )

    assert resp.status_code == 200
    with app.app_context():
        user = User.query.get(profile_user)
        assert user.name == "Ana C"
        assert user.username == "profile_owner"
        assert user.is_superadmin is False
