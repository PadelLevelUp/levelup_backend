"""
PAD-67 — blank message templates must fall back to the built-in defaults.

Root cause covered here: ``NotificationConfig.get_message_templates`` merged the
coach's stored JSON straight over the defaults, so a key saved as ``""`` (the
coach cleared the textarea in Settings → Message templates) *won* the merge and
the engine delivered an empty chat bubble + empty push notification. The
reporter saw it on the "player says they're not coming" confirmation
(``reminder_declined``), but every template key had the same hole.

These tests exercise the real service code path against the SQLite ``app``
fixture; `publish` and `send_push_notification` are patched to avoid I/O.

Run:
    pytest padel_app/tests/test_notification_blank_template_fallback.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]

ALL_TEMPLATE_KEYS = [
    "invite",
    "confirm",
    "decline",
    "spot_filled",
    "reminder",
    "reminder_followup",
    "reminder_confirmed",
    "reminder_declined",
    "waiting_list_offer",
    "waiting_list_confirm",
    "waiting_list_placed",
]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_coach_and_student(app, language=None):
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player

    with app.app_context():
        coach_user = User(
            name="Blank Template Coach",
            username="blank-tpl-coach",
            email="blank-coach@test.com",
            password="hashed",
            status="active",
        )
        if language is not None:
            coach_user.language = language
        db.session.add(coach_user)

        student_user = User(
            name="Blank Template Student",
            username="blank-tpl-student",
            email="blank-student@test.com",
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


def _seed_instance(app, coach_id, student_id, start_offset_hours=48):
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.coach_levels import CoachLevel
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )

    with app.app_context():
        club = Club(name="Blank Tpl Club", description="", location="Test City")
        db.session.add(club)
        db.session.flush()

        level = CoachLevel(coach_id=coach_id, label="Beginner", code="B1", display_order=1)
        db.session.add(level)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
        end = start + timedelta(hours=1)

        lesson = Lesson(
            title="Blank Tpl Class",
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
            level_id=level.id,
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
        return instance.id


def _set_templates(app, coach_id, templates):
    """Persist a raw message_templates JSON blob for the coach."""
    from padel_app.services.notification_service import get_or_create_config

    with app.app_context():
        config = get_or_create_config(coach_id)
        config.message_templates = templates
        config.save()


def _messages(app, message_type=None):
    from padel_app.models.messages import Message

    with app.app_context():
        q = Message.query
        if message_type is not None:
            q = q.filter_by(message_type=message_type)
        return [m.text for m in q.order_by(Message.id).all()]


# ---------------------------------------------------------------------------
# Unit level — template resolution
# ---------------------------------------------------------------------------

class TestBlankTemplateResolution:
    """``get_message_templates`` / ``resolve_message_template`` never yield blanks."""

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t ", None, 0, [], {}])
    def test_blank_stored_template_falls_back_to_default(self, app, blank):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES_PT,
            NotificationConfig,
        )

        with app.app_context():
            config = NotificationConfig(
                coach_id=1,
                auto_notify_enabled=False,
                message_templates={"reminder_declined": blank},
            )
            resolved = config.get_message_templates("pt")

        assert resolved["reminder_declined"] == DEFAULT_MESSAGE_TEMPLATES_PT["reminder_declined"]
        assert resolved["reminder_declined"].strip()

    def test_every_key_falls_back_when_all_blank(self, app):
        """The whole template set cleared → every key still resolves to a default."""
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
            NotificationConfig,
        )

        with app.app_context():
            config = NotificationConfig(
                coach_id=1,
                auto_notify_enabled=False,
                message_templates={k: "" for k in ALL_TEMPLATE_KEYS},
            )
            pt = config.get_message_templates("pt")
            en = config.get_message_templates("en")

        for key in ALL_TEMPLATE_KEYS:
            assert pt[key] == DEFAULT_MESSAGE_TEMPLATES_PT[key], key
            assert en[key] == DEFAULT_MESSAGE_TEMPLATES[key], key
            assert pt[key].strip() and en[key].strip(), key

    def test_real_custom_template_still_wins(self, app):
        """The fallback must not clobber a genuinely customised template."""
        from padel_app.models.notification_config import NotificationConfig

        with app.app_context():
            config = NotificationConfig(
                coach_id=1,
                auto_notify_enabled=False,
                message_templates={
                    "reminder_declined": "Custom decline text",
                    "confirm": "   ",
                },
            )
            resolved = config.get_message_templates("pt")

        assert resolved["reminder_declined"] == "Custom decline text"
        assert resolved["confirm"].strip()

    def test_whitespace_only_template_is_treated_as_blank(self, app):
        """A template of spaces/newlines only is blank — it renders an empty bubble."""
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES_PT,
            resolve_message_template,
        )

        resolved = resolve_message_template({"decline": " \n  "}, "decline", "pt")
        assert resolved == DEFAULT_MESSAGE_TEMPLATES_PT["decline"]

    def test_resolve_handles_missing_key_and_empty_dict(self, app):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            resolve_message_template,
        )

        assert resolve_message_template({}, "confirm", "en") == DEFAULT_MESSAGE_TEMPLATES["confirm"]
        assert resolve_message_template(None, "confirm", "en") == DEFAULT_MESSAGE_TEMPLATES["confirm"]
        # Unknown key with no built-in default resolves to "" so callers skip sending.
        assert resolve_message_template({}, "not_a_real_key", "en") == ""

    def test_non_dict_stored_templates_are_ignored(self, app):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES_PT,
            NotificationConfig,
        )

        with app.app_context():
            config = NotificationConfig(
                coach_id=1, auto_notify_enabled=False, message_templates="not-a-dict",
            )
            resolved = config.get_message_templates("pt")

        assert resolved == DEFAULT_MESSAGE_TEMPLATES_PT

    def test_unknown_custom_keys_are_preserved(self, app):
        from padel_app.models.notification_config import NotificationConfig

        with app.app_context():
            config = NotificationConfig(
                coach_id=1,
                auto_notify_enabled=False,
                message_templates={"my_own_key": "hello"},
            )
            resolved = config.get_message_templates("pt")

        assert resolved["my_own_key"] == "hello"


# ---------------------------------------------------------------------------
# Integration — the reported flow: player declines a reminder
# ---------------------------------------------------------------------------

class TestDeclineConfirmationNeverEmpty:

    def _decline(self, app, ids, instance_id):
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)
                respond_to_reminder(instance_id, "no", ids["student_user_id"], now=now)

    def test_blank_reminder_declined_template_sends_default_not_empty(self, app):
        """PAD-67 core regression: blank ``reminder_declined`` → default text, never ''."""
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])
        _set_templates(app, ids["coach_id"], {"reminder_declined": ""})

        self._decline(app, ids, instance_id)

        texts = _messages(app, message_type="text")
        assert texts, "no confirmation message was sent at all"
        assert DEFAULT_MESSAGE_TEMPLATES_PT["reminder_declined"] in texts
        for t in _messages(app):
            assert (t or "").strip(), f"an empty message was delivered: {t!r}"

    def test_blank_reminder_confirmed_template_sends_default_not_empty(self, app):
        """Same hole on the "yes, I'm coming" reply."""
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])
        _set_templates(app, ids["coach_id"], {"reminder_confirmed": "   "})

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)
                respond_to_reminder(instance_id, "yes", ids["student_user_id"], now=now)

        texts = _messages(app, message_type="text")
        assert DEFAULT_MESSAGE_TEMPLATES_PT["reminder_confirmed"] in texts

    def test_custom_decline_template_is_still_used(self, app):
        """The fallback must not shadow a coach who did author a decline message."""
        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])
        _set_templates(app, ids["coach_id"], {"reminder_declined": "Ok, ficas de fora!"})

        self._decline(app, ids, instance_id)

        assert "Ok, ficas de fora!" in _messages(app, message_type="text")

    def test_no_message_is_ever_created_with_blank_text(self, app):
        """Every template blank → still zero empty messages anywhere."""
        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])
        _set_templates(app, ids["coach_id"], {k: "" for k in ALL_TEMPLATE_KEYS})

        self._decline(app, ids, instance_id)

        all_texts = _messages(app)
        assert all_texts, "expected at least a reminder + a decline confirmation"
        for t in all_texts:
            assert (t or "").strip(), f"an empty message was delivered: {t!r}"

    def test_blank_reminder_template_still_sends_reminder(self, app):
        """Blank ``reminder`` → the scheduled reminder itself falls back too."""
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT
        from padel_app.services.notification_service import send_class_reminders

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])
        _set_templates(app, ids["coach_id"], {"reminder": "  "})

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=datetime.utcnow())

        texts = _messages(app, message_type="notification_reminder")
        assert len(texts) == 1
        expected_prefix = DEFAULT_MESSAGE_TEMPLATES_PT["reminder"].split("{name}")[0]
        assert texts[0].startswith(expected_prefix)
        assert texts[0].strip()


# ---------------------------------------------------------------------------
# Backstop — _send_system_message refuses blank bodies
# ---------------------------------------------------------------------------

class TestEmptyMessageBackstop:

    def test_send_system_message_skips_blank_text(self, app):
        from padel_app.models.messages import Message
        from padel_app.services.notification_service import _send_system_message

        ids = _seed_coach_and_student(app)

        with app.app_context():
            before = Message.query.count()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = _send_system_message(
                    ids["coach_user_id"], ids["student_user_id"], "   ",
                )
            assert result is None
            assert Message.query.count() == before

    def test_send_system_message_still_sends_real_text(self, app):
        from padel_app.models.messages import Message
        from padel_app.services.notification_service import _send_system_message

        ids = _seed_coach_and_student(app)

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = _send_system_message(
                    ids["coach_user_id"], ids["student_user_id"], "hello",
                )
            assert result is not None
            assert Message.query.get(result.id).text == "hello"


# ---------------------------------------------------------------------------
# API surface — GET config never hands the UI a blank template
# ---------------------------------------------------------------------------

class TestConfigDictResolvesBlanks:

    def test_get_config_dict_returns_resolved_templates(self, app):
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT
        from padel_app.services.notification_service import get_config_dict

        ids = _seed_coach_and_student(app)
        _set_templates(app, ids["coach_id"], {"decline": "", "confirm": "Custom confirm"})

        with app.app_context():
            cfg = get_config_dict(ids["coach_id"])

        assert cfg["messageTemplates"]["decline"] == DEFAULT_MESSAGE_TEMPLATES_PT["decline"]
        assert cfg["messageTemplates"]["confirm"] == "Custom confirm"
        for key, value in cfg["messageTemplates"].items():
            assert value.strip(), key

    def test_blank_template_is_not_rewritten_in_storage(self, app):
        """Resolution is read-time only — the coach's stored blank stays blank."""
        from padel_app.services.notification_service import (
            get_config_dict,
            get_or_create_config,
        )

        ids = _seed_coach_and_student(app)
        _set_templates(app, ids["coach_id"], {"decline": ""})

        with app.app_context():
            get_config_dict(ids["coach_id"])
            config = get_or_create_config(ids["coach_id"])
            assert config.message_templates["decline"] == ""
