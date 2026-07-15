"""
Phase 5 — native push notification support (device-token storage + Expo push
sender), wired into notification delivery paths.

Covers:
  - POST /api/notifications/device (register/upsert) and DELETE (idempotent)
  - send_expo_push: request body shape + stale-token cleanup on
    DeviceNotRegistered
  - Delivery-path wiring: send_class_reminders (class payload) and
    create_message_service (message payload) both call the Expo sender when
    the recipient has a registered DeviceToken.
"""
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

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


def _create_user(name, username):
    from padel_app.models import User
    u = User(name=name, username=username, email=f"{username}@test.com", password="x", status="active")
    db.session.add(u)
    db.session.flush()
    return u


# ---------------------------------------------------------------------------
# POST /api/notifications/device + DELETE /api/notifications/device
# ---------------------------------------------------------------------------

def test_register_device_token_creates_row(client, app):
    from padel_app.models import DeviceToken, User

    with app.app_context():
        user = _create_user("Coach A", "device-user-1")
        db.session.commit()
        user_id = user.id

    resp = client.post(
        "/api/notifications/device",
        json={"token": "ExponentPushToken[aaa111]", "platform": "ios"},
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 200

    with app.app_context():
        row = DeviceToken.query.filter_by(token="ExponentPushToken[aaa111]").first()
        assert row is not None
        assert row.user_id == user_id
        assert row.platform == "ios"


def test_reregister_token_reassigns_user_no_duplicate_row(client, app):
    from padel_app.models import DeviceToken

    with app.app_context():
        user_a = _create_user("User A", "device-user-a")
        user_b = _create_user("User B", "device-user-b")
        db.session.commit()
        user_a_id, user_b_id = user_a.id, user_b.id

    token = "ExponentPushToken[shared999]"

    resp1 = client.post(
        "/api/notifications/device",
        json={"token": token, "platform": "ios"},
        headers=_auth_header(app, user_a_id),
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/notifications/device",
        json={"token": token, "platform": "android"},
        headers=_auth_header(app, user_b_id),
    )
    assert resp2.status_code == 200

    with app.app_context():
        rows = DeviceToken.query.filter_by(token=token).all()
        assert len(rows) == 1
        assert rows[0].user_id == user_b_id
        assert rows[0].platform == "android"


def test_delete_device_token_removes_row_and_is_idempotent(client, app):
    from padel_app.models import DeviceToken

    with app.app_context():
        user = _create_user("Device Owner", "device-owner")
        db.session.commit()
        user_id = user.id

    token = "ExponentPushToken[deleteme]"
    client.post(
        "/api/notifications/device",
        json={"token": token, "platform": "ios"},
        headers=_auth_header(app, user_id),
    )

    resp = client.delete(
        "/api/notifications/device",
        json={"token": token},
        headers=_auth_header(app, user_id),
    )
    assert resp.status_code == 200

    with app.app_context():
        assert DeviceToken.query.filter_by(token=token).first() is None

    # Idempotent: deleting again (now nonexistent) is still 200.
    resp2 = client.delete(
        "/api/notifications/device",
        json={"token": token},
        headers=_auth_header(app, user_id),
    )
    assert resp2.status_code == 200

    # Deleting a token that never existed at all is also 200.
    resp3 = client.delete(
        "/api/notifications/device",
        json={"token": "ExponentPushToken[never-existed]"},
        headers=_auth_header(app, user_id),
    )
    assert resp3.status_code == 200


def test_delete_device_token_only_removes_callers_own_token(client, app):
    """A token belonging to another user is left untouched (not deletable by id alone)."""
    from padel_app.models import DeviceToken

    with app.app_context():
        owner = _create_user("Real Owner", "device-real-owner")
        other = _create_user("Other Caller", "device-other-caller")
        db.session.commit()
        owner_id, other_id = owner.id, other.id

    token = "ExponentPushToken[notyours]"
    client.post(
        "/api/notifications/device",
        json={"token": token, "platform": "ios"},
        headers=_auth_header(app, owner_id),
    )

    resp = client.delete(
        "/api/notifications/device",
        json={"token": token},
        headers=_auth_header(app, other_id),
    )
    assert resp.status_code == 200  # still 200, but a no-op

    with app.app_context():
        row = DeviceToken.query.filter_by(token=token).first()
        assert row is not None
        assert row.user_id == owner_id


# ---------------------------------------------------------------------------
# send_expo_push: request shape + stale-token cleanup
# ---------------------------------------------------------------------------

def _mock_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def test_send_expo_push_builds_correct_request_body(app):
    from padel_app.services import notification_service  # ensure models loaded
    from padel_app.utils.expo_push import send_expo_push

    with app.app_context():
        with patch("padel_app.utils.expo_push.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"data": [{"status": "ok"}, {"status": "ok"}]})

            result = send_expo_push(
                ["ExponentPushToken[a]", "ExponentPushToken[b]"],
                "Hello",
                "World",
                {"type": "message", "conversationId": 42},
            )

    assert result is True
    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == [
        {"to": "ExponentPushToken[a]", "title": "Hello", "body": "World", "data": {"type": "message", "conversationId": 42}},
        {"to": "ExponentPushToken[b]", "title": "Hello", "body": "World", "data": {"type": "message", "conversationId": 42}},
    ]


def test_send_expo_push_deletes_device_not_registered_token(app):
    from padel_app.models import DeviceToken
    from padel_app.utils.expo_push import send_expo_push

    with app.app_context():
        user = _create_user("Stale Owner", "stale-owner")
        db.session.commit()
        DeviceToken(user_id=user.id, token="ExponentPushToken[stale]", platform="ios").create()
        DeviceToken(user_id=user.id, token="ExponentPushToken[fresh]", platform="ios").create()

        with patch("padel_app.utils.expo_push.requests.post") as mock_post:
            mock_post.return_value = _mock_response({
                "data": [
                    {"status": "error", "message": "not registered", "details": {"error": "DeviceNotRegistered"}},
                    {"status": "ok"},
                ]
            })
            send_expo_push(
                ["ExponentPushToken[stale]", "ExponentPushToken[fresh]"],
                "Title",
                "Body",
                {},
            )

        assert DeviceToken.query.filter_by(token="ExponentPushToken[stale]").first() is None
        assert DeviceToken.query.filter_by(token="ExponentPushToken[fresh]").first() is not None


def test_send_expo_push_never_raises_on_http_failure(app):
    from padel_app.utils.expo_push import send_expo_push

    with app.app_context():
        with patch("padel_app.utils.expo_push.requests.post", side_effect=Exception("network down")):
            result = send_expo_push(["ExponentPushToken[x]"], "T", "B", {})

    assert result is False


def test_send_expo_push_noop_when_no_tokens(app):
    from padel_app.utils.expo_push import send_expo_push_to_user

    with app.app_context():
        user = _create_user("No Device", "no-device-user")
        db.session.commit()
        with patch("padel_app.utils.expo_push.requests.post") as mock_post:
            result = send_expo_push_to_user(user.id, "T", "B", {})

    assert result is False
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Delivery-path wiring
# ---------------------------------------------------------------------------

def test_class_reminder_pushes_expo_with_class_payload(app):
    """send_class_reminders, for a player with a registered DeviceToken, calls
    the Expo sender with {"type": "class", "classInstanceId": instance.id}."""
    from padel_app.models import DeviceToken
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.coach_levels import CoachLevel
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.services.notification_service import send_class_reminders

    with app.app_context():
        coach_user = _create_user("Coach", "reminder-coach")
        player_user = _create_user("Player", "reminder-player")
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        player = Player(user_id=player_user.id)
        db.session.add(player)
        db.session.flush()

        NotificationConfig(coach_id=coach.id, auto_notify_enabled=True).create()

        club = Club(name="Club", description="", location="City")
        db.session.add(club)
        db.session.flush()

        level = CoachLevel(coach_id=coach.id, label="Beg", code="B1", display_order=1)
        db.session.add(level)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=48)
        lesson = Lesson(title="Class", start_datetime=start, end_datetime=start + timedelta(hours=1),
                         is_recurring=False, type="academy", max_players=4, color="#000",
                         status="active", club_id=club.id)
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(lesson_id=lesson.id, start_datetime=start,
                                   end_datetime=start + timedelta(hours=1), max_players=4,
                                   status="scheduled", level_id=level.id, notifications_enabled=True)
        db.session.add(instance)
        db.session.flush()

        db.session.add(Association_CoachLessonInstance(coach_id=coach.id, lesson_instance_id=instance.id))
        db.session.add(Association_PlayerLessonInstance(player_id=player.id, lesson_instance_id=instance.id))
        db.session.commit()

        DeviceToken(user_id=player_user.id, token="ExponentPushToken[reminder]", platform="ios").create()

        instance_id = instance.id

        with patch("padel_app.services.notification_service.publish"), \
             patch("padel_app.services.notification_service.send_push_notification"), \
             patch("padel_app.utils.expo_push.send_expo_push_to_user") as mock_send:
            mock_send.return_value = True
            send_class_reminders(instance_id)

        assert mock_send.call_count >= 1
        args, kwargs = mock_send.call_args
        assert args[0] == player_user.id
        assert kwargs["data"] == {"type": "class", "classInstanceId": instance_id}


def test_direct_message_pushes_expo_with_message_payload(app):
    """create_message_service, for a recipient with a registered DeviceToken,
    calls the Expo sender with {"type": "message", "conversationId": ...}."""
    from padel_app.models import DeviceToken, Conversation, ConversationParticipant

    with app.app_context():
        sender = _create_user("Sender", "msg-sender")
        recipient = _create_user("Recipient", "msg-recipient")
        db.session.commit()

        conversation = Conversation(
            participant_key=Conversation.build_participant_key([sender.id, recipient.id]),
        )
        db.session.add(conversation)
        db.session.flush()
        db.session.add_all([
            ConversationParticipant(conversation_id=conversation.id, user_id=sender.id),
            ConversationParticipant(conversation_id=conversation.id, user_id=recipient.id),
        ])
        db.session.commit()

        DeviceToken(user_id=recipient.id, token="ExponentPushToken[msg]", platform="android").create()

        conversation_id = conversation.id
        sender_id = sender.id

        from padel_app.services.messaging_service import create_message_service

        with patch("padel_app.services.messaging_service.publish"), \
             patch("padel_app.services.messaging_service.send_push_notification"), \
             patch("padel_app.services.messaging_service.send_expo_push_to_user") as mock_send:
            create_message_service({"conversationId": conversation_id, "text": "hello there"}, sender_id)

        assert mock_send.call_count == 1
        args, kwargs = mock_send.call_args
        assert args[0] == recipient.id
        assert kwargs["data"] == {"type": "message", "conversationId": conversation_id}
