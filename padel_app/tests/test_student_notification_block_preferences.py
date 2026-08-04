"""
PAD-112: a student can block class-vacancy invitation notifications outright.

Three independent levels, each enforced at a different point:

  * AUTO   — `get_eligible_students` drops the candidate, so the engine never
             even creates a NotificationEvent for them;
  * MANUAL — `send_manual_notifications` skips them BEFORE the event is
             created, so no orphan "sent" row is left behind;
  * ALL    — `send_class_reminders` skips them, and `_send_system_message`
             refuses to deliver any class-slot solicitation as a backstop.

Also pinned here: what must NOT be suppressed (plain chat, cancellation
notices), that an explicit `false` clears a flag (the PAD-93 boolean trap), and
that the coach-facing player payload carries the signal from BOTH serializers.

Run:
    pytest padel_app/tests/test_student_notification_block_preferences.py -v
"""

from datetime import datetime, timedelta

import pytest
from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Seed helpers (mirrors test_student_availability_blockers.py)
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
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )

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


def _enrol(instance, player):
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    rel = Association_PlayerLessonInstance(
        player_id=player.id, lesson_instance_id=instance.id,
    )
    db.session.add(rel)
    db.session.commit()
    return rel


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


def _block(user, *, auto=False, manual=False, every=False, reason=None):
    user.notif_block_auto_invitations = auto
    user.notif_block_manual_invitations = manual
    user.notif_block_all = every
    user.notif_block_reason = reason
    db.session.commit()


def _world(app, suffix=""):
    """Coach + two enrolled/eligible students, a class instance and a vacancy."""
    cu = _create_user("Coach", f"coach-nbp{suffix}")
    coach = _create_coach(cu)
    level = _create_level(coach)

    su1 = _create_user("Ana Silva", f"stu-nbp1{suffix}")
    p1 = _create_player(su1)
    su2 = _create_user("Bruno Costa", f"stu-nbp2{suffix}")
    p2 = _create_player(su2)
    _create_coach_player(coach, p1, level)
    _create_coach_player(coach, p2, level)
    db.session.commit()

    start = datetime(2030, 6, 5, 10, 0)
    instance = _create_instance(coach, level, start)
    vacancy = _create_vacancy(instance, coach, level)
    cfg = _config(coach.id)
    return {
        "coach": coach, "coach_user": cu, "level": level,
        "s1": su1, "p1": p1, "s2": su2, "p2": p2,
        "instance": instance, "vacancy": vacancy, "config": cfg,
    }


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_defaults_are_receives_everything(app):
    from padel_app.services.student_notification_preferences import (
        get_user_block_preferences,
    )
    with app.app_context():
        u = _create_user("Fresh", "stu-fresh")
        db.session.commit()
        prefs = get_user_block_preferences(u.id)
        assert prefs == {"auto": False, "manual": False, "all": False, "reason": ""}
        assert u.notifications_blocked is False


# ---------------------------------------------------------------------------
# AUTO — eligibility engine
# ---------------------------------------------------------------------------

def test_auto_block_removes_student_from_eligibility(app):
    from padel_app.services.notification_service import get_eligible_students
    with app.app_context():
        w = _world(app, "-auto")

        before = get_eligible_students(
            w["vacancy"], w["instance"], w["coach"].id, w["config"], 1,
        )
        assert {cp.player_id for cp in before} == {w["p1"].id, w["p2"].id}

        _block(w["s1"], auto=True, reason="Estou lesionado")

        after = get_eligible_students(
            w["vacancy"], w["instance"], w["coach"].id, w["config"], 1,
        )
        assert {cp.player_id for cp in after} == {w["p2"].id}


def test_block_all_also_removes_student_from_eligibility(app):
    from padel_app.services.notification_service import get_eligible_students
    with app.app_context():
        w = _world(app, "-autoall")
        _block(w["s1"], every=True)

        after = get_eligible_students(
            w["vacancy"], w["instance"], w["coach"].id, w["config"], 1,
        )
        assert {cp.player_id for cp in after} == {w["p2"].id}


def test_manual_only_block_does_not_touch_auto_eligibility(app):
    """The three levels are independent — blocking MANUAL leaves AUTO alone."""
    from padel_app.services.notification_service import get_eligible_students
    with app.app_context():
        w = _world(app, "-manonly")
        _block(w["s1"], manual=True)

        after = get_eligible_students(
            w["vacancy"], w["instance"], w["coach"].id, w["config"], 1,
        )
        assert {cp.player_id for cp in after} == {w["p1"].id, w["p2"].id}


# ---------------------------------------------------------------------------
# MANUAL — no orphan NotificationEvent
# ---------------------------------------------------------------------------

def test_manual_block_skips_student_without_creating_an_event(app):
    from padel_app.models.notification_event import NotificationEvent
    from padel_app.models.messages import Message
    from padel_app.services.notification_service import send_manual_notifications
    with app.app_context():
        w = _world(app, "-man")
        _block(w["s1"], manual=True, reason="Estou de férias")

        events = send_manual_notifications(
            w["instance"].id, [w["p1"].id, w["p2"].id], w["coach"].id,
        )

        assert len(events) == 1
        assert events[0].player_id == w["p2"].id

        # The decisive assertion: no orphan event for the blocked student.
        assert NotificationEvent.query.filter_by(player_id=w["p1"].id).count() == 0

        texts = Message.query.filter_by(message_type="notification_invite").all()
        assert len(texts) == 1


def test_manual_block_reports_the_student_and_their_reason(app):
    from padel_app.services.student_notification_preferences import (
        preference_blocked_players,
    )
    with app.app_context():
        w = _world(app, "-manrep")
        _block(w["s1"], manual=True, reason="Estou lesionado")

        blocked = preference_blocked_players(
            [w["p1"].id, w["p2"].id], kind="manual",
        )
        assert blocked == [{
            "playerId": w["p1"].id,
            "name": "Ana Silva",
            "reason": "Estou lesionado",
        }]


def test_auto_only_block_does_not_stop_a_manual_invitation(app):
    """Independence in the other direction — the coach can still hand-pick."""
    from padel_app.services.notification_service import send_manual_notifications
    with app.app_context():
        w = _world(app, "-automan")
        _block(w["s1"], auto=True)

        events = send_manual_notifications(
            w["instance"].id, [w["p1"].id], w["coach"].id,
        )
        assert len(events) == 1
        assert events[0].player_id == w["p1"].id


# ---------------------------------------------------------------------------
# ALL — reminders
# ---------------------------------------------------------------------------

def test_block_all_silences_reminders_but_not_the_rest_of_the_class(app):
    from padel_app.services.notification_service import send_class_reminders
    with app.app_context():
        w = _world(app, "-rem")
        _enrol(w["instance"], w["p1"])
        _enrol(w["instance"], w["p2"])
        _block(w["s1"], every=True, reason="Vou estar fora")

        result = send_class_reminders(
            w["instance"].id, now=w["instance"].start_datetime - timedelta(hours=2),
        )

        assert result["sent"] == 1, "only the unblocked student is reminded"
        assert [b["playerId"] for b in result["blocked"]] == [w["p1"].id]
        assert result["blocked"][0]["reason"] == "Vou estar fora"


def test_invitation_only_block_leaves_reminders_alone(app):
    """A reminder is about a class you are already in, not an invitation."""
    from padel_app.services.notification_service import send_class_reminders
    with app.app_context():
        w = _world(app, "-remauto")
        _enrol(w["instance"], w["p1"])
        _block(w["s1"], auto=True, manual=True)

        result = send_class_reminders(
            w["instance"].id, now=w["instance"].start_datetime - timedelta(hours=2),
        )
        assert result["sent"] == 1
        assert result["blocked"] == []


# ---------------------------------------------------------------------------
# The choke-point backstop
# ---------------------------------------------------------------------------

def test_backstop_suppresses_every_class_slot_solicitation(app):
    from padel_app.services.notification_service import _send_system_message
    with app.app_context():
        w = _world(app, "-choke")
        _block(w["s1"], every=True)

        for message_type in (
            "notification_invite", "notification_reminder", "waiting_list_offer",
        ):
            msg = _send_system_message(
                coach_user_id=w["coach_user"].id,
                player_user_id=w["s1"].id,
                text="Tens vaga na aula de amanhã?",
                message_type=message_type,
            )
            assert msg is None, f"{message_type} must be suppressed"


def test_backstop_never_cuts_the_student_off_from_their_coach(app):
    """Plain chat, cancellation notices and 'you got the spot' still arrive."""
    from padel_app.services.notification_service import _send_system_message
    with app.app_context():
        w = _world(app, "-chokeok")
        _block(w["s1"], every=True)

        for message_type in ("text", "waiting_list_placed"):
            msg = _send_system_message(
                coach_user_id=w["coach_user"].id,
                player_user_id=w["s1"].id,
                text="A aula de amanhã foi cancelada.",
                message_type=message_type,
            )
            assert msg is not None, f"{message_type} must still be delivered"


def test_backstop_leaves_an_unblocked_student_alone(app):
    from padel_app.services.notification_service import _send_system_message
    with app.app_context():
        w = _world(app, "-chokefree")
        _block(w["s1"], auto=True, manual=True)  # but NOT `all`

        msg = _send_system_message(
            coach_user_id=w["coach_user"].id,
            player_user_id=w["s1"].id,
            text="Tens vaga na aula de amanhã?",
            message_type="notification_invite",
        )
        assert msg is not None


# ---------------------------------------------------------------------------
# PATCH /api/auth/me — partial updates and the boolean trap
# ---------------------------------------------------------------------------

def test_profile_update_sets_and_clears_each_flag(app):
    from padel_app.services.user_service import update_own_profile_service
    with app.app_context():
        u = _create_user("Toggler", "stu-toggle")
        db.session.commit()

        update_own_profile_service(u.id, {"blockAllNotifications": True})
        assert u.notif_block_all is True

        # The PAD-93 trap: an explicit false must be written, not swallowed.
        update_own_profile_service(u.id, {"blockAllNotifications": False})
        assert u.notif_block_all is False


def test_profile_update_is_partial_and_independent(app):
    from padel_app.services.user_service import update_own_profile_service
    with app.app_context():
        u = _create_user("Partial", "stu-partial")
        db.session.commit()

        update_own_profile_service(u.id, {"blockAutoInvitations": True})
        update_own_profile_service(u.id, {"blockManualInvitations": True})
        update_own_profile_service(u.id, {"blockManualInvitations": False})

        assert u.notif_block_auto_invitations is True, "auto must survive"
        assert u.notif_block_manual_invitations is False
        assert u.notif_block_all is False

        # Touching an unrelated field leaves the flags alone.
        update_own_profile_service(u.id, {"name": "Renamed"})
        assert u.notif_block_auto_invitations is True


def test_profile_update_reason_is_trimmed_and_clearable(app):
    from padel_app.services.user_service import update_own_profile_service
    with app.app_context():
        u = _create_user("Reasoner", "stu-reason")
        db.session.commit()

        update_own_profile_service(
            u.id, {"notificationBlockReason": "  Estou lesionado  "},
        )
        assert u.notif_block_reason == "Estou lesionado"

        update_own_profile_service(u.id, {"notificationBlockReason": ""})
        assert u.notif_block_reason is None


def test_profile_update_rejects_a_non_boolean_flag(app):
    from padel_app.services.user_service import (
        ProfileValidationError, update_own_profile_service,
    )
    with app.app_context():
        u = _create_user("Bad", "stu-bad")
        db.session.commit()

        try:
            update_own_profile_service(u.id, {"blockAllNotifications": "yes please"})
        except ProfileValidationError as exc:
            assert exc.status == 400
        else:
            raise AssertionError("a non-boolean must be rejected, not coerced")

        assert u.notif_block_all is False


def test_me_route_round_trips_the_preferences(app, client):
    from flask_jwt_extended import create_access_token
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"
    with app.app_context():
        u = _create_user("Api", "stu-api")
        _create_player(u)
        db.session.commit()
        token = create_access_token(identity=str(u.id))

    headers = {"Authorization": f"Bearer {token}"}

    body = client.get("/api/auth/me", headers=headers).get_json()
    assert body["blockAutoInvitations"] is False
    assert body["blockManualInvitations"] is False
    assert body["blockAllNotifications"] is False
    assert body["notificationBlockReason"] == ""

    patched = client.patch("/api/auth/me", headers=headers, json={
        "blockAutoInvitations": True,
        "notificationBlockReason": "Vou estar fora até setembro",
    })
    assert patched.status_code == 200
    assert patched.get_json()["blockAutoInvitations"] is True

    body = client.get("/api/auth/me", headers=headers).get_json()
    assert body["blockAutoInvitations"] is True
    assert body["notificationBlockReason"] == "Vou estar fora até setembro"


# ---------------------------------------------------------------------------
# Coach visibility — both serializers
# ---------------------------------------------------------------------------

def test_both_coach_facing_serializers_carry_the_signal(app):
    from padel_app.services.player_service import _serialize_coach_player_relation
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    with app.app_context():
        w = _world(app, "-serial")
        _block(w["s1"], auto=True, reason="Vou estar fora até setembro")

        rel = Association_CoachPlayer.query.filter_by(
            coach_id=w["coach"].id, player_id=w["p1"].id,
        ).first()

        from_list = _serialize_coach_player_relation(rel)
        from_edit = w["p1"].coach_player_info(w["coach"].id)

        for payload, label in ((from_list, "list"), (from_edit, "edit echo")):
            assert payload["notificationsBlocked"] is True, label
            assert payload["blockAutoInvitations"] is True, label
            assert payload["blockManualInvitations"] is False, label
            assert payload["blockAllNotifications"] is False, label
            assert payload["notificationBlockReason"] == "Vou estar fora até setembro", label


def test_unblocked_student_reports_no_signal(app):
    from padel_app.services.player_service import _serialize_coach_player_relation
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    with app.app_context():
        w = _world(app, "-serialoff")
        rel = Association_CoachPlayer.query.filter_by(
            coach_id=w["coach"].id, player_id=w["p2"].id,
        ).first()
        payload = _serialize_coach_player_relation(rel)
        assert payload["notificationsBlocked"] is False
        assert payload["notificationBlockReason"] == ""
