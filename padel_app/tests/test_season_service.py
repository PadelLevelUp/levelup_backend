from datetime import datetime, timedelta, date

from padel_app.tests.helpers import make_coach


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


def test_overlap_rejected(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)
        _seed_season(coach_id, "Spring", date(2026, 3, 1), date(2026, 5, 31))

        import pytest

        with pytest.raises(ValueError, match="Overlapping seasons"):
            season_service.validate_no_overlap(
                coach, date(2026, 5, 15), date(2026, 8, 1)
            )


def test_non_overlap_accepted(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)
        _seed_season(coach_id, "Spring", date(2026, 3, 1), date(2026, 5, 31))

        # Adjacent (day after) — inclusive check should NOT flag this.
        season_service.validate_no_overlap(coach, date(2026, 6, 1), date(2026, 8, 31))


def test_start_after_end_rejected(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)

        import pytest

        with pytest.raises(ValueError, match="Season start must be before end"):
            season_service.validate_no_overlap(coach, date(2026, 6, 1), date(2026, 5, 1))


def test_resolve_season_end_for_coach(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)
        _seed_season(coach_id, "Spring", date(2026, 3, 1), date(2026, 5, 31))
        _seed_season(coach_id, "Summer", date(2026, 6, 1), date(2026, 8, 31))

        assert season_service.resolve_season_end_for_coach(
            coach, date(2026, 4, 15)
        ) == date(2026, 5, 31)
        assert season_service.resolve_season_end_for_coach(
            coach, date(2026, 7, 1)
        ) == date(2026, 8, 31)
        assert season_service.resolve_season_end_for_coach(
            coach, date(2026, 1, 1)
        ) is None


def test_upsert_seasons_batch_overlap_rejected(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)

        import pytest

        with pytest.raises(ValueError, match="Overlapping seasons"):
            season_service.upsert_seasons(
                coach,
                [
                    {"name": "A", "startDate": "2026-03-01", "endDate": "2026-05-31"},
                    {"name": "B", "startDate": "2026-05-15", "endDate": "2026-08-01"},
                ],
            )


def test_upsert_seasons_creates_and_prunes(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)
        season_service.upsert_seasons(
            coach,
            [
                {"name": "Spring", "startDate": "2026-03-01", "endDate": "2026-05-31"},
                {"name": "Summer", "startDate": "2026-06-01", "endDate": "2026-08-31"},
            ],
        )
        assert len(coach.seasons) == 2

        # Re-send only one — the other should be pruned.
        existing = season_service.list_seasons(coach)[0]
        result = season_service.upsert_seasons(
            coach,
            [
                {
                    "id": existing.id,
                    "name": "Spring Updated",
                    "startDate": "2026-03-01",
                    "endDate": "2026-05-31",
                },
            ],
        )
        assert len(result) == 1
        assert result[0].name == "Spring Updated"


def test_add_class_service_sets_recurrence_end_to_season_end(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.models import Coach
        from padel_app.services import season_service
        from padel_app.services.lesson_service import add_class_service

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)
        _seed_season(coach_id, "Season A", date(2026, 6, 1), date(2026, 9, 30))

        data = {
            "name": "Recurring Class",
            "classType": "academy",
            "maxPlayers": 6,
            "date": "2026-06-15",
            "startTime": "10:00",
            "endTime": "11:00",
            "isRecurring": True,
            "recurrenceRule": {"daysOfWeek": [1]},
            "endDate": "2026-12-31",
            "recursUntilSeasonEnd": True,
        }
        lesson = add_class_service(data, coach, club)

        assert lesson.recurs_until_season_end is True
        assert lesson.recurrence_end == date(2026, 9, 30)


def test_regenerate_prunes_only_instances_after_season_end(app):
    coach_id = make_coach(app)
    with app.app_context():
        from padel_app.sql_db import db
        from padel_app.models import (
            Coach,
            Lesson,
            LessonInstance,
            Association_CoachLesson,
        )
        from padel_app.services import season_service

        coach = Coach.query.get(coach_id)
        club = _make_club(coach_id)
        season = _seed_season(coach_id, "Season A", date(2026, 6, 1), date(2026, 9, 30))

        start = datetime(2026, 6, 15, 10, 0, 0)
        lesson = Lesson(
            title="Recurring Class",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=True,
            recurrence_rule='{"daysOfWeek": [1]}',
            recurrence_end=date(2026, 12, 31),
            recurs_until_season_end=True,
            type="academy",
            max_players=6,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()
        db.session.add(
            Association_CoachLesson(coach_id=coach_id, lesson_id=lesson.id)
        )

        # A past/held instance (before season end) and a far-future instance
        # (after season end).
        past_start = datetime(2026, 6, 22, 10, 0, 0)
        future_start = datetime(2026, 11, 2, 10, 0, 0)
        past = LessonInstance(
            lesson_id=lesson.id,
            start_datetime=past_start,
            end_datetime=past_start + timedelta(hours=1),
            max_players=6,
            status="scheduled",
            original_lesson_occurence_date=past_start.date(),
        )
        future = LessonInstance(
            lesson_id=lesson.id,
            start_datetime=future_start,
            end_datetime=future_start + timedelta(hours=1),
            max_players=6,
            status="scheduled",
            original_lesson_occurence_date=future_start.date(),
        )
        db.session.add_all([past, future])
        db.session.commit()

        past_id, future_id = past.id, future.id

        season_service.regenerate_future_instances_for_season(
            season, now=datetime(2026, 6, 20, 12, 0, 0)
        )

        # Lesson recurrence_end capped to season end.
        assert lesson.recurrence_end == date(2026, 9, 30)
        # Past instance kept, future-beyond-end pruned.
        assert LessonInstance.query.get(past_id) is not None
        assert LessonInstance.query.get(future_id) is None
