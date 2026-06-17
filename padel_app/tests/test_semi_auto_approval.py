"""
Integration tests for the semi-automatic replacement approval flow
(notifications.semi-auto-approval).

These tests use a real SQLite in-memory DB (via the `app` fixture from
conftest.py). `publish` and `send_push_notification` are patched in BOTH the
notification_service and replacement_approval_service namespaces.

Run:
    pytest padel_app/tests/test_semi_auto_approval.py -v
"""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
    "padel_app.services.replacement_approval_service.publish",
    "padel_app.services.replacement_approval_service.send_push_notification",
]


@contextmanager
def _patched_io():
    with ExitStack() as stack:
        for target in PATCHES:
            stack.enter_context(patch(target))
        yield


# ---------------------------------------------------------------------------
# Seed helpers
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


def _create_coach_player(coach, player, level=None, side=None):
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    cp = Association_CoachPlayer(
        coach_id=coach.id, player_id=player.id,
        level_id=level.id if level else None, side=side,
    )
    db.session.add(cp)
    db.session.flush()
    return cp


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


def _seed_config(coach_id, auto_notify=True, mode="semi_automatic"):
    from padel_app.models.notification_config import NotificationConfig
    cfg = NotificationConfig(coach_id=coach_id, auto_notify_enabled=auto_notify,
                             invitation_mode=mode)
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _mark_absent(instance, player, justification="unjustified"):
    from padel_app.models.presences import Presence
    p = Presence(lesson_instance_id=instance.id, player_id=player.id,
                 invited=True, confirmed=True, status="absent",
                 justification=justification)
    p.create()
    return p


def _seed_world(prefix, *, mode="semi_automatic", start_offset_hours=48,
                n_candidates=1, enrolled=1, max_players=4):
    """Standard world: coach + `enrolled` enrolled players + `n_candidates`
    eligible (non-enrolled) coach players, one instance, one config."""
    cu = _create_user(f"Coach {prefix}", f"coach-{prefix}")
    coach = _create_coach(cu)
    level = _create_level(coach)

    enrolled_players = []
    for i in range(enrolled):
        u = _create_user(f"Enrolled {prefix} {i}", f"enr-{prefix}-{i}")
        p = _create_player(u)
        _create_coach_player(coach, p, level)
        enrolled_players.append((u, p))

    candidates = []
    for i in range(n_candidates):
        u = _create_user(f"Candidate {prefix} {i}", f"cand-{prefix}-{i}")
        p = _create_player(u)
        _create_coach_player(coach, p, level)
        candidates.append((u, p))

    instance = _create_instance(
        coach, level, enrolled_players=[p for _, p in enrolled_players],
        start_offset_hours=start_offset_hours, max_players=max_players,
    )
    config = _seed_config(coach.id, mode=mode)
    return {
        "coach_user": cu, "coach": coach, "level": level,
        "enrolled": enrolled_players, "candidates": candidates,
        "instance": instance, "config": config,
    }


def _create_pending_prompt(world, declined_player, *, now=None):
    """Create the pending vacancy + approval prompt via the reminder-decline path.
    Returns (vacancy, prompt, bundle_id)."""
    from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
    from padel_app.models.vacancy import Vacancy
    from padel_app.services.notification_service import (
        _ensure_vacancy_for_player,
    )
    from padel_app.services.replacement_approval_service import create_approval_prompts

    instance = world["instance"]
    coach = world["coach"]
    config = world["config"]

    _mark_absent(instance, declined_player)
    vacancy = _ensure_vacancy_for_player(instance, coach.id, declined_player.id)
    with _patched_io():
        bundle = create_approval_prompts([vacancy], instance, coach.id, config, now=now)
    prompt = ReplacementApprovalPrompt.query.filter_by(vacancy_id=vacancy.id).first()
    return vacancy, prompt, bundle


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------

class TestInvitationModeConfig:

    def test_default_mode_is_automatic(self, app):
        from padel_app.services.notification_service import get_config_dict
        with app.app_context():
            cu = _create_user("Coach", "coach-cfg-default")
            coach = _create_coach(cu)
            cfg = get_config_dict(coach.id)
        assert cfg["invitationMode"] == "automatic"

    def test_update_and_roundtrip_semi_automatic(self, app):
        from padel_app.services.notification_service import get_config_dict, update_config
        with app.app_context():
            cu = _create_user("Coach", "coach-cfg-rt")
            coach = _create_coach(cu)
            update_config(coach.id, {"autoNotifyEnabled": True,
                                     "invitationMode": "semi_automatic"})
            cfg = get_config_dict(coach.id)
        assert cfg["autoNotifyEnabled"] is True
        assert cfg["invitationMode"] == "semi_automatic"

    def test_invalid_mode_rejected(self, app):
        from werkzeug.exceptions import BadRequest
        from padel_app.services.notification_service import update_config
        with app.app_context():
            cu = _create_user("Coach", "coach-cfg-bad")
            coach = _create_coach(cu)
            with pytest.raises(BadRequest):
                update_config(coach.id, {"invitationMode": "bogus"})


# ---------------------------------------------------------------------------
# (1) Reminder decline in semi-auto
# ---------------------------------------------------------------------------

class TestReminderDeclineSemiAuto:

    def test_decline_creates_pending_vacancy_prompt_and_message_no_invites(self, app):
        from padel_app.models import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.presences import Presence
        from padel_app.services.notification_service import respond_to_reminder

        with app.app_context():
            world = _seed_world("rem", n_candidates=2)
            instance = world["instance"]
            decl_user, decl_player = world["enrolled"][0]

            Presence(lesson_instance_id=instance.id, player_id=decl_player.id,
                     invited=True, confirmed=False).create()

            with _patched_io():
                result = respond_to_reminder(instance.id, "no", decl_user.id,
                                             now=datetime.utcnow())

            assert result["action"] == "declined"

            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance.id,
                original_player_id=decl_player.id,
            ).first()
            assert vacancy is not None
            assert vacancy.status == "open"
            assert vacancy.approval_status == "pending"

            prompt = ReplacementApprovalPrompt.query.filter_by(vacancy_id=vacancy.id).first()
            assert prompt is not None
            assert prompt.status == "pending"
            assert prompt.declined_player_id == decl_player.id

            # Full ordered queue contains both eligible candidates, deduped
            queue_ids = [entry["id"] for entry in prompt.queue_snapshot]
            expected_ids = {str(p.id) for _, p in world["candidates"]}
            assert set(queue_ids) == expected_ids
            assert len(queue_ids) == len(set(queue_ids))

            # Prompt persisted as ONE assistant-conversation message
            msgs = Message.query.filter_by(message_type="replacement_approval").all()
            assert len(msgs) == 1
            meta = msgs[0].msg_metadata
            assert meta["bundleId"] == prompt.bundle_id
            assert meta["responded"] is False
            assert meta["lessonInstanceId"] == instance.id
            assert len(meta["vacancies"]) == 1
            v_meta = meta["vacancies"][0]
            assert v_meta["vacancyId"] == vacancy.id
            assert v_meta["declinedPlayerName"].startswith("Enrolled rem")
            assert {e["id"] for e in v_meta["queue"]} == expected_ids

            # The message lives in the assistant<->coach conversation,
            # sent by the disabled assistant user
            sender = msgs[0].sender
            assert sender.username == "levelup-assistant"
            assert sender.status == "disabled"
            participant_ids = {p.user_id for p in msgs[0].conversation.participants}
            assert participant_ids == {sender.id, world["coach_user"].id}

            # Zero invitations were sent
            assert NotificationEvent.query.count() == 0

    def test_assistant_user_never_listed_in_active_users(self, app):
        from padel_app.models import User
        from padel_app.services.replacement_approval_service import (
            get_or_create_assistant_user,
        )
        with app.app_context():
            assistant = get_or_create_assistant_user()
            active = User.query.filter_by(status="active").all()
            assert assistant.id not in {u.id for u in active}


# ---------------------------------------------------------------------------
# (2) Confirm-presences flow (route) with 2 absents → one bundle
# ---------------------------------------------------------------------------

class TestConfirmPresencesBundle:

    def test_two_absents_one_bundle_one_message(self, app, client):
        from flask_jwt_extended import create_access_token
        from padel_app.models import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.models.vacancy import Vacancy

        app.config["JWT_SECRET_KEY"] = "test-secret"

        with app.app_context():
            world = _seed_world("cp", n_candidates=1, enrolled=2)
            instance = world["instance"]
            coach_user = world["coach_user"]
            (u1, p1), (u2, p2) = world["enrolled"]
            instance_id = instance.id
            p1_id, p2_id = p1.id, p2.id

            token = create_access_token(identity=str(coach_user.id))

            with _patched_io():
                res = client.post(
                    "/api/app/class_instance/presences/confirm",
                    json={
                        "classInstance": {"parentClassId": instance.lesson_id,
                                          "originalId": instance_id},
                        "presences": [
                            {"playerId": p1_id, "status": "absent",
                             "justification": "unjustified"},
                            {"playerId": p2_id, "status": "absent",
                             "justification": "unjustified"},
                        ],
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert res.status_code == 200
            body = res.get_json()
            assert body["notifiedPlayers"] == []

            bundle = body["approvalBundle"]
            assert bundle is not None
            assert len(bundle["vacancies"]) == 2

            vacancies = Vacancy.query.filter_by(lesson_instance_id=instance_id).all()
            assert len(vacancies) == 2
            assert all(v.approval_status == "pending" for v in vacancies)
            assert all(v.status == "open" for v in vacancies)

            prompts = ReplacementApprovalPrompt.query.all()
            assert len(prompts) == 2
            assert len({p.bundle_id for p in prompts}) == 1
            assert prompts[0].bundle_id == bundle["bundleId"]

            msgs = Message.query.filter_by(message_type="replacement_approval").all()
            assert len(msgs) == 1
            assert {p.message_id for p in prompts} == {msgs[0].id}

            assert NotificationEvent.query.count() == 0


# ---------------------------------------------------------------------------
# (3) Idempotency
# ---------------------------------------------------------------------------

class TestPromptIdempotency:

    def test_retrigger_does_not_duplicate_prompts_or_messages(self, app):
        from padel_app.models import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.services.notification_service import trigger_invitations

        with app.app_context():
            # max_players=1 → no structural vacancies; only the absent player's
            world = _seed_world("idem", n_candidates=1, max_players=1)
            instance = world["instance"]
            _, decl_player = world["enrolled"][0]
            _mark_absent(instance, decl_player)

            now = datetime.utcnow()
            with _patched_io():
                trigger_invitations(instance, world["coach"].id, now=now)
                trigger_invitations(instance, world["coach"].id, now=now)

            prompts = ReplacementApprovalPrompt.query.all()
            assert len(prompts) == 1
            assert Message.query.filter_by(message_type="replacement_approval").count() == 1
            assert NotificationEvent.query.count() == 0

    def test_create_approval_prompts_returns_existing_bundle(self, app):
        from padel_app.services.replacement_approval_service import create_approval_prompts

        with app.app_context():
            world = _seed_world("idem2", n_candidates=1)
            _, decl_player = world["enrolled"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            with _patched_io():
                again = create_approval_prompts(
                    [vacancy], world["instance"], world["coach"].id, world["config"]
                )
            assert again is not None
            assert again["bundleId"] == bundle["bundleId"]


# ---------------------------------------------------------------------------
# (4) Batch processor skips pending + dismissed
# ---------------------------------------------------------------------------

class TestBatchProcessorGating:

    def test_skips_pending_and_dismissed_vacancies(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import process_invitation_batches

        with app.app_context():
            world = _seed_world("batch", n_candidates=1, enrolled=2)
            instance = world["instance"]
            (_, p1), (_, p2) = world["enrolled"]

            _mark_absent(instance, p1)
            _mark_absent(instance, p2)
            v1 = Vacancy(lesson_instance_id=instance.id, coach_id=world["coach"].id,
                         original_player_id=p1.id, status="open",
                         approval_status="pending")
            v1.create()
            v2 = Vacancy(lesson_instance_id=instance.id, coach_id=world["coach"].id,
                         original_player_id=p2.id, status="open",
                         approval_status="dismissed")
            v2.create()

            with _patched_io():
                processed = process_invitation_batches(now=datetime.utcnow())

            assert processed == 0
            assert NotificationEvent.query.count() == 0
            assert Vacancy.query.get(v1.id).status == "open"
            assert Vacancy.query.get(v2.id).status == "open"


# ---------------------------------------------------------------------------
# (5) yes_now sends immediately
# ---------------------------------------------------------------------------

class TestYesNow:

    def test_yes_now_approves_and_sends(self, app):
        from padel_app.models import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            world = _seed_world("yesnow", n_candidates=1)
            _, decl_player = world["enrolled"][0]
            _, candidate = world["candidates"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            now = datetime.utcnow()
            with _patched_io():
                result = respond_to_approval(bundle["bundleId"], "yes_now",
                                             world["coach"].id, now=now)

            assert result["action"] == "yes_now"
            assert result["vacancies"] == [
                {"vacancyId": vacancy.id, "result": "approved_now"}
            ]

            v = Vacancy.query.get(vacancy.id)
            assert v.approval_status == "approved"
            assert v.invite_not_before is None

            events = NotificationEvent.query.filter_by(vacancy_id=vacancy.id).all()
            assert len(events) == 1
            assert events[0].player_id == candidate.id
            assert events[0].status == "sent"

            # Assistant message marked as responded
            msg = Message.query.filter_by(message_type="replacement_approval").first()
            assert msg.msg_metadata["responded"] is True
            assert msg.msg_metadata["response"] == "yes_now"


# ---------------------------------------------------------------------------
# (6) yes_at_window before the window opens
# ---------------------------------------------------------------------------

class TestYesAtWindow:

    def test_holds_until_window_then_sends(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import process_invitation_batches
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            # Class in 48h, default invitation window opens 24h before class
            world = _seed_world("yaw", n_candidates=1, start_offset_hours=48)
            _, decl_player = world["enrolled"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            now = datetime.utcnow()
            with _patched_io():
                result = respond_to_approval(bundle["bundleId"], "yes_at_window",
                                             world["coach"].id, now=now)

            assert result["vacancies"][0]["result"] == "approved_at_window"

            v = Vacancy.query.get(vacancy.id)
            instance = world["instance"]
            expected_window = instance.start_datetime - timedelta(hours=24)
            assert v.approval_status == "approved"
            assert v.invite_not_before == expected_window

            # Before the window: batch processor sends nothing
            with _patched_io():
                process_invitation_batches(now=now)
            assert NotificationEvent.query.count() == 0

            # After the window opens: batch processor sends
            with _patched_io():
                process_invitation_batches(now=expected_window + timedelta(minutes=1))
            events = NotificationEvent.query.filter_by(vacancy_id=vacancy.id).all()
            assert len(events) == 1
            assert events[0].status == "sent"

    def test_window_already_open_behaves_as_yes_now(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            # Class in 12h → the 24h-before window is already open
            world = _seed_world("yawopen", n_candidates=1, start_offset_hours=12)
            _, decl_player = world["enrolled"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            now = datetime.utcnow()
            with _patched_io():
                result = respond_to_approval(bundle["bundleId"], "yes_at_window",
                                             world["coach"].id, now=now)

            assert result["vacancies"][0]["result"] == "approved_now"

            v = Vacancy.query.get(vacancy.id)
            assert v.approval_status == "approved"
            assert v.invite_not_before is None

            events = NotificationEvent.query.filter_by(vacancy_id=vacancy.id).all()
            assert len(events) == 1
            assert events[0].status == "sent"


# ---------------------------------------------------------------------------
# (8) Dismiss
# ---------------------------------------------------------------------------

class TestDismiss:

    def test_dismiss_never_sends_but_manual_flow_still_works(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import (
            process_invitation_batches,
            send_manual_notifications,
            trigger_invitations,
        )
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            world = _seed_world("dismiss", n_candidates=1)
            _, decl_player = world["enrolled"][0]
            _, candidate = world["candidates"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            now = datetime.utcnow()
            with _patched_io():
                result = respond_to_approval(bundle["bundleId"], "dismiss",
                                             world["coach"].id, now=now)

            assert result["vacancies"][0]["result"] == "dismissed"
            v = Vacancy.query.get(vacancy.id)
            assert v.approval_status == "dismissed"
            assert v.status == "open"  # vacancy remains open

            # Engine never sends for it
            with _patched_io():
                trigger_invitations(world["instance"], world["coach"].id, now=now)
                process_invitation_batches(now=now)
            auto_events = NotificationEvent.query.filter_by(type="auto").count()
            assert auto_events == 0

            # Manual flow untouched
            with _patched_io():
                events = send_manual_notifications(
                    world["instance"].id, [candidate.id], world["coach"].id
                )
            assert len(events) == 1
            assert events[0].type == "manual"

            # Dismissal is terminal — a second decision is a stale no-op
            with _patched_io():
                second = respond_to_approval(bundle["bundleId"], "yes_now",
                                             world["coach"].id, now=now)
            assert second["vacancies"][0]["result"] == "stale"
            v = Vacancy.query.get(vacancy.id)
            assert v.approval_status == "dismissed"
            assert NotificationEvent.query.filter_by(type="auto").count() == 0


# ---------------------------------------------------------------------------
# (9) Stale after vacancy filled
# ---------------------------------------------------------------------------

class TestStale:

    def test_decision_on_filled_vacancy_is_stale_noop(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            world = _seed_world("stale", n_candidates=1)
            _, decl_player = world["enrolled"][0]
            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            # Vacancy filled via the manual flow before the coach decides
            v = Vacancy.query.get(vacancy.id)
            v.status = "filled"
            db.session.commit()

            with _patched_io():
                result = respond_to_approval(bundle["bundleId"], "yes_now",
                                             world["coach"].id, now=datetime.utcnow())

            assert result["vacancies"][0]["result"] == "stale"
            assert ReplacementApprovalPrompt.query.get(prompt.id).status == "stale"
            assert Vacancy.query.get(vacancy.id).approval_status == "pending"
            assert NotificationEvent.query.count() == 0


# ---------------------------------------------------------------------------
# (10) Waiting list disclosure + gating
# ---------------------------------------------------------------------------

class TestWaitingListGating:

    def test_waiting_list_disclosed_and_gated_until_approval(self, app):
        from padel_app.models.Association_PlayerLessonInstance import (
            Association_PlayerLessonInstance,
        )
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.waiting_list_entry import WaitingListEntry
        from padel_app.services.notification_service import process_invitation_batches
        from padel_app.services.replacement_approval_service import respond_to_approval

        with app.app_context():
            world = _seed_world("wl", n_candidates=1)
            instance = world["instance"]
            coach = world["coach"]
            _, decl_player = world["enrolled"][0]

            wl_user = _create_user("Waitlisted wl", "waitlist-wl")
            wl_player = _create_player(wl_user)
            _create_coach_player(coach, wl_player, world["level"])
            WaitingListEntry(lesson_instance_id=instance.id, player_id=wl_player.id,
                             coach_id=coach.id).create()

            vacancy, prompt, bundle = _create_pending_prompt(world, decl_player)

            # Disclosure in the prompt
            assert prompt.waiting_list_player_id == wl_player.id
            v_meta = bundle["vacancies"][0]
            assert v_meta["waitingListPlayerId"] == wl_player.id
            assert v_meta["waitingListPlayerName"] == "Waitlisted wl"

            # While pending: the standing entry does NOT auto-fill the spot
            now = datetime.utcnow()
            with _patched_io():
                process_invitation_batches(now=now)
            assert Vacancy.query.get(vacancy.id).status == "open"
            assert Association_PlayerLessonInstance.query.filter_by(
                player_id=wl_player.id, lesson_instance_id=instance.id
            ).first() is None

            # After approval: waiting-list fill proceeds normally
            with _patched_io():
                respond_to_approval(bundle["bundleId"], "yes_now", coach.id, now=now)

            v = Vacancy.query.get(vacancy.id)
            assert v.status == "filled"
            assert v.filled_by_player_id == wl_player.id
            assert Association_PlayerLessonInstance.query.filter_by(
                player_id=wl_player.id, lesson_instance_id=instance.id
            ).first() is not None


# ---------------------------------------------------------------------------
# (11) Automatic-mode regression
# ---------------------------------------------------------------------------

class TestAutomaticModeUnchanged:

    def test_automatic_mode_sends_without_prompts(self, app):
        from padel_app.models import Message
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import trigger_invitations

        with app.app_context():
            world = _seed_world("auto", mode="automatic", n_candidates=1)
            instance = world["instance"]
            _, decl_player = world["enrolled"][0]
            _, candidate = world["candidates"][0]
            _mark_absent(instance, decl_player)

            with _patched_io():
                notified = trigger_invitations(instance, world["coach"].id,
                                               now=datetime.utcnow())

            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance.id,
                original_player_id=decl_player.id,
            ).first()
            assert vacancy is not None
            assert vacancy.approval_status == "not_required"

            assert ReplacementApprovalPrompt.query.count() == 0
            assert Message.query.filter_by(message_type="replacement_approval").count() == 0

            assert any(n["id"] == str(candidate.id) for n in notified)
            events = NotificationEvent.query.filter_by(vacancy_id=vacancy.id).all()
            assert len(events) == 1
            assert events[0].player_id == candidate.id
            assert events[0].status == "sent"

    def test_reminder_decline_automatic_mode_unchanged(self, app):
        from padel_app.models.notification_event import NotificationEvent
        from padel_app.models.presences import Presence
        from padel_app.models.replacement_approval_prompt import ReplacementApprovalPrompt
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import respond_to_reminder

        with app.app_context():
            # Class in 12h → invitation window already open → automatic mode
            # triggers invitations immediately on decline
            world = _seed_world("autorem", mode="automatic", n_candidates=1,
                                start_offset_hours=12)
            instance = world["instance"]
            decl_user, decl_player = world["enrolled"][0]
            _, candidate = world["candidates"][0]

            Presence(lesson_instance_id=instance.id, player_id=decl_player.id,
                     invited=True, confirmed=False).create()

            with _patched_io():
                result = respond_to_reminder(instance.id, "no", decl_user.id,
                                             now=datetime.utcnow())

            assert result["action"] == "declined"
            vacancy = Vacancy.query.filter_by(
                lesson_instance_id=instance.id,
                original_player_id=decl_player.id,
            ).first()
            assert vacancy is not None
            assert vacancy.approval_status == "not_required"
            assert ReplacementApprovalPrompt.query.count() == 0

            events = NotificationEvent.query.filter_by(vacancy_id=vacancy.id).all()
            assert len(events) == 1
            assert events[0].player_id == candidate.id
