from padel_app.models import User
from padel_app.sql_db import db


def delete_account_service(user_id):
    """
    In-app account deletion (Apple 5.1.1(v)).

    Soft-deletes + anonymizes the user rather than hard-deleting the row:
    - `status` -> "disabled" (already excluded from `/app/users` and drives
      `isActive` everywhere else in the app, so the user disappears from
      pickers/rosters automatically).
    - PII is scrubbed: name, email, phone, generated_code, user_image_id.
    - The row itself is kept so existing `Message.sender_id` foreign keys
      still resolve — the counterpart's chat history stays intact and shows
      the anonymized name instead of being destroyed.

    `name` is NOT NULL, so it is replaced with the sentinel "Deleted user"
    rather than cleared. `email`/`phone`/`generated_code`/`user_image_id`
    are all nullable, so they are set to None outright.

    Session kill (all devices) is handled separately by the JWT blocklist
    loader in padel_app/auth.py, which rejects any token belonging to a
    user whose status is "disabled".
    """
    user = User.query.get_or_404(user_id)

    user.status = "disabled"
    user.name = "Deleted user"
    user.email = None
    user.phone = None
    user.generated_code = None
    user.user_image_id = None

    db.session.commit()
    return user
