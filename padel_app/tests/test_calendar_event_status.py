"""
PAD-96 — a calendar event must read as ``completed`` once its END datetime has
passed, not merely once its DATE is in the past.

The bug: ``_compute_status`` compared only the event DATE against ``today()``,
so a class earlier TODAY (e.g. 15:00-16:00 viewed at 20:30) stayed ``scheduled``
while previous days' classes correctly showed ``completed``. The fix compares
the real end datetime (event date + end time-of-day) against "now", using the
same naive-UTC clock (``utcnow_naive``) the scheduler uses.

Covered spec: calendar.view (rule 11)
"""
from datetime import datetime, timedelta

from padel_app.serializers.calendar_event import (
    _compute_status,
    serialize_calendar_event,
)
from padel_app.sql_db import db


# A fixed "now" so the tests are deterministic regardless of wall clock.
NOW = datetime(2026, 7, 27, 20, 30, 0)


def test_today_already_ended_is_completed():
    """15:00-16:00 today, checked at 20:30 -> completed (the ticket scenario)."""
    start = NOW.replace(hour=15, minute=0)
    end = NOW.replace(hour=16, minute=0)
    assert _compute_status(start, end, now=NOW) == "completed"


def test_today_not_yet_ended_is_scheduled():
    """A class today whose end is still in the future stays scheduled."""
    start = NOW.replace(hour=21, minute=0)
    end = NOW.replace(hour=22, minute=0)
    assert _compute_status(start, end, now=NOW) == "scheduled"


def test_today_in_progress_is_scheduled():
    """A class that started but has not ended yet is still scheduled."""
    start = NOW - timedelta(minutes=15)
    end = NOW + timedelta(minutes=45)
    assert _compute_status(start, end, now=NOW) == "scheduled"


def test_previous_day_is_completed():
    """Regression guard: previous-day classes still read as completed."""
    start = (NOW - timedelta(days=1)).replace(hour=10, minute=0)
    end = (NOW - timedelta(days=1)).replace(hour=11, minute=0)
    assert _compute_status(start, end, now=NOW) == "completed"


def test_future_day_is_scheduled():
    start = (NOW + timedelta(days=2)).replace(hour=10, minute=0)
    end = (NOW + timedelta(days=2)).replace(hour=11, minute=0)
    assert _compute_status(start, end, now=NOW) == "scheduled"


def test_override_date_today_ended_is_completed():
    """Recurrence occurrence materialized for today, ended earlier -> completed.

    The template's start/end carry the time-of-day; ``override_date`` supplies
    the occurrence day. The end datetime is that day + the template end time.
    """
    template_start = datetime(2026, 1, 6, 15, 0)  # some old Monday 15:00
    template_end = datetime(2026, 1, 6, 16, 0)     # 16:00
    assert (
        _compute_status(
            template_start,
            template_end,
            override_date="2026-07-27",  # today, so 16:00 < 20:30 now
            now=NOW,
        )
        == "completed"
    )


def test_override_date_today_not_ended_is_scheduled():
    template_start = datetime(2026, 1, 6, 21, 0)
    template_end = datetime(2026, 1, 6, 22, 0)
    assert (
        _compute_status(
            template_start,
            template_end,
            override_date="2026-07-27",  # today, 22:00 > 20:30 now
            now=NOW,
        )
        == "scheduled"
    )


def _make_instance_ending(app, *, start_dt, end_dt):
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance

    suffix = start_dt.strftime("%H%M%S%f")

    coach_user = User(name="Coach", username=f"st_coach_{suffix}", password="x")
    db.session.add(coach_user)
    db.session.flush()
    db.session.add(Coach(user_id=coach_user.id))
    db.session.flush()

    club = Club(name=f"ST Club {suffix}", description="c", location="x")
    db.session.add(club)
    db.session.flush()

    lesson = Lesson(
        title="Status Class",
        start_datetime=start_dt,
        end_datetime=end_dt,
        is_recurring=False,
        type="academy",
        max_players=4,
        status="active",
        club_id=club.id,
    )
    db.session.add(lesson)
    db.session.flush()

    instance = LessonInstance(
        lesson_id=lesson.id,
        start_datetime=start_dt,
        end_datetime=end_dt,
        max_players=4,
        status="scheduled",
        original_lesson_occurence_date=start_dt.date(),
    )
    db.session.add(instance)
    db.session.commit()
    return instance


def test_serialize_threads_now_for_today_ended_instance(app):
    """serialize_calendar_event honours injected `now` for today's ended class."""
    with app.app_context():
        start = NOW.replace(hour=15, minute=0)
        end = NOW.replace(hour=16, minute=0)
        instance = _make_instance_ending(app, start_dt=start, end_dt=end)

        event = serialize_calendar_event(instance, now=NOW)

        assert event["status"] == "completed"
