"""
PAD-36 — Role-based visibility of class detail payload.

A student who views a class instance must only see their OWN participation:
their own presence/absence row, and no other students' data (participants,
presences, or open-spot notification/invitation recipients). A coach keeps the
full view.

Covered spec: classes.detail-visibility
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
def class_scenario(app):
    """
    Build: 1 coach, 2 students (alice, bob) both enrolled in one lesson instance.
    Bob is marked absent (unjustified). An open-spot notification was sent to bob.
    Returns the ids needed by the tests.
    """
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from padel_app.models.notification_event import NotificationEvent
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    from datetime import datetime, timedelta

    with app.app_context():
        coach_user = User(name="Coach", username="vis_coach", password="x")
        alice_user = User(name="Alice", username="vis_alice", password="x")
        bob_user = User(name="Bob", username="vis_bob", password="x")
        db.session.add_all([coach_user, alice_user, bob_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        alice = Player(user_id=alice_user.id)
        bob = Player(user_id=bob_user.id)
        db.session.add_all([coach, alice, bob])
        db.session.flush()

        club = Club(name="Vis Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        start = datetime.utcnow().replace(microsecond=0) + timedelta(days=1)
        lesson = Lesson(
            title="Visibility Class",
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
        db.session.add_all(
            [
                Association_PlayerLessonInstance(
                    player_id=alice.id, lesson_instance_id=instance.id
                ),
                Association_PlayerLessonInstance(
                    player_id=bob.id, lesson_instance_id=instance.id
                ),
            ]
        )

        # Presences: Alice present, Bob absent (unjustified)
        db.session.add_all(
            [
                Presence(
                    lesson_instance_id=instance.id,
                    player_id=alice.id,
                    status="present",
                    invited=True,
                    confirmed=True,
                ),
                Presence(
                    lesson_instance_id=instance.id,
                    player_id=bob.id,
                    status="absent",
                    justification="unjustified",
                    invited=True,
                    confirmed=False,
                ),
            ]
        )

        # Open-spot notification sent to Bob
        db.session.add(
            NotificationEvent(
                coach_id=coach.id,
                lesson_instance_id=instance.id,
                player_id=bob.id,
                type="auto",
                status="sent",
            )
        )

        db.session.commit()

        return {
            "coach_user_id": coach_user.id,
            "alice_user_id": alice_user.id,
            "alice_player_id": alice.id,
            "bob_player_id": bob.id,
            "instance_id": instance.id,
        }


def _get_detail(client, app, user_id, instance_id):
    return client.post(
        f"/api/app/class_instance?model=lessoninstance&id={instance_id}",
        headers=_auth_header(app, user_id),
    )


def test_coach_sees_full_class_detail(client, app, class_scenario):
    resp = _get_detail(
        client, app, class_scenario["coach_user_id"], class_scenario["instance_id"]
    )
    assert resp.status_code == 200
    data = resp.get_json()

    participant_ids = {p["id"] for p in data["participants"]}
    assert class_scenario["alice_player_id"] in participant_ids
    assert class_scenario["bob_player_id"] in participant_ids

    presence_player_ids = {pr["playerId"] for pr in data["presences"]}
    assert class_scenario["alice_player_id"] in presence_player_ids
    assert class_scenario["bob_player_id"] in presence_player_ids

    invitation_player_ids = {int(inv["playerId"]) for inv in data["invitations"]}
    assert class_scenario["bob_player_id"] in invitation_player_ids


def test_student_sees_only_own_data(client, app, class_scenario):
    resp = _get_detail(
        client, app, class_scenario["alice_user_id"], class_scenario["instance_id"]
    )
    assert resp.status_code == 200
    data = resp.get_json()

    alice_id = class_scenario["alice_player_id"]
    bob_id = class_scenario["bob_player_id"]

    # Participants: never expose other students
    participant_ids = {p["id"] for p in data.get("participants", [])}
    assert bob_id not in participant_ids
    assert participant_ids.issubset({alice_id})

    # Presences: only Alice's own row
    presence_player_ids = {pr["playerId"] for pr in data.get("presences", [])}
    assert bob_id not in presence_player_ids
    assert presence_player_ids.issubset({alice_id})

    # Invitations: never reveal notifications sent to other players
    invitation_player_ids = {
        int(inv["playerId"]) for inv in data.get("invitations", [])
    }
    assert bob_id not in invitation_player_ids
    assert invitation_player_ids.issubset({alice_id})

    # Student still sees shared, non-sensitive class info
    assert data["name"] == "Visibility Class"
    assert "coachId" in data


def test_lesson_instance_detail_scopes_presences_for_student(
    client, app, class_scenario
):
    """GET /lesson_instance/<id> must be authenticated and student-scoped."""
    resp = client.get(
        f"/api/app/lesson_instance/{class_scenario['instance_id']}",
        headers=_auth_header(app, class_scenario["alice_user_id"]),
    )
    assert resp.status_code == 200
    presence_player_ids = {pr["playerId"] for pr in resp.get_json()["presences"]}
    assert class_scenario["bob_player_id"] not in presence_player_ids
    assert presence_player_ids.issubset({class_scenario["alice_player_id"]})


def test_lesson_instance_presences_requires_auth(client, app, class_scenario):
    """GET /lesson_instance/<id>/presences must reject unauthenticated access."""
    resp = client.get(
        f"/api/app/lesson_instance/{class_scenario['instance_id']}/presences"
    )
    assert resp.status_code == 401
