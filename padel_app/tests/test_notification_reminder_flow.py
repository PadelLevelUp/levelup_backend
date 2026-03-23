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
