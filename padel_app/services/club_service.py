import secrets
from datetime import datetime, timedelta

from flask import abort
from werkzeug.security import generate_password_hash

from padel_app.models import (
    Association_CoachClub,
    Club,
    Coach,
    CoachInvitation,
    User,
)
from padel_app.sql_db import db
from padel_app.tools.request_adapter import JsonRequestAdapter

COACH_INVITATION_VALID_DAYS = 7


def create_club_service(data):
    club = Club()
    form = club.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.create()
    return club


def edit_club_service(club_id, data):
    # NOTE: original code queried User model for club_id — preserved as-is
    club = User.query.get_or_404(club_id)

    form = club.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.save()
    return club


# -------------------------------------------------------------------
# Coach invitations (clubs.coach-invitation)
# -------------------------------------------------------------------

def _is_club_member(coach, club_id):
    if coach is None:
        return False
    return (
        Association_CoachClub.query.filter_by(
            coach_id=coach.id, club_id=club_id
        ).first()
        is not None
    )


def create_coach_invitation_service(club_id, coach, email=None, now=None):
    Club.query.get_or_404(club_id)

    if coach is None or not _is_club_member(coach, club_id):
        abort(403, "Only a coach belonging to this club can create invitations")

    invitation = CoachInvitation(
        club_id=club_id,
        token=secrets.token_urlsafe(32),
        email=email,
        invited_by_coach_id=coach.id,
        status="pending",
        expires_at=(now or datetime.utcnow())
        + timedelta(days=COACH_INVITATION_VALID_DAYS),
    )
    db.session.add(invitation)
    db.session.commit()
    return invitation


def get_coach_invitation_service(token, now=None):
    invitation = CoachInvitation.query.filter_by(token=token).first()
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


def accept_coach_invitation_service(token, data=None, coach=None, now=None):
    invitation = get_coach_invitation_service(token, now=now)

    if coach is not None:
        # Existing-coach path: only create the association (no-op if member)
        if not _is_club_member(coach, invitation.club_id):
            db.session.add(
                Association_CoachClub(
                    coach_id=coach.id, club_id=invitation.club_id
                )
            )
        invitation.status = "accepted"
        db.session.commit()
        return None

    # New-user path: register User + Coach and join the club
    data = data or {}
    name = data.get("name")
    username = data.get("username")
    password = data.get("password")
    if not name or not username or not password:
        abort(400, "name, username and password are required")

    if User.query.filter_by(username=username).first() is not None:
        abort(409, "Username already taken")

    user = User(
        name=name,
        username=username,
        email=data.get("email") or invitation.email,
        password=generate_password_hash(password),
        status="active",
    )
    db.session.add(user)
    db.session.flush()

    new_coach = Coach(user_id=user.id)
    db.session.add(new_coach)
    db.session.flush()

    db.session.add(
        Association_CoachClub(coach_id=new_coach.id, club_id=invitation.club_id)
    )
    invitation.status = "accepted"
    db.session.commit()
    return user


def revoke_coach_invitation_service(token, coach):
    invitation = CoachInvitation.query.filter_by(token=token).first()
    if invitation is None:
        abort(404, "Invitation not found")

    if coach is None or not _is_club_member(coach, invitation.club_id):
        abort(403, "Only a coach belonging to this club can revoke invitations")

    if invitation.status != "pending":
        abort(410, f"Invitation is {invitation.status}")

    invitation.status = "revoked"
    db.session.commit()
    return invitation


def list_coach_invitations_service(club_id, coach):
    Club.query.get_or_404(club_id)

    if coach is None or not _is_club_member(coach, club_id):
        abort(403, "Only a coach belonging to this club can list invitations")

    return (
        CoachInvitation.query.filter_by(club_id=club_id, status="pending")
        .order_by(CoachInvitation.created_at.desc())
        .all()
    )
