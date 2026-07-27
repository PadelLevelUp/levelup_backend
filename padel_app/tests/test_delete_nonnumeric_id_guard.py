"""
PAD-101 — the delete endpoints must reject a non-numeric id with a clean 400
instead of letting ``int(...)`` raise and bubble up as a 500.

A freshly-added, not-yet-persisted row in the coach-levels / strengths-weaknesses
editors carries a temporary string id of the form ``new-<Date.now()>``. If the
user deletes that row before a refetch re-keys it with the real numeric id, the
client posts the temp string id to the delete endpoint. This guard turns that
into a well-formed 400.

Run:
    pytest padel_app/tests/test_delete_nonnumeric_id_guard.py -v
"""


def test_delete_coach_level_rejects_nonnumeric_id(client):
    resp = client.post("/api/app/delete/coach_level", json={"id": "new-1753900000000"})
    assert resp.status_code == 400
    assert "numeric" in resp.get_json()["error"]


def test_delete_coach_note_rejects_nonnumeric_id(client):
    resp = client.post("/api/app/delete/coach_note", json={"id": "new-1753900000000"})
    assert resp.status_code == 400
    assert "numeric" in resp.get_json()["error"]


def test_delete_coach_level_missing_id_rejected(client):
    # A missing id (None) is likewise non-numeric and must not 500.
    resp = client.post("/api/app/delete/coach_level", json={})
    assert resp.status_code == 400


def test_delete_coach_level_numeric_id_passes_guard(client):
    # A well-formed numeric id clears the guard; with no such row seeded it
    # resolves to a 404 (first_or_404), NOT a 400 and NOT a 500.
    resp = client.post("/api/app/delete/coach_level", json={"id": 999999})
    assert resp.status_code == 404
