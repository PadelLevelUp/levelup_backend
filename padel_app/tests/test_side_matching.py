"""
Unit tests for the "both" court-side matching logic (PAD-15).

Covers the pure helpers that decide side eligibility and the exact-side-preferred
tiebreaker used by the invitation engine:
  - _side_eligible(player_side, vacancy_side)
  - _side_preference_rank(player_side, vacancy_side)

Run:
    pytest padel_app/tests/test_side_matching.py -v
"""

import pytest


@pytest.fixture
def helpers():
    from padel_app.services.notification_service import (
        _side_eligible,
        _side_preference_rank,
    )
    return _side_eligible, _side_preference_rank


class TestSideEligible:
    def test_exact_match(self, helpers):
        eligible, _ = helpers
        assert eligible("left", "left") is True
        assert eligible("right", "right") is True

    def test_strict_mismatch_is_ineligible(self, helpers):
        eligible, _ = helpers
        assert eligible("left", "right") is False
        assert eligible("right", "left") is False

    def test_both_player_eligible_for_any_specific_side(self, helpers):
        eligible, _ = helpers
        assert eligible("both", "left") is True
        assert eligible("both", "right") is True
        assert eligible("both", "both") is True

    def test_specific_player_eligible_for_both_vacancy(self, helpers):
        eligible, _ = helpers
        assert eligible("left", "both") is True
        assert eligible("right", "both") is True

    def test_no_vacancy_side_accepts_everyone(self, helpers):
        eligible, _ = helpers
        assert eligible("left", None) is True
        assert eligible(None, None) is True
        assert eligible("both", None) is True

    def test_player_without_side_ineligible_for_specific_vacancy(self, helpers):
        eligible, _ = helpers
        # Unchanged from prior left/right behaviour: a player with no recorded
        # side preference does not match any side-constrained vacancy (left,
        # right, or both). Only a vacancy with no side (None) accepts them.
        assert eligible(None, "left") is False
        assert eligible(None, "right") is False
        assert eligible(None, "both") is False


class TestSidePreferenceRank:
    def test_exact_match_ranks_first(self, helpers):
        _, rank = helpers
        assert rank("left", "left") == 0
        assert rank("right", "right") == 0

    def test_both_fallback_ranks_after_exact(self, helpers):
        _, rank = helpers
        # For a left vacancy: exact left (0) < both (1) < wrong side (2)
        assert rank("left", "left") < rank("both", "left")
        assert rank("both", "left") < rank("right", "left")

    def test_both_vacancy_prefers_exact_both_players(self, helpers):
        _, rank = helpers
        assert rank("both", "both") == 0
        # a left player is a "both" fallback for a both vacancy
        assert rank("left", "both") == 1

    def test_no_vacancy_side_is_neutral(self, helpers):
        _, rank = helpers
        assert rank("left", None) == 0
        assert rank("both", None) == 0
