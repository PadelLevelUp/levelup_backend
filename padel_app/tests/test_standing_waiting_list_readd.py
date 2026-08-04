"""
PAD-109: re-adding a player to the standing (permanent) waiting list must not 500.

`uq_waiting_session_player` is UNIQUE(lesson_instance_id, player_id) and does not
include is_active. Removing a standing entry only flips its per-class
WaitingListEntry rows to is_active=False, so `_fan_out_standing_entry()` used to
fall through its is_active=True existence check and re-INSERT the same pair —
raising a UniqueViolation and returning 500 to the coach.

This was invisible until PAD-109 made the player-search box work, because the
coach had no way to pick a player to add in the first place.

Run:
    pytest padel_app/tests/test_standing_waiting_list_readd.py -v
"""
from datetime import timedelta

from padel_app.sql_db import db
from padel_app.utils.dates import utcnow_naive


def _seed(app):
    """One coach, one player, one upcoming class instance owned by that coach."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    from padel_app.models.Association_CoachLessonInstance import Association_CoachLessonInstance
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.clubs import Club

    with app.app_context():
        coach_user = User(name="Coach", username="coach_readd",
                          email="coach_readd@test.com", password="hashed", status="active")
        player_user = User(name="Student", username="student_readd",
                           email="student_readd@test.com", password="hashed", status="active")
        db.session.add_all([coach_user, player_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        player = Player(user_id=player_user.id)
        db.session.add_all([coach, player])
        db.session.flush()
        db.session.add(Association_CoachPlayer(coach_id=coach.id, player_id=player.id))

        club = Club(name="Club", description="", location="City")
        db.session.add(club)
        db.session.flush()

        start = utcnow_naive() + timedelta(days=3)
        lesson = Lesson(title="Class", start_datetime=start,
                        end_datetime=start + timedelta(hours=1), is_recurring=False,
                        type="academy", max_players=4, color="#000",
                        status="active", club_id=club.id)
        db.session.add(lesson)
        db.session.flush()

        instance = LessonInstance(
            lesson_id=lesson.id, start_datetime=start,
            end_datetime=start + timedelta(hours=1), max_players=4,
            status="scheduled", notifications_enabled=True,
        )
        db.session.add(instance)
        db.session.flush()
        db.session.add(Association_CoachLessonInstance(
            coach_id=coach.id, lesson_instance_id=instance.id))
        db.session.commit()

        return {"coach_id": coach.id, "player_id": player.id, "instance_id": instance.id}


def test_remove_then_readd_does_not_raise(app):
    from padel_app.services.notification_service import (
        add_standing_waiting_list_entry,
        remove_standing_waiting_list_entry,
        get_standing_waiting_list,
    )
    from padel_app.models.waiting_list_entry import WaitingListEntry

    ids = _seed(app)

    with app.app_context():
        first = add_standing_waiting_list_entry(ids["coach_id"], ids["player_id"], 3, 30)
        remove_standing_waiting_list_entry(first.id, ids["coach_id"])

        # This is the call that used to raise IntegrityError / UniqueViolation.
        second = add_standing_waiting_list_entry(ids["coach_id"], ids["player_id"], 5, 60)

        # Exactly one row for the (instance, player) pair — reactivated, not duplicated
        rows = WaitingListEntry.query.filter_by(
            lesson_instance_id=ids["instance_id"], player_id=ids["player_id"]
        ).all()
        assert len(rows) == 1
        assert rows[0].is_active is True
        assert rows[0].standing_entry_id == second.id

        # And the coach sees exactly one active standing entry, with the new credits
        entries = get_standing_waiting_list(ids["coach_id"])
        assert len(entries) == 1
        assert entries[0]["creditsTotal"] == 5


def test_adding_twice_without_removing_does_not_duplicate(app):
    from padel_app.services.notification_service import add_standing_waiting_list_entry
    from padel_app.models.waiting_list_entry import WaitingListEntry

    ids = _seed(app)

    with app.app_context():
        add_standing_waiting_list_entry(ids["coach_id"], ids["player_id"], 3, 30)
        second = add_standing_waiting_list_entry(ids["coach_id"], ids["player_id"], 4, 30)

        rows = WaitingListEntry.query.filter_by(
            lesson_instance_id=ids["instance_id"], player_id=ids["player_id"]
        ).all()
        assert len(rows) == 1
        assert rows[0].is_active is True
        assert rows[0].standing_entry_id == second.id


def test_self_joined_waiting_list_row_is_not_hijacked(app):
    """A row the player created themselves keeps its origin (standing_entry_id=None)."""
    from padel_app.services.notification_service import add_standing_waiting_list_entry
    from padel_app.models.waiting_list_entry import WaitingListEntry

    ids = _seed(app)

    with app.app_context():
        own = WaitingListEntry(
            lesson_instance_id=ids["instance_id"],
            player_id=ids["player_id"],
            coach_id=ids["coach_id"],
            standing_entry_id=None,
        )
        own.create()

        add_standing_waiting_list_entry(ids["coach_id"], ids["player_id"], 3, 30)

        rows = WaitingListEntry.query.filter_by(
            lesson_instance_id=ids["instance_id"], player_id=ids["player_id"]
        ).all()
        assert len(rows) == 1
        assert rows[0].is_active is True
        assert rows[0].standing_entry_id is None
