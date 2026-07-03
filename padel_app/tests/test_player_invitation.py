"""
Tests for the players.invite-completion feature.

Covers incomplete-player creation, invitation resolution, acceptance,
duplicate username handling, and expiry.
"""
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


# -------------------------------------------------------------------
# Test helpers
# -------------------------------------------------------------------

def make_coach(app, username="inviter_coach"):
    """Create User + Coach. Returns (user_id, coach_id)."""
    from padel_app.models import User, Coach

    with app.app_context():
        user = User(name=username, username=username, password="pw", status="active")
        db.session.add(user)
        db.session.flush()

        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.commit()
        return user.id, coach.id


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------
# Create incomplete player
# -------------------------------------------------------------------

def test_create_incomplete_player_creates_invitation_and_inactive_user(client, app):
    _, coach_id = make_coach(app)

    resp = client.post(
        "/api/app/incomplete_player",
        json={"coachId": coach_id, "name": "Jane Player"},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["token"]
    assert body["inviteLink"] == f"/invite/player/{body['token']}"
    assert body["expiresAt"]

    from padel_app.models import PlayerInvitation, Association_CoachPlayer

    with app.app_context():
        invitation = PlayerInvitation.query.filter_by(token=body["token"]).one()
        assert invitation.status == "pending"
        assert invitation.invited_by_coach_id == coach_id

        player = invitation.player
        assert player is not None
        assert player.user.status == "inactive"
        assert player.user.name == "Jane Player"
        assert player.user.password is None

        assoc = Association_CoachPlayer.query.filter_by(
            coach_id=coach_id, player_id=player.id
        ).one()
        assert assoc is not None


def test_create_incomplete_player_expires_in_7_days(app):
    from padel_app.services.player_invitation_service import (
        create_incomplete_player_service,
    )

    _, coach_id = make_coach(app)
    now = datetime(2026, 1, 1, 12, 0, 0)

    with app.app_context():
        invitation = create_incomplete_player_service(
            {"coachId": coach_id, "name": "Timed Player"}, now=now
        )
        assert invitation.expires_at == now + timedelta(days=7)


# -------------------------------------------------------------------
# Resolve invitation (public)
# -------------------------------------------------------------------

def test_resolve_valid_invitation_returns_player_name(client, app):
    _, coach_id = make_coach(app)

    create = client.post(
        "/api/app/incomplete_player",
        json={"coachId": coach_id, "name": "Resolve Me"},
    )
    token = create.get_json()["token"]

    resp = client.get(f"/api/app/player-invitations/{token}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["playerName"] == "Resolve Me"
    assert body["status"] == "pending"


def test_resolve_unknown_token_404(client, app):
    resp = client.get("/api/app/player-invitations/not-a-real-token")
    assert resp.status_code == 404


# -------------------------------------------------------------------
# Accept
# -------------------------------------------------------------------

def test_accept_sets_credentials_and_activates_user(client, app):
    _, coach_id = make_coach(app)

    create = client.post(
        "/api/app/incomplete_player",
        json={"coachId": coach_id, "name": "Accept Me"},
    )
    token = create.get_json()["token"]

    resp = client.post(
        f"/api/app/player-invitations/{token}/accept",
        json={"username": "accept_player", "password": "Secret123!"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["accessToken"]

    from padel_app.models import User, PlayerInvitation

    with app.app_context():
        user = User.query.filter_by(username="accept_player").one()
        assert user.status == "active"
        assert user.password != "Secret123!"  # hashed

        invitation = PlayerInvitation.query.filter_by(token=token).one()
        assert invitation.status == "accepted"


def test_accept_with_duplicate_username_409(client, app):
    _, coach_id = make_coach(app, username="taken_username")

    create = client.post(
        "/api/app/incomplete_player",
        json={"coachId": coach_id, "name": "Dup Player"},
    )
    token = create.get_json()["token"]

    resp = client.post(
        f"/api/app/player-invitations/{token}/accept",
        json={"username": "taken_username", "password": "pw123456"},
    )
    assert resp.status_code == 409

    from padel_app.models import PlayerInvitation

    with app.app_context():
        invitation = PlayerInvitation.query.filter_by(token=token).one()
        assert invitation.status == "pending"


def test_accept_expired_invitation_410(app):
    from padel_app.services.player_invitation_service import (
        create_incomplete_player_service,
        accept_player_invitation_service,
    )
    from werkzeug.exceptions import Gone

    _, coach_id = make_coach(app)
    past = datetime(2026, 1, 1, 12, 0, 0)

    with app.app_context():
        invitation = create_incomplete_player_service(
            {"coachId": coach_id, "name": "Expired Player"}, now=past
        )
        token = invitation.token

    later = past + timedelta(days=8)
    with app.app_context():
        with pytest.raises(Gone):
            accept_player_invitation_service(
                token,
                data={"username": "late_player", "password": "pw123456"},
                now=later,
            )
