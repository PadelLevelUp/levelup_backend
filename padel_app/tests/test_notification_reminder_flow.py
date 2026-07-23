"""
Integration tests for the reminder flow and standing waiting list.

These tests use a real SQLite in-memory DB (via the `app` fixture from conftest.py)
to exercise the full code path including ORM queries, without any external I/O.
`publish` and `send_push_notification` are patched to avoid Redis/WebSocket and
push-notification side effects.

Run:
    pytest padel_app/tests/test_notification_reminder_flow.py -v
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_coach_and_student(app):
    """Create and persist a coach user, coach, student user, and player. Returns dict."""
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player

    with app.app_context():
        coach_user = User(
            name="Test Coach",
            username="test-coach",
            email="coach@test.com",
            password="hashed",
            status="active",
        )
        db.session.add(coach_user)

        student_user = User(
            name="Test Student",
            username="test-student",
            email="student@test.com",
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
    """Create a lesson and instance. Returns instance_id."""
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

        level = CoachLevel(coach_id=coach_id, label="Beginner", code="B1", display_order=1)
        db.session.add(level)
        db.session.flush()

        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
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
            level_id=level.id,
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


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]


def _no_io(func):
    """Decorator that mocks out publish and push notifications."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with patch(PATCHES[0]), patch(PATCHES[1]):
            return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# TestSendClassRemindersIntegration
# ---------------------------------------------------------------------------

class TestSendClassRemindersIntegration:

    def test_reminders_sent_to_enrolled_players(self, app):
        """send_class_reminders creates a Presence and a reminder Message for each enrolled player."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.presences import Presence
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)

            presences = Presence.query.filter_by(
                lesson_instance_id=instance_id,
                player_id=ids["student_id"],
            ).all()
            assert len(presences) == 1
            assert presences[0].confirmed is False

            reminder_messages = Message.query.filter_by(
                message_type="notification_reminder",
            ).all()
            assert len(reminder_messages) == 1

    def test_reminders_not_sent_when_instance_in_past(self, app):
        """send_class_reminders silently returns without creating messages if class already started."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)

        with app.app_context():
            # Pass `now` that is AFTER the instance start
            future_now = datetime.utcnow() + timedelta(hours=72)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=future_now)

            reminder_messages = Message.query.filter_by(
                message_type="notification_reminder",
            ).all()
            assert len(reminder_messages) == 0

    def test_reminder_idempotent_does_not_duplicate_presence(self, app):
        """Calling send_class_reminders twice does not create a duplicate Presence row."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)
                send_class_reminders(instance_id, now=now)

            count = Presence.query.filter_by(
                lesson_instance_id=instance_id,
                player_id=ids["student_id"],
            ).count()
            assert count == 1

    def test_reminders_do_not_crash_for_ghost_player(self, app):
        """send_class_reminders must not raise even when a player's user has status=inactive.

        The service sends to all enrolled players who have a user_id, regardless of user.status.
        The important guarantee is zero exceptions, not zero messages.
        """
        from padel_app.models.users import User
        from padel_app.models.players import Player
        from padel_app.models.lessons import Lesson
        from padel_app.models.lesson_instances import LessonInstance
        from padel_app.models.coach_levels import CoachLevel
        from padel_app.models.coaches import Coach
        from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
        from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message
        from padel_app.models.clubs import Club

        with app.app_context():
            coach_user = User(name="Coach2", username="coach2", email="c2@test.com",
                              password="x", status="active")
            ghost_user = User(name="Ghost", username="ghost", email=None,
                              password=None, status="inactive")
            db.session.add_all([coach_user, ghost_user])
            db.session.flush()

            coach = Coach(user_id=coach_user.id)
            ghost = Player(user_id=ghost_user.id)
            db.session.add_all([coach, ghost])
            db.session.flush()

            club = Club(name="Ghost Club", description="", location="City")
            db.session.add(club)
            db.session.flush()

            level = CoachLevel(coach_id=coach.id, label="Beg", code="B2", display_order=1)
            db.session.add(level)
            db.session.flush()

            start = datetime.utcnow() + timedelta(hours=24)
            lesson = Lesson(title="Ghost Class", start_datetime=start,
                            end_datetime=start + timedelta(hours=1),
                            is_recurring=False, type="academy", max_players=4,
                            color="#fff", status="active", club_id=club.id)
            db.session.add(lesson)
            db.session.flush()

            instance = LessonInstance(lesson_id=lesson.id, start_datetime=start,
                                      end_datetime=start + timedelta(hours=1),
                                      max_players=4, status="scheduled", level_id=level.id)
            db.session.add(instance)
            db.session.flush()

            db.session.add(Association_CoachLessonInstance(coach_id=coach.id,
                                                            lesson_instance_id=instance.id))
            db.session.add(Association_PlayerLessonInstance(player_id=ghost.id,
                                                             lesson_instance_id=instance.id))
            db.session.commit()

            instance_id = instance.id
            now = datetime.utcnow()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                # Must not raise even though ghost user has status=inactive
                send_class_reminders(instance_id, now=now)

            # No exception = test passes; message count is irrelevant here
            assert True


# ---------------------------------------------------------------------------
# TestReminderTemplateResolution — PAD-37
#
# The scheduled reminder-sending path must render the coach's configured
# reminder template. It may only fall back to the hardcoded default when the
# coach has NOT configured a custom template for that key.
# ---------------------------------------------------------------------------

class TestReminderTemplateResolution:

    def _reminder_texts(self):
        from padel_app.models.messages import Message
        msgs = (
            Message.query.filter_by(message_type="notification_reminder")
            .order_by(Message.id)
            .all()
        )
        return [m.text for m in msgs]

    def test_uses_coach_custom_reminder_template(self, app):
        """A configured custom `reminder` template is rendered — not the default."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            update_config,
        )
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(
            app, ids["coach_id"], ids["student_id"], start_offset_hours=48
        )

        with app.app_context():
            update_config(
                ids["coach_id"],
                {"messageTemplates": {"reminder": "PAD37 custom hi {name}!"}},
            )
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)

            texts = self._reminder_texts()
            assert len(texts) == 1
            assert texts[0] == "PAD37 custom hi Test!"
            # Explicitly assert the default template was NOT used.
            assert DEFAULT_MESSAGE_TEMPLATES["reminder"] not in texts[0]

    def test_uses_coach_custom_followup_template(self, app):
        """Repeat reminders render the custom `reminder_followup` template."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            update_config,
        )

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(
            app, ids["coach_id"], ids["student_id"], start_offset_hours=48
        )

        with app.app_context():
            update_config(
                ids["coach_id"],
                {
                    "messageTemplates": {
                        "reminder": "FIRST {name}",
                        "reminder_followup": "FOLLOWUP {name}",
                    },
                    "reminderTiming": {
                        "firstReminder": {"type": "hours_before", "value": 48},
                        "reminderCount": 2,
                        "hoursBetweenReminders": 1,
                    },
                },
            )
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

            texts = self._reminder_texts()
            assert texts == ["FIRST Test", "FOLLOWUP Test"]

    def test_falls_back_to_default_only_when_not_configured(self, app):
        """With no custom `reminder` configured, the default template is used.

        Fallback is correct behaviour ONLY when the coach has not authored a
        custom template for the key (PAD-37 acceptance criterion).
        """
        from padel_app.services.notification_service import send_class_reminders
        # PAD-39 made notifications locale-aware; a coach with no explicit
        # language resolves to Portuguese, so the default reminder is the pt
        # variant. Assert against that default (still the "no custom template"
        # fallback the PAD-37 criterion cares about).
        from padel_app.models.notification_config import DEFAULT_MESSAGE_TEMPLATES_PT

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(
            app, ids["coach_id"], ids["student_id"], start_offset_hours=48
        )

        with app.app_context():
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)

            texts = self._reminder_texts()
            assert len(texts) == 1
            # Default template has no unsubstituted {name} placeholder after render.
            expected_prefix = DEFAULT_MESSAGE_TEMPLATES_PT["reminder"].split("{name}")[0]
            assert texts[0].startswith(expected_prefix)

    def test_custom_reminder_partial_config_preserves_other_defaults(self, app):
        """Configuring only `reminder` still renders it (merge keeps custom key)."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            update_config,
        )

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(
            app, ids["coach_id"], ids["student_id"], start_offset_hours=48
        )

        with app.app_context():
            # Simulate a config that only stores the reminder key (older UI shape).
            update_config(
                ids["coach_id"],
                {"messageTemplates": {"reminder": "ONLY-REMINDER-{name}"}},
            )
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=now)

            texts = self._reminder_texts()
            assert texts == ["ONLY-REMINDER-Test"]


def _config_with_repeat(app, coach_id, *, count, hours):
    """Persist a NotificationConfig for the coach with repeat-reminder settings."""
    from padel_app.models.notification_config import NotificationConfig

    with app.app_context():
        NotificationConfig(
            coach_id=coach_id,
            auto_notify_enabled=False,
            reminder_timing={
                "firstReminder": {"type": "hours_before", "value": 48},
                "reminderCount": count,
                "hoursBetweenReminders": hours,
            },
        ).create()


# ---------------------------------------------------------------------------
# TestRepeatReminders — reminderCount / hoursBetweenReminders behavior
# ---------------------------------------------------------------------------

class TestRepeatReminders:

    def test_sends_up_to_reminder_count_then_stops(self, app):
        """With reminderCount=3 and a student who never responds, successive calls
        produce exactly 3 reminder Messages; a 4th call produces none."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        # Class far in the future so all sends are before its start.
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=3, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                send_class_reminders(instance_id, now=t0 + timedelta(hours=2))
                send_class_reminders(instance_id, now=t0 + timedelta(hours=4))
                fourth = send_class_reminders(instance_id, now=t0 + timedelta(hours=6))

            msgs = Message.query.filter_by(message_type="notification_reminder").all()
            assert len(msgs) == 3
            # Each carries the instanceId + an increasing reminderNumber.
            numbers = sorted(m.msg_metadata.get("reminderNumber") for m in msgs)
            assert numbers == [1, 2, 3]
            assert all(m.msg_metadata.get("instanceId") == instance_id for m in msgs)
            assert fourth == {"sent": 0, "more_due": False}

    def test_stops_early_when_student_responds(self, app):
        """If the student confirms after the 1st reminder, the next call sends nothing."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            respond_to_reminder,
        )
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=3, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                first = send_class_reminders(instance_id, now=t0)
                # Student responds (confirm) — no further reminders should be sent.
                respond_to_reminder(instance_id, "yes", ids["student_user_id"])
                second = send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

            msgs = Message.query.filter_by(message_type="notification_reminder").all()
            assert len(msgs) == 1
            assert first["sent"] == 1
            assert second == {"sent": 0, "more_due": False}

    def test_stops_early_when_student_declines(self, app):
        """A decline also halts further reminders."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            respond_to_reminder,
        )
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=3, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                respond_to_reminder(instance_id, "no", ids["student_user_id"], now=t0)
                second = send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

            msgs = Message.query.filter_by(message_type="notification_reminder").all()
            assert len(msgs) == 1
            assert second["sent"] == 0

    def test_more_due_true_until_last_reminder(self, app):
        """more_due is True after reminders 1 and 2 of 3, and False after reminder 3."""
        from padel_app.services.notification_service import send_class_reminders

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=3, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                r1 = send_class_reminders(instance_id, now=t0)
                r2 = send_class_reminders(instance_id, now=t0 + timedelta(hours=2))
                r3 = send_class_reminders(instance_id, now=t0 + timedelta(hours=4))

            assert r1 == {"sent": 1, "more_due": True}
            assert r2 == {"sent": 1, "more_due": True}
            assert r3 == {"sent": 1, "more_due": False}


# ---------------------------------------------------------------------------
# TestReminderSupersede — PAD-49
#
# When a NEW reminder is sent for a (player, instance), any PRIOR un-actioned
# reminders for that same (player, instance) must be marked
# msg_metadata["superseded"] = True so the frontend renders them disabled.
# Reminders the player already responded to keep their badge (not superseded).
# ---------------------------------------------------------------------------

class TestReminderSupersede:

    def test_prior_reminder_superseded_by_new_one(self, app):
        """Sending a second reminder supersedes the first (un-actioned) one;
        the latest reminder stays actionable (no superseded flag)."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=1)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                send_class_reminders(instance_id, now=t0 + timedelta(hours=1))

            msgs = (
                Message.query.filter_by(message_type="notification_reminder")
                .order_by(Message.id)
                .all()
            )
            assert len(msgs) == 2
            first, second = msgs[0], msgs[1]

            # First (older) reminder is superseded but NOT responded.
            assert first.msg_metadata.get("superseded") is True
            assert not first.msg_metadata.get("responded")

            # Second (latest) reminder stays actionable — no superseded flag, not responded.
            assert not second.msg_metadata.get("superseded")
            assert not second.msg_metadata.get("responded")

    def test_responded_reminder_not_superseded(self, app):
        """A reminder the player already responded to keeps its badge and is
        NOT marked superseded when a new reminder is sent."""
        from padel_app.services.notification_service import (
            send_class_reminders,
            respond_to_reminder,
        )
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=1)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                # First reminder, then the student responds "no" to it.
                send_class_reminders(instance_id, now=t0)
                respond_to_reminder(instance_id, "no", ids["student_user_id"], now=t0)

                # Manually clear the presence confirmation so a second reminder is
                # still sent (a real second send only supersedes un-actioned msgs).
                from padel_app.models.presences import Presence
                presence = Presence.query.filter_by(
                    lesson_instance_id=instance_id, player_id=ids["student_id"]
                ).first()
                presence.confirmed = False
                presence.save()

                send_class_reminders(instance_id, now=t0 + timedelta(hours=1))

            msgs = (
                Message.query.filter_by(message_type="notification_reminder")
                .order_by(Message.id)
                .all()
            )
            assert len(msgs) == 2
            responded_msg = msgs[0]

            # The responded reminder keeps its badge and is NOT superseded.
            assert responded_msg.msg_metadata.get("responded") is True
            assert not responded_msg.msg_metadata.get("superseded")

    def test_supersede_only_touches_unresponded_messages(self, app):
        """Superseding only sets the flag on responded-falsy reminders."""
        from padel_app.services.notification_service import send_class_reminders
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=72)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=1)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                send_class_reminders(instance_id, now=t0 + timedelta(hours=1))

            superseded = [
                m for m in Message.query.filter_by(message_type="notification_reminder").all()
                if m.msg_metadata.get("superseded")
            ]
            # Every superseded message must be responded-falsy.
            assert all(not m.msg_metadata.get("responded") for m in superseded)


# ---------------------------------------------------------------------------
# TestPastClassReminderExpiry — PAD-68
#
# Once a class has started (or was canceled/completed) its reminders are dead.
# A late response must NOT be recorded against attendance and must NOT create a
# vacancy or fan out replacement invitations for a class that already happened.
# ---------------------------------------------------------------------------

def _seed_replacement_candidates(app, coach_id, n=3):
    """Create n other players linked to the coach, eligible for invitations."""
    from padel_app.models.users import User
    from padel_app.models.players import Player
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer

    player_ids = []
    with app.app_context():
        for i in range(n):
            u = User(
                name=f"Candidate {i}",
                username=f"candidate-{i}",
                email=f"candidate{i}@test.com",
                password="hashed",
                status="active",
            )
            db.session.add(u)
            db.session.flush()
            p = Player(user_id=u.id)
            db.session.add(p)
            db.session.flush()
            db.session.add(Association_CoachPlayer(coach_id=coach_id, player_id=p.id))
            player_ids.append(p.id)
        db.session.commit()
    return player_ids


def _enable_auto_notify(app, coach_id):
    from padel_app.models.notification_config import NotificationConfig

    with app.app_context():
        config = NotificationConfig.query.filter_by(coach_id=coach_id).first()
        config.auto_notify_enabled = True
        config.save()


class TestPastClassReminderExpiry:

    def test_late_decline_does_not_fan_out_invitations(self, app):
        """PAD-68: answering 'no' after the class already happened sends no
        replacement invitations and creates no vacancy."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)
        _enable_auto_notify(app, ids["coach_id"])
        _seed_replacement_candidates(app, ids["coach_id"], 3)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                # The class happens. Three days later the student finally answers.
                result = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0 + timedelta(days=3)
                )

            assert result == {"action": "expired"}
            assert NotificationEvent.query.filter_by(lesson_instance_id=instance_id).count() == 0
            assert Vacancy.query.filter_by(lesson_instance_id=instance_id).count() == 0

    def test_late_decline_does_not_rewrite_attendance(self, app):
        """PAD-68: a late answer must not retroactively mark the player absent —
        the coach's attendance record for a class that happened is authoritative."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                presence = Presence.query.filter_by(
                    lesson_instance_id=instance_id, player_id=ids["student_id"]
                ).first()
                presence.status = "present"
                presence.save()

                respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0 + timedelta(days=3)
                )

            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "present"
            assert presence.justification is None

    def test_late_response_marks_stale_reminder_expired(self, app):
        """PAD-68: the un-actioned reminder is retired (superseded/expired) so the
        UI stops offering live Yes/No buttons for a class that already happened."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"], now=t0 + timedelta(days=3)
                )

            msg = Message.query.filter_by(message_type="notification_reminder").first()
            assert msg.msg_metadata.get("superseded") is True
            assert msg.msg_metadata.get("expired") is True
            # It was never answered — it must not carry a confirmed/absent badge.
            assert not msg.msg_metadata.get("responded")

    def test_trigger_invitations_noop_for_past_class(self, app):
        """PAD-68: the invitation engine refuses to open or fill vacancies for a
        class that already started, whatever calls it."""
        from padel_app.services.notification_service import trigger_invitations
        from padel_app.models.lesson_instances import LessonInstance
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)
        _enable_auto_notify(app, ids["coach_id"])
        _seed_replacement_candidates(app, ids["coach_id"], 3)

        with app.app_context():
            Presence(
                lesson_instance_id=instance_id,
                player_id=ids["student_id"],
                invited=True,
                confirmed=True,
                status="absent",
            ).create()
            instance = LessonInstance.query.get(instance_id)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                notified = trigger_invitations(
                    instance, ids["coach_id"], now=datetime.utcnow() + timedelta(days=3)
                )

            assert notified == []
            assert NotificationEvent.query.filter_by(lesson_instance_id=instance_id).count() == 0

    def test_invitation_batch_expires_vacancy_for_past_class(self, app):
        """PAD-68: the send chokepoint expires an open vacancy instead of
        inviting anyone once the class has started."""
        from padel_app.services.notification_service import (
            _send_invitation_batch,
            get_or_create_config,
        )
        from padel_app.models.lesson_instances import LessonInstance
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)
        _enable_auto_notify(app, ids["coach_id"])
        _seed_replacement_candidates(app, ids["coach_id"], 3)

        with app.app_context():
            vacancy = Vacancy(
                lesson_instance_id=instance_id,
                coach_id=ids["coach_id"],
                original_player_id=ids["student_id"],
                status="open",
                approval_status="not_required",
            )
            vacancy.create()
            instance = LessonInstance.query.get(instance_id)
            config = get_or_create_config(ids["coach_id"])

            with patch(PATCHES[0]), patch(PATCHES[1]):
                notified = _send_invitation_batch(
                    vacancy, instance, config, ids["coach_id"],
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert notified == []
            assert vacancy.status == "expired"
            assert NotificationEvent.query.filter_by(lesson_instance_id=instance_id).count() == 0

    def test_response_before_class_start_still_works(self, app):
        """Guard rail: the expiry check must not break the normal, timely path."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                result = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0 + timedelta(hours=1)
                )

            assert result == {"action": "declined"}
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "absent"


# ---------------------------------------------------------------------------
# TestResponseAlwaysRecorded — PAD-69
#
# A student's answer must always be durably recorded, so the next reminder pass
# can never mistake them for someone who never replied.
# ---------------------------------------------------------------------------

class TestResponseAlwaysRecorded:

    def test_decline_creates_presence_when_missing(self, app):
        """PAD-69: with no Presence row the answer used to be silently dropped,
        and the follow-up reminder was then sent as if nothing was answered."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.messages import Message
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                # Simulate the Presence row being absent when the student answers.
                Presence.query.filter_by(
                    lesson_instance_id=instance_id, player_id=ids["student_id"]
                ).first().delete()

                respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0 + timedelta(minutes=30)
                )
                send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence is not None
            assert presence.confirmed is True
            assert presence.status == "absent"

            # Exactly one reminder: no follow-up after an answer was given.
            assert Message.query.filter_by(message_type="notification_reminder").count() == 1

    def test_no_followup_reminder_after_decline(self, app):
        """PAD-69 regression guard for the ordinary path."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.messages import Message

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=24)
        _config_with_repeat(app, ids["coach_id"], count=2, hours=2)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0 + timedelta(minutes=30)
                )
                send_class_reminders(instance_id, now=t0 + timedelta(hours=2))

            assert Message.query.filter_by(message_type="notification_reminder").count() == 1


# ---------------------------------------------------------------------------
# TestRespondToReminder
# ---------------------------------------------------------------------------

class TestRespondToReminder:

    def test_yes_response_confirms_presence(self, app):
        """Responding 'yes' sets confirmed=True but does NOT set status — coach controls presence."""
        from padel_app.models.presences import Presence
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])

        with app.app_context():
            # Create presence first (as send_class_reminders would)
            p = Presence(lesson_instance_id=instance_id, player_id=ids["student_id"],
                         invited=True, confirmed=False)
            p.create()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_reminder(instance_id, "yes", ids["student_user_id"])

            assert result["action"] == "confirmed"
            updated = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert updated.status is None  # coach has not marked them present yet
            assert updated.confirmed is True

    def test_yes_response_does_not_create_vacancy(self, app):
        """Responding 'yes' should not create a Vacancy — no spot has opened."""
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"])

        with app.app_context():
            p = Presence(lesson_instance_id=instance_id, player_id=ids["student_id"],
                         invited=True, confirmed=False)
            p.create()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_reminder(instance_id, "yes", ids["student_user_id"])

            assert Vacancy.query.filter_by(lesson_instance_id=instance_id).count() == 0

    def test_no_response_marks_absent(self, app):
        """Responding 'no' sets status='absent', justification='justified', confirmed=True."""
        from padel_app.models.presences import Presence
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=96)

        with app.app_context():
            p = Presence(lesson_instance_id=instance_id, player_id=ids["student_id"],
                         invited=True, confirmed=False)
            p.create()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_reminder(instance_id, "no", ids["student_user_id"])

            assert result["action"] == "declined"
            updated = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert updated.status == "absent"
            assert updated.justification == "justified"
            assert updated.confirmed is True

    def test_no_response_always_creates_vacancy(self, app):
        """Declining before the invitation window opens creates a Vacancy immediately
        (so the invite_start scheduler job finds it when the window opens) but does NOT
        send invitations yet."""
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_student(app)
        # Class is 96h away; default invite start is 24h before — window is still 72h away
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=96)

        with app.app_context():
            p = Presence(lesson_instance_id=instance_id, player_id=ids["student_id"],
                         invited=True, confirmed=False)
            p.create()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_reminder(instance_id, "no", ids["student_user_id"])

            # Vacancy is pre-created so the scheduler job finds it when the window opens
            vacancy_count = Vacancy.query.filter_by(lesson_instance_id=instance_id).count()
            assert vacancy_count >= 1
            # But no invitations were sent yet (window not open)
            event_count = NotificationEvent.query.filter_by(lesson_instance_id=instance_id).count()
            assert event_count == 0

    def test_no_response_after_invite_start_creates_vacancy(self, app):
        """Declining after invitation start has passed should create a Vacancy immediately."""
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.notification_config import NotificationConfig
        from padel_app.services.notification_service import respond_to_reminder

        ids = _seed_coach_and_student(app)
        # Class is 10h away; default invite start is 24h before → invite start has passed
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=10)

        with app.app_context():
            # Enable auto-notify so trigger_invitations can proceed
            NotificationConfig(coach_id=ids["coach_id"], auto_notify_enabled=True).create()

            p = Presence(lesson_instance_id=instance_id, player_id=ids["student_id"],
                         invited=True, confirmed=False)
            p.create()

            # now = current time → invite start was 14h ago (24 - 10) → should trigger vacancy
            now = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_reminder(instance_id, "no", ids["student_user_id"], now=now)

            vacancy_count = Vacancy.query.filter_by(lesson_instance_id=instance_id).count()
            assert vacancy_count >= 1


# ---------------------------------------------------------------------------
# TestStandingWaitingListCRUD
# ---------------------------------------------------------------------------

class TestStandingWaitingListCRUD:

    def test_add_standing_entry_persists_and_is_returned(self, app):
        """add_standing_waiting_list_entry creates an entry and get_standing_waiting_list returns it.

        This exercises the SELECT that was failing due to the missing updated_at column.
        """
        from padel_app.services.notification_service import (
            add_standing_waiting_list_entry,
            get_standing_waiting_list,
        )

        ids = _seed_coach_and_student(app)

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                entry = add_standing_waiting_list_entry(
                    ids["coach_id"], ids["student_id"], credits_total=3, duration_days=30
                )

            result = get_standing_waiting_list(ids["coach_id"])

        assert len(result) == 1
        assert result[0]["creditsTotal"] == 3
        assert result[0]["creditsUsed"] == 0
        assert result[0]["playerId"] == ids["student_id"]

    def test_save_on_standing_entry_does_not_crash(self, app):
        """Calling .save() on a StandingWaitingListEntry should not raise after the migration fix."""
        from padel_app.models.standing_waiting_list_entry import StandingWaitingListEntry

        ids = _seed_coach_and_student(app)

        with app.app_context():
            entry = StandingWaitingListEntry(
                coach_id=ids["coach_id"],
                player_id=ids["student_id"],
                credits_total=2,
                credits_used=0,
                expires_at=datetime.utcnow() + timedelta(days=30),
                is_active=True,
            )
            entry.create()

            # .save() writes updated_at — this would fail before the migration fix
            entry.credits_used = 1
            entry.save()  # must not raise

            updated = StandingWaitingListEntry.query.get(entry.id)
            assert updated.credits_used == 1
            assert updated.updated_at is not None

    def test_remove_standing_entry_deactivates_it(self, app):
        """remove_standing_waiting_list_entry marks the entry inactive and get returns empty list."""
        from padel_app.services.notification_service import (
            add_standing_waiting_list_entry,
            remove_standing_waiting_list_entry,
            get_standing_waiting_list,
        )

        ids = _seed_coach_and_student(app)

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                entry = add_standing_waiting_list_entry(
                    ids["coach_id"], ids["student_id"], credits_total=2, duration_days=14
                )
            entry_id = entry.id

            remove_standing_waiting_list_entry(entry_id, ids["coach_id"])
            result = get_standing_waiting_list(ids["coach_id"])

        assert result == []

    def test_add_deactivates_existing_active_entry(self, app):
        """Adding a second entry for the same coach/player deactivates the first."""
        from padel_app.services.notification_service import add_standing_waiting_list_entry
        from padel_app.models.standing_waiting_list_entry import StandingWaitingListEntry

        ids = _seed_coach_and_student(app)

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                first = add_standing_waiting_list_entry(
                    ids["coach_id"], ids["student_id"], credits_total=2, duration_days=7
                )
                first_id = first.id

                second = add_standing_waiting_list_entry(
                    ids["coach_id"], ids["student_id"], credits_total=5, duration_days=30
                )

            old_entry = StandingWaitingListEntry.query.get(first_id)
            assert old_entry.is_active is False

            new_entry = StandingWaitingListEntry.query.get(second.id)
            assert new_entry.is_active is True
            assert new_entry.credits_total == 5

    def test_fan_out_creates_waiting_list_entry_for_future_instance(self, app):
        """add_standing_waiting_list_entry fans out to create a WaitingListEntry for upcoming instances."""
        from padel_app.services.notification_service import add_standing_waiting_list_entry
        from padel_app.models.waiting_list_entry import WaitingListEntry

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                entry = add_standing_waiting_list_entry(
                    ids["coach_id"], ids["student_id"], credits_total=3, duration_days=30
                )

            wl = WaitingListEntry.query.filter_by(
                standing_entry_id=entry.id,
                lesson_instance_id=instance_id,
                is_active=True,
            ).first()

        assert wl is not None
        assert wl.player_id == ids["student_id"]


# ---------------------------------------------------------------------------
# TestCancelAttendance (PAD-35)
# ---------------------------------------------------------------------------

class TestCancelAttendance:
    """Students may cancel a confirmed attendance any time BEFORE the class
    starts; cancellation reverts the presence to not-attending and frees the
    spot via the same path as a reminder decline. After start it is a 409."""

    def _confirmed_presence(self, app, instance_id, player_id):
        from padel_app.models.presences import Presence
        with app.app_context():
            presence = Presence(
                player_id=player_id,
                lesson_instance_id=instance_id,
                invited=True,
                confirmed=True,
            )
            db.session.add(presence)
            db.session.commit()
            return presence.id

    def test_cancel_before_start_reverts_presence(self, app):
        from padel_app.services.notification_service import cancel_attendance
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = cancel_attendance(instance_id, ids["student_user_id"])

            assert result["action"] == "declined"
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "absent"
            assert presence.justification == "justified"

    def test_cancel_before_start_creates_vacancy(self, app):
        """Cancelling frees the spot — a Vacancy is created for the engine,
        exactly like a reminder decline (auto mode)."""
        from padel_app.services.notification_service import cancel_attendance, get_or_create_config
        from padel_app.models.vacancy import Vacancy

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            # Enable the engine so a vacancy is created.
            config = get_or_create_config(ids["coach_id"])
            config.auto_notify_enabled = True
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                cancel_attendance(instance_id, ids["student_user_id"])

            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance_id,
                original_player_id=ids["student_id"],
            ).first()
            assert vacancy is not None

    def test_cancel_after_start_rejected_409(self, app):
        from werkzeug.exceptions import Conflict
        from padel_app.services.notification_service import cancel_attendance
        from padel_app.models.presences import Presence

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            # now is AFTER the instance start (start is +48h)
            future_now = datetime.utcnow() + timedelta(hours=72)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                with pytest.raises(Conflict):
                    cancel_attendance(instance_id, ids["student_user_id"], now=future_now)

            # Presence unchanged (still confirmed, no absent status).
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.confirmed is True
            assert presence.status is None
            # And it is not flagged as a late cancellation.
            assert presence.late_cancellation is False

    def test_cancel_before_deadline_not_flagged_and_spot_freed(self, app):
        """Cancelling well before the deadline (start 48h away, default 24h
        deadline): succeeds, late_cancellation stays False, spot is freed."""
        from padel_app.services.notification_service import (
            cancel_attendance,
            get_or_create_config,
        )
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            # Enable the engine so a vacancy is created when the spot is freed.
            config = get_or_create_config(ids["coach_id"])
            config.auto_notify_enabled = True
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                # now = current time → start is ~48h away → 24h before the
                # deadline → NOT late.
                result = cancel_attendance(instance_id, ids["student_user_id"])

            assert result["action"] == "declined"
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "absent"
            assert presence.justification == "justified"
            assert presence.late_cancellation is False

            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance_id,
                original_player_id=ids["student_id"],
            ).first()
            assert vacancy is not None

    def test_cancel_after_deadline_before_start_is_flagged_late(self, app):
        """Cancelling inside the deadline window (start 48h away, now +30h →
        18h before start, deadline is 24h before): still allowed, but
        late_cancellation=True and the spot is still freed."""
        from padel_app.services.notification_service import (
            cancel_attendance,
            get_or_create_config,
        )
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            config = get_or_create_config(ids["coach_id"])
            config.auto_notify_enabled = True
            db.session.commit()

            # now = +30h → start is 18h away → past the 24h deadline but before start.
            now = datetime.utcnow() + timedelta(hours=30)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = cancel_attendance(
                    instance_id, ids["student_user_id"], now=now
                )

            assert result["action"] == "declined"
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "absent"
            assert presence.justification == "justified"
            assert presence.late_cancellation is True

            # Spot still freed.
            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance_id,
                original_player_id=ids["student_id"],
            ).first()
            assert vacancy is not None

    def _coach_cancellation_messages(self, app, coach_user_id, student_user_id, instance_id):
        """Return coach-facing cancellation messages: text ones carrying the
        PAD-44 ``cancellation`` metadata marker for this instance."""
        from padel_app.models.messages import Message
        return [
            m for m in Message.query.all()
            if m.msg_metadata
            and m.msg_metadata.get("cancellation") is True
            and m.msg_metadata.get("lessonInstanceId") == instance_id
        ]

    def test_cancel_before_deadline_notifies_coach_not_late(self, app):
        """PAD-44: a non-late cancel produces exactly one coach-facing
        notification identifying the student + class, not marked late, with the
        push directed at the coach."""
        from padel_app.services.notification_service import (
            cancel_attendance,
            get_or_create_config,
        )

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            config = get_or_create_config(ids["coach_id"])
            config.auto_notify_enabled = True
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]) as mock_push:
                cancel_attendance(instance_id, ids["student_user_id"])

            # Exactly one coach-facing cancellation message.
            msgs = self._coach_cancellation_messages(
                app, ids["coach_user_id"], ids["student_user_id"], instance_id
            )
            assert len(msgs) == 1
            msg = msgs[0]
            # Not marked late.
            assert msg.msg_metadata.get("lateCancellation") is False
            # Identifies the student and the class instance.
            assert "Test Student" in msg.text
            assert "Test Class" in msg.text
            assert "late" not in msg.text.lower()
            # Sent from the student into the coach<->student conversation.
            assert msg.sender_id == ids["student_user_id"]

            # Exactly one push, directed at the COACH.
            coach_pushes = [
                c for c in mock_push.call_args_list
                if c.kwargs.get("user_id") == ids["coach_user_id"]
            ]
            assert len(coach_pushes) == 1

    def test_cancel_after_deadline_notifies_coach_marked_late(self, app):
        """PAD-44: a late cancel produces exactly one coach-facing notification
        MARKED as a late cancellation (text + metadata), no duplicate."""
        from padel_app.services.notification_service import (
            cancel_attendance,
            get_or_create_config,
        )

        ids = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, ids["coach_id"], ids["student_id"], start_offset_hours=48)
        self._confirmed_presence(app, instance_id, ids["student_id"])

        with app.app_context():
            config = get_or_create_config(ids["coach_id"])
            config.auto_notify_enabled = True
            db.session.commit()

            # now = +30h → 18h before start → past the 24h deadline → late.
            now = datetime.utcnow() + timedelta(hours=30)
            with patch(PATCHES[0]), patch(PATCHES[1]) as mock_push:
                cancel_attendance(instance_id, ids["student_user_id"], now=now)

            msgs = self._coach_cancellation_messages(
                app, ids["coach_user_id"], ids["student_user_id"], instance_id
            )
            assert len(msgs) == 1
            msg = msgs[0]
            # Marked late in metadata AND text.
            assert msg.msg_metadata.get("lateCancellation") is True
            assert "late" in msg.text.lower()
            assert "Test Student" in msg.text
            assert "Test Class" in msg.text

            # Exactly one coach-directed push (no duplicate).
            coach_pushes = [
                c for c in mock_push.call_args_list
                if c.kwargs.get("user_id") == ids["coach_user_id"]
            ]
            assert len(coach_pushes) == 1

    def test_cancellation_deadline_hours_defaults_to_24_and_round_trips(self, app):
        """cancellationDeadlineHours defaults to 24 and round-trips through
        get_config_dict / update_config (the GET|POST /notify/config path)."""
        from padel_app.services.notification_service import (
            get_config_dict,
            update_config,
        )

        ids = _seed_coach_and_student(app)

        with app.app_context():
            # Default: 24 (GET /notify/config → get_config_dict).
            cfg = get_config_dict(ids["coach_id"])
            assert cfg["restrictions"]["cancellationDeadlineHours"] == 24

            # POST /notify/config → update_config with a new value.
            restrictions = dict(cfg["restrictions"])
            restrictions["cancellationDeadlineHours"] = 12
            update_config(ids["coach_id"], {"restrictions": restrictions})

            # Round-trips back on the next GET.
            cfg2 = get_config_dict(ids["coach_id"])
            assert cfg2["restrictions"]["cancellationDeadlineHours"] == 12
