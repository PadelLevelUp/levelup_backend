"""
PAD-67 follow-up — built-in default templates render in the RECIPIENT's language.

The blank-template fix (see ``test_notification_blank_template_fallback.py``)
resolved every default against the *coach's* configured locale, because that is
the only locale the sending code had on hand. Result: an English-speaking
student of a Portuguese coach received Portuguese defaults, and a
Portuguese-speaking student of an English coach received English ones.

The rule these tests pin down:

* a **built-in default** is ours, so it is delivered in the *recipient's*
  language (``User.language`` — the column ``PATCH /api/auth/me`` writes);
* a **coach's custom template** is the coach's own prose and is delivered
  verbatim to everyone, never translated;
* the fallback chain for a default is
  ``recipient's language → coach's configured locale → DEFAULT_LOCALE``.

These tests exercise the real service code path against the SQLite ``app``
fixture; `publish` and `send_push_notification` are patched to avoid I/O.

Run:
    pytest padel_app/tests/test_notification_template_recipient_locale.py -v
"""

import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_coach_and_students(app, coach_language=None, student_languages=(None,)):
    """Seed one coach and ``len(student_languages)`` students.

    ``None`` leaves ``User.language`` at the column default; pass ``""`` to
    model a user whose language was never set (legacy row).
    """
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player

    with app.app_context():
        coach_user = User(
            name="Locale Coach",
            username="locale-coach",
            email="locale-coach@test.com",
            password="hashed",
            status="active",
        )
        if coach_language is not None:
            coach_user.language = coach_language
        db.session.add(coach_user)
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        student_user_ids = []
        student_ids = []
        for idx, lang in enumerate(student_languages):
            student_user = User(
                name=f"Locale Student {idx}",
                username=f"locale-student-{idx}",
                email=f"locale-student-{idx}@test.com",
                password="hashed",
                status="active",
            )
            if lang is not None:
                student_user.language = lang
            db.session.add(student_user)
            db.session.flush()
            student = Player(user_id=student_user.id)
            db.session.add(student)
            db.session.flush()
            student_user_ids.append(student_user.id)
            student_ids.append(student.id)

        db.session.commit()

        return {
            "coach_user_id": coach_user.id,
            "coach_id": coach.id,
            "student_user_ids": student_user_ids,
            "student_ids": student_ids,
            # Convenience aliases for the single-student cases.
            "student_user_id": student_user_ids[0],
            "student_id": student_ids[0],
        }


def _seed_instance(app, coach_id, student_ids, start_offset_hours=48):
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

    if isinstance(student_ids, int):
        student_ids = [student_ids]

    with app.app_context():
        club = Club(name="Locale Club", description="", location="Test City")
        db.session.add(club)
        db.session.flush()

        level = CoachLevel(coach_id=coach_id, label="Beginner", code="B1", display_order=1)
        db.session.add(level)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
        end = start + timedelta(hours=1)

        lesson = Lesson(
            title="Locale Class",
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
        for student_id in student_ids:
            db.session.add(Association_PlayerLessonInstance(
                player_id=student_id, lesson_instance_id=instance.id,
            ))
        db.session.commit()
        return instance.id


def _set_templates(app, coach_id, templates):
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


def _decline(app, ids, instance_id, acting_user_id=None):
    from padel_app.services.notification_service import respond_to_reminder

    with app.app_context():
        with patch(PATCHES[0]), patch(PATCHES[1]):
            respond_to_reminder(
                instance_id,
                "no",
                acting_user_id or ids["student_user_id"],
                now=datetime.utcnow(),
            )


# ---------------------------------------------------------------------------
# Default-template parity — guards against future drift
# ---------------------------------------------------------------------------

class TestDefaultTemplateSetsAreEquivalent:
    """en and pt defaults must stay key-for-key and placeholder-for-placeholder
    identical: a key present in only one set silently degrades to a blank (or
    wrong-language) message for half the users, and a placeholder that exists in
    one set only renders a broken sentence in that language alone."""

    def test_both_default_sets_have_identical_keys(self):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        assert set(DEFAULT_MESSAGE_TEMPLATES) == set(DEFAULT_MESSAGE_TEMPLATES_PT)

    def test_both_default_sets_have_identical_placeholders(self):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        for key in DEFAULT_MESSAGE_TEMPLATES:
            en = set(PLACEHOLDER_RE.findall(DEFAULT_MESSAGE_TEMPLATES[key]))
            pt = set(PLACEHOLDER_RE.findall(DEFAULT_MESSAGE_TEMPLATES_PT[key]))
            assert en == pt, f"placeholder drift on {key!r}: en={en} pt={pt}"

    def test_no_default_is_blank_in_either_language(self):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        for templates in (DEFAULT_MESSAGE_TEMPLATES, DEFAULT_MESSAGE_TEMPLATES_PT):
            for key, text in templates.items():
                assert isinstance(text, str) and text.strip(), key

    def test_the_two_sets_are_actually_different_text(self):
        """Catches a key copy-pasted from en into the pt set untranslated."""
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        for key in DEFAULT_MESSAGE_TEMPLATES:
            assert DEFAULT_MESSAGE_TEMPLATES[key] != DEFAULT_MESSAGE_TEMPLATES_PT[key], (
                f"{key!r} is identical in both default sets — left untranslated?"
            )


# ---------------------------------------------------------------------------
# Unit level — the resolver and the fallback chain
# ---------------------------------------------------------------------------

class TestResolverPicksRecipientLanguage:

    def test_default_uses_recipient_locale_not_coach_locale(self):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            resolve_message_template,
        )

        resolved = resolve_message_template({}, "decline", "pt", recipient_locale="en")
        assert resolved == DEFAULT_MESSAGE_TEMPLATES["decline"]
        assert resolved.locale == "en"

    def test_default_falls_back_to_coach_locale_when_recipient_unknown(self):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            resolve_message_template,
        )

        for unknown in (None, "", "   "):
            resolved = resolve_message_template({}, "decline", "en", recipient_locale=unknown)
            assert resolved == DEFAULT_MESSAGE_TEMPLATES["decline"]
            assert resolved.locale == "en"

    def test_default_falls_back_to_app_default_when_nothing_is_known(self):
        from padel_app.models.notification_config import (
            DEFAULT_LOCALE,
            DEFAULT_MESSAGE_TEMPLATES_PT,
            resolve_message_template,
        )

        resolved = resolve_message_template({}, "decline", None, recipient_locale=None)
        assert DEFAULT_LOCALE == "pt"
        assert resolved == DEFAULT_MESSAGE_TEMPLATES_PT["decline"]
        assert resolved.locale == "pt"

    def test_custom_template_is_never_translated(self):
        from padel_app.models.notification_config import resolve_message_template

        resolved = resolve_message_template(
            {"decline": "Sem stress, para a próxima!"},
            "decline",
            "pt",
            recipient_locale="en",
        )
        assert resolved == "Sem stress, para a próxima!"
        # Its wording is the coach's, so placeholders render in the coach's locale.
        assert resolved.locale == "pt"

    @pytest.mark.parametrize(
        "tag,expected",
        [("pt", "pt"), ("PT", "pt"), ("pt-PT", "pt"), ("en", "en"), ("en-GB", "en"),
         ("", None), ("   ", None), (None, None), (7, None)],
    )
    def test_normalize_locale(self, tag, expected):
        from padel_app.models.notification_config import normalize_locale

        assert normalize_locale(tag) is expected or normalize_locale(tag) == expected


class TestRecipientLocaleChain:

    def test_recipient_language_wins(self, app):
        from padel_app.services.notification_service import _recipient_locale

        ids = _seed_coach_and_students(app, coach_language="pt", student_languages=["en"])
        with app.app_context():
            assert _recipient_locale(ids["student_user_id"], "pt") == "en"

    def test_blank_recipient_language_falls_through_to_coach(self, app):
        from padel_app.services.notification_service import _recipient_locale

        ids = _seed_coach_and_students(app, coach_language="en", student_languages=[""])
        with app.app_context():
            assert _recipient_locale(ids["student_user_id"], "en") == "en"

    def test_unknown_recipient_falls_through_to_coach_then_app_default(self, app):
        from padel_app.services.notification_service import _recipient_locale

        with app.app_context():
            assert _recipient_locale(None, "en") == "en"
            assert _recipient_locale(999_999, "en") == "en"
            assert _recipient_locale(None, None) == "pt"


# ---------------------------------------------------------------------------
# Integration — student-facing messages
# ---------------------------------------------------------------------------

class TestStudentFacingDefaultsFollowTheStudent:

    def test_en_student_of_pt_coach_gets_the_english_default(self, app):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        ids = _seed_coach_and_students(app, coach_language="pt", student_languages=["en"])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        _decline(app, ids, instance_id)

        texts = _messages(app, message_type="text")
        assert DEFAULT_MESSAGE_TEMPLATES["reminder_declined"] in texts
        assert DEFAULT_MESSAGE_TEMPLATES_PT["reminder_declined"] not in texts

    def test_pt_student_of_en_coach_gets_the_portuguese_default(self, app):
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )

        ids = _seed_coach_and_students(app, coach_language="en", student_languages=["pt"])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        _decline(app, ids, instance_id)

        texts = _messages(app, message_type="text")
        assert DEFAULT_MESSAGE_TEMPLATES_PT["reminder_declined"] in texts
        assert DEFAULT_MESSAGE_TEMPLATES["reminder_declined"] not in texts

    def test_confirmation_default_also_follows_the_student(self, app):
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_students(app, coach_language="pt", student_languages=["en"])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"], now=datetime.utcnow()
                )

        assert DEFAULT_MESSAGE_TEMPLATES["reminder_confirmed"] in _messages(
            app, message_type="text"
        )

    def test_custom_template_reaches_a_foreign_language_student_verbatim(self, app):
        """A coach's own prose is never translated, whatever the student speaks."""
        ids = _seed_coach_and_students(app, coach_language="pt", student_languages=["en"])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])
        _set_templates(app, ids["coach_id"], {"reminder_declined": "Ok, ficas de fora!"})

        _decline(app, ids, instance_id)

        assert "Ok, ficas de fora!" in _messages(app, message_type="text")

    def test_student_without_a_language_falls_back_to_the_coach_locale(self, app):
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES

        ids = _seed_coach_and_students(app, coach_language="en", student_languages=[""])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        _decline(app, ids, instance_id)

        assert DEFAULT_MESSAGE_TEMPLATES["reminder_declined"] in _messages(
            app, message_type="text"
        )

    def test_reminder_body_and_weekday_share_one_language(self, app):
        """The ``{weekday}`` placeholder must follow the resolved template's
        language — an English default with a Portuguese weekday is still a bug."""
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES
        from padel_app.services.notification_service import send_class_reminders

        ids = _seed_coach_and_students(app, coach_language="pt", student_languages=["en"])
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=datetime.utcnow())

        texts = _messages(app, message_type="notification_reminder")
        assert len(texts) == 1
        expected_prefix = DEFAULT_MESSAGE_TEMPLATES["reminder"].split("{name}")[0]
        assert texts[0].startswith(expected_prefix)
        # English weekday names, not "quarta-feira" & friends.
        assert not any(pt_day in texts[0] for pt_day in (
            "segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo",
        ))

    def test_two_students_of_one_coach_each_get_their_own_language(self, app):
        """Per-recipient resolution, not one language per send."""
        from padel_app.models.notification_config import (
            DEFAULT_MESSAGE_TEMPLATES,
            DEFAULT_MESSAGE_TEMPLATES_PT,
        )
        from padel_app.services.notification_service import send_class_reminders

        ids = _seed_coach_and_students(
            app, coach_language="pt", student_languages=["en", "pt"]
        )
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_ids"])

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=datetime.utcnow())

        texts = _messages(app, message_type="notification_reminder")
        assert len(texts) == 2
        en_prefix = DEFAULT_MESSAGE_TEMPLATES["reminder"].split("{name}")[0]
        pt_prefix = DEFAULT_MESSAGE_TEMPLATES_PT["reminder"].split("{name}")[0]
        assert sum(1 for t in texts if t.startswith(en_prefix)) == 1
        assert sum(1 for t in texts if t.startswith(pt_prefix)) == 1


# ---------------------------------------------------------------------------
# Coach-facing surface — must NOT follow any student
# ---------------------------------------------------------------------------

class TestCoachFacingViewKeepsTheCoachLanguage:
    """None of the 13 template-driven messages is coach-facing (all 13 are
    addressed to a student), but the coach's own Settings → Message templates
    view is — and it must keep showing defaults in the coach's language."""

    def test_config_dict_shows_defaults_in_the_coach_language(self, app):
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT
        from padel_app.services.notification_service import get_config_dict

        ids = _seed_coach_and_students(
            app, coach_language="pt", student_languages=["en", "en"]
        )

        with app.app_context():
            cfg = get_config_dict(ids["coach_id"])

        assert cfg["messageTemplates"]["decline"] == DEFAULT_MESSAGE_TEMPLATES_PT["decline"]

    def test_config_dict_shows_defaults_in_english_for_an_en_coach(self, app):
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES
        from padel_app.services.notification_service import get_config_dict

        ids = _seed_coach_and_students(
            app, coach_language="en", student_languages=["pt"]
        )

        with app.app_context():
            cfg = get_config_dict(ids["coach_id"])

        assert cfg["messageTemplates"]["decline"] == DEFAULT_MESSAGE_TEMPLATES["decline"]


# ---------------------------------------------------------------------------
# get_custom_message_templates — the "customised vs default" distinction
# ---------------------------------------------------------------------------

class TestCustomMessageTemplatesAccessor:

    def test_returns_only_non_blank_customisations(self, app):
        from padel_app.services.notification_service import get_or_create_config

        ids = _seed_coach_and_students(app)
        _set_templates(app, ids["coach_id"], {
            "decline": "Custom decline",
            "confirm": "",
            "invite": "   ",
            "reminder": None,
            "spot_filled": 42,
        })

        with app.app_context():
            custom = get_or_create_config(ids["coach_id"]).get_custom_message_templates()

        assert custom == {"decline": "Custom decline"}

    def test_returns_empty_dict_when_nothing_was_ever_saved(self, app):
        from padel_app.services.notification_service import get_or_create_config

        ids = _seed_coach_and_students(app)

        with app.app_context():
            assert get_or_create_config(ids["coach_id"]).get_custom_message_templates() == {}
