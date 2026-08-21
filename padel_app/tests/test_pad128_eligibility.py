"""
PAD-128 — Eligibility Phase 1: the standard bar, one resolver, and enforcement
across the invitation/waiting-list engine. Also covers PAD-122 and PAD-123,
which are fixed by the same rewire.

Covered specs:
  eligibility.rules        — rules 1, 2, 3, 4, 6
  eligibility.enforcement  — rules 1, 2, 4, 5, 10

TWO OPPOSITE SETUPS, deliberately — this is what makes the pair of bugs
distinguishable:

  * **PAD-122** (placement bypasses the bar) can only be shown with an
    explicit bar set. With the bar unset every candidate passes, so the
    assertion could never fail and the test would be vacuous.
  * **PAD-123** (the cancelling student is re-placed into their own vacancy) is
    an UNCONDITIONAL correctness fix — eligibility.enforcement rule 10 says the
    enrolled/`absent` exclusion applies whether or not a bar is defined. Its
    tests therefore run with `eligibility_rules = None`. If they set a bar, the
    bar alone could produce the expected result and the actual claim — that the
    exclusion does not ride on eligibility — would go untested.

Run:
    pytest padel_app/tests/test_pad128_eligibility.py -v
"""
from datetime import timedelta

import pytest

from padel_app.sql_db import db
from padel_app.utils.dates import utcnow_naive


def _seed(app, *, levels=("4", "5", "5-"), eligibility_rules=None, max_players=4):
    """One coach with a 3-level ladder, one club, one upcoming class.

    The ladder is `4` (strongest) → `5` → `5-` (weakest), matching the
    eligibility.rules "within N follows the ladder" criterion. `display_order`
    is written explicitly 1..N so the ladder is positional and unambiguous
    (PAD-70).
    """
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.coach_levels import CoachLevel
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.notification_config import NotificationConfig

    with app.app_context():
        coach_user = User(name="Coach", username="elig_coach", password="x", status="active")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        level_ids = {}
        for order, code in enumerate(levels, start=1):
            lv = CoachLevel(coach_id=coach.id, label=code, code=code, display_order=order)
            db.session.add(lv)
            db.session.flush()
            level_ids[code] = lv.id

        club = Club(name="Elig Club", description="", location="City")
        db.session.add(club)
        db.session.flush()

        start = utcnow_naive() + timedelta(days=3)
        lesson = Lesson(
            title="Class", start_datetime=start, end_datetime=start + timedelta(hours=1),
            is_recurring=False, type="academy", max_players=max_players, color="#000",
            status="active", club_id=club.id,
            default_level_id=level_ids.get("5"),
        )
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(
            lesson_id=lesson.id, start_datetime=start,
            end_datetime=start + timedelta(hours=1), max_players=max_players,
            status="scheduled", notifications_enabled=True,
            level_id=level_ids.get("5"),
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(Association_CoachLessonInstance(
            coach_id=coach.id, lesson_instance_id=instance.id))

        config = NotificationConfig(
            coach_id=coach.id,
            auto_notify_enabled=True,
            eligibility_rules=eligibility_rules,
        )
        db.session.add(config)
        db.session.commit()

        return {
            "coach_id": coach.id,
            "coach_user_id": coach_user.id,
            "instance_id": instance.id,
            "lesson_id": lesson.id,
            "level_ids": level_ids,
        }


def _add_student(coach_id, username, level_id=None, side=None, status="active"):
    """A player on the coach's roster. Returns the player id."""
    from padel_app.models import User
    from padel_app.models.players import Player
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer

    user = User(name=username, username=username, password="x", status=status)
    db.session.add(user)
    db.session.flush()
    player = Player(user_id=user.id)
    db.session.add(player)
    db.session.flush()
    db.session.add(Association_CoachPlayer(
        coach_id=coach_id, player_id=player.id, level_id=level_id, side=side))
    db.session.flush()
    return player.id


def _cp(coach_id, player_id):
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    return Association_CoachPlayer.query.filter_by(
        coach_id=coach_id, player_id=player_id).first()


# ---------------------------------------------------------------------------
# eligibility.rules
# ---------------------------------------------------------------------------

def test_unset_bar_admits_everyone(app):
    """eligibility.rules rule 1 / AC "Unset eligibility admits everyone"."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        no_level = _add_student(ids["coach_id"], "nolevel", None)
        db.session.commit()

        rules = effective_eligibility(instance, ids["coach_id"])
        assert rules is None, "NULL must resolve to None, never to a default rule set"

        for pid in (weak, no_level):
            assert passes_eligibility(_cp(ids["coach_id"], pid), instance, ids["coach_id"], rules)


def test_empty_list_bar_admits_everyone(app):
    """`[]` is equivalent to unset (eligibility.rules rule 1)."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=[])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak2", ids["level_ids"]["5-"])
        db.session.commit()

        rules = effective_eligibility(instance, ids["coach_id"])
        assert rules == []
        assert passes_eligibility(_cp(ids["coach_id"], weak), instance, ids["coach_id"], rules)


def test_level_rule_against_class_with_no_level_admits_nobody(app):
    """eligibility.rules rule 2 (PAD-86 fail-closed) — the state that must NOT
    collapse into "no filter". This is the whole reason the column is nullable."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.lessons import Lesson
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=[{"attribute": "level", "operation": "same_as_class"}])
    with app.app_context():
        # Strip the level from BOTH tiers so there is genuinely none anywhere.
        instance = LessonInstance.query.get(ids["instance_id"])
        instance.level_id = None
        Lesson.query.get(ids["lesson_id"]).default_level_id = None
        matching = _add_student(ids["coach_id"], "matching", ids["level_ids"]["5"])
        db.session.commit()

        instance = LessonInstance.query.get(ids["instance_id"])
        rules = effective_eligibility(instance, ids["coach_id"])
        assert not passes_eligibility(
            _cp(ids["coach_id"], matching), instance, ids["coach_id"], rules
        ), "a defined level rule with no class level must admit NOBODY, not everybody"


@pytest.mark.parametrize(
    "value,expected_codes",
    [(1, {"4", "5", "5-"}), (0, {"5"})],
)
def test_within_n_of_class_follows_the_ladder(app, value, expected_codes):
    """eligibility.rules rule 6 / AC "Within N follows the ladder, not level values".

    Ladder 4 → 5 → 5-, class at 5. N=1 admits all three; N=0 only the exact level.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=[
        {"attribute": "level", "operation": "within_n_of_class", "value": value}
    ])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        players = {
            code: _add_student(ids["coach_id"], f"p_{code}_{value}", ids["level_ids"][code])
            for code in ("4", "5", "5-")
        }
        db.session.commit()

        rules = effective_eligibility(instance, ids["coach_id"])
        passing = {
            code for code, pid in players.items()
            if passes_eligibility(_cp(ids["coach_id"], pid), instance, ids["coach_id"], rules)
        }
        assert passing == expected_codes


def test_side_never_affects_eligibility(app):
    """eligibility.rules rule 4 — side is a wave criterion, never a bar."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=[{"attribute": "level", "operation": "same_as_class"}])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        left = _add_student(ids["coach_id"], "lefty", ids["level_ids"]["5"], side="left")
        right = _add_student(ids["coach_id"], "righty", ids["level_ids"]["5"], side="right")
        both = _add_student(ids["coach_id"], "bothy", ids["level_ids"]["5"], side="both")
        db.session.commit()

        rules = effective_eligibility(instance, ids["coach_id"])
        outcomes = {
            pid: passes_eligibility(_cp(ids["coach_id"], pid), instance, ids["coach_id"], rules)
            for pid in (left, right, both)
        }
        assert all(outcomes.values()), f"side changed an eligibility outcome: {outcomes}"


def test_absence_rule_reads_the_coach_scoped_record(app):
    """eligibility.rules rule 3 / AC "Absence rules read the coach-scoped record"."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.services.notification_service import (
        effective_eligibility, passes_eligibility,
    )

    ids = _seed(app, eligibility_rules=[
        {"attribute": "unjustified_absences", "operation": "less_than_or_equal", "value": 2}
    ])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        absentee = _add_student(ids["coach_id"], "absentee", ids["level_ids"]["5"])
        clean = _add_student(ids["coach_id"], "clean", ids["level_ids"]["5"])

        # `presences` is UNIQUE(player_id, lesson_instance_id), so the three
        # absences need three past classes of this coach — which is also the
        # realistic shape, and what makes the count coach-scoped.
        past = utcnow_naive() - timedelta(days=30)
        for offset in range(3):
            past_instance = LessonInstance(
                lesson_id=ids["lesson_id"],
                start_datetime=past + timedelta(days=offset),
                end_datetime=past + timedelta(days=offset, hours=1),
                max_players=4, status="scheduled",
            )
            db.session.add(past_instance)
            db.session.flush()
            db.session.add(Association_CoachLessonInstance(
                coach_id=ids["coach_id"], lesson_instance_id=past_instance.id))
            db.session.add(Presence(
                lesson_instance_id=past_instance.id, player_id=absentee,
                status="absent", justification="unjustified"))
        db.session.commit()

        rules = effective_eligibility(instance, ids["coach_id"])
        assert not passes_eligibility(
            _cp(ids["coach_id"], absentee), instance, ids["coach_id"], rules)
        assert passes_eligibility(
            _cp(ids["coach_id"], clean), instance, ids["coach_id"], rules)


def test_unset_bar_is_never_defaulted_to_a_rule_set(app):
    """The trap this whole column is shaped around.

    Every other JSON column on NotificationConfig defaults to a non-empty
    constant on NULL, and `get_invitation_groups()` doing exactly that is what
    PAD-122 names as its root cause. If `eligibility_rules` ever acquires the
    same idiom, an unset bar silently becomes a real filter.
    """
    from padel_app.models.notification_config import NotificationConfig
    import padel_app.models.notification_config as config_module

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        assert config.get_eligibility_rules() is None
        assert not hasattr(config_module, "DEFAULT_ELIGIBILITY_RULES"), (
            "a DEFAULT_ELIGIBILITY_RULES constant reintroduces the PAD-122 defect"
        )


# ---------------------------------------------------------------------------
# eligibility.enforcement — invitations
# ---------------------------------------------------------------------------

def test_widest_wave_stops_at_the_bar(app):
    """eligibility.enforcement rule 1 / AC "The widest wave stops at the bar".

    The widest invitation group has NO rules of its own (`DEFAULT_INVITATION_GROUPS`
    entry 3), i.e. "everyone". With a bar set it must mean "everyone eligible".
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.models.vacancy import Vacancy
    from padel_app.services.notification_service import _get_eligible_students_for_group

    ids = _seed(app, eligibility_rules=[
        {"attribute": "level", "operation": "within_n_of_class", "value": 1}
    ])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        near = _add_student(ids["coach_id"], "near", ids["level_ids"]["5-"])
        exact = _add_student(ids["coach_id"], "exact", ids["level_ids"]["5"])
        db.session.commit()

        # Add a 4th level far below so it is 2 ladder steps from the class.
        from padel_app.models.coach_levels import CoachLevel
        far_level = CoachLevel(coach_id=ids["coach_id"], label="6", code="6", display_order=4)
        db.session.add(far_level)
        db.session.flush()
        far = _add_student(ids["coach_id"], "far", far_level.id)

        vacancy = Vacancy(
            lesson_instance_id=instance.id, coach_id=ids["coach_id"],
            status="open", level_id=ids["level_ids"]["5"],
        )
        db.session.add(vacancy)
        db.session.commit()

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        instance = LessonInstance.query.get(ids["instance_id"])
        vacancy = Vacancy.query.filter_by(lesson_instance_id=instance.id).first()

        # Group 3 = the widest ("Open to all") in DEFAULT_INVITATION_GROUPS.
        result = _get_eligible_students_for_group(
            vacancy, instance, ids["coach_id"], config, 3)
        invited = {cp.player_id for cp in result}

        assert near in invited and exact in invited
        assert far not in invited, (
            "the widest wave invited a student below the bar — eligibility is "
            "not being applied before the wave criteria"
        )


# ---------------------------------------------------------------------------
# eligibility.enforcement — waiting list (PAD-122)
# EXPLICIT BAR: with the bar unset these assertions could not fail.
# ---------------------------------------------------------------------------

def test_waiting_list_placement_respects_the_bar(app):
    """PAD-122 / eligibility.enforcement rule 2.

    AC: "Given a coach whose eligibility is [{level, same_as_class}] and a
    student with an active standing waiting-list entry whose level does not
    match a class, when a vacancy opens, then they are not placed."
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.models.vacancy import Vacancy
    from padel_app.models.waiting_list_entry import WaitingListEntry
    from padel_app.services.notification_service import _check_waiting_list

    ids = _seed(app, eligibility_rules=[
        {"attribute": "level", "operation": "same_as_class"}
    ])
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        mismatched = _add_student(ids["coach_id"], "mismatched", ids["level_ids"]["5-"])
        db.session.add(WaitingListEntry(
            lesson_instance_id=instance.id, player_id=mismatched,
            coach_id=ids["coach_id"], is_active=True))
        vacancy = Vacancy(
            lesson_instance_id=instance.id, coach_id=ids["coach_id"],
            status="open", level_id=ids["level_ids"]["5"])
        db.session.add(vacancy)
        db.session.commit()

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        instance = LessonInstance.query.get(ids["instance_id"])
        vacancy = Vacancy.query.filter_by(lesson_instance_id=instance.id).first()

        picked = _check_waiting_list(vacancy, instance, ids["coach_id"], config, 1)
        assert picked is None, (
            "a student below the bar was selected for silent placement — the "
            "waiting-list fill is still bypassing eligibility"
        )


def test_waiting_list_placement_honours_excluded_players(app):
    """PAD-122 / eligibility.enforcement rule 5 — `restrictions.excludedPlayers`
    was skipped entirely by the fill path in every configuration."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.models.vacancy import Vacancy
    from padel_app.models.waiting_list_entry import WaitingListEntry
    from padel_app.services.notification_service import _check_waiting_list

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        banned = _add_student(ids["coach_id"], "banned", ids["level_ids"]["5"])
        db.session.add(WaitingListEntry(
            lesson_instance_id=instance.id, player_id=banned,
            coach_id=ids["coach_id"], is_active=True))
        vacancy = Vacancy(
            lesson_instance_id=instance.id, coach_id=ids["coach_id"], status="open")
        db.session.add(vacancy)

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        config.restrictions = {
            "excludedPlayers": {"enabled": True, "playerIds": [str(banned)]},
        }
        db.session.commit()

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        instance = LessonInstance.query.get(ids["instance_id"])
        vacancy = Vacancy.query.filter_by(lesson_instance_id=instance.id).first()

        assert _check_waiting_list(vacancy, instance, ids["coach_id"], config, 1) is None


# ---------------------------------------------------------------------------
# eligibility.enforcement — waiting list (PAD-123)
# BAR DELIBERATELY UNSET: rule 10 makes these exclusions unconditional, so the
# bar must not be what produces the result.
# ---------------------------------------------------------------------------

def test_cancelling_student_is_not_replaced_into_their_own_vacancy(app):
    """PAD-123 / eligibility.enforcement rule 4 — the `absent` half.

    The student still holds their enrolment association AND an `absent`
    presence, which is exactly the state their own cancellation leaves behind.
    Bar is unset, so only the unconditional exclusion can produce this result.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.models.presences import Presence
    from padel_app.models.vacancy import Vacancy
    from padel_app.models.waiting_list_entry import WaitingListEntry
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    from padel_app.services.notification_service import _check_waiting_list

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        canceller = _add_student(ids["coach_id"], "canceller", ids["level_ids"]["5"])

        db.session.add(Association_PlayerLessonInstance(
            player_id=canceller, lesson_instance_id=instance.id))
        db.session.add(Presence(
            lesson_instance_id=instance.id, player_id=canceller, status="absent"))
        db.session.add(WaitingListEntry(
            lesson_instance_id=instance.id, player_id=canceller,
            coach_id=ids["coach_id"], is_active=True))
        vacancy = Vacancy(
            lesson_instance_id=instance.id, coach_id=ids["coach_id"],
            status="open", original_player_id=canceller)
        db.session.add(vacancy)
        db.session.commit()

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        assert config.get_eligibility_rules() is None, (
            "this test must run with NO bar — otherwise the bar, not the "
            "enrolled/absent exclusion, could be what rejects the candidate"
        )
        instance = LessonInstance.query.get(ids["instance_id"])
        vacancy = Vacancy.query.filter_by(lesson_instance_id=instance.id).first()

        picked = _check_waiting_list(vacancy, instance, ids["coach_id"], config, 1)
        assert picked is None, (
            "the student whose cancellation created the vacancy was selected to "
            "fill it — a credit would be spent placing nobody"
        )


def test_enrolled_student_is_never_placed_into_their_own_class(app):
    """PAD-123 / eligibility.enforcement rule 4 — the enrolment half.

    Someone else's cancellation opens the vacancy; an already-enrolled student
    with a standing entry must not be a candidate. Bar unset, as above.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.models.vacancy import Vacancy
    from padel_app.models.waiting_list_entry import WaitingListEntry
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    from padel_app.services.notification_service import _check_waiting_list

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        enrolled = _add_student(ids["coach_id"], "enrolled", ids["level_ids"]["5"])
        outsider = _add_student(ids["coach_id"], "outsider", ids["level_ids"]["5"])

        db.session.add(Association_PlayerLessonInstance(
            player_id=enrolled, lesson_instance_id=instance.id))
        for pid in (enrolled, outsider):
            db.session.add(WaitingListEntry(
                lesson_instance_id=instance.id, player_id=pid,
                coach_id=ids["coach_id"], is_active=True))
        db.session.add(Vacancy(
            lesson_instance_id=instance.id, coach_id=ids["coach_id"], status="open"))
        db.session.commit()

        config = NotificationConfig.query.filter_by(coach_id=ids["coach_id"]).first()
        instance = LessonInstance.query.get(ids["instance_id"])
        vacancy = Vacancy.query.filter_by(lesson_instance_id=instance.id).first()

        picked = _check_waiting_list(vacancy, instance, ids["coach_id"], config, 1)
        assert picked is not None, "the vacancy should still be offered to the outsider"
        assert picked.player_id == outsider, (
            "an already-enrolled student was selected to fill a spot in the very "
            "class they are already in"
        )


# ---------------------------------------------------------------------------
# API round-trip
# ---------------------------------------------------------------------------

def test_eligibility_rules_round_trip_through_the_config_api(app):
    """eligibility.rules rule 9 — read/written under `eligibilityRules`, and
    `null` must survive the round trip as "unset" rather than becoming `[]`."""
    from padel_app.services.notification_service import get_config_dict, update_config

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        assert get_config_dict(ids["coach_id"])["eligibilityRules"] is None

        bar = [{"attribute": "level", "operation": "within_n_of_class", "value": 1}]
        update_config(ids["coach_id"], {"eligibilityRules": bar})
        assert get_config_dict(ids["coach_id"])["eligibilityRules"] == bar

        # Clearing back to unset must produce None, not [].
        update_config(ids["coach_id"], {"eligibilityRules": None})
        assert get_config_dict(ids["coach_id"])["eligibilityRules"] is None

        # An omitted key must leave the stored bar alone.
        update_config(ids["coach_id"], {"eligibilityRules": bar})
        update_config(ids["coach_id"], {"autoNotifyEnabled": True})
        assert get_config_dict(ids["coach_id"])["eligibilityRules"] == bar
