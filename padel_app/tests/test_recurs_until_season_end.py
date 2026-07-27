"""
PAD-90 — "Recurs until season end" must fail closed.

Ticking "recurs until season end" on a recurring class snapshots the coach's
season end into `lessons.recurrence_end`. When no season covers the class's
start date the resolver returns `None`; before this fix the payload's
`recurrence_end` was left NULL, and `calendar_helpers` treats a NULL
`recurrence_end` as "no end" — so the class recurred forever, generating
instances and reminder jobs indefinitely, with no signal to the coach.

The contract is now fail-closed: the class is rejected with a 400 and nothing
is written. A class flagged `recurs_until_season_end` can never carry a NULL
`recurrence_end`.

Covered spec: calendar.seasons
"""
from datetime import date

import pytest
from flask_jwt_extended import create_access_token

from padel_app.tests.helpers import make_coach


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _make_club(coach_id):
    from padel_app.sql_db import db
    from padel_app.models import Club, Association_CoachClub

    club = Club(name="Season Club", description="c", location="x")
    db.session.add(club)
    db.session.flush()
    db.session.add(Association_CoachClub(coach_id=coach_id, club_id=club.id))
    db.session.commit()
    return club


def _seed_season(coach_id, name, start, end):
    from padel_app.sql_db import db
    from padel_app.models import Season

    season = Season(coach_id=coach_id, name=name, start_date=start, end_date=end)
    db.session.add(season)
    db.session.commit()
    return season


def _class_payload(**overrides):
    data = {
        "name": "Recurring Class",
        "classType": "academy",
        "maxPlayers": 6,
        "date": "2026-06-15",
        "startTime": "10:00",
        "endTime": "11:00",
        "isRecurring": True,
        "recurrenceRule": {"frequency": "weekly", "daysOfWeek": [1]},
        "endDate": None,
        "recursUntilSeasonEnd": True,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

def test_no_season_at_all_is_rejected_and_creates_nothing(app):
    """A coach with zero seasons (the default state) must not get a class."""
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach, Lesson
        from padel_app.services.lesson_service import (
            NoSeasonCoversDateError,
            add_class_service,
        )

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)

        with pytest.raises(NoSeasonCoversDateError):
            add_class_service(_class_payload(), coach, club)

        assert Lesson.query.count() == 0, "no lesson may be created"


def test_season_not_covering_the_start_date_is_rejected(app):
    """A season exists, but the class starts outside it — still fail closed."""
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach, Lesson
        from padel_app.services.lesson_service import (
            NoSeasonCoversDateError,
            add_class_service,
        )

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)
        # Class starts 2026-06-15; this season ends before that.
        _seed_season(coach_id, "Spring", date(2026, 1, 1), date(2026, 5, 31))

        with pytest.raises(NoSeasonCoversDateError):
            add_class_service(_class_payload(), coach, club)

        assert Lesson.query.count() == 0


def test_covering_season_bounds_the_recurrence(app):
    """The happy path stays intact: recurrence_end is the season end."""
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services.lesson_service import add_class_service

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)
        _seed_season(coach_id, "Season A", date(2026, 6, 1), date(2026, 9, 30))

        lesson = add_class_service(_class_payload(), coach, club)

        assert lesson.recurs_until_season_end is True
        assert lesson.recurrence_end == date(2026, 9, 30)


def test_recurs_until_season_end_never_leaves_recurrence_end_null(app):
    """The invariant this ticket exists to protect."""
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach, Lesson
        from padel_app.services.lesson_service import (
            NoSeasonCoversDateError,
            add_class_service,
        )

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)

        with pytest.raises(NoSeasonCoversDateError):
            add_class_service(_class_payload(), coach, club)

        unbounded = Lesson.query.filter(
            Lesson.recurs_until_season_end.is_(True),
            Lesson.recurrence_end.is_(None),
        ).count()
        assert unbounded == 0


def test_toggle_off_still_honours_an_explicit_end_date(app):
    """Untouched path: a plain recurring class keeps its typed end date."""
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services.lesson_service import add_class_service

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)

        lesson = add_class_service(
            _class_payload(recursUntilSeasonEnd=False, endDate="2026-12-31"),
            coach,
            club,
        )

        assert lesson.recurs_until_season_end is False
        assert lesson.recurrence_end is not None


# ---------------------------------------------------------------------------
# Route layer — the coach-facing contract
# ---------------------------------------------------------------------------

def test_add_class_route_returns_400_with_a_machine_readable_code(app, client):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach, Lesson

        coach = Coach.query.get(coach_id)
        user_id = coach.user_id
        _make_club(coach_id)

    headers = _auth_header(app, user_id)
    resp = client.post("/api/app/add_class", json=_class_payload(), headers=headers)

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "no_season_covers_date"
    assert body["error"]

    with app.app_context():
        from padel_app.models import Lesson

        assert Lesson.query.count() == 0


def test_add_class_route_succeeds_when_a_season_covers_the_date(app, client):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach

        coach = Coach.query.get(coach_id)
        user_id = coach.user_id
        _make_club(coach_id)
        _seed_season(coach_id, "Season A", date(2026, 6, 1), date(2026, 9, 30))

    headers = _auth_header(app, user_id)
    resp = client.post("/api/app/add_class", json=_class_payload(), headers=headers)

    assert resp.status_code == 200

    with app.app_context():
        from padel_app.models import Lesson

        lesson = Lesson.query.one()
        assert lesson.recurrence_end == date(2026, 9, 30)
