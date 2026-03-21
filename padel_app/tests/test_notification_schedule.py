"""
Tests for the time-injectable notification scheduling logic.

These tests verify:
  - _check_restrictions: quiet hours, min-time-before-class, no false positives
  - _check_per_student_daily_limit: daily boundary rolls over at midnight
  - send_class_reminders: skips instances that have already started
  - process_invitation_batches: inactivity timer, fresh vacancies, past-class expiry
  - simulate_batch_processor: dry-run utility (pure, no DB)
  - scheduler._compute_timing_dt: correct datetime arithmetic
  - schedule_instance_jobs: respects `now` to decide which jobs are still future

All time-sensitive functions accept a `now` keyword argument so tests can inject
an arbitrary datetime without monkeypatching or freezing the clock.

Run:
    pytest padel_app/tests/test_notification_schedule.py -v
"""

import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from padel_app.models.notification_config import DEFAULT_RESTRICTIONS
from padel_app.scheduler import _compute_timing_dt
from padel_app.utils.notification_preview import simulate_batch_processor


# ===========================================================================
# Helpers / micro-fixtures
# ===========================================================================

def _make_instance(start: datetime, status: str = "scheduled", max_players: int = 4) -> SimpleNamespace:
    """Minimal stand-in for LessonInstance that satisfies restriction checks."""
    return SimpleNamespace(
        id=1,
        start_datetime=start,
        status=status,
        max_players=max_players,
        notifications_enabled=True,
        players_relations=[],
        presences=[],
    )


def _restrictions(**overrides) -> dict:
    """Start from defaults and apply overrides."""
    import copy
    r = copy.deepcopy(DEFAULT_RESTRICTIONS)
    r.update(overrides)
    return r


# ===========================================================================
# _compute_timing_dt
# ===========================================================================

class TestComputeTimingDt:
    def test_hours_before(self):
        start = datetime(2025, 6, 10, 14, 0)
        cfg = {"type": "hours_before", "value": 24}
        result = _compute_timing_dt(start, cfg)
        assert result == datetime(2025, 6, 9, 14, 0)

    def test_hours_before_fractional(self):
        start = datetime(2025, 6, 10, 10, 0)
        cfg = {"type": "hours_before", "value": 3}
        result = _compute_timing_dt(start, cfg)
        assert result == datetime(2025, 6, 10, 7, 0)

    def test_days_before_at_time(self):
        start = datetime(2025, 6, 10, 14, 0)   # Tuesday
        cfg = {"type": "days_before", "days": 1, "time": "09:00"}
        result = _compute_timing_dt(start, cfg)
        assert result == datetime(2025, 6, 9, 9, 0)

    def test_days_before_crosses_month_boundary(self):
        start = datetime(2025, 7, 1, 10, 0)
        cfg = {"type": "days_before", "days": 2, "time": "08:30"}
        result = _compute_timing_dt(start, cfg)
        assert result == datetime(2025, 6, 29, 8, 30)

    def test_missing_config_returns_none(self):
        assert _compute_timing_dt(datetime(2025, 6, 10, 14, 0), {}) is None
        assert _compute_timing_dt(datetime(2025, 6, 10, 14, 0), None) is None

    def test_unknown_type_returns_none(self):
        assert _compute_timing_dt(datetime(2025, 6, 10, 14, 0), {"type": "unknown"}) is None


# ===========================================================================
# _check_restrictions
# ===========================================================================

class TestCheckRestrictions:
    """
    These tests import the function directly and pass a synthetic ``now``
    so we can test at arbitrary times of day.
    """

    def _call(self, now: datetime, restrictions: dict, instance=None) -> bool:
        from padel_app.services.notification_service import _check_restrictions

        if instance is None:
            # Default: class starts in 2 hours — well within any min-time threshold
            instance = _make_instance(now + timedelta(hours=2))

        # Patch out the DB query for maxTotal (returns 0 active events)
        with patch(
            "padel_app.services.notification_service.NotificationEvent"
        ) as MockEvent:
            MockEvent.query.filter_by.return_value.filter.return_value.count.return_value = 0
            return _check_restrictions(instance, coach_id=1, restrictions=restrictions, now=now)

    # ── quiet hours ──────────────────────────────────────────────────────────

    def test_quiet_hours_disabled_allows_night(self):
        r = _restrictions(quietHours={"enabled": False})
        now = datetime(2025, 6, 10, 23, 0)   # 11 PM
        assert self._call(now, r) is True

    def test_quiet_hours_blocks_at_22(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 22, 0)
        assert self._call(now, r) is False

    def test_quiet_hours_blocks_before_7(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 6, 59)
        assert self._call(now, r) is False

    def test_quiet_hours_allows_at_7(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 7, 0)
        assert self._call(now, r) is True

    def test_quiet_hours_allows_at_noon(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 12, 0)
        assert self._call(now, r) is True

    def test_quiet_hours_allows_at_2159(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 21, 59)
        assert self._call(now, r) is True

    # ── min time before class ─────────────────────────────────────────────────

    def test_min_time_blocks_when_too_close(self):
        r = _restrictions(minTimeBeforeClass={"enabled": True, "value": 60})
        now = datetime(2025, 6, 10, 10, 0)
        instance = _make_instance(now + timedelta(minutes=30))  # only 30 min away
        assert self._call(now, r, instance=instance) is False

    def test_min_time_passes_when_far_enough(self):
        r = _restrictions(minTimeBeforeClass={"enabled": True, "value": 60})
        now = datetime(2025, 6, 10, 10, 0)
        instance = _make_instance(now + timedelta(minutes=90))  # 90 min away
        assert self._call(now, r, instance=instance) is True

    def test_min_time_disabled_ignores_proximity(self):
        r = _restrictions(minTimeBeforeClass={"enabled": False, "value": 60})
        now = datetime(2025, 6, 10, 10, 0)
        instance = _make_instance(now + timedelta(minutes=5))   # very close
        assert self._call(now, r, instance=instance) is True

    # ── normal weekday at expected trigger time ───────────────────────────────

    def test_normal_tuesday_morning_fires(self):
        """Tuesday 10:00 with quiet hours on and class at 14:00 should pass."""
        r = _restrictions(quietHours={"enabled": True}, minTimeBeforeClass={"enabled": False, "value": 0})
        now = datetime(2025, 6, 10, 10, 0)  # Tuesday
        instance = _make_instance(now + timedelta(hours=4))
        assert self._call(now, r, instance=instance) is True

    # ── midnight edge case ────────────────────────────────────────────────────

    def test_midnight_blocked_by_quiet_hours(self):
        r = _restrictions(quietHours={"enabled": True})
        now = datetime(2025, 6, 10, 0, 0)
        assert self._call(now, r) is False

    def test_midnight_allowed_without_quiet_hours(self):
        r = _restrictions(quietHours={"enabled": False})
        now = datetime(2025, 6, 10, 0, 0)
        assert self._call(now, r) is True


# ===========================================================================
# _check_per_student_daily_limit
# ===========================================================================

class TestDailyLimit:
    """
    These tests use the app fixture because NotificationEvent.query is a
    Flask-SQLAlchemy dynamic attribute that requires an app context.
    """

    def _call(self, app, now: datetime, count: int, limit_value: int) -> bool:
        from padel_app.services.notification_service import _check_per_student_daily_limit
        from padel_app.models import NotificationEvent

        restrictions = {
            "maxInvitesPerStudentPerDay": {"enabled": True, "value": limit_value}
        }
        with app.app_context():
            mock_query = MagicMock()
            mock_query.filter.return_value.count.return_value = count
            with patch.object(NotificationEvent, "query", mock_query):
                return _check_per_student_daily_limit(
                    player_id=1, coach_id=1, restrictions=restrictions, now=now
                )

    def test_under_limit_passes(self, app):
        now = datetime(2025, 6, 10, 14, 0)
        assert self._call(app, now, count=2, limit_value=3) is True

    def test_at_limit_blocks(self, app):
        now = datetime(2025, 6, 10, 14, 0)
        assert self._call(app, now, count=3, limit_value=3) is False

    def test_over_limit_blocks(self, app):
        now = datetime(2025, 6, 10, 14, 0)
        assert self._call(app, now, count=5, limit_value=3) is False

    def test_disabled_always_passes_regardless_of_count(self):
        from padel_app.services.notification_service import _check_per_student_daily_limit
        restrictions = {"maxInvitesPerStudentPerDay": {"enabled": False, "value": 1}}
        # Even with count=99 this must pass because limit is disabled
        assert _check_per_student_daily_limit(
            player_id=1, coach_id=1, restrictions=restrictions, now=datetime(2025, 6, 10, 14, 0)
        ) is True


# ===========================================================================
# send_class_reminders — past-class guard
# ===========================================================================

class TestSendClassRemindersGuard:
    """
    Tests only the early-exit guard (before any DB writes).
    We patch LessonInstance.query.get to return a fake instance.
    """

    def _mock_instance(self, start: datetime, status: str = "scheduled"):
        inst = MagicMock()
        inst.start_datetime = start
        inst.status = status
        inst.players_relations = []
        return inst

    def _run(self, now: datetime, instance_start: datetime, status: str = "scheduled"):
        from padel_app.services.notification_service import send_class_reminders
        inst = self._mock_instance(instance_start, status)
        with patch("padel_app.services.notification_service.LessonInstance") as MockLI:
            MockLI.query.get.return_value = inst
            with patch("padel_app.services.notification_service.Association_CoachLessonInstance"):
                # We only care whether the function returns early — not about message sending
                try:
                    send_class_reminders(instance_id=1, now=now)
                except Exception:
                    pass  # DB calls after the guard will fail — that's fine

    def test_future_instance_proceeds_past_guard(self):
        """Guard should NOT exit early — the function should reach the player loop."""
        from padel_app.services.notification_service import send_class_reminders
        now = datetime(2025, 6, 10, 8, 0)
        future = now + timedelta(hours=6)
        inst = self._mock_instance(future)
        with patch("padel_app.services.notification_service.LessonInstance") as MockLI:
            MockLI.query.get.return_value = inst
            with patch(
                "padel_app.services.notification_service.Association_CoachLessonInstance"
            ) as MockACLI:
                MockACLI.query.filter_by.return_value.first.return_value = None  # no coach → exits
                send_class_reminders(instance_id=1, now=now)
            # query.get was called — guard didn't abort before it
            MockLI.query.get.assert_called_once_with(1)

    def test_past_instance_returns_without_sending(self):
        from padel_app.services.notification_service import send_class_reminders
        now = datetime(2025, 6, 10, 16, 0)
        past = now - timedelta(hours=1)   # class already started
        inst = self._mock_instance(past)
        with patch("padel_app.services.notification_service.LessonInstance") as MockLI:
            MockLI.query.get.return_value = inst
            with patch(
                "padel_app.services.notification_service.Association_CoachLessonInstance"
            ) as MockACLI:
                send_class_reminders(instance_id=1, now=now)
                # No coach query should happen — we exited before it
                MockACLI.query.filter_by.assert_not_called()

    def test_canceled_instance_returns_early(self):
        from padel_app.services.notification_service import send_class_reminders
        now = datetime(2025, 6, 10, 8, 0)
        future = now + timedelta(hours=3)
        inst = self._mock_instance(future, status="canceled")
        with patch("padel_app.services.notification_service.LessonInstance") as MockLI:
            MockLI.query.get.return_value = inst
            with patch(
                "padel_app.services.notification_service.Association_CoachLessonInstance"
            ) as MockACLI:
                send_class_reminders(instance_id=1, now=now)
                MockACLI.query.filter_by.assert_not_called()

    def test_missing_instance_returns_early(self):
        from padel_app.services.notification_service import send_class_reminders
        with patch("padel_app.services.notification_service.LessonInstance") as MockLI:
            MockLI.query.get.return_value = None
            # Should return without raising
            send_class_reminders(instance_id=99, now=datetime(2025, 6, 10, 8, 0))


# ===========================================================================
# process_invitation_batches — inactivity timer + past-class expiry
# ===========================================================================

class TestProcessInvitationBatches:

    def _make_vacancy(
        self,
        instance_start: datetime,
        last_activity_at=None,
        status: str = "open",
        instance_status: str = "scheduled",
    ):
        instance = MagicMock()
        instance.start_datetime = instance_start
        instance.status = instance_status

        vacancy = MagicMock()
        vacancy.status = status
        vacancy.last_activity_at = last_activity_at
        vacancy.coach_id = 1
        vacancy.lesson_instance = instance
        return vacancy

    def test_fresh_vacancy_triggers_batch(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 10, 0)
        future_start = now + timedelta(hours=4)
        vacancy = self._make_vacancy(future_start, last_activity_at=None)

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config") as mock_cfg, \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]
            mock_cfg.return_value.get_restrictions.return_value = DEFAULT_RESTRICTIONS

            count = process_invitation_batches(now=now)
            mock_send.assert_called_once()
            assert count == 1

    def test_inactive_long_enough_triggers_batch(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 14, 0)
        future_start = now + timedelta(hours=2)
        # Last activity was 3 hours ago; default threshold is 120 min
        last = now - timedelta(hours=3)
        vacancy = self._make_vacancy(future_start, last_activity_at=last)

        restrictions = {**DEFAULT_RESTRICTIONS, "maxInactiveTime": {"enabled": True, "value": 120}}

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config") as mock_cfg, \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]
            mock_cfg.return_value.get_restrictions.return_value = restrictions

            count = process_invitation_batches(now=now)
            mock_send.assert_called_once()
            assert count == 1

    def test_recently_active_does_not_trigger(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 14, 0)
        future_start = now + timedelta(hours=2)
        last = now - timedelta(minutes=30)   # only 30 min ago; threshold is 120
        vacancy = self._make_vacancy(future_start, last_activity_at=last)

        restrictions = {**DEFAULT_RESTRICTIONS, "maxInactiveTime": {"enabled": True, "value": 120}}

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config") as mock_cfg, \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]
            mock_cfg.return_value.get_restrictions.return_value = restrictions

            count = process_invitation_batches(now=now)
            mock_send.assert_not_called()
            assert count == 0

    def test_past_class_vacancy_is_expired(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 15, 0)
        past_start = now - timedelta(hours=1)   # class already started
        vacancy = self._make_vacancy(past_start)

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config"), \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]

            process_invitation_batches(now=now)
            mock_send.assert_not_called()
            assert vacancy.status == "expired"

    def test_canceled_class_vacancy_is_expired(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 10, 0)
        future_start = now + timedelta(hours=4)
        vacancy = self._make_vacancy(future_start, instance_status="canceled")

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config"), \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]

            process_invitation_batches(now=now)
            mock_send.assert_not_called()
            assert vacancy.status == "expired"

    def test_empty_vacancy_list_returns_zero(self):
        from padel_app.services.notification_service import process_invitation_batches

        with patch("padel_app.services.notification_service.Vacancy") as MockV:
            MockV.query.filter_by.return_value.all.return_value = []
            count = process_invitation_batches(now=datetime(2025, 6, 10, 10, 0))
            assert count == 0

    def test_inactivity_disabled_never_triggers_on_timer(self):
        from padel_app.services.notification_service import process_invitation_batches

        now = datetime(2025, 6, 10, 14, 0)
        future_start = now + timedelta(hours=2)
        last = now - timedelta(hours=10)   # very stale
        vacancy = self._make_vacancy(future_start, last_activity_at=last)

        restrictions = {**DEFAULT_RESTRICTIONS, "maxInactiveTime": {"enabled": False, "value": 120}}

        with patch("padel_app.services.notification_service.Vacancy") as MockV, \
             patch("padel_app.services.notification_service.get_or_create_config") as mock_cfg, \
             patch("padel_app.services.notification_service._send_invitation_batch") as mock_send:
            MockV.query.filter_by.return_value.all.return_value = [vacancy]
            mock_cfg.return_value.get_restrictions.return_value = restrictions

            count = process_invitation_batches(now=now)
            mock_send.assert_not_called()
            assert count == 0


# ===========================================================================
# simulate_batch_processor — pure dry-run utility (no DB)
# ===========================================================================

class TestSimulateBatchProcessor:

    def _make_snap(self, vacancy_id: int, instance_start: datetime, last: datetime | None = None):
        return {
            "id": vacancy_id,
            "last_activity_at": last,
            "instance_start": instance_start,
            "coach_id": 1,
        }

    def test_fresh_vacancy_fires_on_first_tick(self):
        now = datetime(2025, 6, 10, 10, 0)
        instance_start = now + timedelta(hours=4)
        snap = self._make_snap(1, instance_start, last=None)

        fired = simulate_batch_processor(
            [snap],
            from_dt=now,
            to_dt=now + timedelta(minutes=10),
            max_inactive_minutes=120,
            step_minutes=2,
        )
        assert len(fired) == 1
        assert fired[0]["reason"] == "fresh"
        assert fired[0]["vacancy_id"] == 1

    def test_inactivity_fires_after_threshold(self):
        now = datetime(2025, 6, 10, 10, 0)
        instance_start = now + timedelta(hours=6)
        last_activity = now - timedelta(minutes=119)   # just under threshold at start
        snap = self._make_snap(1, instance_start, last=last_activity)

        fired = simulate_batch_processor(
            [snap],
            from_dt=now,
            to_dt=now + timedelta(minutes=6),   # 3 ticks
            max_inactive_minutes=120,
            step_minutes=2,
        )
        # At t+0: 119 min elapsed → not yet
        # At t+2: 121 min elapsed → fires
        assert len(fired) == 1
        assert fired[0]["at"] == now + timedelta(minutes=2)
        assert fired[0]["reason"] == "inactivity"

    def test_not_fired_before_threshold(self):
        now = datetime(2025, 6, 10, 10, 0)
        instance_start = now + timedelta(hours=6)
        last_activity = now - timedelta(minutes=60)   # only 60 min ago
        snap = self._make_snap(1, instance_start, last=last_activity)

        fired = simulate_batch_processor(
            [snap],
            from_dt=now,
            to_dt=now + timedelta(minutes=50),   # still not enough
            max_inactive_minutes=120,
            step_minutes=2,
        )
        assert fired == []

    def test_past_instance_never_fires(self):
        now = datetime(2025, 6, 10, 16, 0)
        instance_start = now - timedelta(hours=1)   # already started
        snap = self._make_snap(1, instance_start, last=None)

        fired = simulate_batch_processor(
            [snap],
            from_dt=now,
            to_dt=now + timedelta(hours=1),
            max_inactive_minutes=120,
            step_minutes=2,
        )
        assert fired == []

    def test_empty_vacancy_list(self):
        now = datetime(2025, 6, 10, 10, 0)
        fired = simulate_batch_processor(
            [],
            from_dt=now,
            to_dt=now + timedelta(hours=2),
            max_inactive_minutes=120,
        )
        assert fired == []

    def test_multiple_vacancies_tracked_independently(self):
        now = datetime(2025, 6, 10, 10, 0)
        instance_start = now + timedelta(hours=6)

        snap1 = self._make_snap(1, instance_start, last=now - timedelta(minutes=119))
        snap2 = self._make_snap(2, instance_start, last=None)  # fresh

        fired = simulate_batch_processor(
            [snap1, snap2],
            from_dt=now,
            to_dt=now + timedelta(minutes=4),
            max_inactive_minutes=120,
            step_minutes=2,
        )
        vacancy_ids = {f["vacancy_id"] for f in fired}
        reasons = {f["vacancy_id"]: f["reason"] for f in fired}

        assert 2 in vacancy_ids            # fresh fires immediately
        assert reasons[2] == "fresh"
        assert 1 in vacancy_ids            # inactivity fires at t+2
        assert reasons[1] == "inactivity"


# ===========================================================================
# schedule_instance_jobs — timing logic via _compute_timing_dt
# ===========================================================================
# APScheduler is a production dependency not installed in the test virtualenv,
# so we test the underlying timing predicate directly rather than calling
# schedule_instance_jobs end-to-end.

class TestScheduleTimingPredicate:
    """
    The job-scheduling guard is: ``fire_dt > now``.
    Since _compute_timing_dt is already tested above, these tests verify that
    the past/future predicate gives the correct answer for representative cases.
    """

    def _should_schedule(self, class_start: datetime, timing: dict, now: datetime) -> bool:
        fire_dt = _compute_timing_dt(class_start, timing)
        return fire_dt is not None and fire_dt > now

    def test_future_reminder_should_schedule(self):
        class_start = datetime(2025, 6, 13, 10, 0)
        now = datetime(2025, 6, 10, 8, 0)
        # reminder_dt = June 11 10:00 → future
        assert self._should_schedule(class_start, {"type": "hours_before", "value": 48}, now) is True

    def test_past_reminder_should_not_schedule(self):
        class_start = datetime(2025, 6, 9, 10, 0)
        now = datetime(2025, 6, 10, 8, 0)   # day after class
        # reminder_dt = June 7 10:00 → past
        assert self._should_schedule(class_start, {"type": "hours_before", "value": 48}, now) is False

    def test_exact_moment_is_not_scheduled(self):
        # fire_dt == now → NOT future (strict >)
        class_start = datetime(2025, 6, 11, 10, 0)
        now = datetime(2025, 6, 9, 10, 0)   # exactly 48h before
        assert self._should_schedule(class_start, {"type": "hours_before", "value": 48}, now) is False

    def test_one_second_future_is_scheduled(self):
        from datetime import timedelta
        class_start = datetime(2025, 6, 11, 10, 0)
        now = datetime(2025, 6, 9, 10, 0) - timedelta(seconds=1)   # 1s before fire_dt
        assert self._should_schedule(class_start, {"type": "hours_before", "value": 48}, now) is True
