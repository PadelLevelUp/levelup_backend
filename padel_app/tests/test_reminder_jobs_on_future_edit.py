"""
Reminder jobs are lost when a recurring class is edited "this and all future"
from one of its materialized occurrences.

`edit_class_service(model="LessonInstance", scope="future")` delegates to
`_apply_future_edit_to_lesson()`, which — whenever the edited occurrence is not
the series' own start date — calls `duplicate_lesson_helper()` to split the
series into a **brand new Lesson** and truncates the parent's `recurrence_end`.

The sibling branch (`model="Lesson"`, scope="future") cancels the parent's
occurrence jobs and schedules jobs for the resulting lesson. The
`model="LessonInstance"` branch did neither, so the new lesson carried **no
APScheduler reminder jobs at all**: none of its occurrences ever materialized
and none of its students were ever reminded. Nothing raised and nothing was
logged — the only recovery was the weekly `extend_schedule_window` pass, up to
7 days later, which never back-fills the occurrences missed in between.

Observed in prod 2026-08-04: coach "Preparação Mundial" (Mon–Fri, 6 enrolled
students) was split into a new lesson by exactly this edit and had zero rows in
`apscheduler_jobs`; no reminders went out for any of its classes.

Covered spec: classes.instances (recurring occurrence materialization) +
notifications.reminders (every scheduled class reminds its enrolled students)
"""
import json
from datetime import datetime, timedelta

import pytest

from padel_app.sql_db import db


@pytest.fixture
def memory_scheduler(app):
    """Point the scheduler module at a real BackgroundScheduler with an
    in-memory jobstore, so tests assert on the jobs actually created rather
    than on a mock having been called.

    Started paused: jobs are registered but never executed.
    """
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.background import BackgroundScheduler
    from padel_app import scheduler as sched_mod

    prev_app, prev_sched = sched_mod._app, sched_mod._scheduler

    sched = BackgroundScheduler(jobstores={"default": MemoryJobStore()}, timezone="UTC")
    sched.start(paused=True)
    sched_mod._app, sched_mod._scheduler = app, sched

    yield sched

    sched.shutdown(wait=False)
    sched_mod._app, sched_mod._scheduler = prev_app, prev_sched


@pytest.fixture
def recurring_series(app):
    """Coach + a weekly recurring lesson at 10:00 running for 6 weeks.
    Returns (coach_id, lesson_id, first_start)."""
    from padel_app.models import User
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.clubs import Club
    from padel_app.models.coaches import Coach
    from padel_app.models.lessons import Lesson

    with app.app_context():
        coach_user = User(name="Coach", username="future_edit_coach", password="x")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name="Future Edit Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        start = (
            datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        lesson = Lesson(
            title="Recurring Series",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=True,
            recurrence_rule=json.dumps(
                {"frequency": "weekly", "daysOfWeek": [(start.weekday() + 1) % 7]}
            ),
            recurrence_end=(start + timedelta(weeks=6)).date(),
            type="academy",
            max_players=4,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()
        db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
        db.session.commit()
        return coach.id, lesson.id, start


def _reminder_job_ids(sched, lesson_id):
    prefix = f"reminder_lesson_{lesson_id}_"
    return sorted(j.id for j in sched.get_jobs() if j.id.startswith(prefix))


def test_future_edit_from_instance_schedules_jobs_for_the_new_lesson(
    app, memory_scheduler, recurring_series
):
    """The lesson the split produces must carry reminder jobs of its own."""
    coach_id, lesson_id, first_start = recurring_series
    from padel_app.models import Lesson
    from padel_app.scheduler import schedule_lesson_reminder_jobs
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    # Jobs as they exist before the edit, for the original series.
    with app.app_context():
        schedule_lesson_reminder_jobs(lesson_id, coach_id)
    jobs_before = _reminder_job_ids(memory_scheduler, lesson_id)
    assert jobs_before, "precondition: the original series must have reminder jobs"

    # Edit "this and all future" from a LATER occurrence. Editing at the
    # series' own start date would leave `lesson_to_edit is lesson` and the
    # assertions below would hold without the fix — the split must be forced.
    occ_date = (first_start + timedelta(weeks=2)).date()

    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, occ_date)
        instance_id = instance.id
        db.session.commit()

    with app.app_context():
        result, status = edit_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_id,
                "date": occ_date.isoformat(),
            },
            "scope": "future",
            "updates": {"startTime": "14:00", "endTime": "15:00"},
        })
        db.session.commit()

    assert status == 201
    new_lesson_id = result["id"]

    # Guards against a vacuous pass: the edit really did split the series.
    assert new_lesson_id != lesson_id, (
        "expected a new Lesson from duplicate_lesson_helper — without a split "
        "this test cannot detect the missing scheduling"
    )

    new_jobs = _reminder_job_ids(memory_scheduler, new_lesson_id)
    assert new_jobs, (
        f"lesson {new_lesson_id} carries the rest of the series but has no "
        f"reminder jobs — its classes would silently send no reminders"
    )


def test_future_edit_from_instance_cancels_the_parents_orphaned_jobs(
    app, memory_scheduler, recurring_series
):
    """The parent's recurrence is truncated at the boundary, so its jobs at or
    after that date now point at occurrences the parent no longer produces."""
    coach_id, lesson_id, first_start = recurring_series
    from padel_app.models import Lesson
    from padel_app.scheduler import schedule_lesson_reminder_jobs
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    with app.app_context():
        schedule_lesson_reminder_jobs(lesson_id, coach_id)

    occ_date = (first_start + timedelta(weeks=2)).date()
    boundary = f"reminder_lesson_{lesson_id}_{occ_date.isoformat()}"

    assert boundary in _reminder_job_ids(memory_scheduler, lesson_id), (
        "precondition: the parent must own a job at the split boundary"
    )

    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, occ_date)
        instance_id = instance.id
        db.session.commit()

    with app.app_context():
        _, status = edit_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_id,
                "date": occ_date.isoformat(),
            },
            "scope": "future",
            "updates": {"startTime": "14:00", "endTime": "15:00"},
        })
        db.session.commit()

    assert status == 201

    remaining = _reminder_job_ids(memory_scheduler, lesson_id)
    orphaned = [
        job_id for job_id in remaining
        if job_id[len(f"reminder_lesson_{lesson_id}_"):] >= occ_date.isoformat()
    ]
    assert not orphaned, (
        f"parent lesson kept jobs at/after the truncation boundary: {orphaned}"
    )


def test_moved_date_edit_keeps_the_parents_jobs_before_the_new_boundary(
    app, memory_scheduler, recurring_series
):
    """When the edit MOVES the date, the parent is truncated at `new_date`, not
    at `event_date`. Occurrences in [event_date, new_date) still belong to the
    parent, so their reminder jobs must survive."""
    coach_id, lesson_id, first_start = recurring_series
    from padel_app.models import Lesson
    from padel_app.scheduler import schedule_lesson_reminder_jobs
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    with app.app_context():
        schedule_lesson_reminder_jobs(lesson_id, coach_id)

    occ_date = (first_start + timedelta(weeks=2)).date()
    moved_to = (first_start + timedelta(weeks=4)).date()

    # A job that sits strictly between the edited occurrence and the new
    # boundary — the parent keeps this occurrence, so it must keep its job.
    survivor = f"reminder_lesson_{lesson_id}_{(first_start + timedelta(weeks=3)).date().isoformat()}"
    assert survivor in _reminder_job_ids(memory_scheduler, lesson_id), (
        "precondition: parent must own a job between event_date and new_date"
    )

    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, occ_date)
        instance_id = instance.id
        db.session.commit()

    with app.app_context():
        _, status = edit_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_id,
                "date": occ_date.isoformat(),
            },
            "scope": "future",
            "updates": {"date": moved_to.isoformat()},
        })
        db.session.commit()

    assert status == 201
    assert survivor in _reminder_job_ids(memory_scheduler, lesson_id), (
        "cancelled a job for an occurrence the truncated parent still produces"
    )


def test_moved_date_edit_via_lesson_model_keeps_jobs_before_the_boundary(
    app, memory_scheduler, recurring_series
):
    """Same boundary rule for the sibling `model="Lesson"` branch, which had the
    same off-by-one (it cancelled from event_date rather than the new date)."""
    coach_id, lesson_id, first_start = recurring_series
    from padel_app.scheduler import schedule_lesson_reminder_jobs
    from padel_app.services.lesson_service import edit_class_service

    with app.app_context():
        schedule_lesson_reminder_jobs(lesson_id, coach_id)

    occ_date = (first_start + timedelta(weeks=2)).date()
    moved_to = (first_start + timedelta(weeks=4)).date()
    survivor = f"reminder_lesson_{lesson_id}_{(first_start + timedelta(weeks=3)).date().isoformat()}"

    assert survivor in _reminder_job_ids(memory_scheduler, lesson_id)

    with app.app_context():
        _, status = edit_class_service({
            "event": {
                "model": "Lesson",
                "originalId": lesson_id,
                "date": occ_date.isoformat(),
            },
            "scope": "future",
            "updates": {"date": moved_to.isoformat()},
        })
        db.session.commit()

    assert status == 201
    assert survivor in _reminder_job_ids(memory_scheduler, lesson_id), (
        "cancelled a job for an occurrence the truncated parent still produces"
    )


def test_future_edit_survives_a_scheduler_failure(
    app, memory_scheduler, recurring_series
):
    """The edit is committed before the scheduler is touched, so a scheduler
    failure must not turn a successful edit into an error response (PAD-10)."""
    from unittest.mock import patch

    coach_id, lesson_id, first_start = recurring_series
    from padel_app.models import Lesson
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    occ_date = (first_start + timedelta(weeks=2)).date()

    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, occ_date)
        instance_id = instance.id
        db.session.commit()

    with app.app_context():
        with patch(
            "padel_app.scheduler.schedule_lesson_reminder_jobs",
            side_effect=RuntimeError("jobstore down"),
        ):
            result, status = edit_class_service({
                "event": {
                    "model": "LessonInstance",
                    "originalId": instance_id,
                    "date": occ_date.isoformat(),
                },
                "scope": "future",
                "updates": {"startTime": "14:00", "endTime": "15:00"},
            })
            db.session.commit()

    assert status == 201, "a scheduler failure must not fail the edit"

    # The edit itself really landed rather than being rolled back.
    with app.app_context():
        edited = Lesson.query.get(result["id"])
        assert edited.start_datetime.strftime("%H:%M") == "14:00"
