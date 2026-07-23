"""
PAD-69 — Boolean form coercion regression tests.

`Field.set_boolean_value` used to compare the incoming value against the
*string* ``"true"``. Every JSON payload reaches the form layer through
``JsonRequestAdapter``, which puts real Python booleans into ``request.form``,
so ``True == "true"`` was False and every JSON boolean was silently written as
``False``.

The user-visible consequence (PAD-69): a student declines a reminder
(``Presence.confirmed = True``), the coach then confirms attendance
(``add_presences`` re-writes the presence through the form layer) and the
decline is wiped — so the next reminder pass sees an unanswered student and
sends a follow-up "Consegues vir?".

These tests cover the coercion itself and the end-to-end scenario. They fail on
main and pass with the fix.

Run:
    pytest padel_app/tests/test_boolean_coercion.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from padel_app.sql_db import db
from padel_app.tools.input_tools import Field


class DummyRequest:
    """Mimics a Flask request for the form layer."""

    def __init__(self, form=None):
        self.form = form if form is not None else {}
        self.files = {}


def _bool_field():
    return Field("1", "Model", "Flag", "flag", "Boolean")


# ---------------------------------------------------------------------------
# Unit — Field.set_boolean_value coercion semantics
# ---------------------------------------------------------------------------

class TestBooleanCoercion:

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Real Python booleans (JSON API payloads via JsonRequestAdapter).
            (True, True),
            (False, False),
            # HTML-form strings (the Jinja editor's hidden input).
            ("true", True),
            ("false", False),
            ("True", True),
            ("TRUE", True),
            ("  true  ", True),
            ("1", True),
            ("0", False),
            ("on", True),
            ("yes", True),
            # Empty value means "unchecked".
            ("", False),
            # Anything else is False.
            ("maybe", False),
        ],
    )
    def test_coercion_matrix(self, raw, expected):
        field = _bool_field()
        field.set_value(DummyRequest(form={"flag": raw}))
        assert field.value is expected

    def test_missing_key_is_false_and_does_not_raise(self):
        """An absent checkbox must coerce to False, never raise a KeyError."""
        field = _bool_field()
        field.set_value(DummyRequest(form={}))
        assert field.value is False

    def test_missing_key_on_a_real_werkzeug_multidict(self):
        """Real Flask form objects raise BadRequestKeyError on indexing —
        the helper must use .get()."""
        from werkzeug.datastructures import MultiDict

        field = _bool_field()
        field.set_value(DummyRequest(form=MultiDict({"other": "x"})))
        assert field.value is False

    def test_form_set_values_preserves_real_booleans(self, app):
        """The full form path (the one add_presences uses) round-trips True."""
        from padel_app.models.presences import Presence
        from padel_app.tools.request_adapter import JsonRequestAdapter

        with app.app_context():
            form = Presence.get_create_form()
            request = JsonRequestAdapter(
                {
                    "status": "absent",
                    "justification": "justified",
                    "invited": True,
                    "confirmed": True,
                    "validated": True,
                },
                form,
            )
            values = form.set_values(request)

        assert values["invited"] is True
        assert values["confirmed"] is True
        assert values["validated"] is True

    def test_form_set_values_still_honours_string_true(self, app):
        """Legacy HTML-form semantics are unchanged."""
        from padel_app.models.presences import Presence
        from padel_app.tools.request_adapter import JsonRequestAdapter

        with app.app_context():
            form = Presence.get_create_form()
            request = JsonRequestAdapter(
                {"invited": "true", "confirmed": "false", "validated": ""},
                form,
            )
            values = form.set_values(request)

        assert values["invited"] is True
        assert values["confirmed"] is False
        assert values["validated"] is False

    def test_omitted_boolean_is_false(self, app):
        """A Boolean the payload never mentions stays False (unchanged)."""
        from padel_app.models.presences import Presence
        from padel_app.tools.request_adapter import JsonRequestAdapter

        with app.app_context():
            form = Presence.get_create_form()
            values = form.set_values(JsonRequestAdapter({"status": "present"}, form))

        assert values["invited"] is False
        assert values["confirmed"] is False


# ---------------------------------------------------------------------------
# Seed helpers — mirror test_notification_reminder_flow.py
# ---------------------------------------------------------------------------

def _seed_coach_and_student(app):
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player

    with app.app_context():
        coach_user = User(name="Bool Coach", username="bool-coach",
                          email="bool-coach@test.com", password="hashed", status="active")
        student_user = User(name="Bool Student", username="bool-student",
                            email="bool-student@test.com", password="hashed", status="active")
        db.session.add_all([coach_user, student_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        student = Player(user_id=student_user.id)
        db.session.add_all([coach, student])
        db.session.flush()
        db.session.commit()

        return {
            "coach_user_id": coach_user.id,
            "student_user_id": student_user.id,
            "coach_id": coach.id,
            "student_id": student.id,
        }


def _seed_instance(app, coach_id, student_id, start_offset_hours=72):
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

    with app.app_context():
        club = Club(name="Bool Club", description="", location="Test City")
        db.session.add(club)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
        end = start + timedelta(hours=1)

        lesson = Lesson(title="Bool Class", start_datetime=start, end_datetime=end,
                        is_recurring=False, type="academy", max_players=4,
                        color="#000000", status="active", club_id=club.id)
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(lesson_id=lesson.id, start_datetime=start,
                                  end_datetime=end, max_players=4, status="scheduled",
                                  notifications_enabled=True)
        db.session.add(instance)
        db.session.flush()

        db.session.add(Association_CoachLessonInstance(
            coach_id=coach_id, lesson_instance_id=instance.id))
        db.session.add(Association_PlayerLessonInstance(
            player_id=student_id, lesson_instance_id=instance.id))
        db.session.commit()
        return {"lesson_id": lesson.id, "instance_id": instance.id}


def _config_with_repeat(app, coach_id, *, count=3, hours=2):
    from padel_app.models.notification_config import NotificationConfig

    with app.app_context():
        NotificationConfig(
            coach_id=coach_id,
            auto_notify_enabled=False,
            reminder_timing={
                "firstReminder": {"type": "hours_before", "value": 72},
                "reminderCount": count,
                "hoursBetweenReminders": hours,
            },
        ).create()


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]


# ---------------------------------------------------------------------------
# add_presences must not clobber reminder-flow state
# ---------------------------------------------------------------------------

class TestAddPresencesPreservesReminderState:

    def test_confirm_attendance_preserves_invited_confirmed(self, app):
        from padel_app.models.presences import Presence
        from padel_app.services.lesson_service import confirm_presences_service

        ids = _seed_coach_and_student(app)
        seeded = _seed_instance(app, ids["coach_id"], ids["student_id"])

        with app.app_context():
            Presence(
                player_id=ids["student_id"],
                lesson_instance_id=seeded["instance_id"],
                invited=True,
                confirmed=True,
                status="absent",
                justification="justified",
            ).create()

            confirm_presences_service(
                {"parentClassId": seeded["lesson_id"], "originalId": seeded["instance_id"]},
                [{"playerId": ids["student_id"], "status": "absent",
                  "justification": "justified"}],
            )
            db.session.commit()

            presence = Presence.query.filter_by(
                lesson_instance_id=seeded["instance_id"],
                player_id=ids["student_id"],
            ).first()

            assert presence.invited is True
            assert presence.confirmed is True
            assert presence.status == "absent"
            assert presence.justification == "justified"

    def test_confirm_attendance_sets_validated_true(self, app):
        """`add_presences` hardcodes validated=True — it must actually persist."""
        from padel_app.models.presences import Presence
        from padel_app.services.lesson_service import confirm_presences_service

        ids = _seed_coach_and_student(app)
        seeded = _seed_instance(app, ids["coach_id"], ids["student_id"])

        with app.app_context():
            confirm_presences_service(
                {"parentClassId": seeded["lesson_id"], "originalId": seeded["instance_id"]},
                [{"playerId": ids["student_id"], "status": "present",
                  "justification": None}],
            )
            db.session.commit()

            presence = Presence.query.filter_by(
                lesson_instance_id=seeded["instance_id"],
                player_id=ids["student_id"],
            ).first()

            assert presence.validated is True
            assert presence.status == "present"

    def test_confirm_attendance_preserves_late_cancellation(self, app):
        """Attendance marking must not silently clear the late-cancellation flag."""
        from padel_app.models.presences import Presence
        from padel_app.services.lesson_service import confirm_presences_service

        ids = _seed_coach_and_student(app)
        seeded = _seed_instance(app, ids["coach_id"], ids["student_id"])

        with app.app_context():
            Presence(
                player_id=ids["student_id"],
                lesson_instance_id=seeded["instance_id"],
                invited=True,
                confirmed=True,
                late_cancellation=True,
                status="absent",
                justification="justified",
            ).create()

            confirm_presences_service(
                {"parentClassId": seeded["lesson_id"], "originalId": seeded["instance_id"]},
                [{"playerId": ids["student_id"], "status": "absent",
                  "justification": "justified"}],
            )
            db.session.commit()

            presence = Presence.query.filter_by(
                lesson_instance_id=seeded["instance_id"],
                player_id=ids["student_id"],
            ).first()
            assert presence.late_cancellation is True


# ---------------------------------------------------------------------------
# PAD-69 end-to-end
# ---------------------------------------------------------------------------

class TestPad69NoFollowupAfterDecline:
    """reminder → student declines → coach confirms attendance → next reminder
    pass must NOT re-remind the student."""

    def _reminder_count(self, app, instance_id, user_id):
        from padel_app.models.messages import Message
        from padel_app.models.conversation_participants import ConversationParticipant

        with app.app_context():
            total = 0
            for message in Message.query.filter_by(
                message_type="notification_reminder"
            ).all():
                if not message.msg_metadata:
                    continue
                if message.msg_metadata.get("instanceId") != instance_id:
                    continue
                participants = ConversationParticipant.query.filter_by(
                    conversation_id=message.conversation_id
                ).all()
                recipients = [p.user_id for p in participants if p.user_id != message.sender_id]
                if user_id in recipients:
                    total += 1
            return total

    def test_no_followup_reminder_after_coach_confirms_attendance(self, app):
        from padel_app.models.presences import Presence
        from padel_app.services.lesson_service import confirm_presences_service
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )

        ids = _seed_coach_and_student(app)
        seeded = _seed_instance(app, ids["coach_id"], ids["student_id"],
                                start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=3, hours=2)
        instance_id = seeded["instance_id"]

        t0 = datetime.utcnow()

        # 1. First reminder goes out.
        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
        assert self._reminder_count(app, instance_id, ids["student_user_id"]) == 1

        # 2. Student declines — the decline is recorded on the presence.
        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_reminder(instance_id, "no", ids["student_user_id"], now=t0)
            db.session.commit()

            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.confirmed is True
            assert presence.status == "absent"

        # 3. Coach confirms attendance for the class (marks the decliner absent).
        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                confirm_presences_service(
                    {"parentClassId": seeded["lesson_id"], "originalId": instance_id},
                    [{"playerId": ids["student_id"], "status": "absent",
                      "justification": "justified"}],
                )
            db.session.commit()

            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            # The decline survives the coach's action.
            assert presence.confirmed is True, (
                "confirm_presences wiped the student's decline — PAD-69"
            )
            assert presence.invited is True
            assert presence.validated is True

        # 4. Next reminder pass: the decliner must NOT be reminded again.
        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                second = send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

        assert second["sent"] == 0
        assert self._reminder_count(app, instance_id, ids["student_user_id"]) == 1
