import re

from padel_app.models import User
from padel_app.sql_db import db
from padel_app.tools.request_adapter import JsonRequestAdapter


def create_user_service(data):
    user = User()
    form = user.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.create()
    return user


def edit_user_service(user_id, data):
    user = User.query.get_or_404(user_id)

    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()
    return user


def activate_user_service(user_id, data):
    user = User.query.get_or_404(user_id)

    data['status'] = 'active'

    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()
    return user


# ── PAD-81: self-service profile editing ─────────────────────────────────────

#: Fields a user is allowed to change on their own account via PATCH /api/auth/me.
OWN_PROFILE_FIELDS = ("name", "abbreviation", "email", "phone", "language")

SUPPORTED_LANGUAGES = ("pt", "en")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Matches the `users.abbreviation` column width used by the badge label.
ABBREVIATION_MAX_LENGTH = 4


class ProfileValidationError(Exception):
    """Raised when a self-service profile update is rejected.

    Carries the HTTP status the route should surface (400 for malformed input,
    409 for a conflict with another user's data).
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def update_own_profile_service(user_id, data):
    """
    Apply a partial update to the signed-in user's own profile (PAD-81).

    Only the keys present in `data` are touched, so the frontend can PATCH a
    single field without clobbering the rest. Values are normalised (trimmed,
    email lowercased, abbreviation uppercased) and validated before the commit —
    previously `PATCH /api/auth/me` silently ignored everything except
    `language`, which is what made the UI report a save that never happened.
    """
    user = User.query.get_or_404(user_id)

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise ProfileValidationError("Name is required")
        user.name = name

    if "abbreviation" in data:
        abbreviation = (data.get("abbreviation") or "").strip().upper()
        user.abbreviation = abbreviation[:ABBREVIATION_MAX_LENGTH] or None

    if "email" in data:
        email = (data.get("email") or "").strip().lower()
        if not email:
            user.email = None
        else:
            if not _EMAIL_RE.match(email):
                raise ProfileValidationError("Invalid email address")
            taken = (
                User.query.filter(User.email == email, User.id != user.id).first()
            )
            if taken is not None:
                raise ProfileValidationError("Email already in use", status=409)
            user.email = email

    if "phone" in data:
        phone = (data.get("phone") or "").strip()
        user.phone = phone or None

    if "language" in data:
        language = data.get("language")
        if language not in SUPPORTED_LANGUAGES:
            raise ProfileValidationError("Unsupported language")
        user.language = language

    db.session.commit()
    return user
