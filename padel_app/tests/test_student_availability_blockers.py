"""
PAD-28: Student availability blockers suppress AUTOMATIC class invitations.

Verifies:
  - A student with an availability blocker overlapping a class window is
    filtered out of the auto-invitation eligibility list.
  - A student whose blocker does NOT overlap is still eligible.
  - A blocker that is not marked blocks_auto_invitations does not suppress.
  - Recurring weekly blockers suppress the matching weekday occurrence.

Run:
    pytest padel_app/tests/test_student_availability_blockers.py -v
"""

import json
from datetime import datetime, timedelta

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Seed helpers (mirrors test_notification_integration.py)
# ---------------------------------------------------------------------------

def _create_user(name, username, status="active"):
    from padel_app.models.users import User
    u = User(name=name, username=username, email=f"{username}@test.com",
             password="hashed", status=status)
    db.session.add(u)
    db.session.flush()
    return u


def _create_coach(user):
    from padel_app.models.coaches import Coach
    c = Coach(user_id=user.id)
    db.session.add(c)
    db.session.flush()
    return c


def _create_player(user):
    from padel_app.models.players import Player
    p = Player(user_id=user.id)
    db.session.add(p)
    db.session.flush()
    return p


def _create_level(coach, label="Beg", code="B1"):
    from padel_app.models.coach_levels import CoachLevel
    lv = CoachLevel(coach_id=coach.id, label=label, code=code, display_order=1)
    db.session.add(lv)
    db.session.flush()
    return lv


def _create_coach_player(coach, player, level=None, side=None):
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    cp = Association_CoachPlayer(
        coach_id=coach.id, player_id=player.id,
        level_id=level.id if level else None, side=side,
    )
    db.session.add(cp)
    db.session.flush()
    return cp


def _create_instance(coach, level, start, max_players=4):
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance

    club = Club(name="Test Club", description="", location="City")
    db.session.add(club)
    db.session.flush()

    lesson = Lesson(title="Test Class", start_datetime=start,
                    end_datetime=start + timedelta(hours=1),
                    is_recurring=False, type="academy", max_players=max_players,
                    color="#000", status="active", club_id=club.id)
    db.session.add(lesson)
    db.session.flush()

    instance = LessonInstance(
        lesson_id=lesson.id, start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        max_players=max_players, status="scheduled",
        level_id=level.id, notifications_enabled=True,
    )
    db.session.add(instance)
    db.session.flush()
    db.session.add(Association_CoachLessonInstance(
        coach_id=coach.id, lesson_instance_id=instance.id))
    db.session.commit()
    return instance


def _create_vacancy(instance, coach, level, side=None):
    from padel_app.models.vacancy import Vacancy
    v = Vacancy(
        lesson_instance_id=instance.id, coach_id=coach.id,
        original_player_id=None, side=side, level_id=level.id,
        status="open", current_round_number=1, current_batch_number=0,
    )
    db.session.add(v)
    db.session.commit()
    return v


def _config(coach_id):
    from padel_app.models.notification_config import NotificationConfig
    cfg = NotificationConfig(coach_id=coach_id, auto_notify_enabled=True)
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _add_blocker(user_id, start, end, *, blocks_auto=True, recurrence_rule=None,
                 recurrence_end=None):
    from padel_app.models.calendar_blocks import CalendarBlock
    b = CalendarBlock(
        user_id=user_id, type="unavailable",
        start_datetime=start, end_datetime=end,
        is_recurring=bool(recurrence_rule),
        recurrence_rule=recurrence_rule, recurrence_end=recurrence_end,
        blocks_auto_invitations=blocks_auto,
        title="Unavailable",
    )
    db.session.add(b)
    db.session.commit()
    return b


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _setup(app, *, blocker=None):
    """Build coach + one eligible student and return (instance, vacancy, config,
    coach_id, player_id). `blocker` is a callable(user_id, instance) -> None."""
    from padel_app.services.notification_service import get_eligible_students

    cu = _create_user("Coach", "coach-avail")
    coach = _create_coach(cu)
    level = _create_level(coach)

    su = _create_user("Student", "student-avail")
    player = _create_player(su)
    _create_coach_player(coach, player, level=level, side="right")

    start = (datetime.utcnow() + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    instance = _create_instance(coach, level, start)
    vacancy = _create_vacancy(instance, coach, level)
    cfg = _config(coach.id)

    if blocker:
        blocker(su.id, instance)

    return get_eligible_students, instance, vacancy, cfg, coach.id, player.id


def test_overlapping_blocker_suppresses_auto_invite(app):
    with app.app_context():
        def make_blocker(user_id, instance):
            _add_blocker(user_id, instance.start_datetime, instance.end_datetime)

        get_eligible, instance, vacancy, cfg, coach_id, player_id = _setup(
            app, blocker=make_blocker)

        eligible = get_eligible(vacancy, instance, coach_id, cfg, 1)
        assert player_id not in {cp.player_id for cp in eligible}


def test_no_blocker_student_is_eligible(app):
    with app.app_context():
        get_eligible, instance, vacancy, cfg, coach_id, player_id = _setup(app)
        eligible = get_eligible(vacancy, instance, coach_id, cfg, 1)
        assert player_id in {cp.player_id for cp in eligible}


def test_non_overlapping_blocker_does_not_suppress(app):
    with app.app_context():
        def make_blocker(user_id, instance):
            # Blocker is the day AFTER the class — no overlap.
            bstart = instance.start_datetime + timedelta(days=1)
            _add_blocker(user_id, bstart, bstart + timedelta(hours=1))

        get_eligible, instance, vacancy, cfg, coach_id, player_id = _setup(
            app, blocker=make_blocker)

        eligible = get_eligible(vacancy, instance, coach_id, cfg, 1)
        assert player_id in {cp.player_id for cp in eligible}


def test_blocker_without_flag_does_not_suppress(app):
    with app.app_context():
        def make_blocker(user_id, instance):
            # Overlaps but not marked as blocking auto invitations.
            _add_blocker(user_id, instance.start_datetime, instance.end_datetime,
                         blocks_auto=False)

        get_eligible, instance, vacancy, cfg, coach_id, player_id = _setup(
            app, blocker=make_blocker)

        eligible = get_eligible(vacancy, instance, coach_id, cfg, 1)
        assert player_id in {cp.player_id for cp in eligible}


def test_recurring_weekly_blocker_suppresses_matching_weekday(app):
    with app.app_context():
        def make_blocker(user_id, instance):
            # Weekly on the class's weekday, starting a week before the class.
            # Frontend stores JS getDay() (Sun=0..Sat=6); build_rrule maps it.
            js_weekday = (instance.start_datetime.weekday() + 1) % 7
            rule = json.dumps({"frequency": "weekly", "daysOfWeek": [js_weekday]})
            bstart = (instance.start_datetime - timedelta(days=7)).replace(
                hour=instance.start_datetime.hour, minute=0)
            _add_blocker(
                user_id, bstart, bstart + timedelta(hours=2),
                recurrence_rule=rule,
                recurrence_end=(instance.start_datetime + timedelta(days=7)).date(),
            )

        get_eligible, instance, vacancy, cfg, coach_id, player_id = _setup(
            app, blocker=make_blocker)

        eligible = get_eligible(vacancy, instance, coach_id, cfg, 1)
        assert player_id not in {cp.player_id for cp in eligible}
