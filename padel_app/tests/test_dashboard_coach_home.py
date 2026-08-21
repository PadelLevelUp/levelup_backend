"""Coach home blocks: hero, needs-you queue, next 7 days, this week.

The rules these lock down are the ones the redesign turns on — the hero is
absent rather than empty, the queue can reach zero, and every metric ships with
its denominator.
"""
from datetime import datetime, timedelta


def _seed(app, *, now):
    """A coach with two upcoming classes and four coached players.

    Class A starts in 45 minutes with 2 of 6 seats taken (so it is both the hero
    and an empty-seats queue item); class B is tomorrow and full.
    """
    from padel_app.sql_db import db
    from padel_app.models import (
        Association_CoachLesson,
        Association_CoachPlayer,
        Association_PlayerLessonInstance,
        LessonInstance,
        Presence,
        User,
    )
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.Association_CoachClub import Association_CoachClub

    with app.app_context():
        coach_user = User(name="Bernardo Teles", username="ch_coach", password="x")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name="CH Club", description="d", location="l")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        players = []
        for i in range(4):
            u = User(name=f"Rui Santos{i}", username=f"ch_p{i}", password="x")
            db.session.add(u)
            db.session.flush()
            p = Player(user_id=u.id)
            db.session.add(p)
            db.session.flush()
            db.session.add(Association_CoachPlayer(coach_id=coach.id, player_id=p.id))
            players.append(p)

        def make_class(title, start, capacity, signed_up):
            lesson = Lesson(
                title=title,
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                is_recurring=False,
                type="academy",
                max_players=capacity,
                status="active",
                club_id=club.id,
            )
            db.session.add(lesson)
            db.session.flush()
            db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
            inst = LessonInstance(
                lesson_id=lesson.id,
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                max_players=capacity,
                status="scheduled",
                notifications_enabled=True,
                original_lesson_occurence_date=start.date(),
            )
            db.session.add(inst)
            db.session.flush()
            for p in signed_up:
                db.session.add(
                    Association_PlayerLessonInstance(player_id=p.id, lesson_instance_id=inst.id)
                )
            return inst

        soon = make_class("B1 Class", now + timedelta(minutes=45), 6, players[:2])
        make_class("A2 Class", now + timedelta(days=1), 4, players)

        # A class that already ended, with one attendance still unvalidated.
        past = make_class("Past Class", now - timedelta(days=2), 4, players[:1])
        db.session.add(
            Presence(
                lesson_instance_id=past.id,
                player_id=players[0].id,
                status="present",
                validated=False,
            )
        )
        db.session.commit()

        return coach.id, coach_user.id, soon.id


def test_hero_is_the_soonest_class_with_its_roster(app):
    from padel_app.helpers.dashboard.coach_home import build_next_class_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, _, _ = _seed(app, now=now)

    with app.app_context():
        block = build_next_class_block(coach_id=coach_id, now=now)

    assert block is not None
    data = block["data"]
    assert data["title"] == "B1 Class"
    assert (data["filled"], data["capacity"]) == (2, 6)
    # Starts in 45 min, so the relative chip is on.
    assert data["minutesUntil"] == 45
    assert data["isToday"] is True
    assert [p["initials"] for p in data["players"]] == ["RS", "RS"]


def test_hero_chip_is_null_when_the_class_is_not_imminent(app):
    """`minutesUntil` doubles as the "should the chip show" switch."""
    from padel_app.helpers.dashboard.coach_home import build_next_class_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, _, _ = _seed(app, now=now)

    with app.app_context():
        # Three hours before the class — past the 2-hour threshold.
        block = build_next_class_block(coach_id=coach_id, now=now - timedelta(hours=3))

    assert block["data"]["minutesUntil"] is None


def test_hero_is_omitted_entirely_when_nothing_is_scheduled(app):
    """Absent, not empty — the redesign has no hero empty state."""
    from padel_app.helpers.dashboard.coach_home import build_next_class_block
    from padel_app.tests.helpers import make_coach

    coach_id = make_coach(app)

    with app.app_context():
        assert build_next_class_block(coach_id=coach_id, now=datetime(2026, 8, 4, 10, 0)) is None


def test_queue_orders_empty_seats_then_replies_then_validation(app):
    from padel_app.helpers.dashboard.coach_home import build_needs_you_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, user_id, _ = _seed(app, now=now)

    with app.app_context():
        block = build_needs_you_block(coach_id=coach_id, user_id=user_id, now=now)

    kinds = [item["kind"] for item in block["data"]["items"]]
    assert kinds == ["empty_seats", "validation"]
    assert block["data"]["count"] == len(block["data"]["items"])

    seats = block["data"]["items"][0]
    assert seats["classTitle"] == "B1 Class"
    assert seats["seatsMissing"] == 4
    assert (seats["filled"], seats["capacity"]) == (2, 6)

    validation = block["data"]["items"][1]
    assert (validation["count"], validation["classCount"]) == (1, 1)


def test_queue_reaches_zero(app):
    """A coach with nothing outstanding gets an empty list, not a placeholder."""
    from padel_app.helpers.dashboard.coach_home import build_needs_you_block
    from padel_app.tests.helpers import make_coach

    coach_id = make_coach(app)

    with app.app_context():
        from padel_app.models import User
        from padel_app.models.coaches import Coach

        user_id = User.query.join(Coach, Coach.user_id == User.id).filter(
            Coach.id == coach_id
        ).one().id
        block = build_needs_you_block(coach_id=coach_id, user_id=user_id, now=datetime(2026, 8, 4))

    assert block["data"] == {"count": 0, "items": []}


def test_schedule_reports_total_beyond_the_rows_it_ships(app):
    from padel_app.helpers.dashboard.coach_home import build_schedule_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, _, _ = _seed(app, now=now)

    with app.app_context():
        block = build_schedule_block(coach_id=coach_id, now=now)

    data = block["data"]
    # Two upcoming classes; the past one must not appear.
    assert data["totalCount"] == 2
    assert [i["title"] for i in data["items"]] == ["B1 Class", "A2 Class"]
    assert data["items"][0]["dayOfMonth"] == 4
    assert (data["items"][1]["filled"], data["items"][1]["capacity"]) == (4, 4)


def test_week_pulse_carries_denominators_and_counts_active_players(app):
    from padel_app.helpers.dashboard.coach_home import build_week_pulse_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, _, _ = _seed(app, now=now)

    with app.app_context():
        block = build_week_pulse_block(coach_id=coach_id, now=now)

    players = block["data"]["players"]
    # All four are signed up to the full class tomorrow, so all four are active.
    assert players == {"active": 4, "total": 4, "idle": 0}

    seats = block["data"]["seatsFilled"]
    assert seats["total"] > 0
    assert seats["pct"] == round(100 * seats["filled"] / seats["total"])
    assert len(seats["trend"]) == 7


def test_delta_is_reported_when_a_prior_week_exists(app):
    """The seed's past class sits in the previous week, so a delta is real."""
    from padel_app.helpers.dashboard.coach_home import build_week_pulse_block

    now = datetime(2026, 8, 4, 10, 0)
    coach_id, _, _ = _seed(app, now=now)

    with app.app_context():
        block = build_week_pulse_block(coach_id=coach_id, now=now)

    assert isinstance(block["data"]["seatsFilled"]["deltaPct"], int)


def test_delta_is_null_without_a_prior_week_to_compare(app):
    """Omit the delta rather than claim "+0%" against nothing."""
    from padel_app.helpers.dashboard.coach_home import build_week_pulse_block
    from padel_app.tests.helpers import make_coach

    coach_id = make_coach(app)

    with app.app_context():
        block = build_week_pulse_block(coach_id=coach_id, now=datetime(2026, 8, 4, 10, 0))

    seats = block["data"]["seatsFilled"]
    assert seats["deltaPct"] is None
    # And a coach with no classes reads 0%, not a divide-by-zero.
    assert (seats["pct"], seats["filled"], seats["total"]) == (0, 0, 0)
