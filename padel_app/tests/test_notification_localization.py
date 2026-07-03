"""
PAD-38 — Notification message localization.

Reminder / invite / waiting-list messages must render every placeholder fully
substituted in the platform's user-facing language (Portuguese):

  * ``{weekday}``  -> Portuguese weekday name (e.g. "quarta-feira"), never the
    English ``strftime("%A")`` output ("Wednesday").
  * ``{level}``    -> the class-name / modality; when the instance has no level
    it renders as an empty string, never the literal filler word "this".

These are tactical fixes only (a PT weekday map + resolving the stray "this"
default); full locale-driven i18n is deferred to PAD-39.

Run:
    pytest padel_app/tests/test_notification_localization.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db

# Reuse the seed helpers / IO patches from the reminder-flow suite.
from padel_app.tests.test_notification_reminder_flow import (
    _seed_coach_and_student,
    PATCHES,
)

# English weekday names that must NEVER leak into a rendered pt message.
_ENGLISH_WEEKDAYS = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]

# Portuguese weekday names, indexed the same way as datetime.weekday()
# (Monday == 0 ... Sunday == 6).
_PT_WEEKDAYS = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


def _seed_instance_without_level(app, coach_id, student_id, start_offset_hours=48):
    """Create a lesson + instance with NO level assigned. Returns instance_id."""
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

    with app.app_context():
        club = Club(name="PT Club", description="", location="Lisboa")
        db.session.add(club)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
        end = start + timedelta(hours=1)

        lesson = Lesson(
            title="Aula PT",
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
            level_id=None,  # <-- no level: exercises the {level} fallback
            notifications_enabled=True,
        )
        db.session.add(instance)
        db.session.flush()

        db.session.add(Association_CoachLessonInstance(
            coach_id=coach_id, lesson_instance_id=instance.id,
        ))
        db.session.add(Association_PlayerLessonInstance(
            player_id=student_id, lesson_instance_id=instance.id,
        ))
        db.session.commit()
        return instance.id, start


def _seed_pt_reminder_template(app, coach_id):
    """Persist a NotificationConfig with a Portuguese reminder template."""
    from padel_app.models.notification_config import NotificationConfig

    with app.app_context():
        NotificationConfig(
            coach_id=coach_id,
            auto_notify_enabled=False,
            message_templates={
                "reminder": "Olá {name}, tens aula de {level} esta {weekday} às {time}. Vens?",
            },
        ).create()


class TestNotificationLocalization:

    def test_reminder_weekday_is_portuguese_and_no_this_artifact(self, app):
        """A pt reminder for a level-less instance renders the Portuguese weekday
        and no "this"/raw-placeholder artifacts."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id, start = _seed_instance_without_level(
            app, ids["coach_id"], ids["student_id"], start_offset_hours=48
        )
        _seed_pt_reminder_template(app, ids["coach_id"])

        expected_pt_weekday = _PT_WEEKDAYS[start.weekday()]

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)

            msg = Message.query.filter_by(
                message_type="notification_reminder",
            ).first()
            assert msg is not None
            text = msg.text

            # (a) Portuguese weekday present, English weekday absent.
            assert expected_pt_weekday in text, (
                f"expected pt weekday '{expected_pt_weekday}' in: {text!r}"
            )
            for en in _ENGLISH_WEEKDAYS:
                assert en not in text, f"English weekday '{en}' leaked into: {text!r}"

            # (b) No stray "this" filler in the class-name slot.
            #     Template around {level} is "aula de {level} esta" — with no level
            #     that must collapse cleanly, never "aula de this esta".
            assert "aula de this" not in text, f"stray 'this' artifact in: {text!r}"

            # (c) No raw placeholder token survived interpolation.
            for token in ("{level}", "{weekday}", "{time}", "{name}"):
                assert token not in text, f"raw placeholder {token} in: {text!r}"

            # (d) An empty {level} must not leave a double space / ungrammatical gap.
            assert "  " not in text, f"double-space artifact in: {text!r}"
