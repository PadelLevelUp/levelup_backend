"""Tests for the /api/app/check_field_available endpoint.

Covers PAD-7 (unique username/email) and PAD-17 (warn-only, case-insensitive
duplicate player name check).
"""
from padel_app.sql_db import db


def _make_user(app, name, username, email=None):
    from padel_app.models import User

    with app.app_context():
        user = User(name=name, username=username, email=email, password="secret")
        db.session.add(user)
        db.session.commit()
        return user.id


def test_name_duplicate_is_case_insensitive_and_warns(app, client):
    """PAD-17: a duplicate name returns 409 regardless of case."""
    _make_user(app, name="John Doe", username="johnd", email="john@example.com")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "name", "value": "JOHN DOE"},
    )
    assert res.status_code == 409
    body = res.get_json()
    assert body["available"] is False
    assert body["message"]


def test_name_unique_value_is_available(app, client):
    """PAD-17: a genuinely new name is available."""
    _make_user(app, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "name", "value": "Jane Smith"},
    )
    assert res.status_code == 200
    assert res.get_json()["available"] is True


def test_username_uniqueness_still_exact(app, client):
    """PAD-7: username uniqueness check still returns 409 for an exact match."""
    _make_user(app, name="John Doe", username="johnd")

    res = client.post(
        "/api/app/check_field_available",
        json={"model": "user", "field": "username", "value": "johnd"},
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
