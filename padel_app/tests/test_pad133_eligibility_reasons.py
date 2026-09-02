"""
PAD-133 — Eligibility: the manual-add warning with named reasons, and the
stricter-bar save report.

Covered specs:
  eligibility.enforcement — rules 6, 7, 8, 9

The load-bearing invariant here is AGREEMENT. `passes_eligibility` (the bar) and
`eligibility_failures` (the explanation) run the SAME evaluator, so an empty
failure list must always mean "passes". Two evaluators would let the answer and
the reason drift — the client would warn about a student it then enrolled
silently, or enrol one it had just called ineligible. `test_failures_and_bool_never_disagree`
is what holds that line; it is the single most important test in this file.

Run:
    pytest padel_app/tests/test_pad133_eligibility_reasons.py -v
"""
from datetime import timedelta

from padel_app.sql_db import db
from padel_app.utils.dates import utcnow_naive

from padel_app.tests.test_pad128_eligibility import _seed, _add_student, _cp


def _enrol(player_id, instance_id):
    """Put a player IN the class (what rule 9 means by "enrolled")."""
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )
    db.session.add(Association_PlayerLessonInstance(
        player_id=player_id, lesson_instance_id=instance_id))
    db.session.flush()


# ---------------------------------------------------------------------------
# The agreement invariant (rules 6/7 rest on it)
# ---------------------------------------------------------------------------

def test_failures_and_bool_never_disagree(app):
    """`eligibility_failures` empty <=> `passes_eligibility` True.

    Swept across the whole ladder and several bars. If these two ever disagree,
    the coach is warned about a student who is then enrolled without a warning,
    or vice versa.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        passes_eligibility, eligibility_failures,
    )

    bars = [
        [{"attribute": "level", "operation": "same_as_class"}],
        [{"attribute": "level", "operation": "within_n_of_class", "value": 1}],
        [{"attribute": "level", "operation": "equal_or_above_class"}],
        [{"attribute": "level", "operation": "equal_or_below_class"}],
        [{"attribute": "level", "operation": "one_below_or_above_class"}],
    ]

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        pids = [
            _add_student(ids["coach_id"], "strong", ids["level_ids"]["4"]),
            _add_student(ids["coach_id"], "same", ids["level_ids"]["5"]),
            _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"]),
            _add_student(ids["coach_id"], "nolevel", None),
        ]
        db.session.commit()

        checked = 0
        for rules in bars:
            for pid in pids:
                cp = _cp(ids["coach_id"], pid)
                ok = passes_eligibility(cp, instance, ids["coach_id"], rules)
                failures = eligibility_failures(cp, instance, ids["coach_id"], rules)
                assert ok == (not failures), (
                    f"disagreement: passes={ok} but failures={failures} "
                    f"for {rules}"
                )
                checked += 1
        # Guard against the sweep silently collapsing to nothing.
        assert checked == len(bars) * len(pids) == 20


def test_unset_bar_reports_no_failures(app):
    """An unset/empty bar admits everyone (eligibility.rules rule 1)."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        pid = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()
        cp = _cp(ids["coach_id"], pid)

        assert eligibility_failures(cp, instance, ids["coach_id"], None) == []
        assert eligibility_failures(cp, instance, ids["coach_id"], []) == []


# ---------------------------------------------------------------------------
# Rule 7 — the warning names WHAT failed, one line per failed rule
# ---------------------------------------------------------------------------

def test_level_failure_carries_actual_threshold_and_signed_distance(app):
    """"2 levels below this class" must be derivable without any prose."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    # Ladder 4 (strongest) -> 5 -> 5-; the class is level "5".
    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()

        rules = [{"attribute": "level", "operation": "same_as_class"}]
        (failure,) = eligibility_failures(
            _cp(ids["coach_id"], weak), instance, ids["coach_id"], rules)

        assert failure["attribute"] == "level"
        assert failure["operation"] == "same_as_class"
        assert failure["actual"] == "5-"          # the student's level
        assert failure["threshold"] == "5"        # the class's level
        # Positive == weaker than the class. The sign is what lets the client
        # say "below" rather than a direction-less "1 away".
        assert failure["ladder_distance"] == 1


def test_stronger_student_gets_a_negative_distance(app):
    """The sign distinguishes "above" from "below" — same magnitude, opposite way."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        strong = _add_student(ids["coach_id"], "strong", ids["level_ids"]["4"])
        db.session.commit()

        rules = [{"attribute": "level", "operation": "same_as_class"}]
        (failure,) = eligibility_failures(
            _cp(ids["coach_id"], strong), instance, ids["coach_id"], rules)

        assert failure["ladder_distance"] == -1


def test_every_failed_rule_is_reported_not_just_the_first(app):
    """Rule 7: one line PER failed rule.

    Short-circuiting is exactly the defect this function exists to fix, so a
    student failing two rules must produce two records.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()

        rules = [
            {"attribute": "level", "operation": "same_as_class"},
            {"attribute": "unjustified_absences", "operation": "less_than", "value": 0},
        ]
        failures = eligibility_failures(
            _cp(ids["coach_id"], weak), instance, ids["coach_id"], rules)

        assert len(failures) == 2, failures
        assert {f["attribute"] for f in failures} == {"level", "unjustified_absences"}


def test_absence_failure_reports_the_real_count_not_a_bool(app):
    """Rule 7's "(4, limit is 2)" is unrenderable without the actual number."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        pid = _add_student(ids["coach_id"], "absentee", ids["level_ids"]["5"])
        db.session.commit()

        rules = [{
            "attribute": "unjustified_absences",
            "operation": "less_than",
            "value": 0,
        }]
        (failure,) = eligibility_failures(
            _cp(ids["coach_id"], pid), instance, ids["coach_id"], rules)

        assert failure["actual"] == 0          # a real count, an int
        assert failure["threshold"] == 0       # the configured limit
        assert not isinstance(failure["actual"], bool)


def test_fail_closed_cases_carry_a_reason_code(app):
    """A student with no level fails, and says WHY.

    These are the fail-closed paths where `actual`/`threshold` cannot be
    meaningful, so emitting bare nulls would leave the client with nothing to
    render. A machine-readable `reason` is the substitute.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import eligibility_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        no_level = _add_student(ids["coach_id"], "nolevel", None)
        db.session.commit()

        rules = [{"attribute": "level", "operation": "same_as_class"}]
        (failure,) = eligibility_failures(
            _cp(ids["coach_id"], no_level), instance, ids["coach_id"], rules)

        assert failure["reason"] == "student_has_no_level"
        assert failure["threshold"] == "5"  # still names the class's level


# ---------------------------------------------------------------------------
# The invitation engine must not pay for the explanation
# ---------------------------------------------------------------------------

def test_bool_path_still_short_circuits(app):
    """`_passes_group_rules` stops at the first failure — behaviour AND cost.

    Evaluating every rule on the hot matching loop would add absence/attendance
    queries per candidate. The shared evaluator keeps the fast path fast.
    """
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import _group_rule_failures

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()
        cp = _cp(ids["coach_id"], weak)

        rules = [
            {"attribute": "level", "operation": "same_as_class"},
            {"attribute": "unjustified_absences", "operation": "less_than", "value": 0},
        ]
        short = _group_rule_failures(rules, cp, None, ids["coach_id"], instance,
                                     short_circuit=True)
        full = _group_rule_failures(rules, cp, None, ids["coach_id"], instance,
                                    short_circuit=False)

        assert len(short) == 1, "fast path must stop at the first failure"
        assert len(full) == 2, "explanation path must evaluate every rule"
        # Both agree on the verdict, which is the point of one evaluator.
        assert bool(short) == bool(full)


# ---------------------------------------------------------------------------
# Rule 6 — the manual-add surface
# ---------------------------------------------------------------------------

def test_manual_add_lists_only_the_failing_students(app):
    """Passing students are omitted, so an empty list means "no warning"."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.notification_config import NotificationConfig
    from padel_app.services.notification_service import (
        eligibility_failures_for_players,
    )

    rules = [{"attribute": "level", "operation": "same_as_class"}]
    ids = _seed(app, eligibility_rules=rules)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        ok = _add_student(ids["coach_id"], "same", ids["level_ids"]["5"])
        bad = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()

        result = eligibility_failures_for_players(
            instance, ids["coach_id"], [ok, bad])

        assert [r["playerId"] for r in result] == [bad]
        assert result[0]["name"] == "weak"
        assert result[0]["failures"][0]["actual"] == "5-"


def test_manual_add_is_silent_when_no_bar_is_set(app):
    """Rule 1: an unset bar admits everyone, so there is nothing to confirm."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        eligibility_failures_for_players,
    )

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        db.session.commit()

        assert eligibility_failures_for_players(
            instance, ids["coach_id"], [weak]) == []


def test_manual_add_ignores_players_outside_the_roster(app):
    """A player the coach does not have cannot be judged against their bar."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        eligibility_failures_for_players,
    )

    rules = [{"attribute": "level", "operation": "same_as_class"}]
    ids = _seed(app, eligibility_rules=rules)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        db.session.commit()
        # 999999 is on nobody's roster.
        assert eligibility_failures_for_players(
            instance, ids["coach_id"], [999999]) == []


# ---------------------------------------------------------------------------
# Rule 9 — the stricter-bar save report (and rule 8: nobody is removed)
# ---------------------------------------------------------------------------

def test_stricter_bar_names_the_enrolled_students_it_would_exclude(app):
    """Rule 9's acceptance criterion, with the exact 6-enrolled/3-failing shape."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.notification_service import (
        students_failing_eligibility_bar,
    )

    ids = _seed(app, eligibility_rules=None, max_players=6)
    with app.app_context():
        instance = LessonInstance.query.get(ids["instance_id"])
        passing, failing = [], []
        for i in range(3):
            pid = _add_student(ids["coach_id"], f"ok{i}", ids["level_ids"]["5"])
            _enrol(pid, instance.id)
            passing.append(pid)
        for i in range(3):
            pid = _add_student(ids["coach_id"], f"bad{i}", ids["level_ids"]["5-"])
            _enrol(pid, instance.id)
            failing.append(pid)
        db.session.commit()

        rules = [{"attribute": "level", "operation": "same_as_class"}]
        affected = students_failing_eligibility_bar(ids["coach_id"], rules)

        assert sorted(a["playerId"] for a in affected) == sorted(failing)
        assert all(a["classTitle"] == "Class" for a in affected)
        assert all(a["failures"] for a in affected)

        # Rule 8: reporting NEVER un-enrols. All 6 are still in the class.
        db.session.expire_all()
        again = LessonInstance.query.get(ids["instance_id"])
        assert len(list(again.players_relations)) == 6


def test_report_is_empty_for_an_unset_bar(app):
    """Clearing the bar excludes nobody, so there is nothing to report."""
    from padel_app.services.notification_service import (
        students_failing_eligibility_bar,
    )

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        assert students_failing_eligibility_bar(ids["coach_id"], None) == []
        assert students_failing_eligibility_bar(ids["coach_id"], []) == []


def test_report_ignores_classes_that_already_happened(app):
    """A bar cannot retroactively un-enrol anyone, so past classes are noise."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.services.notification_service import (
        students_failing_eligibility_bar,
    )

    ids = _seed(app, eligibility_rules=None)
    with app.app_context():
        future = LessonInstance.query.get(ids["instance_id"])
        past_start = utcnow_naive() - timedelta(days=5)
        past = LessonInstance(
            lesson_id=ids["lesson_id"], start_datetime=past_start,
            end_datetime=past_start + timedelta(hours=1), max_players=4,
            status="scheduled", notifications_enabled=True,
            level_id=ids["level_ids"]["5"],
        )
        db.session.add(past)
        db.session.flush()
        db.session.add(Association_CoachLessonInstance(
            coach_id=ids["coach_id"], lesson_instance_id=past.id))

        weak = _add_student(ids["coach_id"], "weak", ids["level_ids"]["5-"])
        _enrol(weak, past.id)
        _enrol(weak, future.id)
        db.session.commit()

        rules = [{"attribute": "level", "operation": "same_as_class"}]
        affected = students_failing_eligibility_bar(ids["coach_id"], rules)

        # Reported once — for the upcoming class only, not the past one.
        assert [a["instanceId"] for a in affected] == [future.id]
