"""Tests for the /api/app/check_field_available endpoint.

Covers PAD-7 (unique username/email) and PAD-17 (warn-only, case-insensitive
duplicate player name check — scoped to the requesting coach's own roster).

PAD-92: the endpoint is now `@jwt_required()` and the roster scope comes from
the caller's token rather than the request body — an anonymous caller could
previously enumerate any coach's roster by name.
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


def _make_user(app, name, username, email=None):
    from padel_app.models import User

    with app.app_context():
        user = User(name=name, username=username, email=email, password="secret")
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_coach(app, username):
    """Create a bare User+Coach and return (user_id, coach_id)."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach

    with app.app_context():
        user = User(name=f"Coach {username}", username=username, password="secret")
        db.session.add(user)
        db.session.flush()
        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.commit()
        return user.id, coach.id


def _make_roster_player(app, coach_id, name, username):
    """Create a User+Player and link it to ``coach_id`` via Association_CoachPlayer."""
    from padel_app.models import User, Player, Association_CoachPlayer

    with app.app_context():
        user = User(name=name, username=username, password="secret")
        db.session.add(user)
        db.session.flush()
        player = Player(user_id=user.id)
        db.session.add(player)
        db.session.flush()
        rel = Association_CoachPlayer(coach_id=coach_id, player_id=player.id)
        db.session.add(rel)
        db.session.commit()
        return player.id


def test_name_duplicate_in_own_roster_warns(app, client):
    """PAD-17: a duplicate name on the REQUESTING coach's roster returns 409,
    case-insensitively."""
    user_id, coach_id = _make_coach(app, "coach_own")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "JOHN DOE",
            "scope": coach_id,
        },
        headers=_auth_header(app, user_id),
    )
    assert res.status_code == 409
    body = res.get_json()
    assert body["available"] is False
    assert body["message"]


def test_name_duplicate_in_other_coach_roster_does_not_warn(app, client):
    """PAD-17 fix: a same-name player on a DIFFERENT coach's roster must NOT warn
    the requesting coach (no cross-club false positive)."""
    requesting_user, requesting_coach = _make_coach(app, "coach_a")
    _, other_coach = _make_coach(app, "coach_b")
    # The only "John Doe" belongs to the OTHER coach.
    _make_roster_player(app, other_coach, name="John Doe", username="johnd_other")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "John Doe",
            "scope": requesting_coach,
        },
        headers=_auth_header(app, requesting_user),
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_name_unique_value_is_available(app, client):
    """PAD-17: a genuinely new name is available on the coach's roster."""
    user_id, coach_id = _make_coach(app, "coach_unique")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "Jane Smith",
            "scope": coach_id,
        },
        headers=_auth_header(app, user_id),
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_name_without_body_scope_uses_the_callers_own_roster(app, client):
    """PAD-92: the scope no longer has to be supplied — it is the caller's own
    coach id, taken from the JWT. Omitting it therefore still checks (only) the
    caller's roster, so their own duplicate warns."""
    user_id, coach_id = _make_coach(app, "coach_noscope")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "name", "value": "John Doe"},
        headers=_auth_header(app, user_id),
    )
    assert res.status_code == 409
    assert res.get_json()["available"] is False


def test_name_scope_naming_another_coach_is_forbidden(app, client):
    """PAD-92: a body `scope` pointing at somebody else's roster is a 403, not a
    silent cross-roster probe."""
    attacker_user, _ = _make_coach(app, "coach_attacker")
    _, victim_coach = _make_coach(app, "coach_victim")
    _make_roster_player(app, victim_coach, name="John Doe", username="johnd_victim")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "John Doe",
            "scope": victim_coach,
        },
        headers=_auth_header(app, attacker_user),
    )
    assert res.status_code == 403


def test_anonymous_caller_is_rejected(app, client):
    """PAD-92: the endpoint used to be reachable with no credentials at all."""
    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "username", "value": "anything"},
    )
    assert res.status_code == 401


def test_username_uniqueness_still_exact_and_global(app, client):
    """PAD-7: username uniqueness check still returns 409 for an exact match,
    globally (unaffected by the name-scoping change)."""
    caller_id, _ = _make_coach(app, "coach_username_check")
    _make_user(app, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "username", "value": "johnd"},
        headers=_auth_header(app, caller_id),
    )
    assert res.status_code == 409
    assert res.get_json()["available"] is False


def test_email_uniqueness_still_exact_and_global(app, client):
    """PAD-7: email uniqueness check still returns 409 for an exact match."""
    caller_id, _ = _make_coach(app, "coach_email_check")
    _make_user(app, name="John Doe", username="johnd", email="john@example.com")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "email", "value": "john@example.com"},
        headers=_auth_header(app, caller_id),
    )
    assert res.status_code == 409
    assert res.get_json()["available"] is False


def test_disallowed_field_is_rejected(app, client):
    """A non-whitelisted (model, field) pair is rejected with 400."""
    caller_id, _ = _make_coach(app, "coach_disallowed_check")
    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "phone", "value": "12345"},
        headers=_auth_header(app, caller_id),
    )
    assert res.status_code == 400
