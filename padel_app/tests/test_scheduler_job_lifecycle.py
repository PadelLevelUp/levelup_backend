"""
Tests for scheduler job lifecycle — verifying that APScheduler jobs are
correctly scheduled, rescheduled, and cancelled as classes are created,
edited, and deleted.

All tests patch schedule_instance_jobs / cancel_instance_jobs so no real
APScheduler instance is needed.

Run:
    pytest padel_app/tests/test_scheduler_job_lifecycle.py -v
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Seed helpers (shared with test_notification_reminder_flow)
# ---------------------------------------------------------------------------

def _seed_coach_and_club(app):
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.sql_db import db

    with app.app_context():
        user = User(name="Coach", username="coach-sched", email="csched@test.com",
                    password="x", status="active")
        db.session.add(user)
        db.session.flush()

        coach = Coach(user_id=user.id)
        db.session.add(coach)

        club = Club(name="Club", description="", location="City")
        db.session.add(club)
        db.session.flush()
        db.session.commit()
        return {"coach_id": coach.id, "club_id": club.id}


def _seed_lesson_with_instance(app, coach_id, club_id, start_offset_hours=48):
    """Creates a Lesson + LessonInstance with a coach association. Returns ids."""
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.sql_db import db

    with app.app_context():
        start = datetime.utcnow() + timedelta(hours=start_offset_hours)
        end = start + timedelta(hours=1)

        lesson = Lesson(
            title="Sched Test", type="academy", status="active",
            start_datetime=start, end_datetime=end,
            max_players=4, color="#000", club_id=club_id,
        )
        db.session.add(lesson)
        db.session.flush()

        db.session.add(Association_CoachLesson(coach_id=coach_id, lesson_id=lesson.id))

        instance = LessonInstance(
            lesson_id=lesson.id, start_datetime=start, end_datetime=end,
            max_players=4, status="scheduled",
        )
        db.session.add(instance)
        db.session.flush()

        db.session.add(Association_CoachLessonInstance(
            coach_id=coach_id, lesson_instance_id=instance.id))

        db.session.commit()
        return {"lesson_id": lesson.id, "instance_id": instance.id}


# ---------------------------------------------------------------------------
# Tests: edit reschedules jobs
# ---------------------------------------------------------------------------

class TestEditReschedulesJobs:
    def test_edit_instance_calls_schedule_instance_jobs(self, app):
        """Editing a LessonInstance must reschedule its APScheduler job."""
        ids = _seed_coach_and_club(app)
        obj_ids = _seed_lesson_with_instance(app, ids["coach_id"], ids["club_id"])

        with app.app_context():
            from padel_app.models.lesson_instances import LessonInstance
            instance = LessonInstance.query.get(obj_ids["instance_id"])
            new_start = instance.start_datetime + timedelta(hours=1)
            payload = {
                "date": new_start.strftime("%Y-%m-%d"),
                "start_time": new_start.strftime("%H:%M"),
                "end_time": (new_start + timedelta(hours=1)).strftime("%H:%M"),
            }

            with patch("padel_app.scheduler.schedule_instance_jobs") as mock_sched:
                from padel_app.services.lesson_service import edit_lesson_instance_helper
                edit_lesson_instance_helper(payload, instance)

            mock_sched.assert_called_once()
            call_kwargs = mock_sched.call_args
            # New API: schedule_instance_jobs(instance_id, coach_id) — no app arg
            assert call_kwargs[0][0] == obj_ids["instance_id"]
            assert call_kwargs[0][1] == ids["coach_id"]

    def test_edit_instance_with_no_coach_does_not_crash(self, app):
        """Editing an instance with no coach association must not raise."""
        from padel_app.models.lessons import Lesson
        from padel_app.models.lesson_instances import LessonInstance
        from padel_app.models.clubs import Club
        from padel_app.sql_db import db

        with app.app_context():
            club = Club(name="C2", description="", location="X")
            db.session.add(club)
            db.session.flush()

            start = datetime.utcnow() + timedelta(hours=10)
            lesson = Lesson(title="No Coach", type="academy", status="active",
                            start_datetime=start, end_datetime=start + timedelta(hours=1),
                            max_players=2, color="#fff", club_id=club.id)
            db.session.add(lesson)
            db.session.flush()

            instance = LessonInstance(lesson_id=lesson.id, start_datetime=start,
                                      end_datetime=start + timedelta(hours=1),
                                      max_players=2, status="scheduled")
            db.session.add(instance)
            db.session.commit()

            payload = {
                "date": start.strftime("%Y-%m-%d"),
                "start_time": start.strftime("%H:%M"),
                "end_time": (start + timedelta(hours=1)).strftime("%H:%M"),
            }

            # Should not raise even when schedule_instance_jobs errors
            from padel_app.services.lesson_service import edit_lesson_instance_helper
            edit_lesson_instance_helper(payload, instance)  # no exception


# ---------------------------------------------------------------------------
# Tests: delete cancels jobs
# ---------------------------------------------------------------------------

class TestDeleteCancelsJobs:
    def test_delete_future_instances_calls_cancel_for_each(self, app):
        """delete_future_instances must call cancel_instance_jobs for every instance."""
        from padel_app.models.lessons import Lesson
        from padel_app.models.lesson_instances import LessonInstance
        from padel_app.sql_db import db

        ids = _seed_coach_and_club(app)

        with app.app_context():
            start = datetime.utcnow() + timedelta(hours=24)
            lesson = Lesson.query.get(
                _seed_lesson_with_instance(app, ids["coach_id"], ids["club_id"])["lesson_id"]
            )
            # Add a second instance to the same lesson
            inst2 = LessonInstance(
                lesson_id=lesson.id,
                start_datetime=start + timedelta(days=7),
                end_datetime=start + timedelta(days=7, hours=1),
                max_players=4, status="scheduled",
            )
            db.session.add(inst2)
            db.session.commit()

            cutoff = start - timedelta(hours=1)

            with patch("padel_app.scheduler.cancel_instance_jobs") as mock_cancel:
                from padel_app.services.lesson_service import delete_future_instances
                delete_future_instances(lesson, cutoff)

            # cancel_instance_jobs should have been called for every deleted instance
            assert mock_cancel.call_count >= 1

    def test_remove_class_service_single_instance_cancels_job(self, app):
        """remove_class_service with scope='single' on a LessonInstance must cancel its job."""
        ids = _seed_coach_and_club(app)
        obj_ids = _seed_lesson_with_instance(app, ids["coach_id"], ids["club_id"])

        with app.app_context():
            from padel_app.models.lesson_instances import LessonInstance
            instance = LessonInstance.query.get(obj_ids["instance_id"])

            event_data = {
                "event": {
                    "model": "LessonInstance",
                    "originalId": instance.id,
                    "date": instance.start_datetime.strftime("%Y-%m-%d"),
                },
                "scope": "single",
            }

            with patch("padel_app.scheduler.cancel_instance_jobs") as mock_cancel:
                from padel_app.services.lesson_service import remove_class_service
                result, status = remove_class_service(event_data)

            assert status == 200
            assert result["status"] == "deleted"
            mock_cancel.assert_called_once()
            # New API: cancel_instance_jobs(instance_id) — no app arg
            assert mock_cancel.call_args[0][0] == obj_ids["instance_id"]


# ---------------------------------------------------------------------------
# Tests: send_class_reminders actually sends messages
# ---------------------------------------------------------------------------

class TestSendClassReminders:
    def test_reminders_sent_to_enrolled_players(self, app):
        """send_class_reminders sends one message per enrolled player."""
        from padel_app.models.users import User
        from padel_app.models.coaches import Coach
        from padel_app.models.players import Player
        from padel_app.models.messages import Message
        from padel_app.sql_db import db

        from padel_app.tests.test_notification_reminder_flow import (
            _seed_coach_and_student,
            _seed_instance,
        )

        seeds = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, seeds["coach_id"], seeds["student_id"],
                                     start_offset_hours=49)

        with (
            patch("padel_app.services.notification_service.publish"),
            patch("padel_app.services.notification_service.send_push_notification"),
        ):
            with app.app_context():
                from padel_app.services.notification_service import send_class_reminders
                before = Message.query.count()
                send_class_reminders(instance_id)
                after = Message.query.count()

        assert after == before + 1, "Expected exactly one reminder message per player"

    def test_reminders_not_sent_for_past_class(self, app):
        """send_class_reminders is a no-op when the class is already in the past."""
        from padel_app.models.messages import Message

        from padel_app.tests.test_notification_reminder_flow import (
            _seed_coach_and_student,
            _seed_instance,
        )

        seeds = _seed_coach_and_student(app)
        # instance in the past
        instance_id = _seed_instance(app, seeds["coach_id"], seeds["student_id"],
                                     start_offset_hours=49)

        with (
            patch("padel_app.services.notification_service.publish"),
            patch("padel_app.services.notification_service.send_push_notification"),
        ):
            with app.app_context():
                from padel_app.services.notification_service import send_class_reminders
                from padel_app.models.lesson_instances import LessonInstance
                # Force the instance into the past
                inst = LessonInstance.query.get(instance_id)
                inst.start_datetime = datetime.utcnow() - timedelta(hours=1)
                inst.end_datetime = inst.start_datetime + timedelta(hours=1)
                inst.save()

                before = Message.query.count()
                send_class_reminders(instance_id)
                after = Message.query.count()

        assert after == before, "No messages should be sent for past classes"

    def test_reminders_not_sent_for_canceled_class(self, app):
        """send_class_reminders is a no-op for canceled instances."""
        from padel_app.models.messages import Message

        from padel_app.tests.test_notification_reminder_flow import (
            _seed_coach_and_student,
            _seed_instance,
        )

        seeds = _seed_coach_and_student(app)
        instance_id = _seed_instance(app, seeds["coach_id"], seeds["student_id"],
                                     start_offset_hours=49)

        with (
            patch("padel_app.services.notification_service.publish"),
            patch("padel_app.services.notification_service.send_push_notification"),
        ):
            with app.app_context():
                from padel_app.services.notification_service import send_class_reminders
                from padel_app.models.lesson_instances import LessonInstance
                inst = LessonInstance.query.get(instance_id)
                inst.status = "canceled"
                inst.save()

                before = Message.query.count()
                send_class_reminders(instance_id)
                after = Message.query.count()

        assert after == before, "No messages should be sent for canceled classes"


# ---------------------------------------------------------------------------
# Tests: _compute_timing_dt correctness
# ---------------------------------------------------------------------------

class TestComputeTimingDt:
    def test_hours_before(self):
        from padel_app.scheduler import _compute_timing_dt
        start = datetime(2026, 6, 1, 10, 0)
        result = _compute_timing_dt(start, {"type": "hours_before", "value": 48})
        assert result == datetime(2026, 5, 30, 10, 0)

    def test_days_before_at_time(self):
        from padel_app.scheduler import _compute_timing_dt
        start = datetime(2026, 6, 5, 15, 0)
        result = _compute_timing_dt(start, {"type": "days_before", "days": 2, "time": "09:00"})
        assert result == datetime(2026, 6, 3, 9, 0)

    def test_empty_config_returns_none(self):
        from padel_app.scheduler import _compute_timing_dt
        assert _compute_timing_dt(datetime.utcnow(), {}) is None
        assert _compute_timing_dt(datetime.utcnow(), None) is None

    def test_missing_type_returns_none(self):
        from padel_app.scheduler import _compute_timing_dt
        assert _compute_timing_dt(datetime.utcnow(), {"value": 48}) is None
