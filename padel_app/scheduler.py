"""
APScheduler integration for the notification engine.

Manages two types of jobs:
  - reminder_{instance_id}       — fires send_class_reminders() at reminder time
  - invite_start_{instance_id}   — fires trigger_invitations() at invitation-start time
  - process_batches              — recurring every 2 minutes, fires process_invitation_batches()

Call init_scheduler(app) from create_app(). Jobs are persisted in the PostgreSQL job store
so they survive server restarts.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta


def _compute_timing_dt(instance_start: datetime, timing_config: dict) -> datetime | None:
    """
    Compute the absolute datetime for a timing config relative to a class start.

    timing_config shapes:
      {"type": "hours_before", "value": N}
      {"type": "days_before",  "days": N, "time": "HH:MM"}
    """
    if not timing_config:
        return None

    timing_type = timing_config.get("type")

    if timing_type == "hours_before":
        value = timing_config.get("value", 24)
        return instance_start - timedelta(hours=value)

    if timing_type in ("days_before", "days_before_at_time"):
        days = timing_config.get("days", 1)
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
# Job functions (called by APScheduler — each runs with an app context)
# ---------------------------------------------------------------------------

def _run_send_reminders(app, instance_id: int):
    with app.app_context():
        from padel_app.services.notification_service import send_class_reminders
        try:
            send_class_reminders(instance_id)
        except Exception as exc:
            app.logger.error("send_class_reminders(%s) failed: %s", instance_id, exc)


def _run_trigger_invitations(app, instance_id: int, coach_id: int):
    with app.app_context():
        from padel_app.models import LessonInstance
        from padel_app.services.notification_service import trigger_invitations
        try:
            instance = LessonInstance.query.get(instance_id)
            if instance:
                trigger_invitations(instance, coach_id)
        except Exception as exc:
            app.logger.error("trigger_invitations(%s) failed: %s", instance_id, exc)


def _run_process_batches(app):
    with app.app_context():
        from padel_app.services.notification_service import process_invitation_batches
        try:
            process_invitation_batches()
        except Exception as exc:
            app.logger.error("process_invitation_batches failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_scheduler(app, test_config=None):
    """
    Create and start the APScheduler BackgroundScheduler.
    Skipped entirely during tests (test_config is not None) and in the Werkzeug
    watcher process (to prevent double-start in development).
    """
    if test_config is not None:
        return

    # In development, Werkzeug starts the app twice (watcher + reloader child).
    # Only start the scheduler in the child process.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    db_url = app.config.get("SQLALCHEMY_DATABASE_URI")
    jobstores = {"default": SQLAlchemyJobStore(url=db_url)}
    scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")

    # TEST_MODE=true shortens the interval to 30 s so you can verify the full
    # delivery pipeline end-to-end without waiting 2 minutes between batches.
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"
    batch_trigger = (
        IntervalTrigger(seconds=30) if test_mode else IntervalTrigger(minutes=2)
    )

    # Recurring batch processor — runs every 2 minutes (30 s in TEST_MODE)
    scheduler.add_job(
        func=_run_process_batches,
        args=[app],
        trigger=batch_trigger,
        id="process_batches",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.start()
    app.extensions["scheduler"] = scheduler

    import atexit
    atexit.register(lambda: scheduler.shutdown(wait=False))

    # Re-schedule all future jobs on startup in case jobs were lost during a
    # server restart or were never added (e.g. timing was misconfigured before).
    try:
        with app.app_context():
            from padel_app.models import Coach
            for coach in Coach.query.all():
                reschedule_all_future_jobs(app, coach.id)
    except Exception as exc:
        app.logger.warning("Startup rescheduling failed: %s", exc)


def schedule_instance_jobs(app, instance_id: int, coach_id: int, *, now: datetime | None = None):
    """
    Schedule reminder + invitation-start jobs for a lesson instance.
    Called when an instance is created or timing settings change.
    Jobs whose computed time is in the past are silently skipped.

    Pass ``now`` in tests to control which jobs are considered "in the future".
    """
    scheduler = app.extensions.get("scheduler")
    if scheduler is None:
        return

    from apscheduler.triggers.date import DateTrigger

    with app.app_context():
        from padel_app.models import LessonInstance
        from padel_app.services.notification_service import get_or_create_config

        instance = LessonInstance.query.get(instance_id)
        if not instance:
            return

        config = get_or_create_config(coach_id)
        now = now or datetime.utcnow()

        reminder_dt = _compute_reminder_dt(instance, config.get_reminder_timing())
        if reminder_dt and reminder_dt > now:
            scheduler.add_job(
                func=_run_send_reminders,
                args=[app, instance_id],
                trigger=DateTrigger(run_date=reminder_dt),
                id=f"reminder_{instance_id}",
                replace_existing=True,
                misfire_grace_time=300,
            )

        invite_start_dt = _compute_invite_start_dt(instance, config.get_invitation_start_timing())
        if invite_start_dt and invite_start_dt > now:
            scheduler.add_job(
                func=_run_trigger_invitations,
                args=[app, instance_id, coach_id],
                trigger=DateTrigger(run_date=invite_start_dt),
                id=f"invite_start_{instance_id}",
                replace_existing=True,
                misfire_grace_time=300,
            )


def cancel_instance_jobs(app, instance_id: int):
    """Remove scheduled jobs for a lesson instance (e.g., when it is canceled)."""
    scheduler = app.extensions.get("scheduler")
    if scheduler is None:
        return

    for job_id in (f"reminder_{instance_id}", f"invite_start_{instance_id}"):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass  # job may not exist


def reschedule_all_future_jobs(app, coach_id: int):
    """
    Re-schedule jobs for all upcoming instances belonging to this coach.
    Called when the coach updates reminder or invitation-start timing in settings.
    """
    scheduler = app.extensions.get("scheduler")
    if scheduler is None:
        return

    with app.app_context():
        from datetime import datetime as _dt
        from padel_app.models import Association_CoachLessonInstance, LessonInstance

        now = _dt.utcnow()
        future_instances = (
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

        for instance in future_instances:
            schedule_instance_jobs(app, instance.id, coach_id)
