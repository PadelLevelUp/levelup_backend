"""Tests for the /api/app/check_field_available endpoint.

Covers PAD-7 (unique username/email) and PAD-17 (warn-only, case-insensitive
duplicate player name check — scoped to the requesting coach's own roster).
"""
from padel_app.sql_db import db


def _make_user(app, name, username, email=None):
    from padel_app.models import User

    with app.app_context():
        user = User(name=name, username=username, email=email, password="secret")
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_coach(app, username):
    """Create a bare User+Coach and return the coach id."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach

    with app.app_context():
        user = User(name=f"Coach {username}", username=username, password="secret")
        db.session.add(user)
        db.session.flush()
        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.commit()
        return coach.id


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
    coach_id = _make_coach(app, "coach_own")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "JOHN DOE",
            "scope": coach_id,
        },
    )
    assert res.status_code == 409
    body = res.get_json()
    assert body["available"] is False
    assert body["message"]


def test_name_duplicate_in_other_coach_roster_does_not_warn(app, client):
    """PAD-17 fix: a same-name player on a DIFFERENT coach's roster must NOT warn
    the requesting coach (no cross-club false positive)."""
    requesting_coach = _make_coach(app, "coach_a")
    other_coach = _make_coach(app, "coach_b")
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
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_name_unique_value_is_available(app, client):
    """PAD-17: a genuinely new name is available on the coach's roster."""
    coach_id = _make_coach(app, "coach_unique")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={
            "model": "user",
            "field": "name",
            "value": "Jane Smith",
            "scope": coach_id,
        },
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_name_without_scope_skips_warn(app, client):
    """PAD-17 fix: with no coach scope we cannot tell whose roster to check, so we
    skip the warn rather than emit a false global duplicate."""
    coach_id = _make_coach(app, "coach_noscope")
    _make_roster_player(app, coach_id, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "name", "value": "John Doe"},
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_username_uniqueness_still_exact_and_global(app, client):
    """PAD-7: username uniqueness check still returns 409 for an exact match,
    globally (unaffected by the name-scoping change)."""
    _make_user(app, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "username", "value": "johnd"},
    )
    assert res.status_code == 409
    assert res.get_json()["available"] is False


def test_email_uniqueness_still_exact_and_global(app, client):
    """PAD-7: email uniqueness check still returns 409 for an exact match."""
    _make_user(app, name="John Doe", username="johnd", email="john@example.com")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "email", "value": "john@example.com"},
    )
    assert res.status_code == 409
    assert res.get_json()["available"] is False


def test_disallowed_field_is_rejected(app, client):
    """A non-whitelisted (model, field) pair is rejected with 400."""
    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "phone", "value": "12345"},
    )
    assert res.status_code == 400
