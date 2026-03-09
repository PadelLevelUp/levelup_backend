from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, get_jwt
from werkzeug.security import check_password_hash

from padel_app.models import User, TokenBlocklist
from padel_app.sql_db import db

bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

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

    return jsonify({
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "roles": ["coach"] if user.coach else ["player"],
        "coachId": user.coach.id if user.coach else None,
    })