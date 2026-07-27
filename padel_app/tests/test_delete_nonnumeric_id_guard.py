"""
PAD-101 — the delete endpoints must reject a non-numeric id with a clean 400
instead of letting ``int(...)`` raise and bubble up as a 500.

A freshly-added, not-yet-persisted row in the coach-levels / strengths-weaknesses
editors carries a temporary string id of the form ``new-<Date.now()>``. If the
user deletes that row before a refetch re-keys it with the real numeric id, the
client posts the temp string id to the delete endpoint. This guard turns that
into a well-formed 400.

Batch note: PAD-92 landed alongside this and put ``@jwt_required()`` on these
routes, routing the id through the shared ``_required_int_id`` helper — which
subsumes PAD-101's inline guard. The contract PAD-101 pins is unchanged (bad id
-> 400, never 500), but it can now only be observed by an authenticated caller;
an anonymous request is rejected at the auth layer with 401 before the id is
ever parsed. These tests therefore authenticate first.

Run:
    pytest padel_app/tests/test_delete_nonnumeric_id_guard.py -v
"""
import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


@pytest.fixture
def coach_headers(app):
    """An authenticated coach — the only caller that reaches the id guard."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach

    with app.app_context():
        user = User(
            name="PAD-101 Guard Coach",
            username="pad101_guard_coach",
            password="testpass123",
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Coach(user_id=user.id))
        db.session.commit()
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _assert_bad_id(resp):
    """A malformed id is a 400 — emphatically not a 500 from a bare int().

    PAD-92 raises this through ``abort(400, ...)``, which renders Flask's HTML
    error page rather than the JSON envelope PAD-101 originally returned, so the
    reason is asserted against the response text. The status code is the part of
    the contract clients actually depend on.
    """
    assert resp.status_code == 400, resp.get_data(as_text=True)
    text = resp.get_data(as_text=True).lower()
    assert "integer" in text or "numeric" in text, text


def test_delete_coach_level_rejects_nonnumeric_id(client, coach_headers):
    resp = client.post(
        "/api/app/delete/coach_level",
        json={"id": "new-1753900000000"},
        headers=coach_headers,
    )
    _assert_bad_id(resp)


def test_delete_coach_note_rejects_nonnumeric_id(client, coach_headers):
    resp = client.post(
        "/api/app/delete/coach_note",
        json={"id": "new-1753900000000"},
        headers=coach_headers,
    )
    _assert_bad_id(resp)


def test_delete_coach_level_missing_id_rejected(client, coach_headers):
    # A missing id (None) is likewise unusable and must not 500.
    resp = client.post("/api/app/delete/coach_level", json={}, headers=coach_headers)
    assert resp.status_code == 400, resp.get_data(as_text=True)


def test_delete_coach_level_numeric_id_passes_guard(client, coach_headers):
    # A well-formed numeric id clears the guard; with no such row seeded it
    # resolves to a 404 (first_or_404), NOT a 400 and NOT a 500.
    resp = client.post(
        "/api/app/delete/coach_level", json={"id": 999999}, headers=coach_headers
    )
    assert resp.status_code == 404, resp.get_data(as_text=True)


def test_delete_coach_level_anonymous_is_rejected(client):
    # PAD-92's contract: anonymous callers never reach the id guard at all.
    resp = client.post(
        "/api/app/delete/coach_level", json={"id": "new-1753900000000"}
    )
    assert resp.status_code == 401, resp.get_data(as_text=True)
