"""
PAD-72 — The class-detail guest ("invited") list is keyed by STUDENT.

The invitation engine legitimately writes one NotificationEvent per invite
SENT: multi-round matching, a manual invite on top of an automatic one, or a
re-invite after a decline. The coach's guest list must still show each student
exactly once, carrying that student's most meaningful invite state.

Covered spec: calendar.event-detail (rules 6-9)
"""
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Pure de-duplication logic
# ---------------------------------------------------------------------------

class _Event:
    """Minimal stand-in for a NotificationEvent row."""

    def __init__(self, id, player_id, status, round_number=1):
        self.id = id
        self.player_id = player_id
        self.status = status
        self.round_number = round_number

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"_Event(id={self.id}, player={self.player_id}, status={self.status})"


def test_dedupe_keeps_one_row_per_student():
    from padel_app.serializers.lesson import dedupe_invitation_events

    events = [
        _Event(1, player_id=10, status="sent"),
        _Event(2, player_id=20, status="sent"),
        _Event(3, player_id=10, status="sent", round_number=2),
        _Event(4, player_id=10, status="sent", round_number=3),
    ]

    result = dedupe_invitation_events(events)

    assert [e.player_id for e in result] == [10, 20]


def test_dedupe_preserves_first_appearance_order():
    from padel_app.serializers.lesson import dedupe_invitation_events

    events = [
        _Event(1, player_id=30, status="sent"),
        _Event(2, player_id=10, status="sent"),
        _Event(3, player_id=20, status="sent"),
        _Event(4, player_id=30, status="confirmed", round_number=2),
    ]

    result = dedupe_invitation_events(events)

    assert [e.player_id for e in result] == [30, 10, 20]


@pytest.mark.parametrize(
    "statuses, expected",
    [
        # An actual response beats a still-pending invite, whichever came first.
        (["sent", "confirmed"], "confirmed"),
        (["confirmed", "sent"], "confirmed"),
        (["sent", "expired"], "expired"),
        (["expired", "sent"], "expired"),
        # A confirmation outranks a decline.
        (["expired", "confirmed"], "confirmed"),
        # Delivered beats not-yet-delivered.
        (["queued", "sent"], "sent"),
        (["sent", "queued"], "sent"),
    ],
)
def test_dedupe_prefers_the_most_meaningful_status(statuses, expected):
    from padel_app.serializers.lesson import dedupe_invitation_events

    events = [
        _Event(i + 1, player_id=10, status=status, round_number=i + 1)
        for i, status in enumerate(statuses)
    ]

    result = dedupe_invitation_events(events)

    assert len(result) == 1
    assert result[0].status == expected


def test_dedupe_breaks_status_ties_with_the_most_recent_record():
    from padel_app.serializers.lesson import dedupe_invitation_events

    events = [
        _Event(7, player_id=10, status="sent", round_number=1),
        _Event(8, player_id=10, status="sent", round_number=3),
        _Event(9, player_id=10, status="sent", round_number=2),
    ]

    result = dedupe_invitation_events(events)

    assert len(result) == 1
    # Later round wins over a later row id.
    assert result[0].id == 8


def test_dedupe_of_empty_and_single_lists():
    from padel_app.serializers.lesson import dedupe_invitation_events

    assert dedupe_invitation_events([]) == []
    only = _Event(1, player_id=10, status="sent")
    assert dedupe_invitation_events([only]) == [only]


# ---------------------------------------------------------------------------
# End-to-end through the class-detail payload
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def repeat_invite_scenario(app):
    """One coach, one enrolled student (Alice) and one repeatedly-invited
    student (Bob) with four NotificationEvents for the same instance."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_event import NotificationEvent
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )

    with app.app_context():
        coach_user = User(name="Coach", username="dedupe_coach", password="x")
        alice_user = User(name="Alice", username="dedupe_alice", password="x")
        bob_user = User(name="Bob", username="dedupe_bob", password="x")
        db.session.add_all([coach_user, alice_user, bob_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        alice = Player(user_id=alice_user.id)
        bob = Player(user_id=bob_user.id)
        db.session.add_all([coach, alice, bob])
        db.session.flush()

        club = Club(name="Dedupe Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        start = datetime.utcnow().replace(microsecond=0) + timedelta(days=1)
        lesson = Lesson(
            title="Dedupe Class",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=False,
            type="academy",
            max_players=6,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(
            lesson_id=lesson.id,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            max_players=6,
            status="scheduled",
            original_lesson_occurence_date=start.date(),
        )
        db.session.add(instance)
        db.session.flush()

        db.session.add(
            Association_CoachLessonInstance(
                coach_id=coach.id, lesson_instance_id=instance.id
            )
        )
        db.session.add(
            Association_PlayerLessonInstance(
                player_id=alice.id, lesson_instance_id=instance.id
            )
        )

        # Bob got invited four times for this one class; Alice once.
        db.session.add_all(
            [
                NotificationEvent(
                    coach_id=coach.id,
                    lesson_instance_id=instance.id,
                    player_id=bob.id,
                    type="auto",
                    round_number=1,
                    status="expired",
                ),
                NotificationEvent(
                    coach_id=coach.id,
                    lesson_instance_id=instance.id,
                    player_id=bob.id,
                    type="auto",
                    round_number=2,
                    status="sent",
                ),
                NotificationEvent(
                    coach_id=coach.id,
                    lesson_instance_id=instance.id,
                    player_id=alice.id,
                    type="manual",
                    round_number=1,
                    status="sent",
                ),
                NotificationEvent(
                    coach_id=coach.id,
                    lesson_instance_id=instance.id,
                    player_id=bob.id,
                    type="manual",
                    round_number=1,
                    status="confirmed",
                ),
                NotificationEvent(
                    coach_id=coach.id,
                    lesson_instance_id=instance.id,
                    player_id=bob.id,
                    type="manual",
                    round_number=1,
                    status="sent",
                ),
            ]
        )
        db.session.commit()

        return {
            "coach_user_id": coach_user.id,
            "alice_player_id": alice.id,
            "bob_player_id": bob.id,
            "instance_id": instance.id,
        }


def test_class_detail_lists_each_invited_student_once(
    client, app, repeat_invite_scenario
):
    resp = client.post(
        "/api/app/class_instance"
        f"?model=lessoninstance&id={repeat_invite_scenario['instance_id']}",
        headers=_auth_header(app, repeat_invite_scenario["coach_user_id"]),
    )
    assert resp.status_code == 200
    invitations = resp.get_json()["invitations"]

    player_ids = [int(inv["playerId"]) for inv in invitations]
    assert len(player_ids) == len(set(player_ids)), invitations
    assert sorted(player_ids) == sorted(
        [
            repeat_invite_scenario["alice_player_id"],
            repeat_invite_scenario["bob_player_id"],
        ]
    )

    bob_entry = next(
        inv
        for inv in invitations
        if int(inv["playerId"]) == repeat_invite_scenario["bob_player_id"]
    )
    # Bob answered "yes" on one of his invites — that beats the pending ones.
    assert bob_entry["status"] == "confirmed"
    assert bob_entry["playerName"] == "Bob"
    # The surviving row keeps a real NotificationEvent id so coach actions
    # (coach_respond) still resolve.
    assert isinstance(bob_entry["id"], int)
