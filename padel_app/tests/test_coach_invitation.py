"""
Tests for the clubs.coach-invitation feature.

Covers invitation creation, resolution, acceptance (new user and
existing coach), expiry, revocation, and listing.
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

def _make_coach_with_club(app, username="inviter_coach", club_name="Test Club"):
    """Create User + Coach + Club + membership. Returns (user_id, coach_id, club_id)."""
    from padel_app.models import User, Coach, Club, Association_CoachClub

    with app.app_context():
        user = User(name=username, username=username, password="pw", status="active")
        db.session.add(user)
        db.session.flush()

        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name=club_name)
        db.session.add(club)
        db.session.flush()

        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))
        db.session.commit()
        return user.id, coach.id, club.id


def _make_coach_without_club(app, username="outsider_coach"):
    """Create User + Coach with no club. Returns (user_id, coach_id)."""
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


def _make_invitation(app, club_id, coach_id, **overrides):
    """Create a CoachInvitation directly. Returns its token."""
    import secrets as pysecrets
    from padel_app.models import CoachInvitation

    with app.app_context():
        invitation = CoachInvitation(
            club_id=club_id,
            token=overrides.pop("token", pysecrets.token_urlsafe(32)),
            invited_by_coach_id=coach_id,
            status=overrides.pop("status", "pending"),
            expires_at=overrides.pop(
                "expires_at", datetime.utcnow() + timedelta(days=7)
            ),
            **overrides,
        )
        db.session.add(invitation)
        db.session.commit()
        return invitation.token


# -------------------------------------------------------------------
# Create invitation
# -------------------------------------------------------------------

def test_create_invitation_as_member(client, app):
    user_id, coach_id, club_id = _make_coach_with_club(app)

    resp = client.post(
        f"/api/app/club/{club_id}/coach-invitations",
        json={},
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["token"]
    assert body["inviteLink"] == f"/invite/coach/{body['token']}"
    assert body["expiresAt"]

    from padel_app.models import CoachInvitation

    with app.app_context():
        invitation = CoachInvitation.query.filter_by(token=body["token"]).one()
        assert invitation.status == "pending"
        assert invitation.club_id == club_id
        assert invitation.invited_by_coach_id == coach_id


def test_create_invitation_expires_in_7_days(app):
    from padel_app.services.club_service import create_coach_invitation_service
    from padel_app.models import Coach

    _, coach_id, club_id = _make_coach_with_club(app)
    now = datetime(2026, 1, 1, 12, 0, 0)

    with app.app_context():
        coach = Coach.query.get(coach_id)
        invitation = create_coach_invitation_service(club_id, coach, now=now)
        assert invitation.expires_at == now + timedelta(days=7)


def test_create_invitation_as_non_member_403(client, app):
    _, _, club_id = _make_coach_with_club(app)
    outsider_user_id, _ = _make_coach_without_club(app)

    resp = client.post(
        f"/api/app/club/{club_id}/coach-invitations",
        json={},
        headers=_auth_header(app, outsider_user_id),
    )
    assert resp.status_code == 403


# -------------------------------------------------------------------
# Resolve invitation (public)
# -------------------------------------------------------------------

def test_resolve_valid_invitation_returns_club_name(client, app):
    _, coach_id, club_id = _make_coach_with_club(app, club_name="Padel Academy")
    token = _make_invitation(app, club_id, coach_id)

    resp = client.get(f"/api/app/coach-invitations/{token}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["clubName"] == "Padel Academy"
    assert body["status"] == "pending"


def test_resolve_unknown_token_404(client, app):
    resp = client.get("/api/app/coach-invitations/not-a-real-token")
    assert resp.status_code == 404


def test_resolve_expired_invitation_410(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(
        app, club_id, coach_id,
        expires_at=datetime.utcnow() - timedelta(days=1),
    )

    resp = client.get(f"/api/app/coach-invitations/{token}")
    assert resp.status_code == 410

    from padel_app.models import CoachInvitation

    with app.app_context():
        invitation = CoachInvitation.query.filter_by(token=token).one()
        assert invitation.status == "expired"


def test_resolve_revoked_invitation_410(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id, status="revoked")

    resp = client.get(f"/api/app/coach-invitations/{token}")
    assert resp.status_code == 410


# -------------------------------------------------------------------
# Accept — new user
# -------------------------------------------------------------------

def test_accept_as_new_user_creates_active_coach(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"name": "New Coach", "username": "new_coach", "password": "Secret123!"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["accessToken"]

    from padel_app.models import (
        User,
        CoachInvitation,
        Association_CoachClub,
    )

    with app.app_context():
        user = User.query.filter_by(username="new_coach").one()
        assert user.status == "active"
        assert user.coach is not None
        assert user.password != "Secret123!"  # hashed

        assoc = Association_CoachClub.query.filter_by(
            coach_id=user.coach.id, club_id=club_id
        ).one()
        assert assoc is not None

        invitation = CoachInvitation.query.filter_by(token=token).one()
        assert invitation.status == "accepted"


def test_accept_twice_410(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id)

    first = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"name": "C1", "username": "c1", "password": "pw123456"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"name": "C2", "username": "c2", "password": "pw123456"},
    )
    assert second.status_code == 410


def test_accept_with_missing_fields_400(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"username": "no_name_or_pw"},
    )
    assert resp.status_code == 400


def test_accept_with_duplicate_username_409(client, app):
    _, coach_id, club_id = _make_coach_with_club(app, username="taken_username")
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"name": "Dup", "username": "taken_username", "password": "pw123456"},
    )
    assert resp.status_code == 409

    from padel_app.models import CoachInvitation

    with app.app_context():
        invitation = CoachInvitation.query.filter_by(token=token).one()
        assert invitation.status == "pending"


def test_accept_expired_invitation_410(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(
        app, club_id, coach_id,
        expires_at=datetime.utcnow() - timedelta(minutes=1),
    )

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={"name": "Late", "username": "late_coach", "password": "pw123456"},
    )
    assert resp.status_code == 410


# -------------------------------------------------------------------
# Accept — existing coach
# -------------------------------------------------------------------

def test_accept_as_existing_coach_creates_association_only(client, app):
    _, inviter_coach_id, club_id = _make_coach_with_club(app)
    outsider_user_id, outsider_coach_id = _make_coach_without_club(app)
    token = _make_invitation(app, club_id, inviter_coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={},
        headers=_auth_header(app, outsider_user_id),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}

    from padel_app.models import User, CoachInvitation, Association_CoachClub

    with app.app_context():
        assoc = Association_CoachClub.query.filter_by(
            coach_id=outsider_coach_id, club_id=club_id
        ).one()
        assert assoc is not None

        invitation = CoachInvitation.query.filter_by(token=token).one()
        assert invitation.status == "accepted"

        # No new user was created
        assert User.query.count() == 2


def test_accept_as_existing_member_is_noop(client, app):
    user_id, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/accept",
        json={},
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 200

    from padel_app.models import Association_CoachClub

    with app.app_context():
        count = Association_CoachClub.query.filter_by(
            coach_id=coach_id, club_id=club_id
        ).count()
        assert count == 1


# -------------------------------------------------------------------
# Revoke
# -------------------------------------------------------------------

def test_revoke_as_member(client, app):
    user_id, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/revoke",
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}

    from padel_app.models import CoachInvitation

    with app.app_context():
        invitation = CoachInvitation.query.filter_by(token=token).one()
        assert invitation.status == "revoked"


def test_revoke_as_non_member_403(client, app):
    _, coach_id, club_id = _make_coach_with_club(app)
    outsider_user_id, _ = _make_coach_without_club(app)
    token = _make_invitation(app, club_id, coach_id)

    resp = client.post(
        f"/api/app/coach-invitations/{token}/revoke",
        headers=_auth_header(app, outsider_user_id),
    )
    assert resp.status_code == 403


def test_revoke_unknown_token_404(client, app):
    user_id, _, _ = _make_coach_with_club(app)

    resp = client.post(
        "/api/app/coach-invitations/unknown-token/revoke",
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 404


def test_revoke_accepted_invitation_410(client, app):
    user_id, coach_id, club_id = _make_coach_with_club(app)
    token = _make_invitation(app, club_id, coach_id, status="accepted")

    resp = client.post(
        f"/api/app/coach-invitations/{token}/revoke",
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 410


# -------------------------------------------------------------------
# List pending invitations
# -------------------------------------------------------------------

def test_list_pending_invitations(client, app):
    user_id, coach_id, club_id = _make_coach_with_club(app)
    pending_token = _make_invitation(app, club_id, coach_id, email="a@b.com")
    _make_invitation(app, club_id, coach_id, status="accepted")
    _make_invitation(app, club_id, coach_id, status="revoked")

    resp = client.get(
        f"/api/app/club/{club_id}/coach-invitations",
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["token"] == pending_token
    assert body[0]["email"] == "a@b.com"
    assert body[0]["expiresAt"]
    assert body[0]["createdAt"]


def test_list_invitations_as_non_member_403(client, app):
    _, _, club_id = _make_coach_with_club(app)
    outsider_user_id, _ = _make_coach_without_club(app)

    resp = client.get(
        f"/api/app/club/{club_id}/coach-invitations",
        headers=_auth_header(app, outsider_user_id),
    )
    assert resp.status_code == 403


# -------------------------------------------------------------------
# GET /coach includes club
# -------------------------------------------------------------------

def test_coach_detail_includes_club(client, app):
    user_id, _, club_id = _make_coach_with_club(app, club_name="My Club")

    resp = client.get("/api/app/coach", headers=_auth_header(app, user_id))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["club"] == {"id": club_id, "name": "My Club"}


def test_coach_detail_without_club(client, app):
    user_id, _ = _make_coach_without_club(app)

    resp = client.get("/api/app/coach", headers=_auth_header(app, user_id))
    assert resp.status_code == 200
    assert resp.get_json()["club"] is None
