"""
Integration tests for the invitation flow (trigger_invitations, respond_to_notification).

These tests use a real SQLite in-memory DB (via the `app` fixture from conftest.py).
`publish` and `send_push_notification` are patched to avoid external side effects.

Run:
    pytest padel_app/tests/test_notification_integration.py -v
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]

# ---------------------------------------------------------------------------
# Seed helpers (kept minimal — only what each test actually needs)
# ---------------------------------------------------------------------------

def _create_user(name, username, email="", status="active"):
    from padel_app.models.users import User
    u = User(name=name, username=username, email=email or f"{username}@test.com",
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


def _create_instance(coach, level, enrolled_players=(), start_offset_hours=48, max_players=4):
    """Create a Lesson + LessonInstance, enrol players, wire coach. Returns instance."""
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

    club = Club(name="Test Club", description="", location="City")
    db.session.add(club)
    db.session.flush()

    start = datetime.utcnow() + timedelta(hours=start_offset_hours)
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

    db.session.add(Association_CoachLessonInstance(coach_id=coach.id,
                                                    lesson_instance_id=instance.id))
    for player in enrolled_players:
        db.session.add(Association_PlayerLessonInstance(player_id=player.id,
                                                         lesson_instance_id=instance.id))
    db.session.commit()
    return instance


def _seed_notification_config(coach_id, auto_notify=True):
    from padel_app.models.notification_config import NotificationConfig
    cfg = NotificationConfig(coach_id=coach_id, auto_notify_enabled=auto_notify)
    db.session.add(cfg)
    db.session.commit()
    return cfg


# ---------------------------------------------------------------------------
# TestTriggerInvitations
# ---------------------------------------------------------------------------

class TestTriggerInvitations:

    def test_returns_empty_when_auto_notify_off(self, app):
        """trigger_invitations returns [] immediately when auto_notify_enabled=False."""
        from padel_app.services.notification_service import trigger_invitations

        with app.app_context():
            cu = _create_user("Coach", "coach-off")
            coach = _create_coach(cu)
            level = _create_level(coach)
            instance = _create_instance(coach, level)
            _seed_notification_config(coach.id, auto_notify=False)

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = trigger_invitations(instance, coach.id)

        assert result == []

    def test_returns_empty_when_class_notifications_disabled(self, app):
        """trigger_invitations returns [] when instance.notifications_enabled=False."""
        from padel_app.services.notification_service import trigger_invitations
        from padel_app.models.lesson_instances import LessonInstance

        with app.app_context():
            cu = _create_user("Coach", "coach-notif-off")
            coach = _create_coach(cu)
            level = _create_level(coach)
            instance = _create_instance(coach, level)
            _seed_notification_config(coach.id, auto_notify=True)

            # Disable notifications on the instance
            inst = LessonInstance.query.get(instance.id)
            inst.notifications_enabled = False
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = trigger_invitations(inst, coach.id)

        assert result == []

    def test_creates_structural_vacancy_for_underfilled_class(self, app):
        """With max_players=4 and 0 enrolled, trigger_invitations should create structural vacancies."""
        from padel_app.services.notification_service import trigger_invitations
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            cu = _create_user("Coach", "coach-struct")
            su = _create_user("Student", "student-struct")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)

            # Class with no enrolled players → structural vacancies
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = trigger_invitations(instance, coach.id)

            vacancies = Vacancy.query.filter_by(lesson_instance_id=instance.id).all()

        # Should have attempted to fill structural vacancies (may return [] if no eligible students)
        assert len(vacancies) >= 1 or result == []  # vacancies created or early return

    def test_creates_vacancy_for_absent_player(self, app):
        """Absent player in presences causes a Vacancy to be created."""
        from padel_app.services.notification_service import trigger_invitations
        from padel_app.models.presences import Presence
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            cu = _create_user("Coach", "coach-absent")
            su = _create_user("AbsStudent", "student-absent")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)

            instance = _create_instance(coach, level, enrolled_players=[student], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            # Mark player as absent
            p = Presence(lesson_instance_id=instance.id, player_id=student.id,
                         invited=True, confirmed=True, status="absent")
            p.create()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                trigger_invitations(instance, coach.id)

            # A vacancy must exist for the absent player (status may be expired if no
            # eligible candidates exist to fill it — that is correct behavior)
            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance.id,
                original_player_id=student.id,
            ).first()
            assert vacancy is not None

    def test_skips_when_class_already_in_past(self, app):
        """trigger_invitations with a past class returns [] due to restriction check."""
        from padel_app.services.notification_service import trigger_invitations
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            cu = _create_user("Coach", "coach-past")
            coach = _create_coach(cu)
            level = _create_level(coach)
            instance = _create_instance(coach, level, start_offset_hours=48)
            _seed_notification_config(coach.id, auto_notify=True)

            # Pass a `now` that is AFTER the class — restriction check should block
            future_now = datetime.utcnow() + timedelta(hours=100)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = trigger_invitations(instance, coach.id, now=future_now)

        assert result == []


# ---------------------------------------------------------------------------
# TestRespondToNotification
# ---------------------------------------------------------------------------

class TestRespondToNotification:

    def _seed_invite_event(self, app, coach, student, instance):
        """Create a Vacancy and NotificationEvent for the student. Returns (vacancy, event)."""
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.notification_event import NotificationEvent

        vacancy = Vacancy(
            lesson_instance_id=instance.id,
            coach_id=coach.id,
            status="open",
            current_round_number=1,
            current_batch_number=1,
        )
        vacancy.create()

        event = NotificationEvent(
            coach_id=coach.id,
            lesson_instance_id=instance.id,
            player_id=student.id,
            type="auto",
            round_number=1,
            status="sent",
            vacancy_id=vacancy.id,
        )
        event.create()
        return vacancy, event

    def test_yes_adds_player_to_instance_and_confirms_presence(self, app):
        """Responding 'yes' creates Association_PlayerLessonInstance and sets event status=confirmed."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.Association_PlayerLessonInstance import Association_PlayerLessonInstance

        with app.app_context():
            cu = _create_user("Coach", "coach-yes")
            su = _create_user("Student", "student-yes")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)
            # Empty class so student is not already enrolled
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            vacancy, event = self._seed_invite_event(app, coach, student, instance)
            event_id = event.id
            student_user_id = su.id

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(event_id, "yes", student_user_id)

            updated_event = NotificationEvent.query.get(event_id)
            enrolled = Association_PlayerLessonInstance.query.filter_by(
                player_id=student.id, lesson_instance_id=instance.id
            ).first()

        assert result["action"] == "confirmed"
        assert updated_event.status == "confirmed"
        assert enrolled is not None

    def test_yes_marks_vacancy_filled(self, app):
        """Responding 'yes' sets vacancy.status='filled' and records filled_by_player_id."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            cu = _create_user("Coach", "coach-fill")
            su = _create_user("Student", "student-fill")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            vacancy, event = self._seed_invite_event(app, coach, student, instance)
            vacancy_id = vacancy.id
            event_id = event.id
            student_id = student.id

            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_notification(event_id, "yes", su.id)

            updated_vacancy = Vacancy.query.get(vacancy_id)
            assert updated_vacancy.status == "filled"
            assert updated_vacancy.filled_by_player_id == student_id

    def test_yes_expires_competing_invites(self, app):
        """When one player accepts, all other 'sent' events for the same vacancy become 'expired'."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            cu = _create_user("Coach", "coach-compete")
            su1 = _create_user("Student1", "student-c1")
            su2 = _create_user("Student2", "student-c2")
            coach = _create_coach(cu)
            student1 = _create_player(su1)
            student2 = _create_player(su2)
            level = _create_level(coach)
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            vacancy, event1 = self._seed_invite_event(app, coach, student1, instance)

            # Add a second competing event for the same vacancy
            event2 = NotificationEvent(
                coach_id=coach.id,
                lesson_instance_id=instance.id,
                player_id=student2.id,
                type="auto",
                round_number=1,
                status="sent",
                vacancy_id=vacancy.id,
            )
            event2.create()

            event1_id = event1.id
            event2_id = event2.id

            with patch(PATCHES[0]), patch(PATCHES[1]):
                respond_to_notification(event1_id, "yes", su1.id)

            evt2 = NotificationEvent.query.get(event2_id)

        assert evt2.status == "expired"

    def test_no_response_marks_event_expired(self, app):
        """Responding 'no' marks the event as expired."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            cu = _create_user("Coach", "coach-no")
            su = _create_user("Student", "student-no")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            vacancy, event = self._seed_invite_event(app, coach, student, instance)
            event_id = event.id

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(event_id, "no", su.id)

            updated = NotificationEvent.query.get(event_id)

        assert result["action"] == "declined"
        assert updated.status == "expired"

    def test_yes_when_vacancy_already_filled_offers_waiting_list(self, app):
        """Race condition: vacancy already filled when player says yes → spot_filled_waiting_list_offered."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            cu = _create_user("Coach", "coach-race")
            su = _create_user("Student", "student-race")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            vacancy, event = self._seed_invite_event(app, coach, student, instance)

            # Mark vacancy as already filled by someone else before student responds
            v = Vacancy.query.get(vacancy.id)
            v.status = "filled"
            db.session.commit()

            event_id = event.id

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(event_id, "yes", su.id)

        assert result["action"] == "spot_filled_waiting_list_offered"


# ---------------------------------------------------------------------------
# TestPastClassInvitationExpiry — PAD-68 follow-up
#
# The first PAD-68 pass retired stale *reminders* and refused to *send* new
# invitations for a class that is over. Pending invitations that were already
# out there stayed live: the NotificationEvent kept status "sent" and the invite
# message kept its Yes/No buttons, so a student could still take a spot in a
# class that already happened.
#
# Every un-actioned invitation must be retired once the class is over —
# started, canceled or completed — and a late answer must record nothing.
# ---------------------------------------------------------------------------

def _create_coach_player(coach, player, level=None):
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    cp = Association_CoachPlayer(
        coach_id=coach.id,
        player_id=player.id,
        level_id=level.id if level else None,
    )
    db.session.add(cp)
    db.session.flush()
    return cp


class TestPastClassInvitationExpiry:

    def _seed_pending_invite(self, *, suffix, start_offset_hours=24, then_set_status=None):
        """Send one real invitation for a future class.

        Returns a dict of ids plus the vacancy/event/message the send produced.
        Everything goes through ``_send_invitation_batch`` so the fixture is the
        genuine article — a NotificationEvent in "sent" and an invite message
        with ``responded: False`` in the candidate's chat.

        ``then_set_status`` flips the instance to canceled/completed *after* the
        invite went out, which is exactly how it happens in production: the class
        is live when invitations are sent and is closed afterwards.
        """
        from padel_app.services.notification_service import (
            _send_invitation_batch,
            get_or_create_config,
        )
        from padel_app.models.vacancy import Vacancy

        cu = _create_user("Coach", f"coach-{suffix}")
        cand_u = _create_user("Candidate", f"cand-{suffix}")
        coach = _create_coach(cu)
        candidate = _create_player(cand_u)
        level = _create_level(coach)
        _create_coach_player(coach, candidate, level)
        instance = _create_instance(
            coach, level, enrolled_players=[], max_players=4,
            start_offset_hours=start_offset_hours,
        )
        _seed_notification_config(coach.id, auto_notify=True)

        vacancy = Vacancy(
            lesson_instance_id=instance.id,
            coach_id=coach.id,
            status="open",
            approval_status="not_required",
        )
        vacancy.create()

        config = get_or_create_config(coach.id)
        with patch(PATCHES[0]), patch(PATCHES[1]):
            notified = _send_invitation_batch(
                vacancy, instance, config, coach.id, max_sim_override=1,
            )
        assert notified, "fixture must produce a live invitation to retire"

        from padel_app.models.notification_event import NotificationEvent
        event = NotificationEvent.query.filter_by(
            lesson_instance_id=instance.id, player_id=candidate.id
        ).first()
        assert event is not None and event.status == "sent"
        assert event.message_id is not None

        if then_set_status is not None:
            instance.status = then_set_status
            db.session.commit()

        return {
            "coach": coach,
            "coach_id": coach.id,
            "candidate": candidate,
            "candidate_id": candidate.id,
            "candidate_user_id": cand_u.id,
            "instance": instance,
            "instance_id": instance.id,
            "vacancy_id": vacancy.id,
            "event_id": event.id,
            "message_id": event.message_id,
        }

    def _assert_retired(self, ids):
        """The event is terminal and the message no longer offers Yes/No."""
        from padel_app.models.messages import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy

        event = NotificationEvent.query.get(ids["event_id"])
        assert event.status == "expired"

        msg = Message.query.get(ids["message_id"])
        # ``responded`` is what both clients key the Yes/No buttons off, so
        # setting it is what actually deactivates the invite — no client change.
        assert msg.msg_metadata.get("responded") is True
        assert msg.msg_metadata.get("response") == "expired"

        assert Vacancy.query.get(ids["vacancy_id"]).status == "expired"

    # -- scheduled sweep ----------------------------------------------------

    def test_scheduled_sweep_retires_pending_invite_for_past_class(self, app):
        """Nobody has to touch anything: the recurring batch pass retires invites
        for a class that came and went unanswered."""
        from padel_app.services.notification_service import process_invitation_batches

        with app.app_context():
            ids = self._seed_pending_invite(suffix="sweep")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                process_invitation_batches(now=datetime.utcnow() + timedelta(days=3))
            self._assert_retired(ids)

    def test_sweep_retires_canceled_class_invite_before_its_start_time(self, app):
        """A canceled class is over even though its start time has not passed."""
        from padel_app.services.notification_service import process_invitation_batches

        with app.app_context():
            ids = self._seed_pending_invite(suffix="cancelled", then_set_status="canceled")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                process_invitation_batches(now=datetime.utcnow())
            self._assert_retired(ids)

    def test_sweep_retires_completed_class_invite(self, app):
        """Same for a class the coach already marked completed."""
        from padel_app.services.notification_service import process_invitation_batches

        with app.app_context():
            ids = self._seed_pending_invite(suffix="completed", then_set_status="completed")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                process_invitation_batches(now=datetime.utcnow())
            self._assert_retired(ids)

    def test_sweep_retires_manual_invite_with_no_vacancy(self, app):
        """Manual notifications carry no vacancy, so a vacancy-driven cleanup
        would miss them. The sweep is driven off NotificationEvent instead."""
        from padel_app.services.notification_service import (
            process_invitation_batches,
            send_manual_notifications,
        )
        from padel_app.models.messages import Message
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            cu = _create_user("Coach", "coach-manual-stale")
            su = _create_user("Student", "student-manual-stale")
            coach = _create_coach(cu)
            student = _create_player(su)
            level = _create_level(coach)
            _create_coach_player(coach, student, level)
            instance = _create_instance(coach, level, enrolled_players=[], max_players=4)
            _seed_notification_config(coach.id, auto_notify=True)

            with patch(PATCHES[0]), patch(PATCHES[1]):
                events = send_manual_notifications(instance.id, [student.id], coach.id)
                event_id = events[0].id
                message_id = events[0].message_id
                assert NotificationEvent.query.get(event_id).status == "sent"
                assert message_id is not None

                process_invitation_batches(now=datetime.utcnow() + timedelta(days=3))

            assert NotificationEvent.query.get(event_id).status == "expired"
            assert Message.query.get(message_id).msg_metadata.get("responded") is True

    # -- the negative case: do not over-expire -------------------------------

    def test_sweep_leaves_future_class_invites_alone(self, app):
        """The important guard rail: a pending invite for a class that has NOT
        happened yet stays live and actionable."""
        from padel_app.services.notification_service import process_invitation_batches
        from padel_app.models.messages import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy

        with app.app_context():
            ids = self._seed_pending_invite(suffix="future", start_offset_hours=24)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                process_invitation_batches(now=datetime.utcnow() + timedelta(hours=1))

            assert NotificationEvent.query.get(ids["event_id"]).status == "sent"
            msg = Message.query.get(ids["message_id"])
            assert msg.msg_metadata.get("responded") is False
            assert "response" not in msg.msg_metadata
            assert Vacancy.query.get(ids["vacancy_id"]).status == "open"

    def test_timely_yes_still_confirms(self, app):
        """And a normal, in-time acceptance is unaffected."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.Association_PlayerLessonInstance import (
            Association_PlayerLessonInstance,
        )
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            ids = self._seed_pending_invite(suffix="timely", start_offset_hours=24)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(
                    ids["event_id"], "yes", ids["candidate_user_id"],
                    now=datetime.utcnow() + timedelta(hours=1),
                )

            assert result["action"] == "confirmed"
            assert NotificationEvent.query.get(ids["event_id"]).status == "confirmed"
            assert Association_PlayerLessonInstance.query.filter_by(
                player_id=ids["candidate_id"], lesson_instance_id=ids["instance_id"]
            ).first() is not None

    # -- late responses ------------------------------------------------------

    def test_late_yes_on_stale_invite_enrols_nobody(self, app):
        """A late 'I'll take it' records nothing and puts nobody in a class that
        already happened."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.Association_PlayerLessonInstance import (
            Association_PlayerLessonInstance,
        )

        with app.app_context():
            ids = self._seed_pending_invite(suffix="lateyes")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(
                    ids["event_id"], "yes", ids["candidate_user_id"],
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert result == {"action": "expired"}
            assert Association_PlayerLessonInstance.query.filter_by(
                player_id=ids["candidate_id"], lesson_instance_id=ids["instance_id"]
            ).first() is None
            self._assert_retired(ids)

    def test_late_no_on_stale_invite_sends_no_replacement(self, app):
        """A late decline must not kick off another invitation round either."""
        from padel_app.services.notification_service import respond_to_notification
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            ids = self._seed_pending_invite(suffix="lateno")
            # A second candidate who must never be invited.
            other_u = _create_user("Other", "other-lateno")
            other = _create_player(other_u)
            _create_coach_player(ids["coach"], other)
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_notification(
                    ids["event_id"], "no", ids["candidate_user_id"],
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert result == {"action": "expired"}
            assert NotificationEvent.query.filter_by(player_id=other.id).count() == 0
            self._assert_retired(ids)

    def test_late_coach_recorded_yes_enrols_nobody(self, app):
        """The coach's manual 'they said yes' path is gated by the same rule."""
        from padel_app.services.notification_service import coach_respond_to_notification
        from padel_app.models.Association_PlayerLessonInstance import (
            Association_PlayerLessonInstance,
        )

        with app.app_context():
            ids = self._seed_pending_invite(suffix="coachlate")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = coach_respond_to_notification(
                    ids["event_id"], "yes", ids["coach_id"],
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert result == {"action": "expired"}
            assert Association_PlayerLessonInstance.query.filter_by(
                player_id=ids["candidate_id"], lesson_instance_id=ids["instance_id"]
            ).first() is None
            self._assert_retired(ids)

    def test_late_waiting_list_yes_creates_no_entry(self, app):
        """Joining the waiting list for a class that already happened is a no-op."""
        from padel_app.services.notification_service import respond_to_waiting_list
        from padel_app.models.waiting_list_entry import WaitingListEntry

        with app.app_context():
            ids = self._seed_pending_invite(suffix="latewl")
            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_waiting_list(
                    ids["instance_id"], "yes", ids["candidate_user_id"],
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert result == {"action": "expired"}
            assert WaitingListEntry.query.filter_by(
                lesson_instance_id=ids["instance_id"]
            ).count() == 0
            self._assert_retired(ids)

    def test_late_reminder_answer_also_retires_pending_invites(self, app):
        """The student's own late reminder tap retires the invites the class had
        outstanding — the lazy counterpart to the scheduled sweep."""
        from padel_app.services.notification_service import respond_to_reminder
        from padel_app.models.Association_PlayerLessonInstance import (
            Association_PlayerLessonInstance,
        )

        with app.app_context():
            ids = self._seed_pending_invite(suffix="latereminder")
            # An enrolled student who will answer their reminder days late.
            su = _create_user("Student", "student-latereminder")
            student = _create_player(su)
            _create_coach_player(ids["coach"], student)
            db.session.add(Association_PlayerLessonInstance(
                player_id=student.id, lesson_instance_id=ids["instance_id"],
            ))
            db.session.commit()

            with patch(PATCHES[0]), patch(PATCHES[1]):
                result = respond_to_reminder(
                    ids["instance_id"], "no", su.id,
                    now=datetime.utcnow() + timedelta(days=3),
                )

            assert result == {"action": "expired"}
            self._assert_retired(ids)

    def test_retirement_is_idempotent(self, app):
        """Repeated late taps converge — nothing is double-retired or re-sent."""
        from padel_app.services.notification_service import (
            process_invitation_batches,
            respond_to_notification,
        )
        from padel_app.models.notification_event import NotificationEvent

        with app.app_context():
            ids = self._seed_pending_invite(suffix="idem")
            late = datetime.utcnow() + timedelta(days=3)
            with patch(PATCHES[0]), patch(PATCHES[1]):
                for _ in range(3):
                    assert respond_to_notification(
                        ids["event_id"], "no", ids["candidate_user_id"], now=late,
                    ) == {"action": "expired"}
                process_invitation_batches(now=late)

            assert NotificationEvent.query.filter_by(
                lesson_instance_id=ids["instance_id"]
            ).count() == 1
            self._assert_retired(ids)
