"""
PAD-86 — the invitation engine must resolve a class's level the SAME way everywhere.

Reported symptom: a class whose level lives only on the parent ``Lesson``
(``lesson.default_level_id``) produced structural vacancies with
``level_id = None``. ``_passes_group_rules()`` then treated "no level on the
vacancy" as "the level filter is switched off", so an invitation group whose
ONLY rule was a level rule silently matched the coach's ENTIRE roster — the
exact opposite of what the coach configured. The ``{level}`` message
placeholder rendered empty for the same reason.

Fix: one ``effective_level_id()`` helper (instance level, falling back to the
parent lesson's default level) used by vacancy creation, eligibility, message
rendering and the invitation-group preview — and level rules that FAIL CLOSED
when there is genuinely no level anywhere.

Run:
    pytest padel_app/tests/test_effective_level_resolution.py -v
"""

from datetime import datetime, timedelta

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _create_coach(username):
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


def _create_level(coach, code, display_order=None):
    from padel_app.models.coach_levels import CoachLevel

    lv = CoachLevel(coach_id=coach.id, label=code, code=code,
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


def _create_class(coach, suffix, *, lesson_level=None, instance_level=None,
                  max_players=4):
    """A club + lesson + (empty) lesson instance wired to ``coach``."""
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )

    club = Club(name=f"Club {suffix}", description="c", location="x")
    db.session.add(club)
    db.session.flush()

    start = datetime.utcnow().replace(microsecond=0) + timedelta(days=1)
    lesson = Lesson(
        title=f"Class {suffix}",
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        is_recurring=False,
        type="academy",
        max_players=max_players,
        status="active",
        club_id=club.id,
        default_level_id=lesson_level.id if lesson_level else None,
    )
    db.session.add(lesson)
    db.session.flush()

    instance = LessonInstance(
        lesson_id=lesson.id,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        max_players=max_players,
        status="scheduled",
        original_lesson_occurence_date=start.date(),
        level_id=instance_level.id if instance_level else None,
    )
    db.session.add(instance)
    db.session.flush()

    db.session.add(Association_CoachLessonInstance(
        coach_id=coach.id, lesson_instance_id=instance.id,
    ))
    db.session.commit()
    return lesson, instance


class _FakeVacancy:
    """Minimal stand-in for Vacancy — _passes_group_rules only reads these."""

    def __init__(self, level):
        self.level = level
        self.level_id = level.id if level else None
        self.side = None


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

class TestEffectiveLevelId:

    def test_instance_level_wins(self, app):
        from padel_app.services.notification_service import effective_level_id

        with app.app_context():
            coach = _create_coach("eff-own")
            own = _create_level(coach, "4", display_order=1)
            fallback = _create_level(coach, "5", display_order=2)
            db.session.commit()
            _, instance = _create_class(coach, "own", lesson_level=fallback,
                                        instance_level=own)

            assert effective_level_id(instance) == own.id

    def test_falls_back_to_lesson_default_level(self, app):
        from padel_app.services.notification_service import effective_level_id

        with app.app_context():
            coach = _create_coach("eff-fallback")
            lesson_level = _create_level(coach, "5", display_order=1)
            db.session.commit()
            _, instance = _create_class(coach, "fallback",
                                        lesson_level=lesson_level)

            assert instance.level_id is None
            assert effective_level_id(instance) == lesson_level.id

    def test_none_when_no_level_anywhere(self, app):
        from padel_app.services.notification_service import effective_level_id

        with app.app_context():
            coach = _create_coach("eff-none")
            db.session.commit()
            _, instance = _create_class(coach, "none")

            assert effective_level_id(instance) is None

    def test_accepts_a_lesson_directly(self, app):
        from padel_app.services.notification_service import effective_level_id

        with app.app_context():
            coach = _create_coach("eff-lesson")
            lesson_level = _create_level(coach, "5", display_order=1)
            db.session.commit()
            lesson, _ = _create_class(coach, "lesson", lesson_level=lesson_level)

            assert effective_level_id(lesson) == lesson_level.id

    def test_none_object_is_safe(self, app):
        from padel_app.services.notification_service import effective_level_id

        with app.app_context():
            assert effective_level_id(None) is None


# ---------------------------------------------------------------------------
# Structural vacancies — the reported bug
# ---------------------------------------------------------------------------

class TestStructuralVacancyLevel:

    def test_structural_vacancy_inherits_lesson_default_level(self, app):
        """The bug: the level lives on the Lesson, the vacancy came out level-less."""
        from padel_app.services.notification_service import _create_structural_vacancies

        with app.app_context():
            coach = _create_coach("struct-fallback")
            lesson_level = _create_level(coach, "5", display_order=1)
            db.session.commit()
            _, instance = _create_class(coach, "struct-fallback",
                                        lesson_level=lesson_level, max_players=2)

            vacancies = _create_structural_vacancies(instance, coach.id)

            assert len(vacancies) == 2
            assert all(v.level_id == lesson_level.id for v in vacancies)

    def test_structural_vacancy_prefers_the_instance_level(self, app):
        from padel_app.services.notification_service import _create_structural_vacancies

        with app.app_context():
            coach = _create_coach("struct-own")
            own = _create_level(coach, "4", display_order=1)
            fallback = _create_level(coach, "5", display_order=2)
            db.session.commit()
            _, instance = _create_class(coach, "struct-own",
                                        lesson_level=fallback,
                                        instance_level=own, max_players=1)

            vacancies = _create_structural_vacancies(instance, coach.id)

            assert [v.level_id for v in vacancies] == [own.id]

    def test_structural_vacancy_has_no_level_when_there_is_none(self, app):
        from padel_app.services.notification_service import _create_structural_vacancies

        with app.app_context():
            coach = _create_coach("struct-none")
            db.session.commit()
            _, instance = _create_class(coach, "struct-none", max_players=1)

            vacancies = _create_structural_vacancies(instance, coach.id)

            assert [v.level_id for v in vacancies] == [None]


# ---------------------------------------------------------------------------
# End-to-end eligibility through a level-only invitation group
# ---------------------------------------------------------------------------

class TestGroupEligibilityForStructuralVacancy:

    def test_level_only_group_does_not_match_the_whole_roster(self, app):
        """A level-only group must invite same-level students ONLY."""
        from padel_app.services.notification_service import (
            _create_structural_vacancies,
            _get_eligible_students_for_group,
            get_or_create_config,
        )

        with app.app_context():
            coach = _create_coach("group-struct")
            lv5 = _create_level(coach, "5", display_order=1)
            lv6 = _create_level(coach, "6", display_order=2)
            db.session.commit()
            _, instance = _create_class(coach, "group-struct",
                                        lesson_level=lv5, max_players=1)

            same = _create_coach_player(coach, lv5, "group-same")
            other = _create_coach_player(coach, lv6, "group-other")
            db.session.commit()

            config = get_or_create_config(coach.id)
            config.invitation_groups = [
                {"id": "1", "rules": [
                    {"attribute": "level", "operation": "same_as_vacancy"},
                ]},
            ]
            config.save()

            vacancy = _create_structural_vacancies(instance, coach.id)[0]
            eligible = _get_eligible_students_for_group(
                vacancy, instance, coach.id, config, 1
            )

            eligible_ids = {cp.player_id for cp in eligible}
            assert same.player_id in eligible_ids
            assert other.player_id not in eligible_ids


class TestLegacyVacancyFallback:
    """Vacancy rows written BEFORE this fix have level_id NULL — they must not
    fail closed while the class itself still has a level."""

    def test_level_less_vacancy_row_matches_on_the_class_level(self, app):
        from padel_app.models.vacancy import Vacancy
        from padel_app.services.notification_service import (
            _get_eligible_students_for_group,
            get_or_create_config,
        )

        with app.app_context():
            coach = _create_coach("legacy-vac")
            lv5 = _create_level(coach, "5", display_order=1)
            lv6 = _create_level(coach, "6", display_order=2)
            db.session.commit()
            _, instance = _create_class(coach, "legacy-vac", lesson_level=lv5)

            same = _create_coach_player(coach, lv5, "legacy-same")
            other = _create_coach_player(coach, lv6, "legacy-other")
            db.session.commit()

            # The shape produced by the pre-PAD-86 engine.
            vacancy = Vacancy(
                lesson_instance_id=instance.id,
                coach_id=coach.id,
                status="open",
                approval_status="not_required",
            )
            vacancy.create()

            config = get_or_create_config(coach.id)
            config.invitation_groups = [
                {"id": "1", "rules": [
                    {"attribute": "level", "operation": "same_as_vacancy"},
                ]},
            ]
            config.save()

            eligible_ids = {
                cp.player_id
                for cp in _get_eligible_students_for_group(
                    vacancy, instance, coach.id, config, 1
                )
            }
            assert same.player_id in eligible_ids
            assert other.player_id not in eligible_ids


# ---------------------------------------------------------------------------
# Fail-closed semantics when there is no level at all
# ---------------------------------------------------------------------------

class TestLevelRulesFailClosed:

    def test_no_level_on_vacancy_means_nobody_passes(self, app):
        from padel_app.services.notification_service import _passes_group_rules

        with app.app_context():
            coach = _create_coach("closed-rules")
            lv5 = _create_level(coach, "5", display_order=1)
            db.session.commit()

            cp = _create_coach_player(coach, lv5, "closed-student")
            cp_no_level = _create_coach_player(coach, None, "closed-nolevel")
            db.session.commit()

            vacancy = _FakeVacancy(None)
            for op in ("same_as_vacancy", "one_above_vacancy", "one_below_vacancy",
                       "all_above_vacancy", "all_below_vacancy"):
                rules = [{"attribute": "level", "operation": op}]
                assert _passes_group_rules(rules, cp, vacancy, coach.id) is False, op
                assert _passes_group_rules(rules, cp_no_level, vacancy, coach.id) is False, op

    def test_non_level_rules_are_unaffected_by_a_level_less_vacancy(self, app):
        """Only LEVEL rules fail closed — a group without level rules still matches."""
        from padel_app.services.notification_service import _passes_group_rules

        with app.app_context():
            coach = _create_coach("closed-other")
            lv5 = _create_level(coach, "5", display_order=1)
            db.session.commit()
            cp = _create_coach_player(coach, lv5, "closed-other-student")
            db.session.commit()

            vacancy = _FakeVacancy(None)
            assert _passes_group_rules([], cp, vacancy, coach.id) is True
            assert _passes_group_rules(
                [{"attribute": "side", "operation": "same_as_vacancy"}],
                cp, vacancy, coach.id,
            ) is True


# ---------------------------------------------------------------------------
# {level} message placeholder
# ---------------------------------------------------------------------------

class TestLevelPlaceholder:

    def test_level_code_falls_back_to_the_lesson_default_level(self, app):
        from padel_app.services.notification_service import effective_level_code

        with app.app_context():
            coach = _create_coach("code-fallback")
            lesson_level = _create_level(coach, "5", display_order=1)
            db.session.commit()
            _, instance = _create_class(coach, "code-fallback",
                                        lesson_level=lesson_level)

            assert effective_level_code(instance) == "5"

    def test_level_code_is_empty_when_there_is_no_level(self, app):
        from padel_app.services.notification_service import effective_level_code

        with app.app_context():
            coach = _create_coach("code-none")
            db.session.commit()
            _, instance = _create_class(coach, "code-none")

            assert effective_level_code(instance) == ""
