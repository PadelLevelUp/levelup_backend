"""
PAD-140 — the coach-facing Presences overview: roster stats, trend, validation queue.

The three reads are all scoped to one coach through `Association_CoachLesson`,
and all derive "attended" from `Presence.status == "present"` so they can never
disagree with `compute_player_kpis()` or the per-player history page.

The subtle one is guest attendance. `Presence.invited` is True for *every*
enrolled player (materialization sets it), so it cannot identify a guest. A
guest is a player with a presence but no `Association_PlayerLesson` row for the
parent lesson — someone invited into a one-off spot. `test_guest_is_not_counted
_as_enrolled` pins that distinction.

Covered spec: attendance.presence (validated field), attendance.stats
"""
from datetime import datetime, timedelta

import pytest

from padel_app.sql_db import db


@pytest.fixture
def coach_world(app):
    """A coach with two enrolled players, one guest, and two past classes.

    Layout, all in the past so they are validatable:

      * academy class (2 days ago) — Ana present, Bruno absent/justified,
        Carla present as a GUEST (no enrolment in the parent lesson).
      * private class (1 day ago)  — Ana present, Bruno unanswered
        (so this class is "needs your input", the other is "ready").

    Returns a dict of ids.
    """
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    from padel_app.models.Association_PlayerLesson import Association_PlayerLesson
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence

    with app.app_context():
        coach_user = User(name="Po Coach", username="po_coach", password="x")
        ana_user = User(name="Ana", username="po_ana", password="x")
        bruno_user = User(name="Bruno", username="po_bruno", password="x")
        carla_user = User(name="Carla", username="po_carla", password="x")
        db.session.add_all([coach_user, ana_user, bruno_user, carla_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        ana = Player(user_id=ana_user.id)
        bruno = Player(user_id=bruno_user.id)
        carla = Player(user_id=carla_user.id)
        db.session.add_all([coach, ana, bruno, carla])
        db.session.flush()

        club = Club(name="PO Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        # All three are on the coach's roster — the guest too. Being on the
        # roster is not the same as being enrolled in a given class.
        for player in (ana, bruno, carla):
            db.session.add(
                Association_CoachPlayer(coach_id=coach.id, player_id=player.id)
            )

        now = datetime.utcnow().replace(microsecond=0)

        def make_class(title, kind, days_ago, enrolled):
            start = (now - timedelta(days=days_ago)).replace(hour=10, minute=0, second=0)
            lesson = Lesson(
                title=title,
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                is_recurring=False,
                type=kind,
                max_players=4,
                status="active",
                club_id=club.id,
            )
            db.session.add(lesson)
            db.session.flush()
            db.session.add(
                Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id)
            )
            for player in enrolled:
                db.session.add(
                    Association_PlayerLesson(player_id=player.id, lesson_id=lesson.id)
                )
            instance = LessonInstance(
                lesson_id=lesson.id,
                original_lesson_occurence_date=start.date(),
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                max_players=4,
                status="scheduled",
            )
            db.session.add(instance)
            db.session.flush()
            return lesson, instance

        academy_lesson, academy = make_class("Academy Beginners", "academy", 2, [ana, bruno])
        private_lesson, private = make_class("Private — Ana", "private", 1, [ana, bruno])

        db.session.add_all([
            # Academy: everyone answered -> "ready".
            Presence(lesson_instance_id=academy.id, player_id=ana.id,
                     invited=True, confirmed=True, status="present", validated=False),
            Presence(lesson_instance_id=academy.id, player_id=bruno.id,
                     invited=True, confirmed=False, status="absent",
                     justification="justified", validated=False),
            # Carla filled a vacancy: presence + instance spot, but NO
            # Association_PlayerLesson on the parent lesson. That is a guest.
            Presence(lesson_instance_id=academy.id, player_id=carla.id,
                     invited=True, confirmed=True, status="present", validated=False),
            # Private: Bruno never answered -> "needs your input".
            Presence(lesson_instance_id=private.id, player_id=ana.id,
                     invited=True, confirmed=True, status="present", validated=False),
            Presence(lesson_instance_id=private.id, player_id=bruno.id,
                     invited=True, confirmed=False, status=None, validated=False),
        ])
        db.session.commit()

        return {
            "coach_id": coach.id,
            "ana_id": ana.id,
            "bruno_id": bruno.id,
            "carla_id": carla.id,
            "academy_instance_id": academy.id,
            "private_instance_id": private.id,
        }


def _window():
    now = datetime.utcnow()
    return now - timedelta(days=30), now + timedelta(days=1)


def test_presence_stats_counts_per_player(app, coach_world):
    """Each metric column is derived from the presence rows, not invented."""
    from padel_app.services.presence_overview_service import build_presence_stats

    start, end = _window()
    with app.app_context():
        payload = build_presence_stats(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    by_name = {p["name"]: p for p in payload["players"]}

    ana = by_name["Ana"]
    assert ana["total"] == 2, "attended both classes"
    assert ana["academy"] == 1
    assert ana["private"] == 1
    assert ana["justified"] == 0 and ana["unjustified"] == 0

    bruno = by_name["Bruno"]
    assert bruno["total"] == 0, "absent from one, never answered the other"
    assert bruno["justified"] == 1
    assert bruno["unjustified"] == 0

    # Roster-wide totals feed the KPI tiles and the private/academy donut.
    assert payload["totals"]["presences"] == 3
    assert payload["totals"]["academy"] == 2
    assert payload["totals"]["private"] == 1
    assert payload["totals"]["activePlayers"] == 3


def test_guest_is_not_counted_as_enrolled(app, coach_world):
    """A guest is identified by having no enrolment in the parent lesson.

    `invited` is True for every player here, so a naive `invited`-based rule
    would report all three as guests.
    """
    from padel_app.services.presence_overview_service import build_presence_stats

    start, end = _window()
    with app.app_context():
        payload = build_presence_stats(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    by_name = {p["name"]: p for p in payload["players"]}
    assert by_name["Carla"]["invitesReceived"] == 1
    assert by_name["Carla"]["invitesJoined"] == 1
    assert by_name["Ana"]["invitesReceived"] == 0, "enrolled, so never a guest"
    assert by_name["Bruno"]["invitesReceived"] == 0
    assert payload["totals"]["guestAttendances"] == 1


def test_roster_player_with_no_activity_still_appears(app, coach_world):
    """Zero rows matter most — they are how a coach spots someone drifting away."""
    from padel_app.models.players import Player
    from padel_app.models import User
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    from padel_app.services.presence_overview_service import build_presence_stats

    with app.app_context():
        user = User(name="Zed", username="po_zed", password="x")
        db.session.add(user)
        db.session.flush()
        player = Player(user_id=user.id)
        db.session.add(player)
        db.session.flush()
        db.session.add(
            Association_CoachPlayer(
                coach_id=coach_world["coach_id"], player_id=player.id
            )
        )
        db.session.commit()

    start, end = _window()
    with app.app_context():
        payload = build_presence_stats(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    zed = next(p for p in payload["players"] if p["name"] == "Zed")
    assert zed["total"] == 0


def test_trend_buckets_are_gap_filled(app, coach_world):
    """Empty days still appear, so the chart's x-axis stays continuous."""
    from padel_app.services.presence_overview_service import build_presence_trend

    start, end = _window()
    with app.app_context():
        payload = build_presence_trend(
            coach_id=coach_world["coach_id"],
            range_start=start,
            range_end=end,
            granularity="day",
        )

    assert payload["granularity"] == "day"
    assert payload["total"] == 3, "three present rows across both classes"
    counts = [b["count"] for b in payload["buckets"]]
    assert sum(counts) == 3
    assert any(c == 0 for c in counts), "range is wider than the activity"


def test_pending_validation_splits_ready_from_needs_input(app, coach_world):
    """`ready` means everyone answered — not that the coach decided anything."""
    from padel_app.services.presence_overview_service import list_pending_validation

    start, end = _window()
    with app.app_context():
        payload = list_pending_validation(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    assert payload["pendingCount"] == 2
    by_title = {c["title"]: c for c in payload["pending"]}

    academy = by_title["Academy Beginners"]
    assert academy["ready"] is True
    assert academy["unanswered"] == 0

    private = by_title["Private — Ana"]
    assert private["ready"] is False, "Bruno never answered"
    assert private["unanswered"] == 1
    # Unanswered players sort first so the coach sees what needs attention.
    assert private["players"][0]["response"] == "none"


def test_future_class_is_not_listed(app, coach_world):
    """Validating a class that has not happened yet is meaningless."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.presence_overview_service import list_pending_validation

    with app.app_context():
        instance = LessonInstance.query.get(coach_world["private_instance_id"])
        future = datetime.utcnow() + timedelta(days=3)
        instance.start_datetime = future
        instance.end_datetime = future + timedelta(hours=1)
        db.session.commit()

    start, end = datetime.utcnow() - timedelta(days=30), datetime.utcnow() + timedelta(days=10)
    with app.app_context():
        payload = list_pending_validation(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    titles = [c["title"] for c in payload["pending"]]
    assert "Private — Ana" not in titles
    assert "Academy Beginners" in titles


def test_fully_validated_class_moves_to_validated_bucket(app, coach_world):
    """A class is validated when every one of its presences is."""
    from padel_app.models.presences import Presence
    from padel_app.services.presence_overview_service import list_pending_validation

    with app.app_context():
        for presence in Presence.query.filter_by(
            lesson_instance_id=coach_world["academy_instance_id"]
        ):
            presence.validated = True
        db.session.commit()

    start, end = _window()
    with app.app_context():
        payload = list_pending_validation(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    assert [c["title"] for c in payload["validated"]] == ["Academy Beginners"]
    assert "Academy Beginners" not in [c["title"] for c in payload["pending"]]


def test_unvalidate_reopens_without_erasing_what_was_recorded(app, coach_world):
    """Undo clears `validated` only — status/justification survive."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from padel_app.services.presence_overview_service import unvalidate_instance

    instance_id = coach_world["academy_instance_id"]
    with app.app_context():
        for presence in Presence.query.filter_by(lesson_instance_id=instance_id):
            presence.validated = True
        db.session.commit()

        unvalidate_instance(LessonInstance.query.get(instance_id))

        rows = Presence.query.filter_by(lesson_instance_id=instance_id).all()
        assert all(not p.validated for p in rows)
        statuses = sorted((p.status or "none") for p in rows)
        assert statuses == ["absent", "present", "present"], "records preserved"


def test_another_coachs_classes_are_invisible(app, coach_world):
    """Scoping runs through Association_CoachLesson, not the roster."""
    from padel_app.tests.helpers import make_coach
    from padel_app.services.presence_overview_service import (
        build_presence_stats,
        list_pending_validation,
    )

    other_coach_id = make_coach(app)
    start, end = _window()

    with app.app_context():
        stats = build_presence_stats(
            coach_id=other_coach_id, range_start=start, range_end=end
        )
        pending = list_pending_validation(
            coach_id=other_coach_id, range_start=start, range_end=end
        )

    assert stats["players"] == []
    assert pending["pendingCount"] == 0


def test_coach_marked_absence_is_not_reported_as_a_student_decline(app, coach_world):
    """A validated absence is the coach's record, not something the student said.

    Both paths write status='absent', confirmed=False; only `add_presences`
    also sets validated=True. Without that guard the UI would tell the coach
    "said they couldn't make it" about a decision the coach made themselves.
    """
    from padel_app.models.presences import Presence
    from padel_app.services.presence_overview_service import list_pending_validation

    instance_id = coach_world["private_instance_id"]
    with app.app_context():
        # Coach marks the silent player absent and validates the class.
        presence = Presence.query.filter_by(
            lesson_instance_id=instance_id, player_id=coach_world["bruno_id"]
        ).first()
        presence.status = "absent"
        presence.justification = "unjustified"
        presence.validated = True
        db.session.commit()

    start, end = _window()
    with app.app_context():
        payload = list_pending_validation(
            coach_id=coach_world["coach_id"], range_start=start, range_end=end
        )

    klass = next(
        c for c in payload["pending"] + payload["validated"]
        if c["lessonInstanceId"] == instance_id
    )
    bruno = next(p for p in klass["players"] if p["playerId"] == coach_world["bruno_id"])
    assert bruno["response"] == "none", "must not attribute this to the student"
    assert bruno["status"] == "absent", "the coach's record itself is untouched"


def test_walk_in_occupies_a_spot_in_effective_filled_spots(app, coach_world):
    """A walk-in added from the Presences tab must count toward capacity.

    `effective_filled_spots` counts `players_relations` (the instance
    association), NOT presences — so creating only a Presence row would leave
    the walk-in invisible to the calendar badge, the class-detail capacity
    field and the invitation engine, all of which read that one property
    (`calendar.view` rule 9).
    """
    from padel_app.models import User
    from padel_app.models.players import Player
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from padel_app.services.lesson_service import add_presences

    instance_id = coach_world["private_instance_id"]

    with app.app_context():
        before = LessonInstance.query.get(instance_id).effective_filled_spots

        user = User(name="Walk In", username="po_walkin", password="x")
        db.session.add(user)
        db.session.flush()
        walk_in = Player(user_id=user.id)
        db.session.add(walk_in)
        db.session.commit()
        walk_in_id = walk_in.id

        instance = LessonInstance.query.get(instance_id)
        add_presences(instance, [{"playerId": walk_in_id, "status": "present"}])

    with app.app_context():
        instance = LessonInstance.query.get(instance_id)
        assert instance.effective_filled_spots == before + 1, (
            "walk-in must occupy a spot"
        )
        presence = Presence.query.filter_by(
            lesson_instance_id=instance_id, player_id=walk_in_id
        ).first()
        assert presence is not None and presence.status == "present"
        assert presence.validated is True


def test_marking_an_existing_player_does_not_double_count(app, coach_world):
    """Re-marking someone already on the roster must not add a second spot."""
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.lesson_service import add_presences

    instance_id = coach_world["private_instance_id"]
    with app.app_context():
        before = LessonInstance.query.get(instance_id).effective_filled_spots
        instance = LessonInstance.query.get(instance_id)
        add_presences(
            instance, [{"playerId": coach_world["ana_id"], "status": "present"}]
        )

    with app.app_context():
        assert (
            LessonInstance.query.get(instance_id).effective_filled_spots == before
        )
