"""
PAD-66 — Unread message count must clear when the recipient reads a
freshly-sent message.

`create_message_service` stamped `sent_at` with `datetime.now()` (naive LOCAL
time) while `mark_conversation_read_service` sets `last_read_at = utcnow_naive()`
(UTC) and unread is `sent_at > last_read_at`. Under a positive UTC offset a
just-sent message was stamped in the future, so it stayed unread even right
after the recipient read it — until real UTC time caught up (~1h at +1).

The test forces a non-UTC process timezone (UTC+2, no DST) so the bug is
deterministic regardless of the host/CI timezone: with local-time stamping the
message is stamped 2h ahead of UTC and never clears; with the UTC fix it clears
immediately.

Covered spec: messaging (unread counts)
"""
import os
import time as _time

import pytest

from padel_app.sql_db import db


@pytest.fixture
def force_utc_plus_2():
    """Run the test body as if the host were UTC+2 (no DST)."""
    if not hasattr(_time, "tzset"):
        pytest.skip("tzset not available on this platform")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-2"  # POSIX sign inversion → local = UTC+2
    _time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset()


def _make_conversation(app):
    from padel_app.models import User, Message  # noqa: F401
    from padel_app.models.conversations import Conversation
    from padel_app.models.conversation_participants import ConversationParticipant

    coach_user = User(name="Coach", username="msg_coach", password="x")
    student_user = User(name="Student", username="msg_student", password="x")
    db.session.add_all([coach_user, student_user])
    db.session.flush()

    conversation = Conversation(
        is_group=False,
        participant_key=Conversation.build_participant_key(
            [coach_user.id, student_user.id]
        ),
    )
    db.session.add(conversation)
    db.session.flush()

    db.session.add(ConversationParticipant(
        conversation_id=conversation.id, user_id=coach_user.id
    ))
    db.session.add(ConversationParticipant(
        conversation_id=conversation.id, user_id=student_user.id
    ))
    db.session.commit()
    return coach_user.id, student_user.id, conversation.id


def test_unread_clears_after_reading_freshly_sent_message(app, force_utc_plus_2, monkeypatch):
    from padel_app.models import User
    from padel_app.services import messaging_service
    from padel_app.services.messaging_service import (
        create_message_service,
        get_unread_count,
        mark_conversation_read_service,
    )

    # Patch the real-time publish (Redis) — external I/O.
    monkeypatch.setattr(messaging_service, "publish", lambda *a, **k: None)

    with app.app_context():
        coach_id, student_id, conv_id = _make_conversation(app)

        # Coach sends a message to the student.
        create_message_service(
            {"text": "Hello", "conversationId": conv_id}, coach_id
        )

        # Recipient has an unread message.
        assert get_unread_count(student_id) == 1

        # Recipient reads the conversation.
        student = User.query.get(student_id)
        mark_conversation_read_service(conv_id, student)

        # The unread count must now be zero — even though the message was just
        # sent (the bug left it at 1 for ~1h due to local-time sent_at).
        assert get_unread_count(student_id) == 0
