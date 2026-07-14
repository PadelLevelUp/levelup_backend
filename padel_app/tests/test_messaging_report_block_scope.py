"""
Phase 3 — Report/Block for messaging (Apple 1.2 UGC) + conversation scoping
+ conversation-access IDOR fix.

Scope rule: a coach may only start a conversation with players belonging to
one of the coach's clubs; a player/student may only start a conversation
with a coach. Block is bidirectional and prevents both starting a new
conversation and sending a new message in an existing one. Reporting a
message requires the reporter to be a participant of that message's
conversation. GET /app/conversation/<id> requires the caller to be a
participant.
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
def scenario(app):
    """
    Two clubs (A, B). Coach1 is in club A. Player1 is in club A (coach1's
    club). Player2 is in club B (NOT coach1's club). Coach2 exists but is
    unrelated to any club. Student (player1) may message any coach.
    """
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_PlayerClub import Association_PlayerClub

    with app.app_context():
        coach1_user = User(name="Coach One", username="scope_coach1", password="x", status="active")
        coach2_user = User(name="Coach Two", username="scope_coach2", password="x", status="active")
        player1_user = User(name="Player One", username="scope_player1", password="x", status="active")
        player2_user = User(name="Player Two", username="scope_player2", password="x", status="active")
        db.session.add_all([coach1_user, coach2_user, player1_user, player2_user])
        db.session.flush()

        coach1 = Coach(user_id=coach1_user.id)
        coach2 = Coach(user_id=coach2_user.id)
        player1 = Player(user_id=player1_user.id)
        player2 = Player(user_id=player2_user.id)
        db.session.add_all([coach1, coach2, player1, player2])
        db.session.flush()

        club_a = Club(name="Club A", description="a", location="x")
        club_b = Club(name="Club B", description="b", location="x")
        db.session.add_all([club_a, club_b])
        db.session.flush()

        db.session.add(Association_CoachClub(coach_id=coach1.id, club_id=club_a.id))
        db.session.add(Association_PlayerClub(player_id=player1.id, club_id=club_a.id))
        db.session.add(Association_PlayerClub(player_id=player2.id, club_id=club_b.id))
        db.session.commit()

        return {
            "coach1_user_id": coach1_user.id,
            "coach2_user_id": coach2_user.id,
            "player1_user_id": player1_user.id,
            "player2_user_id": player2_user.id,
        }


def _create_conversation(client, app, user_id, other_user_id):
    return client.post(
        "/api/app/conversation",
        json={"otherParticipants": [other_user_id]},
        headers=_auth_header(app, user_id),
    )


# --- Scope enforcement ---------------------------------------------------

def test_coach_can_message_player_in_own_club(client, app, scenario):
    resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    assert resp.status_code == 201


def test_coach_cannot_message_player_outside_own_club(client, app, scenario):
    resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player2_user_id"]
    )
    assert resp.status_code == 403


def test_student_can_message_any_coach(client, app, scenario):
    resp = _create_conversation(
        client, app, scenario["player1_user_id"], scenario["coach2_user_id"]
    )
    assert resp.status_code == 201


def test_student_cannot_message_another_student(client, app, scenario):
    resp = _create_conversation(
        client, app, scenario["player1_user_id"], scenario["player2_user_id"]
    )
    assert resp.status_code == 403


# --- Messageable-users picker ---------------------------------------------

def test_messageable_users_scoped_for_coach(client, app, scenario):
    resp = client.get(
        "/api/app/messageable-users", headers=_auth_header(app, scenario["coach1_user_id"])
    )
    assert resp.status_code == 200
    ids = {u["id"] for u in resp.get_json()}
    assert scenario["player1_user_id"] in ids
    assert scenario["player2_user_id"] not in ids


def test_messageable_users_scoped_for_student(client, app, scenario):
    resp = client.get(
        "/api/app/messageable-users", headers=_auth_header(app, scenario["player1_user_id"])
    )
    assert resp.status_code == 200
    ids = {u["id"] for u in resp.get_json()}
    assert scenario["coach1_user_id"] in ids
    assert scenario["coach2_user_id"] in ids
    assert scenario["player2_user_id"] not in ids


# --- Block/unblock ---------------------------------------------------------

def _block(client, app, blocker_id, blocked_id):
    return client.post(
        f"/api/app/users/{blocked_id}/block", headers=_auth_header(app, blocker_id)
    )


def _unblock(client, app, blocker_id, blocked_id):
    return client.delete(
        f"/api/app/users/{blocked_id}/block", headers=_auth_header(app, blocker_id)
    )


def test_block_is_idempotent(client, app, scenario):
    resp1 = _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    resp2 = _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    assert resp1.status_code == 200
    assert resp2.status_code == 200


def test_blocked_user_prevents_new_conversation(client, app, scenario):
    _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    assert resp.status_code == 403


def test_blocked_user_prevents_new_message_in_existing_conversation(client, app, scenario):
    create_resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    assert create_resp.status_code == 201
    conversation_id = create_resp.get_json()["id"]

    # Block after the conversation already exists (the counterpart blocks the coach).
    _block(client, app, scenario["player1_user_id"], scenario["coach1_user_id"])

    resp = client.post(
        "/api/app/message",
        json={"conversationId": conversation_id, "text": "hello"},
        headers=_auth_header(app, scenario["coach1_user_id"]),
    )
    assert resp.status_code == 403


def test_blocked_users_absent_from_messageable_users(client, app, scenario):
    _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    resp = client.get(
        "/api/app/messageable-users", headers=_auth_header(app, scenario["coach1_user_id"])
    )
    ids = {u["id"] for u in resp.get_json()}
    assert scenario["player1_user_id"] not in ids


def test_unblock_restores_ability_to_message(client, app, scenario):
    _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    _unblock(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    assert resp.status_code == 201


def test_blocked_users_list(client, app, scenario):
    _block(client, app, scenario["coach1_user_id"], scenario["player1_user_id"])
    resp = client.get(
        "/api/app/blocked-users", headers=_auth_header(app, scenario["coach1_user_id"])
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["id"] == scenario["player1_user_id"]


# --- Report ------------------------------------------------------------

def test_participant_can_report_message(client, app, scenario):
    create_resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    conversation_id = create_resp.get_json()["id"]
    msg_resp = client.post(
        "/api/app/message",
        json={"conversationId": conversation_id, "text": "inappropriate"},
        headers=_auth_header(app, scenario["coach1_user_id"]),
    )
    message_id = msg_resp.get_json()["id"]

    resp = client.post(
        f"/api/app/messages/{message_id}/report",
        json={"reason": "spam"},
        headers=_auth_header(app, scenario["player1_user_id"]),
    )
    assert resp.status_code == 201

    from padel_app.models import MessageReport
    with app.app_context():
        reports = MessageReport.query.filter_by(message_id=message_id).all()
        assert len(reports) == 1
        assert reports[0].reason == "spam"
        assert reports[0].reporter_id == scenario["player1_user_id"]


def test_non_participant_cannot_report_message(client, app, scenario):
    create_resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    conversation_id = create_resp.get_json()["id"]
    msg_resp = client.post(
        "/api/app/message",
        json={"conversationId": conversation_id, "text": "hi"},
        headers=_auth_header(app, scenario["coach1_user_id"]),
    )
    message_id = msg_resp.get_json()["id"]

    resp = client.post(
        f"/api/app/messages/{message_id}/report",
        headers=_auth_header(app, scenario["coach2_user_id"]),
    )
    assert resp.status_code == 403


# --- IDOR fix on GET /conversation/<id> ---------------------------------

def test_non_participant_gets_403_on_conversation_detail(client, app, scenario):
    create_resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    conversation_id = create_resp.get_json()["id"]

    resp = client.get(
        f"/api/app/conversation/{conversation_id}",
        headers=_auth_header(app, scenario["coach2_user_id"]),
    )
    assert resp.status_code == 403


def test_participant_can_get_conversation_detail(client, app, scenario):
    create_resp = _create_conversation(
        client, app, scenario["coach1_user_id"], scenario["player1_user_id"]
    )
    conversation_id = create_resp.get_json()["id"]

    resp = client.get(
        f"/api/app/conversation/{conversation_id}",
        headers=_auth_header(app, scenario["player1_user_id"]),
    )
    assert resp.status_code == 200
