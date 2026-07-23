"""
PAD-70 — the invitation engine must respect the coach's CUSTOM level ordering.

Reported symptom: a coach whose ladder is ``4`` (strongest), ``5``, ``5-``
(weakest) had eight invitation groups keyed on "one level above the vacancy" /
"same level as the vacancy". A vacancy opened at level ``4`` and a ``5-``
student — two steps away in the ladder — was invited immediately, as if they
were one level above.

Root cause: adjacency was computed from the raw ``display_order`` INTEGER rather
than from the coach's ladder POSITION, so any level carrying an unset order
(``NULL`` / the column default ``0``) sorted ahead of every explicitly ordered
level and was read as "one above" the top of the ladder.

Run:
    pytest padel_app/tests/test_level_ladder_ordering.py -v
"""

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _create_coach(username="ladder-coach"):
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach

    user = User(name="Coach", username=username, email=f"{username}@test.com",
                password="hashed", status="active")
    db.session.add(user)
    db.session.flush()
    coach = Coach(user_id=user.id)
    db.session.add(coach)
    db.session.flush()
    return coach


def _create_level(coach, code, label=None, display_order=None):
    from padel_app.models.coach_levels import CoachLevel

    lv = CoachLevel(coach_id=coach.id, label=label or code, code=code,
                    display_order=display_order)
    db.session.add(lv)
    db.session.flush()
    return lv


def _create_coach_player(coach, level, username):
    from padel_app.models.users import User
    from padel_app.models.players import Player
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer

    user = User(name=username, username=username, email=f"{username}@test.com",
                password="hashed", status="active")
    db.session.add(user)
    db.session.flush()
    player = Player(user_id=user.id)
    db.session.add(player)
    db.session.flush()
    cp = Association_CoachPlayer(coach_id=coach.id, player_id=player.id,
                                 level_id=level.id if level else None)
    db.session.add(cp)
    db.session.flush()
    return cp


class _FakeVacancy:
    """Minimal stand-in for Vacancy — _passes_group_rules only reads these."""

    def __init__(self, level):
        self.level = level
        self.level_id = level.id if level else None
        self.side = None


# ---------------------------------------------------------------------------
# Ladder adjacency
# ---------------------------------------------------------------------------

class TestLadderAdjacency:

    def test_one_above_is_the_adjacent_level_not_two_steps_away(self, app):
        """Ticket ladder 4 -> 5 -> 5-: only `4` is one level above a `5` vacancy."""
        from padel_app.services.notification_service import _level_ids_one_above

        with app.app_context():
            coach = _create_coach("ladder-adj")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            lv5m = _create_level(coach, "5-", display_order=3)
            db.session.commit()

            assert _level_ids_one_above(lv5, coach.id) == {lv4.id}
            # The top of the ladder has nothing above it.
            assert _level_ids_one_above(lv4, coach.id) == set()
            # `5-` is two steps from `4` — never adjacent to it.
            assert lv5m.id not in _level_ids_one_above(lv4, coach.id)

    def test_one_below_is_the_adjacent_level(self, app):
        from padel_app.services.notification_service import _level_ids_one_below

        with app.app_context():
            coach = _create_coach("ladder-below")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            lv5m = _create_level(coach, "5-", display_order=3)
            db.session.commit()

            assert _level_ids_one_below(lv4, coach.id) == {lv5.id}
            assert _level_ids_one_below(lv5, coach.id) == {lv5m.id}
            # The bottom of the ladder has nothing below it.
            assert _level_ids_one_below(lv5m, coach.id) == set()

    def test_unset_display_order_sorts_last_not_first(self, app):
        """A level with NULL/0 order must not masquerade as the strongest level."""
        from padel_app.services.notification_service import (
            _level_ids_one_above,
            _level_ids_one_below,
        )

        with app.app_context():
            coach = _create_coach("ladder-null")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            # Created through a path that never supplied an order (import,
            # single-level POST, legacy row predating the column).
            lv5m = _create_level(coach, "5-", display_order=None)
            lv6 = _create_level(coach, "6", display_order=0)
            db.session.commit()

            # `4` is still the top of the ladder.
            assert _level_ids_one_above(lv4, coach.id) == set()
            # ... and the unordered levels land at the BOTTOM, in id order.
            assert _level_ids_one_below(lv5, coach.id) == {lv5m.id}
            assert _level_ids_one_below(lv5m, coach.id) == {lv6.id}
            assert _level_ids_one_below(lv6, coach.id) == set()

    def test_duplicate_display_orders_do_not_widen_adjacency(self, app):
        """Two levels sharing an order still yield a single-step ladder."""
        from padel_app.services.notification_service import _level_ids_one_above

        with app.app_context():
            coach = _create_coach("ladder-dupe")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            lv5m = _create_level(coach, "5-", display_order=2)
            db.session.commit()

            # Ladder resolves to 4, 5, 5- (ties broken by id).
            assert _level_ids_one_above(lv5, coach.id) == {lv4.id}
            assert _level_ids_one_above(lv5m, coach.id) == {lv5.id}

    def test_levels_of_other_coaches_are_ignored(self, app):
        from padel_app.services.notification_service import _level_ids_one_above

        with app.app_context():
            coach = _create_coach("ladder-mine")
            other = _create_coach("ladder-theirs")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            _create_level(other, "X", display_order=1)
            db.session.commit()

            assert _level_ids_one_above(lv5, coach.id) == {lv4.id}


# ---------------------------------------------------------------------------
# Group rules — the actual reported symptom
# ---------------------------------------------------------------------------

class TestGroupLevelRules:

    def test_two_steps_away_student_fails_one_above_rule(self, app):
        """The reported bug: a `5-` student invited for a `4` vacancy."""
        from padel_app.services.notification_service import _passes_group_rules

        with app.app_context():
            coach = _create_coach("rules-report")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            # `5-` added later without an explicit order — the production shape.
            lv5m = _create_level(coach, "5-", display_order=None)
            db.session.commit()

            vacancy = _FakeVacancy(lv4)
            rules = [{"attribute": "level", "operation": "one_above_vacancy"}]

            cp_5m = _create_coach_player(coach, lv5m, "student-5minus")
            cp_5 = _create_coach_player(coach, lv5, "student-5")
            cp_4 = _create_coach_player(coach, lv4, "student-4")
            db.session.commit()

            # Nothing is above the top of the ladder.
            assert _passes_group_rules(rules, cp_5m, vacancy, coach.id) is False
            assert _passes_group_rules(rules, cp_5, vacancy, coach.id) is False
            assert _passes_group_rules(rules, cp_4, vacancy, coach.id) is False

    def test_one_above_admits_only_the_adjacent_level(self, app):
        from padel_app.services.notification_service import _passes_group_rules

        with app.app_context():
            coach = _create_coach("rules-adj")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            lv5m = _create_level(coach, "5-", display_order=3)
            db.session.commit()

            vacancy = _FakeVacancy(lv5)
            rules = [{"attribute": "level", "operation": "one_above_vacancy"}]

            cp_4 = _create_coach_player(coach, lv4, "adj-4")
            cp_5m = _create_coach_player(coach, lv5m, "adj-5minus")
            db.session.commit()

            assert _passes_group_rules(rules, cp_4, vacancy, coach.id) is True
            assert _passes_group_rules(rules, cp_5m, vacancy, coach.id) is False

    def test_all_above_below_use_ladder_position(self, app):
        from padel_app.services.notification_service import _passes_group_rules

        with app.app_context():
            coach = _create_coach("rules-all")
            lv4 = _create_level(coach, "4", display_order=1)
            lv5 = _create_level(coach, "5", display_order=2)
            # Unordered: belongs at the BOTTOM, so it is "below" the vacancy.
            lv5m = _create_level(coach, "5-", display_order=None)
            db.session.commit()

            vacancy = _FakeVacancy(lv5)
            cp_4 = _create_coach_player(coach, lv4, "all-4")
            cp_5m = _create_coach_player(coach, lv5m, "all-5minus")
            db.session.commit()

            above = [{"attribute": "level", "operation": "all_above_vacancy"}]
            below = [{"attribute": "level", "operation": "all_below_vacancy"}]

            assert _passes_group_rules(above, cp_4, vacancy, coach.id) is True
            assert _passes_group_rules(above, cp_5m, vacancy, coach.id) is False
            assert _passes_group_rules(below, cp_5m, vacancy, coach.id) is True
            assert _passes_group_rules(below, cp_4, vacancy, coach.id) is False


# ---------------------------------------------------------------------------
# display_order normalisation on write
# ---------------------------------------------------------------------------

class TestDisplayOrderNormalisation:

    def test_upsert_renumbers_levels_contiguously(self, app):
        from padel_app.services.coach_service import upsert_coach_levels
        from padel_app.models.coach_levels import CoachLevel

        with app.app_context():
            coach = _create_coach("norm-upsert")
            db.session.commit()

            upsert_coach_levels(coach, [
                {"code": "4", "label": "4", "displayOrder": 1},
                {"code": "5", "label": "5", "displayOrder": 2},
                {"code": "5-", "label": "5-"},  # client omitted the order
            ])
            db.session.commit()

            levels = {lv.code: lv.display_order
                      for lv in CoachLevel.query.filter_by(coach_id=coach.id).all()}
            assert levels == {"4": 1, "5": 2, "5-": 3}

    def test_single_level_create_appends_to_the_bottom(self, app):
        from padel_app.services.coach_service import create_coach_level_service
        from padel_app.models.coach_levels import CoachLevel

        with app.app_context():
            coach = _create_coach("norm-create")
            _create_level(coach, "4", display_order=1)
            _create_level(coach, "5", display_order=2)
            db.session.commit()

            create_coach_level_service({"coach": coach.id, "code": "5-", "label": "5-"})
            db.session.commit()

            created = CoachLevel.query.filter_by(coach_id=coach.id, code="5-").first()
            assert created.display_order == 3

    def test_bulk_import_without_order_column_appends_in_row_order(self, app):
        from padel_app.services.import_service import bulk_create_coach_levels
        from padel_app.models.coach_levels import CoachLevel

        with app.app_context():
            coach = _create_coach("norm-import")
            db.session.commit()

            bulk_create_coach_levels(
                [{"code": "4", "label": "4"},
                 {"code": "5", "label": "5"},
                 {"code": "5-", "label": "5-"}],
                coach,
            )
            db.session.commit()

            levels = {lv.code: lv.display_order
                      for lv in CoachLevel.query.filter_by(coach_id=coach.id).all()}
            assert levels == {"4": 1, "5": 2, "5-": 3}
