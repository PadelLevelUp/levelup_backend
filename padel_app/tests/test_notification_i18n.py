"""
Integration tests for locale-aware notification rendering (PAD-39).

These exercise the full send_class_reminders path with a coach whose
user.language is set to "pt" or "en", asserting the weekday name and default
template are rendered in the coach's locale. Also verifies the persisted
default language and the missing-level filler fix (PAD-38).

Run:
    pytest padel_app/tests/test_notification_i18n.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]


# ---------------------------------------------------------------------------
# Seed helpers (adapted from test_notification_reminder_flow.py)
# ---------------------------------------------------------------------------

def _seed_coach_and_student(app, language="pt"):
    """Create and persist a coach user (with language), coach, student user, player."""
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player

    with app.app_context():
        coach_user = User(
            name="Test Coach",
            username="i18n-coach",
            email="i18n-coach@test.com",
            password="hashed",
            status="active",
            language=language,
        )
        db.session.add(coach_user)

        student_user = User(
            name="Test Student",
            username="i18n-student",
            email="i18n-student@test.com",
            password="hashed",
            status="active",
        )
        db.session.add(student_user)
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)

        student = Player(user_id=student_user.id)
        db.session.add(student)
        db.session.flush()

        db.session.commit()
        return {
            "coach_user_id": coach_user.id,
            "student_user_id": student_user.id,
            "coach_id": coach.id,
            "student_id": student.id,
        }


def _next_weekday(weekday: int, *, min_hours_ahead: int = 48) -> datetime:
    """Return a future datetime at 10:00 on the given weekday (Mon=0..Sun=6),
    at least ``min_hours_ahead`` hours from now so reminders send."""
    base = datetime.utcnow() + timedelta(hours=min_hours_ahead)
    days_ahead = (weekday - base.weekday()) % 7
    target = base + timedelta(days=days_ahead)
    return target.replace(hour=10, minute=0, second=0, microsecond=0)


def _seed_instance(app, coach_id, student_id, *, start_datetime, with_level=True):
    """Create a lesson and instance starting at an explicit datetime. Returns instance_id."""
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.coach_levels import CoachLevel
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

    with app.app_context():
        club = Club(name="Test Club", description="", location="Test City")
        db.session.add(club)
        db.session.flush()

        level_id = None
        if with_level:
            level = CoachLevel(coach_id=coach_id, label="Beginner", code="B1", display_order=1)
            db.session.add(level)
            db.session.flush()
            level_id = level.id

        start = start_datetime
        end = start + timedelta(hours=1)

        lesson = Lesson(
            title="Test Class",
            start_datetime=start,
            end_datetime=end,
            is_recurring=False,
            type="academy",
            max_players=4,
            color="#000000",
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(
            lesson_id=lesson.id,
            start_datetime=start,
            end_datetime=end,
            max_players=4,
            status="scheduled",
            level_id=level_id,
            notifications_enabled=True,
        )
        db.session.add(instance)
        db.session.flush()

        db.session.add(Association_CoachLessonInstance(
            coach_id=coach_id,
            lesson_instance_id=instance.id,
        ))
        db.session.add(Association_PlayerLessonInstance(
            player_id=student_id,
            lesson_instance_id=instance.id,
        ))
        db.session.commit()
        return instance.id


def _reminder_text(app):
    from padel_app.models.messages import Message
    with app.app_context():
        msgs = Message.query.filter_by(message_type="notification_reminder").all()
        assert len(msgs) == 1
        return msgs[0].text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reminder_weekday_portuguese(app):
    """A PT coach's reminder renders the weekday name in Portuguese and no
    English weekday / 'this' filler."""
    from babel.dates import format_date
    from padel_app.services.notification_service import send_class_reminders

    ids = _seed_coach_and_student(app, language="pt")
    wednesday = _next_weekday(2)  # Wed=2
    instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_datetime=wednesday)

    with app.app_context():
        with patch(PATCHES[0]), patch(PATCHES[1]):
            send_class_reminders(instance_id, now=datetime.utcnow())

    text = _reminder_text(app)
    expected_pt = format_date(wednesday, format="EEEE", locale="pt")  # e.g. "quarta-feira"
    expected_en = format_date(wednesday, format="EEEE", locale="en")  # "Wednesday"
    assert expected_pt in text
    assert expected_en not in text
    assert "this" not in text
    assert "{weekday}" not in text and "{level}" not in text


def test_reminder_weekday_english(app):
    """An EN coach's reminder renders the English weekday name."""
    from babel.dates import format_date
    from padel_app.services.notification_service import send_class_reminders

    ids = _seed_coach_and_student(app, language="en")
    wednesday = _next_weekday(2)
    instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_datetime=wednesday)

    with app.app_context():
        with patch(PATCHES[0]), patch(PATCHES[1]):
            send_class_reminders(instance_id, now=datetime.utcnow())

    text = _reminder_text(app)
    expected_en = format_date(wednesday, format="EEEE", locale="en")
    assert expected_en in text
    assert "{weekday}" not in text and "{level}" not in text


def test_default_language_is_pt(app):
    """A User created without an explicit language defaults to 'pt'."""
    from padel_app.models.users import User

    with app.app_context():
        user = User(
            name="No Lang",
            username="nolang-user",
            email="nolang@test.com",
            password="hashed",
            status="active",
        )
        db.session.add(user)
        db.session.commit()

        fetched = User.query.get(user.id)
        assert fetched.language == "pt"


def test_missing_level_renders_empty_no_filler(app):
    """An instance with no level renders the reminder without the literal 'this'
    filler and with no raw {level}/{weekday} tokens."""
    from padel_app.services.notification_service import send_class_reminders

    ids = _seed_coach_and_student(app, language="pt")
    wednesday = _next_weekday(2)
    instance_id = _seed_instance(
        app, ids["coach_id"], ids["student_id"], start_datetime=wednesday, with_level=False
    )

    with app.app_context():
        with patch(PATCHES[0]), patch(PATCHES[1]):
            send_class_reminders(instance_id, now=datetime.utcnow())

    text = _reminder_text(app)
    assert "this" not in text
    assert "{level}" not in text
    assert "{weekday}" not in text
