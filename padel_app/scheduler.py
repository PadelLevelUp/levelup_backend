"""
APScheduler integration for the LevelUp notification engine.

Architecture
------------
- Module-level singletons: ``_app`` and ``_scheduler`` are set once by
  ``init_scheduler()`` and reused by all public functions.  Job runner
  functions look them up at *call time* rather than receiving them as
  pickled arguments — the Flask app object is not picklable.

Jobs
----
- ``reminder_lesson_{lesson_id}_{YYYY-MM-DD}`` — DateTrigger — fires _run_reminder_for_lesson_occurrence()
- ``reminder_{instance_id}``                   — DateTrigger — fires send_class_reminders() (legacy, for already-materialized instances)
- ``invite_start_{instance_id}``               — DateTrigger — fires trigger_invitations()
- ``process_batches``                          — IntervalTrigger (2 min) — fires process_invitation_batches()
- ``extend_schedule_window``                   — IntervalTrigger (7 days) — extends 60-day reminder horizon

Public API (no ``app`` argument needed)
---------------------------------------
- ``init_scheduler(app, test_config=None)``
- ``schedule_lesson_reminder_jobs(lesson_id, coach_id, horizon_days=60)``
- ``cancel_lesson_reminder_jobs(lesson_id, from_date=None)``
- ``cancel_lesson_occurrence_job(lesson_id, date_str)``
- ``schedule_instance_jobs(instance_id, coach_id)``
- ``cancel_instance_jobs(instance_id)``
- ``reschedule_all_future_jobs(coach_id)``

Convenience hooks for lesson_service (safe no-ops when scheduler is absent)
---------------------------------------------------------------------------
- ``_maybe_schedule_instance(instance)``
- ``_maybe_cancel_instance(instance_id)``
"""

from __future__ import annotations

import atexit
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_app = None        # Flask application instance — set by init_scheduler()
_scheduler = None  # BackgroundScheduler instance — set by init_scheduler()


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------

@contextmanager
def _app_ctx():
    """Push an app context only when one isn't already active.

    When scheduler job functions are called from APScheduler's background
    thread there is no active Flask context, so we push one.  When the same
    function is called from a request handler (e.g. update_config →
    reschedule_all_future_jobs) a context is already active; creating a
    *nested* one would cause its teardown handler to fire on exit, which
    calls db.session.remove() and detaches objects from the outer request.
    """
    from flask import has_app_context
    if has_app_context():
        yield
    else:
        with _app.app_context():
            yield


# ---------------------------------------------------------------------------
# Timing helpers (pure functions — no Flask dependency)
# ---------------------------------------------------------------------------

def _compute_timing_dt(instance_start: datetime, timing_config: dict) -> datetime | None:
    """Return the absolute UTC datetime for a timing config relative to class start.

    Accepted shapes:
      {"type": "hours_before",       "value": N}
      {"type": "days_before",        "days": N, "time": "HH:MM"}
      {"type": "days_before_at_time","days": N, "time": "HH:MM"}

    ``instance_start`` must be a naive UTC datetime.
    """
    if not timing_config:
        return None

    t = timing_config.get("type")

    if t == "hours_before":
        value = int(timing_config.get("value", 24))
        return instance_start - timedelta(hours=value)

    if t in ("days_before", "days_before_at_time"):
        days = int(timing_config.get("days", 1))
        time_str = timing_config.get("time", "09:00")
        try:
            hour, minute = (int(p) for p in time_str.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 9, 0
        target_date = instance_start.date() - timedelta(days=days)
        return datetime(target_date.year, target_date.month, target_date.day, hour, minute)

    return None


def _compute_reminder_dt(instance, timing_config: dict) -> datetime | None:
    return _compute_timing_dt(instance.start_datetime, timing_config)


def _compute_invite_start_dt(instance, timing_config: dict) -> datetime | None:
    return _compute_timing_dt(instance.start_datetime, timing_config)


# ---------------------------------------------------------------------------
# Job runner functions
# (called by APScheduler on its background thread — no app arg, use _app)
# ---------------------------------------------------------------------------

def _run_reminder_for_lesson_occurrence(lesson_id: int, date_str: str) -> None:
    """Materialize a lesson occurrence (if needed) and send reminders.

    This is the primary reminder runner — works directly from the Lesson
    template without requiring a LessonInstance to exist beforehand.
    """
    app = _app
    if app is None:
        return
    with app.app_context():
        from datetime import date as _date
        from padel_app.models import Lesson
        from padel_app.services.lesson_service import get_or_materialize_instance
        from padel_app.services.notification_service import send_class_reminders
        try:
            app.logger.info(
                "reminder_for_lesson_occurrence: lesson=%s date=%s — starting",
                lesson_id, date_str,
            )
            lesson = Lesson.query.get(lesson_id)
            if not lesson:
                app.logger.warning(
                    "reminder_for_lesson_occurrence: lesson %s not found — skipping",
                    lesson_id,
                )
                return
            date = _date.fromisoformat(date_str)
            instance = get_or_materialize_instance(lesson, date)
            if instance.status in ("canceled", "completed"):
                app.logger.info(
                    "reminder_for_lesson_occurrence: instance %s status=%s — skipping",
                    instance.id, instance.status,
                )
                return
            send_class_reminders(instance.id)
            app.logger.info(
                "reminder_for_lesson_occurrence: lesson=%s date=%s instance=%s — done",
                lesson_id, date_str, instance.id,
            )
        except Exception as exc:
            app.logger.error(
                "reminder_for_lesson_occurrence(%s, %s) failed: %s",
                lesson_id, date_str, exc,
            )


def _run_send_reminders(instance_id: int) -> None:
    """Legacy runner for already-materialized LessonInstance reminders."""
    app = _app
    if app is None:
        return
    with app.app_context():
        from padel_app.services.notification_service import send_class_reminders
        try:
            send_class_reminders(instance_id)
            app.logger.info("Reminder sent for instance %s", instance_id)
        except Exception as exc:
            app.logger.error("send_class_reminders(%s) failed: %s", instance_id, exc)


def _run_trigger_invitations(instance_id: int, coach_id: int) -> None:
    app = _app
    if app is None:
        return
    with app.app_context():
        from padel_app.models import LessonInstance
        from padel_app.services.notification_service import trigger_invitations
        try:
            instance = LessonInstance.query.get(instance_id)
            if instance:
                trigger_invitations(instance, coach_id)
        except Exception as exc:
            app.logger.error("trigger_invitations(%s) failed: %s", instance_id, exc)


def _run_process_batches() -> None:
    app = _app
    if app is None:
        return
    with app.app_context():
        from padel_app.services.notification_service import process_invitation_batches
        try:
            process_invitation_batches()
        except Exception as exc:
            app.logger.error("process_invitation_batches failed: %s", exc)


def _run_extend_schedule_window() -> None:
    """Extend the rolling 60-day reminder horizon for all coaches.

    Runs weekly so that lesson occurrences entering the 60-day window
    are always covered, even for long-running recurring series.
    """
    app = _app
    if app is None:
        return
    with app.app_context():
        try:
            from padel_app.models import Coach
            total = 0
            for coach in Coach.query.all():
                total += _schedule_lesson_occurrences_for_coach(coach.id)
            app.logger.info(
                "extend_schedule_window: scheduled %d lesson occurrence reminder jobs",
                total,
            )
        except Exception as exc:
            app.logger.error("extend_schedule_window failed: %s", exc)



# ---------------------------------------------------------------------------
# Scheduler initialisation
# ---------------------------------------------------------------------------

def init_scheduler(app, test_config=None) -> None:
    """Create and start the APScheduler BackgroundScheduler.

    Skipped in these contexts:
    - Tests:             ``test_config`` is not None
    - Flask CLI:         ``flask db upgrade``, ``flask shell``, etc.
    - Werkzeug watcher:  outer watcher process (``WERKZEUG_RUN_MAIN`` set but ≠ "true")
    """
    global _app, _scheduler

    # Skip during pytest / test runs
    if test_config is not None:
        return

    # Skip during Flask CLI sub-commands that are not `flask run`
    argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
    if argv0 in ("flask", "flask.exe") and len(sys.argv) > 1 and sys.argv[1] != "run":
        return

    # Werkzeug dev-reloader spawns two processes:
    #   outer watcher  → WERKZEUG_RUN_MAIN is set but not "true"  → skip
    #   inner worker   → WERKZEUG_RUN_MAIN == "true"              → proceed
    # Production / gunicorn → WERKZEUG_RUN_MAIN not set at all    → proceed
    werkzeug_run_main = os.environ.get("WERKZEUG_RUN_MAIN")
    if app.debug and werkzeug_run_main is not None and werkzeug_run_main != "true":
        return

    _app = app

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    db_url = app.config["SQLALCHEMY_DATABASE_URI"]
    jobstores = {
        "default": SQLAlchemyJobStore(
            url=db_url,
            engine_options={
                "pool_pre_ping": True,   # survive DB restart / test DB reset
                "pool_recycle": 300,     # proactively recycle idle connections
                "pool_size": 2,          # scheduler needs few connections
                "max_overflow": 1,
            },
        )
    }

    sched = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

    # TEST_MODE shortens the batch interval for E2E verification
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    batch_seconds = 30 if test_mode else 120

    sched.add_job(
        func=_run_process_batches,
        trigger=IntervalTrigger(seconds=batch_seconds),
        id="process_batches",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )

    sched.add_job(
        func=_run_extend_schedule_window,
        trigger=IntervalTrigger(days=7),
        id="extend_schedule_window",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    sched.start()
    _scheduler = sched
    app.extensions["scheduler"] = sched

    # Graceful shutdown when the process exits
    atexit.register(lambda: sched.shutdown(wait=False))

    # Re-schedule all future jobs in case the server restarted and jobs were lost
    _startup_reschedule(app)


def _startup_reschedule(app) -> None:
    """Idempotently re-schedule all future reminder/invite jobs at startup."""
    try:
        with app.app_context():
            from padel_app.models import Coach
            instance_total = 0
            lesson_total = 0
            for coach in Coach.query.all():
                instance_total += _reschedule_for_coach(coach.id)
                lesson_total += _schedule_lesson_occurrences_for_coach(coach.id)
            app.logger.info(
                "APScheduler started — rescheduled %d instance jobs + %d lesson occurrence jobs across all coaches.",
                instance_total,
                lesson_total,
            )
    except Exception as exc:
        app.logger.warning("APScheduler startup rescheduling failed: %s", exc)


def _reschedule_for_coach(coach_id: int) -> int:
    """Schedule/replace jobs for all future non-cancelled instances of a coach.

    Returns the number of instances processed.
    """
    if _scheduler is None or _app is None:
        return 0

    with _app_ctx():
        from padel_app.models import Association_CoachLessonInstance, LessonInstance

        now = datetime.utcnow()
        instances = (
            LessonInstance.query
            .join(
                Association_CoachLessonInstance,
                LessonInstance.id == Association_CoachLessonInstance.lesson_instance_id,
            )
            .filter(
                Association_CoachLessonInstance.coach_id == coach_id,
                LessonInstance.start_datetime > now,
                LessonInstance.status != "canceled",
            )
            .all()
        )

        for instance in instances:
            schedule_instance_jobs(instance.id, coach_id)

        return len(instances)


def _schedule_lesson_occurrences_for_coach(coach_id: int) -> int:
    """Schedule lesson-level reminder jobs for all active lessons of a coach.

    Returns the total number of jobs scheduled.
    """
    if _scheduler is None or _app is None:
        return 0

    with _app_ctx():
        from padel_app.models import Association_CoachLesson, Lesson

        lessons = (
            Lesson.query
            .join(Association_CoachLesson, Lesson.id == Association_CoachLesson.lesson_id)
            .filter(
                Association_CoachLesson.coach_id == coach_id,
                Lesson.status == "active",
            )
            .all()
        )

        total = 0
        for lesson in lessons:
            total += schedule_lesson_reminder_jobs(lesson.id, coach_id)
        return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def schedule_lesson_reminder_jobs(
    lesson_id: int,
    coach_id: int,
    *,
    horizon_days: int = 60,
    now: datetime | None = None,
) -> int:
    """Schedule DateTrigger reminder jobs for all upcoming occurrences of a lesson
    within the next ``horizon_days``.

    Works for both recurring and non-recurring lessons.  When the job fires it
    materializes the LessonInstance (if not already done) and sends reminders.

    Returns the number of jobs scheduled.
    """
    if _scheduler is None or _app is None:
        return 0

    from apscheduler.triggers.date import DateTrigger
    from padel_app.tools.calendar_tools import expand_occurrences

    with _app_ctx():
        from padel_app.models import Lesson
        from padel_app.services.notification_service import get_or_create_config

        lesson = Lesson.query.get(lesson_id)
        if not lesson:
            return 0

        config = get_or_create_config(coach_id)
        cutoff = now or datetime.utcnow()
        horizon = cutoff + timedelta(days=horizon_days)

        occurrences = expand_occurrences(
            lesson.start_datetime,
            lesson.recurrence_rule,
            lesson.recurrence_end,
            cutoff,
            horizon,
        )

        scheduled = 0
        for occ_dt in occurrences:
            # expand_occurrences returns tz-aware UTC; _compute_timing_dt needs naive UTC
            occ_dt_naive = occ_dt.replace(tzinfo=None) if occ_dt.tzinfo else occ_dt
            reminder_dt = _compute_timing_dt(occ_dt_naive, config.get_reminder_timing())

            if not reminder_dt:
                continue

            date_str = occ_dt_naive.date().isoformat()

            if reminder_dt <= cutoff:
                _app.logger.warning(
                    "schedule_lesson_reminder_jobs: reminder for lesson %s on %s is in the past (%s) — skipping",
                    lesson_id, date_str, reminder_dt,
                )
                continue

            job_id = f"reminder_lesson_{lesson_id}_{date_str}"
            _scheduler.add_job(
                func=_run_reminder_for_lesson_occurrence,
                args=[lesson_id, date_str],
                trigger=DateTrigger(run_date=reminder_dt),
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            _app.logger.info(
                "schedule_lesson_reminder_jobs: scheduled %s to fire at %s",
                job_id, reminder_dt,
            )
            scheduled += 1

        return scheduled


def cancel_lesson_reminder_jobs(lesson_id: int, from_date=None) -> None:
    """Remove all lesson-level reminder jobs for a lesson.

    If ``from_date`` (a ``datetime.date``) is given, only removes jobs for
    occurrences on or after that date.  Used when a recurrence is truncated.
    """
    if _scheduler is None:
        return

    prefix = f"reminder_lesson_{lesson_id}_"
    for job in list(_scheduler.get_jobs()):
        if not job.id.startswith(prefix):
            continue
        if from_date is not None:
            try:
                from datetime import date as _date
                job_date = _date.fromisoformat(job.id[len(prefix):])
                if job_date < from_date:
                    continue
            except ValueError:
                pass
        try:
            job.remove()
        except Exception:
            pass


def cancel_lesson_occurrence_job(lesson_id: int, date_str: str) -> None:
    """Remove the reminder job for a single lesson occurrence."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"reminder_lesson_{lesson_id}_{date_str}")
    except Exception:
        pass


def schedule_instance_jobs(instance_id: int, coach_id: int, *, now: datetime | None = None) -> None:
    """Schedule (or replace) reminder + invitation-start jobs for a materialized instance.

    Jobs whose computed fire time is in the past are skipped with a warning.
    Pass ``now`` in tests to control the "future" boundary.
    """
    if _scheduler is None or _app is None:
        return

    from apscheduler.triggers.date import DateTrigger

    with _app_ctx():
        from padel_app.models import LessonInstance
        from padel_app.services.notification_service import get_or_create_config

        instance = LessonInstance.query.get(instance_id)
        if not instance:
            return

        config = get_or_create_config(coach_id)
        cutoff = now or datetime.utcnow()

        reminder_dt = _compute_reminder_dt(instance, config.get_reminder_timing())
        if reminder_dt and reminder_dt > cutoff:
            _scheduler.add_job(
                func=_run_send_reminders,
                args=[instance_id],
                trigger=DateTrigger(run_date=reminder_dt),
                id=f"reminder_{instance_id}",
                replace_existing=True,
                misfire_grace_time=300,
            )
        elif reminder_dt:
            _app.logger.warning(
                "schedule_instance_jobs: reminder for instance %s is in the past (%s) — skipping",
                instance_id, reminder_dt,
            )

        invite_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
        if invite_dt and invite_dt > cutoff:
            _scheduler.add_job(
                func=_run_trigger_invitations,
                args=[instance_id, coach_id],
                trigger=DateTrigger(run_date=invite_dt),
                id=f"invite_start_{instance_id}",
                replace_existing=True,
                misfire_grace_time=300,
            )


def cancel_instance_jobs(instance_id: int) -> None:
    """Remove scheduled reminder + invitation-start jobs for an instance."""
    if _scheduler is None:
        return

    for job_id in (f"reminder_{instance_id}", f"invite_start_{instance_id}"):
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass  # job may not exist — that's fine


def reschedule_all_future_jobs(coach_id: int) -> None:
    """Reschedule all future jobs for a coach.

    Called when the coach updates reminder / invitation-start timing in settings.
    Also reschedules lesson-level occurrence jobs so the new timing applies.
    """
    if _scheduler is None or _app is None:
        return

    with _app_ctx():
        from padel_app.models import Association_CoachLessonInstance, LessonInstance

        now = datetime.utcnow()
        instances = (
            LessonInstance.query
            .join(
                Association_CoachLessonInstance,
                LessonInstance.id == Association_CoachLessonInstance.lesson_instance_id,
            )
            .filter(
                Association_CoachLessonInstance.coach_id == coach_id,
                LessonInstance.start_datetime > now,
                LessonInstance.status != "canceled",
            )
            .all()
        )

        for instance in instances:
            schedule_instance_jobs(instance.id, coach_id)

        # Re-schedule lesson-level occurrence jobs with updated timing
        _schedule_lesson_occurrences_for_coach(coach_id)


# ---------------------------------------------------------------------------
# Convenience hooks for lesson_service
# (safe no-ops when the scheduler is not running: tests, CLI, etc.)
# ---------------------------------------------------------------------------

def _maybe_schedule_instance(instance) -> None:
    """Schedule jobs for an instance if the scheduler is running.

    Resolves coach_id from the instance's coaches_relations.
    Logs failures instead of silently swallowing them.
    """
    try:
        coach_rels = getattr(instance, "coaches_relations", None)
        if coach_rels:
            schedule_instance_jobs(instance.id, coach_rels[0].coach_id)
        else:
            if _app:
                _app.logger.warning(
                    "_maybe_schedule_instance: instance %s has no coaches_relations — skipping",
                    getattr(instance, "id", "?"),
                )
    except Exception as exc:
        if _app:
            _app.logger.warning(
                "_maybe_schedule_instance(%s) failed: %s",
                getattr(instance, "id", "?"),
                exc,
            )


def _maybe_cancel_instance(instance_id: int) -> None:
    """Cancel jobs for an instance if the scheduler is running.

    Silently ignores all errors.
    """
    try:
        cancel_instance_jobs(instance_id)
    except Exception:
        pass
