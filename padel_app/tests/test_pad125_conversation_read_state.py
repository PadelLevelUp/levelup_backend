"""
PAD-125 — Conversation detail must compute read state from the *user* id, not
the participant row's primary key.

`Conversation.last_read_by()` did:

    next((p for p in self.participants if p.id == user_id), None)

`p` is a `ConversationParticipant`, so `p.id` is that join row's primary key
while `user_id` is a `users.id`. Two independent sequences compared as if they
were one. `serialize_conversation_detail()` feeds the result into
`serialize_message()` for every message, so the usual outcome (no row matches)
is `last_read_at = None` and *every* message serializes as
`isRead: false` / `status: "delivered"`; the rarer outcome (a row whose PK
happens to equal the caller's user id) is worse — the caller is shown another
participant's read state.

Covered spec: messaging.read-tracking rule 3 — "Unread = messages where
`sent_at > participant.last_read_at`", the participant being the *calling*
user's row.

VACUITY GUARD: this bug is invisible whenever `participant.id == user_id`,
which is exactly what a naive seed produces (first conversation, first users →
both sequences start at 1). Every test below therefore forces the two id spaces
apart and asserts that divergence up front. If a future change to the fixtures
makes the ids line up again, the guard fails loudly rather than letting these
tests silently pass against the buggy code.
"""
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


BASE = datetime(2026, 8, 1, 12, 0, 0)


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _build_conversation(participant_row_ids=None):
    """
    Two users, one conversation, five messages one minute apart.

    `participant_row_ids` is an explicit ``{user_id_key: row_id}`` mapping used
    to drive the `conversation_participants.id` sequence away from `users.id`.
    Keys are the literal strings "a" and "b".
    """
    from padel_app.models import User, Message
    from padel_app.models.conversations import Conversation
    from padel_app.models.conversation_participants import ConversationParticipant

    user_a = User(name="Reader A", username="pad125_a", password="x")
    user_b = User(name="Reader B", username="pad125_b", password="x")
    db.session.add_all([user_a, user_b])
    db.session.flush()

    conversation = Conversation(
        is_group=False,
        participant_key=Conversation.build_participant_key(
            [user_a.id, user_b.id]
        ),
    )
    db.session.add(conversation)
    db.session.flush()

    if participant_row_ids is None:
        # Default divergence: push the join-row PKs well clear of users.id.
        participant_row_ids = {"a": user_a.id + 500, "b": user_b.id + 500}

    part_a = ConversationParticipant(
        id=participant_row_ids["a"],
        conversation_id=conversation.id,
        user_id=user_a.id,
    )
    part_b = ConversationParticipant(
        id=participant_row_ids["b"],
        conversation_id=conversation.id,
        user_id=user_b.id,
    )
    db.session.add_all([part_a, part_b])

    # Five messages, one minute apart, sent by B so A is the reader.
    messages = []
    for i in range(5):
        m = Message(
            text=f"message {i + 1}",
            sent_at=BASE + timedelta(minutes=i),
            sender_id=user_b.id,
            conversation_id=conversation.id,
        )
        db.session.add(m)
        messages.append(m)

    db.session.commit()

    return {
        "user_a_id": user_a.id,
        "user_b_id": user_b.id,
        "conversation_id": conversation.id,
        "part_a_id": part_a.id,
        "part_b_id": part_b.id,
        "message_ids": [m.id for m in messages],
    }


def _assert_ids_diverge(world):
    """The vacuity guard — see module docstring."""
    assert world["part_a_id"] != world["user_a_id"], (
        "seed produced participant.id == user_id for A; the PAD-125 bug is "
        "invisible under that alignment and this test would pass against the "
        "unfixed code"
    )
    assert world["part_b_id"] != world["user_b_id"], (
        "seed produced participant.id == user_id for B; same vacuity problem"
    )


def test_read_state_uses_user_id_not_participant_row_id(app, client):
    """
    AC1 — a reader who has read up to message 3 sees 1-3 read and 4-5 unread.

    Pre-fix: no participant row has `id == user_a.id`, so `last_read_by()`
    returns None and all five messages come back `isRead: false`.
    """
    from padel_app.models.conversation_participants import ConversationParticipant

    with app.app_context():
        world = _build_conversation()
        _assert_ids_diverge(world)

        # A has read up to and including message 3 (BASE + 2 min).
        part_a = db.session.get(ConversationParticipant, world["part_a_id"])
        part_a.last_read_at = BASE + timedelta(minutes=2)
        db.session.commit()

    resp = client.get(
        f"/api/app/conversation/{world['conversation_id']}",
        headers=_auth_header(app, world["user_a_id"]),
    )
    assert resp.status_code == 200, resp.data

    messages = resp.get_json()["messages"]
    assert len(messages) == 5

    read_flags = [m["isRead"] for m in messages]
    statuses = [m["status"] for m in messages]

    assert read_flags == [True, True, True, False, False], (
        f"expected first three read, got {read_flags}"
    )
    assert statuses == ["read", "read", "read", "delivered", "delivered"]


def test_each_participant_sees_their_own_read_state(app, client):
    """
    AC2 — the sharp case. The join-row PKs are swapped onto the *other*
    participant's user id, so the buggy lookup (`p.id == user_id`) resolves to
    the wrong row and each caller is shown the other's read state.

    Pre-fix: A's lookup matches B's participant row (and vice versa), so A sees
    B's cursor and B sees A's.
    """
    from padel_app.models import User
    from padel_app.models.conversations import Conversation
    from padel_app.models.conversation_participants import ConversationParticipant

    with app.app_context():
        # Peek at the ids the users will get so the join rows can be given the
        # crossed PKs deliberately.
        probe_a = User(name="probe", username="pad125_probe", password="x")
        db.session.add(probe_a)
        db.session.flush()
        next_user_id = probe_a.id
        db.session.delete(probe_a)
        db.session.commit()

        # users will be next_user_id and next_user_id + 1 (or thereabouts);
        # build first, then cross the participant PKs onto each other's user id.
        world = _build_conversation(
            participant_row_ids={"a": next_user_id + 900, "b": next_user_id + 901}
        )

        # Now cross them: A's row takes B's user id as its PK and vice versa.
        part_a = db.session.get(ConversationParticipant, world["part_a_id"])
        part_b = db.session.get(ConversationParticipant, world["part_b_id"])

        a_user_id, b_user_id = world["user_a_id"], world["user_b_id"]

        # A read everything (through message 5); B read only message 1.
        part_a.last_read_at = BASE + timedelta(minutes=4)
        part_b.last_read_at = BASE
        db.session.commit()

        # Swap the PKs via raw UPDATEs (two-step through a parking value to
        # dodge the unique-PK collision mid-swap).
        db.session.execute(
            db.text(
                "UPDATE conversation_participants SET id = :tmp WHERE id = :old"
            ),
            {"tmp": 999999, "old": part_a.id},
        )
        db.session.execute(
            db.text(
                "UPDATE conversation_participants SET id = :new WHERE id = :old"
            ),
            {"new": a_user_id, "old": part_b.id},
        )
        db.session.execute(
            db.text(
                "UPDATE conversation_participants SET id = :new WHERE id = :tmp"
            ),
            {"new": b_user_id, "tmp": 999999},
        )
        db.session.commit()

        # Guard: A's join row now carries B's user id as its PK, and vice
        # versa — the exact alignment that makes the buggy lookup resolve to
        # the wrong participant.
        conversation = db.session.get(Conversation, world["conversation_id"])
        by_user = {p.user_id: p for p in conversation.participants}
        assert by_user[a_user_id].id == b_user_id
        assert by_user[b_user_id].id == a_user_id

    conv_id = world["conversation_id"]

    # A read through message 5 → everything read.
    resp_a = client.get(
        f"/api/app/conversation/{conv_id}",
        headers=_auth_header(app, a_user_id),
    )
    assert resp_a.status_code == 200, resp_a.data
    flags_a = [m["isRead"] for m in resp_a.get_json()["messages"]]

    # B read only message 1 → first read, rest unread.
    resp_b = client.get(
        f"/api/app/conversation/{conv_id}",
        headers=_auth_header(app, b_user_id),
    )
    assert resp_b.status_code == 200, resp_b.data
    flags_b = [m["isRead"] for m in resp_b.get_json()["messages"]]

    assert flags_a == [True, True, True, True, True], (
        f"A read through message 5 but saw {flags_a} — read state was taken "
        f"from the wrong participant row"
    )
    assert flags_b == [True, False, False, False, False], (
        f"B read only message 1 but saw {flags_b} — read state was taken "
        f"from the wrong participant row"
    )
    assert flags_a != flags_b, (
        "both participants resolved to the same read state; the lookup is not "
        "keyed on the calling user"
    )
