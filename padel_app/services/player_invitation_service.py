import secrets
from datetime import datetime, timedelta

from flask import abort
from werkzeug.security import generate_password_hash

from padel_app.models import (
    Association_CoachPlayer,
    Player,
    PlayerInvitation,
    PlayerLevelHistory,
    User,
)
from padel_app.sql_db import db
from padel_app.tools.username_tools import unique_placeholder_username

PLAYER_INVITATION_VALID_DAYS = 7


def create_incomplete_player_service(data, now=None):
    if not data.get("coachId"):
        abort(400, "coachId is required")
    if not data.get("name"):
        abort(400, "name is required")

    user = User(
        name=data["name"],
        username=unique_placeholder_username(),
        email=data.get("email") or None,
        password=None,
        status="inactive",
    )
    db.session.add(user)
    db.session.flush()

    player = Player(user_id=user.id)
    db.session.add(player)
    db.session.flush()

    db.session.add(
        Association_CoachPlayer(
            coach_id=int(data["coachId"]),
            player_id=player.id,
            level_id=int(data["levelId"]) if data.get("levelId") else None,
            side=data.get("side") or None,
            notes=data.get("notes") or None,
        )
    )

    if data.get("levelId"):
        db.session.add(
            PlayerLevelHistory(
                coach_id=int(data["coachId"]),
                player_id=player.id,
                level_id=int(data["levelId"]),
            )
        )

    invitation = PlayerInvitation(
        player_id=player.id,
        token=secrets.token_urlsafe(32),
        invited_by_coach_id=int(data["coachId"]),
        status="pending",
        expires_at=(now or datetime.utcnow())
        + timedelta(days=PLAYER_INVITATION_VALID_DAYS),
    )
    db.session.add(invitation)
    db.session.commit()
    return invitation


def get_player_invitation_service(token, now=None):
    invitation = PlayerInvitation.query.filter_by(token=token).first()
    if invitation is None:
        abort(404, "Invitation not found")

    if invitation.status == "pending" and invitation.expires_at < (
        now or datetime.utcnow()
    ):
        invitation.status = "expired"
        db.session.commit()

    if invitation.status != "pending":
        abort(410, f"Invitation is {invitation.status}")

    return invitation


def accept_player_invitation_service(token, data=None, now=None):
    invitation = get_player_invitation_service(token, now=now)

    data = data or {}
    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        abort(400, "username and password are required")

    existing = User.query.filter_by(username=username).first()
    if existing is not None and existing.id != invitation.player.user_id:
        abort(409, "Username already taken")

    user = invitation.player.user
    user.username = username
    user.password = generate_password_hash(password)
    if data.get("email"):
        user.email = data["email"]
    if data.get("phone"):
        user.phone = data["phone"]
    user.status = "active"

    invitation.status = "accepted"
    db.session.commit()
    return user


def revoke_player_invitation_service(token, coach):
    invitation = PlayerInvitation.query.filter_by(token=token).first()
    if invitation is None:
        abort(404, "Invitation not found")

    if coach is None or coach.id != invitation.invited_by_coach_id:
        abort(403, "Only the inviting coach can revoke this invitation")

    if invitation.status != "pending":
        abort(410, f"Invitation is {invitation.status}")

    invitation.status = "revoked"
    db.session.commit()
    return invitation
