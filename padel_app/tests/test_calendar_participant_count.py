"""
PAD-71 — the calendar's participant count must be EFFECTIVE filled spots.

The weekly calendar renders `participantCount / maxPlayers` on each class card.
It must show the same value as the class-detail "capacity" field: enrolled
players minus everyone who declined (``Presence.status == "absent"``). Players
who have not answered their invite yet still occupy their spot.

`LessonInstance.effective_filled_spots` is the single source of truth shared by
the calendar payload, the class-detail capacity field and the invitation
engine's capacity checks.

Covered spec: calendar.view (rules 8-10)
"""
from datetime import datetime, timedelta

import pytest

from padel_app.sql_db import db


def _make_instance(app, *, enrolled: int, declined: int, max_players: int):
    """Create a lesson instance with `enrolled` players, `declined` of them absent."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from padel_app.models.Association_CoachLessonInstance import (
        Association_CoachLessonInstance,
    )
    from padel_app.models.Association_PlayerLessonInstance import (
        Association_PlayerLessonInstance,
    )

    suffix = f"{enrolled}_{declined}_{max_players}"

    coach_user = User(name="Coach", username=f"pc_coach_{suffix}", password="x")
    db.session.add(coach_user)
    db.session.flush()
    coach = Coach(user_id=coach_user.id)
    db.session.add(coach)
    db.session.flush()

    club = Club(name=f"PC Club {suffix}", description="c", location="x")
    db.session.add(club)
    db.session.flush()

    start = datetime.utcnow().replace(microsecond=0) + timedelta(days=1)
    lesson = Lesson(
        title="Capacity Class",
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        is_recurring=False,
        type="academy",
        max_players=max_players,
        status="active",
        club_id=club.id,
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
    )
    db.session.add(instance)
    db.session.flush()

    db.session.add(
        Association_CoachLessonInstance(
            coach_id=coach.id, lesson_instance_id=instance.id
        )
    )

    for i in range(enrolled):
        player_user = User(name=f"P{i}", username=f"pc_p{i}_{suffix}", password="x")
        db.session.add(player_user)
        db.session.flush()
        player = Player(user_id=player_user.id)
        db.session.add(player)
        db.session.flush()

        db.session.add(
            Association_PlayerLessonInstance(
                player_id=player.id, lesson_instance_id=instance.id
            )
        )
        db.session.add(
            Presence(
                player_id=player.id,
                lesson_instance_id=instance.id,
                invited=True,
                confirmed=i < declined,
                status="absent" if i < declined else None,
            )
        )

    db.session.commit()
    return instance


@pytest.mark.parametrize(
    "enrolled,declined,max_players,expected",
    [
        (6, 3, 6, 3),   # ticket example: 6 enrolled, 3 declined -> 3/6
        (4, 0, 6, 4),   # nobody answered yet -> everyone still counts
        (2, 2, 4, 0),   # everyone declined -> empty class
        (0, 0, 4, 0),   # no enrolments
    ],
)
def test_effective_filled_spots(app, enrolled, declined, max_players, expected):
    with app.app_context():
        instance = _make_instance(
            app, enrolled=enrolled, declined=declined, max_players=max_players
        )
        assert instance.effective_filled_spots == expected


def test_calendar_event_participant_count_excludes_declines(app):
    """The serialized calendar event carries the effective count, not enrolment."""
    from padel_app.serializers.calendar_event import serialize_calendar_event

    with app.app_context():
        instance = _make_instance(app, enrolled=6, declined=3, max_players=6)

        event = serialize_calendar_event(instance)

        assert event["participantCount"] == 3
        assert event["maxPlayers"] == 6


def test_calendar_event_matches_invitation_engine_capacity(app):
    """Calendar payload and the invitation engine read the same source of truth."""
    from padel_app.serializers.calendar_event import serialize_calendar_event
    from padel_app.services.notification_service import _effective_filled_spots

    with app.app_context():
        instance = _make_instance(app, enrolled=5, declined=2, max_players=6)

        event = serialize_calendar_event(instance)

        assert event["participantCount"] == _effective_filled_spots(instance) == 3


def test_lesson_template_participant_count_is_enrolment(app):
    """A non-materialized Lesson has no presences, so enrolment is the count."""
    from padel_app.serializers.calendar_event import serialize_calendar_event

    with app.app_context():
        instance = _make_instance(app, enrolled=3, declined=1, max_players=6)
        lesson = instance.lesson

        event = serialize_calendar_event(lesson)

        # Nobody is enrolled on the Lesson template itself.
        assert event["participantCount"] == len(lesson.players_relations)
