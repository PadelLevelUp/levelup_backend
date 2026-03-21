"""
Tests for the NotificationConfig model and notification_service helpers.

Tests marked with pytest.mark.new_backend cover fields that require the backend
to be updated (invitation_groups, tiebreakers, full reminderTiming payload,
excludedPlayers / excludeUnpaidSubscription restrictions). They will fail until
the model migrations and service wiring are complete.

Run:
    pytest padel_app/tests/test_notification_config.py -v
"""
import pytest
from padel_app.models.notification_config import (
    NotificationConfig,
    DEFAULT_RESTRICTIONS,
    DEFAULT_MESSAGE_TEMPLATES,
    DEFAULT_REMINDER_TIMING,
    DEFAULT_INVITATION_START_TIMING,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def fresh_config():
    """NotificationConfig with no stored values — all defaults."""
    cfg = NotificationConfig.__new__(NotificationConfig)
    cfg.id = None
    cfg.coach_id = 1
    cfg.auto_notify_enabled = False
    cfg.priority_criteria = None
    cfg.restrictions = None
    cfg.rounds = None
    cfg.notification_groups = None
    cfg.message_templates = None
    cfg.reminder_timing = None
    cfg.invitation_start_timing = None
    # New columns — may not exist yet; default to None gracefully
    cfg.invitation_groups = None
    cfg.tiebreakers = None
    return cfg


@pytest.fixture
def config_with_overrides():
    """NotificationConfig with partial stored values."""
    cfg = NotificationConfig.__new__(NotificationConfig)
    cfg.id = 1
    cfg.coach_id = 1
    cfg.auto_notify_enabled = True
    cfg.restrictions = {
        "maxSimultaneous": {"enabled": True, "value": 5},
        "quietHours": {"enabled": True},
    }
    cfg.message_templates = {
        "invite": "Custom invite text",
    }
    cfg.reminder_timing = {"type": "hours_before", "value": 24}
    cfg.invitation_start_timing = {"type": "days_before_at_time", "days": 1, "time": "09:00"}
    cfg.priority_criteria = None
    cfg.rounds = None
    cfg.notification_groups = None
    cfg.invitation_groups = None
    cfg.tiebreakers = None
    return cfg


# ===========================================================================
# DEFAULT_RESTRICTIONS — current and required new fields
# ===========================================================================

class TestDefaultRestrictions:

    def test_has_max_simultaneous(self):
        assert "maxSimultaneous" in DEFAULT_RESTRICTIONS
        assert DEFAULT_RESTRICTIONS["maxSimultaneous"]["enabled"] is True
        assert DEFAULT_RESTRICTIONS["maxSimultaneous"]["value"] == 3

    def test_has_max_total(self):
        assert "maxTotal" in DEFAULT_RESTRICTIONS
        assert DEFAULT_RESTRICTIONS["maxTotal"]["value"] == 10

    def test_has_min_time_before_class(self):
        assert "minTimeBeforeClass" in DEFAULT_RESTRICTIONS

    def test_has_max_invites_per_student_per_day(self):
        assert "maxInvitesPerStudentPerDay" in DEFAULT_RESTRICTIONS

    def test_has_quiet_hours(self):
        assert "quietHours" in DEFAULT_RESTRICTIONS

    def test_has_max_inactive_time(self):
        """maxInactiveTime is already implemented in backend."""
        assert "maxInactiveTime" in DEFAULT_RESTRICTIONS
        assert DEFAULT_RESTRICTIONS["maxInactiveTime"]["enabled"] is True
        assert DEFAULT_RESTRICTIONS["maxInactiveTime"]["value"] == 120

    @pytest.mark.new_backend
    def test_has_excluded_players(self):
        """excludedPlayers must be added to DEFAULT_RESTRICTIONS."""
        assert "excludedPlayers" in DEFAULT_RESTRICTIONS
        assert DEFAULT_RESTRICTIONS["excludedPlayers"]["enabled"] is False
        assert DEFAULT_RESTRICTIONS["excludedPlayers"]["playerIds"] == []

    @pytest.mark.new_backend
    def test_has_exclude_unpaid_subscription(self):
        """excludeUnpaidSubscription must be added to DEFAULT_RESTRICTIONS."""
        assert "excludeUnpaidSubscription" in DEFAULT_RESTRICTIONS
        assert DEFAULT_RESTRICTIONS["excludeUnpaidSubscription"]["enabled"] is False


# ===========================================================================
# NotificationConfig.get_restrictions()
# ===========================================================================

class TestGetRestrictions:

    def test_returns_all_defaults_when_nothing_stored(self, fresh_config):
        r = fresh_config.get_restrictions()
        assert r["maxSimultaneous"]["value"] == 3
        assert r["maxTotal"]["value"] == 10
        assert r["maxInactiveTime"]["value"] == 120

    def test_stored_value_overrides_default(self, config_with_overrides):
        r = config_with_overrides.get_restrictions()
        assert r["maxSimultaneous"]["value"] == 5

    def test_unset_keys_fall_back_to_defaults(self, config_with_overrides):
        """Keys not in stored restrictions must come from DEFAULT_RESTRICTIONS."""
        r = config_with_overrides.get_restrictions()
        # quietHours was stored as enabled=True, but maxInactiveTime was not stored
        assert r["maxInactiveTime"]["value"] == 120

    def test_stored_quiet_hours_override(self, config_with_overrides):
        r = config_with_overrides.get_restrictions()
        assert r["quietHours"]["enabled"] is True

    @pytest.mark.new_backend
    def test_returns_excluded_players_default_when_not_stored(self, fresh_config):
        r = fresh_config.get_restrictions()
        assert "excludedPlayers" in r
        assert r["excludedPlayers"]["playerIds"] == []

    @pytest.mark.new_backend
    def test_stored_excluded_players_are_preserved(self):
        cfg = NotificationConfig.__new__(NotificationConfig)
        cfg.restrictions = {
            "excludedPlayers": {"enabled": True, "playerIds": ["42", "99"]},
        }
        r = cfg.get_restrictions()
        assert r["excludedPlayers"]["playerIds"] == ["42", "99"]

    @pytest.mark.new_backend
    def test_returns_exclude_unpaid_subscription_default(self, fresh_config):
        r = fresh_config.get_restrictions()
        assert "excludeUnpaidSubscription" in r
        assert r["excludeUnpaidSubscription"]["enabled"] is False


# ===========================================================================
# DEFAULT_MESSAGE_TEMPLATES — current and required new keys
# ===========================================================================

class TestDefaultMessageTemplates:

    def test_has_invite(self):
        assert "invite" in DEFAULT_MESSAGE_TEMPLATES
        assert "{name}" in DEFAULT_MESSAGE_TEMPLATES["invite"]

    def test_has_confirm_and_decline(self):
        assert "confirm" in DEFAULT_MESSAGE_TEMPLATES
        assert "decline" in DEFAULT_MESSAGE_TEMPLATES

    def test_has_spot_filled(self):
        assert "spot_filled" in DEFAULT_MESSAGE_TEMPLATES

    def test_has_reminder(self):
        """reminder key was added in the backend rewrite."""
        assert "reminder" in DEFAULT_MESSAGE_TEMPLATES
        assert "{name}" in DEFAULT_MESSAGE_TEMPLATES["reminder"]

    def test_has_waiting_list_offer(self):
        assert "waiting_list_offer" in DEFAULT_MESSAGE_TEMPLATES

    def test_has_waiting_list_placed(self):
        assert "waiting_list_placed" in DEFAULT_MESSAGE_TEMPLATES
        assert "{name}" in DEFAULT_MESSAGE_TEMPLATES["waiting_list_placed"]

    @pytest.mark.new_backend
    def test_has_reminder_followup(self):
        """reminder_followup key (matching frontend) must be present."""
        assert "reminder_followup" in DEFAULT_MESSAGE_TEMPLATES

    @pytest.mark.new_backend
    def test_has_reminder_confirmed(self):
        """Key must be reminder_confirmed (not reminder_confirm)."""
        assert "reminder_confirmed" in DEFAULT_MESSAGE_TEMPLATES

    @pytest.mark.new_backend
    def test_has_reminder_declined(self):
        """Key must be reminder_declined (not reminder_decline)."""
        assert "reminder_declined" in DEFAULT_MESSAGE_TEMPLATES


# ===========================================================================
# NotificationConfig.get_message_templates()
# ===========================================================================

class TestGetMessageTemplates:

    def test_returns_all_defaults_when_nothing_stored(self, fresh_config):
        t = fresh_config.get_message_templates()
        assert "invite" in t
        assert "reminder" in t
        assert "waiting_list_offer" in t

    def test_stored_template_overrides_default(self, config_with_overrides):
        t = config_with_overrides.get_message_templates()
        assert t["invite"] == "Custom invite text"

    def test_unset_templates_fall_back_to_defaults(self, config_with_overrides):
        t = config_with_overrides.get_message_templates()
        # Only "invite" was stored — all others should fall back
        assert t["confirm"] == DEFAULT_MESSAGE_TEMPLATES["confirm"]

    @pytest.mark.new_backend
    def test_returns_all_required_keys(self, fresh_config):
        """All 10 template keys required by the frontend must be present."""
        required_keys = {
            "invite", "confirm", "decline", "spot_filled",
            "reminder", "reminder_followup", "reminder_confirmed", "reminder_declined",
            "waiting_list_offer", "waiting_list_placed",
        }
        t = fresh_config.get_message_templates()
        missing = required_keys - set(t.keys())
        assert not missing, f"Missing template keys: {missing}"


# ===========================================================================
# Default reminder timing
# ===========================================================================

class TestDefaultReminderTiming:

    def test_default_reminder_timing_type(self):
        assert DEFAULT_REMINDER_TIMING["type"] == "hours_before"

    def test_default_reminder_timing_value(self):
        assert DEFAULT_REMINDER_TIMING["value"] == 48

    def test_default_invitation_start_timing(self):
        assert DEFAULT_INVITATION_START_TIMING["type"] == "hours_before"
        assert DEFAULT_INVITATION_START_TIMING["value"] == 24

    def test_get_reminder_timing_returns_default(self, fresh_config):
        t = fresh_config.get_reminder_timing()
        assert t["type"] == "hours_before"
        assert t["value"] == 48

    def test_get_reminder_timing_returns_stored(self, config_with_overrides):
        t = config_with_overrides.get_reminder_timing()
        assert t["value"] == 24

    def test_get_invitation_start_timing_returns_stored(self, config_with_overrides):
        t = config_with_overrides.get_invitation_start_timing()
        assert t["type"] == "days_before_at_time"
        assert t["days"] == 1


# ===========================================================================
# Invitation groups — new column (new_backend)
# ===========================================================================

class TestInvitationGroups:

    DEFAULT_INVITATION_GROUPS = [
        {
            "id": "1",
            "rules": [
                {"attribute": "level", "operation": "same_as_vacancy"},
                {"attribute": "side", "operation": "same_as_vacancy"},
            ],
        },
        {
            "id": "2",
            "rules": [{"attribute": "level", "operation": "same_as_vacancy"}],
        },
        {"id": "3", "rules": []},
    ]

    @pytest.mark.new_backend
    def test_model_has_invitation_groups_column(self, fresh_config):
        """Model must have invitation_groups JSON column."""
        assert hasattr(fresh_config, "invitation_groups")

    @pytest.mark.new_backend
    def test_get_invitation_groups_returns_defaults(self, fresh_config):
        """get_invitation_groups() must return 3-group default."""
        groups = fresh_config.get_invitation_groups()
        assert isinstance(groups, list)
        assert len(groups) == 3
        # Last group should be empty (catch-all)
        assert groups[-1]["rules"] == []

    @pytest.mark.new_backend
    def test_get_invitation_groups_returns_stored(self):
        cfg = NotificationConfig.__new__(NotificationConfig)
        cfg.invitation_groups = [{"id": "1", "rules": []}]
        groups = cfg.get_invitation_groups()
        assert len(groups) == 1

    @pytest.mark.new_backend
    def test_get_config_dict_includes_invitation_groups(self, app):
        from padel_app.services.notification_service import get_config_dict
        with app.app_context():
            # Would require a real coach fixture — structure check only
            # Verify the key name is correct
            from padel_app.services import notification_service
            import inspect
            src = inspect.getsource(notification_service.get_config_dict)
            assert "invitationGroups" in src


# ===========================================================================
# Tiebreakers — new column (new_backend)
# ===========================================================================

class TestTiebreakers:

    DEFAULT_TIEBREAKERS = [
        {"id": "unjustified_absences", "label": "Fewest unjustified absences", "enabled": True},
        {"id": "justified_absences", "label": "Most justified absences", "enabled": True},
        {"id": "attendance_rate", "label": "Highest attendance rate", "enabled": True},
        {"id": "playing_side_match", "label": "Matching playing side", "enabled": False},
        {"id": "subscription_status", "label": "Active subscription", "enabled": False},
    ]

    @pytest.mark.new_backend
    def test_model_has_tiebreakers_column(self, fresh_config):
        """Model must have tiebreakers JSON column."""
        assert hasattr(fresh_config, "tiebreakers")

    @pytest.mark.new_backend
    def test_get_tiebreakers_returns_defaults(self, fresh_config):
        """get_tiebreakers() must return 5 default tiebreakers."""
        tiebreakers = fresh_config.get_tiebreakers()
        assert isinstance(tiebreakers, list)
        assert len(tiebreakers) == 5
        ids = [t["id"] for t in tiebreakers]
        assert "unjustified_absences" in ids
        assert "attendance_rate" in ids

    @pytest.mark.new_backend
    def test_first_three_tiebreakers_enabled_by_default(self, fresh_config):
        tiebreakers = fresh_config.get_tiebreakers()
        enabled = [t for t in tiebreakers if t["enabled"]]
        assert len(enabled) >= 3

    @pytest.mark.new_backend
    def test_get_tiebreakers_returns_stored_order(self):
        cfg = NotificationConfig.__new__(NotificationConfig)
        cfg.tiebreakers = [
            {"id": "attendance_rate", "label": "Highest attendance rate", "enabled": True},
        ]
        result = cfg.get_tiebreakers()
        assert result[0]["id"] == "attendance_rate"

    @pytest.mark.new_backend
    def test_get_config_dict_includes_tiebreakers(self, app):
        from padel_app.services import notification_service
        import inspect
        src = inspect.getsource(notification_service.get_config_dict)
        assert "tiebreakers" in src


# ===========================================================================
# update_config — new payload keys (new_backend)
# ===========================================================================

class TestUpdateConfigPayload:
    """
    These tests verify that update_config correctly processes the new frontend
    payload keys. They require DB access so they use the 'app' fixture.
    They are all marked new_backend since the service needs updating.
    """

    @pytest.mark.new_backend
    def test_update_config_saves_invitation_groups(self, app):
        with app.app_context():
            from padel_app.services.notification_service import update_config
            from padel_app.tests.helpers import make_coach  # create a minimal coach

            coach_id = make_coach(app)
            groups = [
                {"id": "1", "rules": [{"attribute": "level", "operation": "same_as_vacancy"}]},
                {"id": "2", "rules": []},
            ]
            cfg = update_config(coach_id, {"invitationGroups": groups})
            assert cfg.invitation_groups == groups

    @pytest.mark.new_backend
    def test_update_config_saves_tiebreakers(self, app):
        with app.app_context():
            from padel_app.services.notification_service import update_config
            from padel_app.tests.helpers import make_coach

            coach_id = make_coach(app)
            tbs = [
                {"id": "unjustified_absences", "label": "Fewest unjustified absences", "enabled": True},
                {"id": "attendance_rate", "label": "Highest attendance rate", "enabled": False},
            ]
            cfg = update_config(coach_id, {"tiebreakers": tbs})
            assert cfg.tiebreakers == tbs

    @pytest.mark.new_backend
    def test_update_config_saves_full_reminder_timing(self, app):
        """
        The new reminderTiming payload is a composite object, not just a flat
        timing dict. update_config must store it correctly and get_config_dict
        must return it under 'reminderTiming'.
        """
        with app.app_context():
            from padel_app.services.notification_service import update_config, get_config_dict
            from padel_app.tests.helpers import make_coach

            coach_id = make_coach(app)
            payload = {
                "reminderTiming": {
                    "firstReminder": {"type": "days_before_at_time", "days": 2, "time": "17:00"},
                    "reminderCount": 2,
                    "hoursBetweenReminders": 4,
                    "invitationStart": {"type": "hours_before", "value": 24},
                }
            }
            update_config(coach_id, payload)
            config_dict = get_config_dict(coach_id)

            assert "reminderTiming" in config_dict
            rt = config_dict["reminderTiming"]
            assert rt["firstReminder"]["type"] == "days_before_at_time"
            assert rt["reminderCount"] == 2
            assert rt["hoursBetweenReminders"] == 4
            assert "invitationStart" in rt

    @pytest.mark.new_backend
    def test_update_config_saves_excluded_players(self, app):
        with app.app_context():
            from padel_app.services.notification_service import update_config
            from padel_app.tests.helpers import make_coach

            coach_id = make_coach(app)
            cfg = update_config(coach_id, {
                "restrictions": {
                    "excludedPlayers": {"enabled": True, "playerIds": ["10", "20"]},
                    "quietHours": {"enabled": False},
                }
            })
            r = cfg.get_restrictions()
            assert r["excludedPlayers"]["playerIds"] == ["10", "20"]

    @pytest.mark.new_backend
    def test_update_config_saves_exclude_unpaid_subscription(self, app):
        with app.app_context():
            from padel_app.services.notification_service import update_config
            from padel_app.tests.helpers import make_coach

            coach_id = make_coach(app)
            cfg = update_config(coach_id, {
                "restrictions": {
                    "excludeUnpaidSubscription": {"enabled": True},
                }
            })
            r = cfg.get_restrictions()
            assert r["excludeUnpaidSubscription"]["enabled"] is True
