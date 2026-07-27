"""
PAD-75 — Notify enrolled students when a coach cancels a class.

When a coach removes/cancels a scheduled class that has enrolled student(s),
each enrolled student must receive an in-app cancellation notification (a system
message in their coach<->student conversation), delivered through the same
channel as other class notifications. No message is sent when the class has no
enrolled students.

`publish` and `send_push_notification` are patched to avoid external side
effects, mirroring the other notification integration tests.

Run:
    pytest padel_app/tests/test_notification_class_cancellation.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]


# ---------------------------------------------------------------------------
# Seed helpers (mirrors test_notification_integration.py)
# ---------------------------------------------------------------------------

def _create_user(name, username, language=None, status="active"):
    from padel_app.models.users import User
    u = User(name=name, username=username, email=f"{username}@test.com",
             password="hashed", status=status)
    if language is not None:
        u.language = language
    db.session.add(u)
    db.session.flush()
    return u


def _create_coach(user):
    from padel_app.models.coaches import Coach
    c = Coach(user_id=user.id)
    db.session.add(c)
    db.session.flush()
    return c


def _create_player(user):
    from padel_app.models.players import Player
    p = Player(user_id=user.id)
    db.session.add(p)
    db.session.flush()
    return p


def _create_level(coach, label="Beg", code="B1"):
    from padel_app.models.coach_levels import CoachLevel
    lv = CoachLevel(coach_id=coach.id, label=label, code=code, display_order=1)
    db.session.add(lv)
    db.session.flush()
    return lv


def _create_instance(coach, level, enrolled_players=(), start_offset_hours=48, max_players=4):
    """Create a Lesson + LessonInstance, enrol players, wire coach. Returns instance."""
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

    club = Club(name="Test Club", description="", location="City")
    db.session.add(club)
    db.session.flush()

    start = datetime.utcnow() + timedelta(hours=start_offset_hours)
    lesson = Lesson(title="Test Class", start_datetime=start,
                    end_datetime=start + timedelta(hours=1),
                    is_recurring=False, type="academy", max_players=max_players,
                    color="#000", status="active", club_id=club.id)
    db.session.add(lesson)
    db.session.flush()

    instance = LessonInstance(
        lesson_id=lesson.id, start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        max_players=max_players, status="scheduled",
        level_id=level.id, notifications_enabled=True,
    )
    db.session.add(instance)
    db.session.flush()

    db.session.add(Association_CoachLessonInstance(coach_id=coach.id,
                                                    lesson_instance_id=instance.id))
    for player in enrolled_players:
        db.session.add(Association_PlayerLessonInstance(player_id=player.id,
                                                         lesson_instance_id=instance.id))
    db.session.commit()
    return instance


def _remove_payload(instance):
    return {
        "scope": "single",
        "event": {
            "model": "LessonInstance",
            "originalId": instance.id,
            "date": instance.start_datetime.strftime("%Y-%m-%d"),
        },
    }


def _cancellation_messages():
    """All Message rows flagged as class-cancellation notifications."""
    from padel_app.models.messages import Message
    return [
        m for m in Message.query.all()
        if m.msg_metadata and m.msg_metadata.get("classCancellation")
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClassCancellationNotifications:

    def test_cancel_class_notifies_every_enrolled_student(self, app):
        """Cancelling a class with N enrolled students sends exactly N notifications."""
        from padel_app.services.lesson_service import remove_class_service

        with app.app_context():
            coach_user = _create_user("Coach", "cancel-coach")
            coach = _create_coach(coach_user)
            level = _create_level(coach)

            s1 = _create_player(_create_user("Ann", "cancel-s1"))
            s2 = _create_player(_create_user("Bob", "cancel-s2"))
            instance = _create_instance(coach, level, enrolled_players=(s1, s2))

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result, status = remove_class_service(_remove_payload(instance))

            assert status == 200, result

            msgs = _cancellation_messages()
            assert len(msgs) == 2, f"expected 2 cancellation messages, got {len(msgs)}"
            # Every notification is sent by the coach, and carries some text.
            assert all(m.sender_id == coach_user.id for m in msgs)
            assert all((m.text or "").strip() for m in msgs)
            # One per distinct enrolled student.
            recipient_convs = {m.conversation_id for m in msgs}
            assert len(recipient_convs) == 2

    def test_cancel_class_with_no_students_sends_nothing(self, app):
        """Cancelling a class with no enrolled students sends no notifications."""
        from padel_app.services.lesson_service import remove_class_service

        with app.app_context():
            coach_user = _create_user("Coach", "empty-coach")
            coach = _create_coach(coach_user)
            level = _create_level(coach)
            instance = _create_instance(coach, level, enrolled_players=())

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result, status = remove_class_service(_remove_payload(instance))

            assert status == 200, result
            assert _cancellation_messages() == []

    def test_cancellation_copy_localized_to_coach_locale(self, app):
        """A Portuguese-locale coach's students get the PT cancellation copy."""
        from padel_app.services.lesson_service import remove_class_service

        with app.app_context():
            coach_user = _create_user("Treinador", "pt-coach", language="pt")
            coach = _create_coach(coach_user)
            level = _create_level(coach)
            student = _create_player(_create_user("Aluno", "pt-student", language="pt"))
            instance = _create_instance(coach, level, enrolled_players=(student,))

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result, status = remove_class_service(_remove_payload(instance))

            assert status == 200, result
            msgs = _cancellation_messages()
            assert len(msgs) == 1
            # PT copy — "cancelada" is unique to the Portuguese template.
            assert "cancelada" in msgs[0].text.lower()
