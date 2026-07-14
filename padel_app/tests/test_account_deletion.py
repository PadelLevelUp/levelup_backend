"""
Phase 2 — In-app account deletion (Apple 5.1.1(v)).

DELETE /api/auth/me soft-deletes + anonymizes the calling user: status ->
"disabled", PII scrubbed, row kept (so message authorship for other users
still resolves), and all of that user's JWTs are rejected from then on
(session kill, all devices) via the blocklist loader in padel_app/auth.py.
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
def target_user(app):
    """The user who will delete their own account."""
    from padel_app.models import User

    with app.app_context():
        user = User(
            name="Target User",
            username="delete_target",
            email="target@example.com",
            phone="555-1234",
            generated_code=4242,
            password="x",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        return user.id


@pytest.fixture
def other_user(app):
    """A bystander user, unaffected by target_user's deletion."""
    from padel_app.models import User

    with app.app_context():
        user = User(
            name="Other User",
            username="delete_other",
            password="x",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def test_delete_me_returns_200(client, app, target_user):
    resp = client.delete("/api/auth/me", headers=_auth_header(app, target_user))
    assert resp.status_code == 200


def test_delete_me_anonymizes_and_disables_user(client, app, target_user):
    from padel_app.models import User

    resp = client.delete("/api/auth/me", headers=_auth_header(app, target_user))
    assert resp.status_code == 200

    with app.app_context():
        user = User.query.get(target_user)
        assert user is not None  # row is kept, not hard-deleted
        assert user.status == "disabled"
        assert user.name == "Deleted user"
        assert user.email is None
        assert user.phone is None
        assert user.generated_code is None
        assert user.user_image_id is None


def test_reused_jwt_after_deletion_is_rejected(client, app, target_user):
    headers = _auth_header(app, target_user)

    # Token is valid before deletion.
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200

    del_resp = client.delete("/api/auth/me", headers=headers)
    assert del_resp.status_code == 200

    # Same token, issued before deletion, must now be rejected.
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code in (401, 422)


def test_deleted_user_hidden_from_app_users(client, app, target_user, other_user):
    resp = client.delete("/api/auth/me", headers=_auth_header(app, target_user))
    assert resp.status_code == 200

    resp = client.get("/api/app/users", headers=_auth_header(app, other_user))
    assert resp.status_code == 200
    user_ids = {u["id"] for u in resp.get_json()}
    assert target_user not in user_ids


def test_message_from_deleted_user_still_resolves_with_anonymized_name(
    client, app, target_user, other_user
):
    from padel_app.models import User, Conversation, ConversationParticipant, Message
    from padel_app.serializers.message import serialize_message

    with app.app_context():
        conversation = Conversation(
            participant_key=Conversation.build_participant_key(
                [target_user, other_user]
            ),
        )
        db.session.add(conversation)
        db.session.flush()

        db.session.add_all(
            [
                ConversationParticipant(
                    conversation_id=conversation.id, user_id=target_user
                ),
                ConversationParticipant(
                    conversation_id=conversation.id, user_id=other_user
                ),
            ]
        )

        message = Message(
            text="hello before deletion",
            sender_id=target_user,
            conversation_id=conversation.id,
        )
        db.session.add(message)
        db.session.commit()
        message_id = message.id

    resp = client.delete("/api/auth/me", headers=_auth_header(app, target_user))
    assert resp.status_code == 200

    with app.app_context():
        message = Message.query.get(message_id)
        # Serializing must not error and must still reference the sender.
        payload = serialize_message(message, None)
        assert payload["senderId"] == target_user

        sender = User.query.get(message.sender_id)
        assert sender is not None
        assert sender.name == "Deleted user"


def test_other_users_token_unaffected_by_someone_elses_deletion(
    client, app, target_user, other_user
):
    other_headers = _auth_header(app, other_user)

    # Sanity: other_user's token works before target_user's deletion.
    resp = client.get("/api/auth/me", headers=other_headers)
    assert resp.status_code == 200

    del_resp = client.delete("/api/auth/me", headers=_auth_header(app, target_user))
    assert del_resp.status_code == 200

    # other_user's token, unrelated to the deleted account, must still work.
    resp = client.get("/api/auth/me", headers=other_headers)
    assert resp.status_code == 200
    assert resp.get_json()["id"] == other_user
