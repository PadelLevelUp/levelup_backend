from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, get_jwt
from werkzeug.security import check_password_hash

from padel_app.models import User, TokenBlocklist
from padel_app.sql_db import db
from padel_app.services.account_service import delete_account_service
from padel_app.services.user_service import (
    OWN_PROFILE_FIELDS,
    ProfileValidationError,
    update_own_profile_service,
)

bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")


def _serialize_me(user):
    """The payload the app hydrates its session and Settings profile form from."""
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "roles": ["coach"] if user.coach else ["player"],
        "coachId": user.coach.id if user.coach else None,
        "isSuperAdmin": user.is_superadmin,
        "language": getattr(user, "language", "pt") or "pt",
        # PAD-81: the Settings profile form is hydrated from here instead of
        # from hardcoded local defaults.
        "abbreviation": user.abbreviation_display,
        "email": user.email,
        "phone": user.phone,
    }

@bp.post("/login")
def login():
    data = request.get_json() or {}

    username = data.get("username")
    password = data.get("password")
    

    if not username or not password:
        return {"error": "Email and password required"}, 400

    user = User.query.filter_by(username=username).first()

    if not user or not check_password_hash(user.password, password):
        return {"error": "Invalid credentials"}, 401

    access_token = create_access_token(identity=str(user.id))

    return {
        "accessToken": access_token,
        "user": {
            "id": user.id,
            "name": user.name,
            "role": user.role,
        }
    }
    
@bp.post("/logout")
@jwt_required()
def logout():
    jti = get_jwt()["jti"]
    db.session.add(TokenBlocklist(jti=jti))
    db.session.commit()
    return {"message": "Successfully logged out"}, 200

@bp.get("/me")
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)

    return jsonify(_serialize_me(user))


@bp.delete("/me")
@jwt_required()
def delete_me():
    user_id = int(get_jwt_identity())
    delete_account_service(user_id)
    return jsonify({"message": "Account deleted"}), 200


@bp.patch("/me")
@jwt_required()
def update_me():
    user_id = int(get_jwt_identity())
    data = request.get_json() or {}
    # PAD-81: only the fields a user may change on their own account, and only
    # the ones actually present in the payload (partial update).
    payload = {k: v for k, v in data.items() if k in OWN_PROFILE_FIELDS}

    try:
        user = update_own_profile_service(user_id, payload)
    except ProfileValidationError as exc:
        db.session.rollback()
        return {"error": exc.message}, exc.status

    return jsonify(_serialize_me(user))