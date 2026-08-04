"""
PAD-28: Student availability blockers suppress AUTOMATIC class invitations.
PAD-107: ...and MANUAL ones too — a blocked student must never be solicited.

Verifies:
  - A student with an availability blocker overlapping a class window is
    filtered out of the auto-invitation eligibility list.
  - A student whose blocker does NOT overlap is still eligible.
  - A blocker that is not marked blocks_auto_invitations does not suppress.
  - Recurring weekly blockers suppress the matching weekday occurrence.
  - (PAD-107) Neither the manual-notify path nor the reminder path creates a
    Message / NotificationEvent for a blocked student, while unblocked
    classmates still get theirs.

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


# ---------------------------------------------------------------------------
# PAD-107 — manual paths must respect the blocker too
# ---------------------------------------------------------------------------

def _enrol(instance, player):
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    db.session.add(Association_PlayerLessonInstance(
        player_id=player.id, lesson_instance_id=instance.id))
    db.session.commit()


def _messages_for_user(user_id):
    """Every Message in any conversation the user takes part in."""
    from padel_app.models import Message
    from padel_app.models.conversation_participants import ConversationParticipant

    conv_ids = [
        cp.conversation_id
        for cp in ConversationParticipant.query.filter_by(user_id=user_id).all()
    ]
    if not conv_ids:
        return []
    return Message.query.filter(Message.conversation_id.in_(conv_ids)).all()


def _setup_two_students(suffix):
    """Coach + a BLOCKED student and an AVAILABLE one, both enrolled."""
    cu = _create_user("Coach", f"coach-{suffix}")
    coach = _create_coach(cu)
    level = _create_level(coach)

    blocked_user = _create_user("Blocked Student", f"blocked-{suffix}")
    blocked = _create_player(blocked_user)
    _create_coach_player(coach, blocked, level=level, side="right")

    free_user = _create_user("Free Student", f"free-{suffix}")
    free = _create_player(free_user)
    _create_coach_player(coach, free, level=level, side="left")

    start = (datetime.utcnow() + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0)
    instance = _create_instance(coach, level, start)
    _config(coach.id)

    _enrol(instance, blocked)
    _enrol(instance, free)

    # The blocker covers exactly the class slot.
    _add_blocker(blocked_user.id, instance.start_datetime, instance.end_datetime)

    return coach, instance, (blocked, blocked_user), (free, free_user)


def test_pad107_manual_notify_skips_blocked_student(app):
    """No NotificationEvent, no Message and no push for a blocked student."""
    with app.app_context():
        from padel_app.models import NotificationEvent
        from padel_app.services.notification_service import send_manual_notifications

        coach, instance, (blocked, blocked_user), (free, free_user) = (
            _setup_two_students("manual"))

        events = send_manual_notifications(
            instance.id, [blocked.id, free.id], coach.id)

        assert {e.player_id for e in events} == {free.id}
        assert NotificationEvent.query.filter_by(player_id=blocked.id).count() == 0
        assert _messages_for_user(blocked_user.id) == []
        # The available classmate is unaffected.
        assert len(_messages_for_user(free_user.id)) == 1


def test_pad107_reminders_skip_blocked_student_but_reach_the_rest(app):
    """"Lembrar" reminds everyone except the unavailable student."""
    with app.app_context():
        from padel_app.services.notification_service import send_class_reminders

        coach, instance, (blocked, blocked_user), (free, free_user) = (
            _setup_two_students("remind"))

        result = send_class_reminders(instance.id)

        assert result["sent"] == 1
        assert [b["playerId"] for b in result["blocked"]] == [blocked.id]
        assert result["blocked"][0]["name"] == "Blocked Student"
        assert _messages_for_user(blocked_user.id) == []
        assert len(_messages_for_user(free_user.id)) == 1


def test_pad107_send_system_message_backstop_refuses_invite(app):
    """Even a direct call to the delivery choke point is refused."""
    with app.app_context():
        from padel_app.services.notification_service import _send_system_message

        coach, instance, (blocked, blocked_user), (free, free_user) = (
            _setup_two_students("backstop"))

        msg = _send_system_message(
            coach_user_id=coach.user_id,
            player_user_id=blocked_user.id,
            text="Want to play?",
            message_type="notification_invite",
            msg_metadata={"lessonInstanceId": instance.id},
        )

        assert msg is None
        assert _messages_for_user(blocked_user.id) == []


def test_pad107_backstop_does_not_block_plain_chat(app):
    """Unavailability silences class solicitations, not the coach's chat."""
    with app.app_context():
        from padel_app.services.notification_service import _send_system_message

        coach, instance, (blocked, blocked_user), (free, free_user) = (
            _setup_two_students("chat"))

        msg = _send_system_message(
            coach_user_id=coach.user_id,
            player_user_id=blocked_user.id,
            text="See you next week!",
            message_type="text",
            msg_metadata={"lessonInstanceId": instance.id},
        )

        assert msg is not None
        assert len(_messages_for_user(blocked_user.id)) == 1


def test_pad107_blocked_players_for_instance_reports_names_only(app):
    """The coach-facing payload leaks no blocker detail."""
    with app.app_context():
        from padel_app.services.student_availability_service import (
            blocked_players_for_instance,
        )

        coach, instance, (blocked, blocked_user), (free, free_user) = (
            _setup_two_students("payload"))

        blocked_list = blocked_players_for_instance(instance)

        assert blocked_list == [
            {"playerId": blocked.id, "name": "Blocked Student"}
        ]


def test_pad107_availability_conflicts_only_sees_own_roster(app, client):
    """A coach cannot probe another coach's student's private calendar."""
    from flask_jwt_extended import create_access_token

    with app.app_context():
        app.config["JWT_SECRET_KEY"] = "test-jwt-secret"

        owner, instance, (blocked, blocked_user), _free = _setup_two_students("authz")

        # A second, unrelated coach with no link to `blocked`.
        stranger_user = _create_user("Stranger Coach", "stranger-authz")
        stranger = _create_coach(stranger_user)
        db.session.commit()

        date_str = instance.start_datetime.strftime("%Y-%m-%d")
        payload = {
            "date": date_str,
            "startTime": instance.start_datetime.strftime("%H:%M"),
            "endTime": instance.end_datetime.strftime("%H:%M"),
            "playerIds": [blocked.id],
        }

        def _post(user_id):
            token = create_access_token(identity=str(user_id))
            return client.post(
                "/api/app/notify/availability_conflicts",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )

        # The owning coach legitimately sees the conflict...
        own = _post(owner.user_id)
        assert own.status_code == 200
        assert [b["playerId"] for b in own.get_json()["blocked"]] == [blocked.id]

        # ...the stranger learns nothing, not even that the player exists.
        other = _post(stranger.user_id)
        assert other.status_code == 200
        assert other.get_json()["blocked"] == []
