"""
PAD-65 — Deleting a single (edited) occurrence of a recurring class must keep
it gone.

Editing one occurrence of a recurring class materializes a `LessonInstance`
override. Deleting that instance with scope="single" used to do a bare
`obj.delete()` without excluding the date from the parent recurrence, so the
series re-projected the occurrence (with its pre-edit values) on reload — the
coach "deleted it and it came back". The fix excludes the original occurrence
date from the parent recurrence, mirroring the Lesson scope="single" path.

Covered spec: classes.instances (recurring occurrence removal)
"""
from datetime import datetime, timedelta

import pytest

from padel_app.sql_db import db


@pytest.fixture
def recurring_with_coach(app):
    """Coach + a weekly recurring lesson spanning several weeks. Returns
    (coach_id, user_id, lesson_id, first_start_datetime)."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.lessons import Lesson
    import json

    with app.app_context():
        coach_user = User(name="Coach", username="rd_coach", password="x")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name="RD Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        start = (datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
                 + timedelta(days=1))
        lesson = Lesson(
            title="Recurring Class",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=True,
            recurrence_rule=json.dumps(
                {"frequency": "weekly", "daysOfWeek": [(start.weekday() + 1) % 7]}
            ),
            recurrence_end=(start + timedelta(weeks=6)).date(),
            type="academy",
            max_players=4,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()
        db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
        db.session.commit()
        return coach.id, coach_user.id, lesson.id, start


def _events_on(app, coach_id, date, title):
    """Return calendar events for `title` on `date` for the coach."""
    from padel_app.models.coaches import Coach
    from padel_app.services.lesson_service import get_lesson_instances_in_range

    with app.app_context():
        coach = Coach.query.get(coach_id)
        range_start = datetime.combine(date - timedelta(days=1), datetime.min.time())
        range_end = datetime.combine(date + timedelta(days=1), datetime.max.time())
        events = get_lesson_instances_in_range(coach, range_start, range_end)
        return [
            e for e in events
            if e.get("title") == title and e.get("date") == date.isoformat()
        ]


def test_delete_single_edited_occurrence_stays_gone(app, recurring_with_coach):
    coach_id, user_id, lesson_id, first_start = recurring_with_coach
    from padel_app.services.lesson_service import (
        get_or_materialize_instance,
        remove_class_service,
    )
    from padel_app.models.lessons import Lesson

    second_date = (first_start + timedelta(weeks=1)).date()
    third_date = (first_start + timedelta(weeks=2)).date()

    # Sanity: before deletion the second occurrence is projected.
    assert _events_on(app, coach_id, second_date, "Recurring Class"), \
        "second occurrence should exist before deletion"

    # Edit → materialize an override for the second occurrence (title change).
    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, second_date)
        instance.overwrite_title = "Edited Occurrence"
        instance.save()
        db.session.commit()
        instance_id = instance.id

    # Delete just that occurrence.
    with app.app_context():
        result, status = remove_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_id,
                "date": second_date.isoformat(),
            },
            "scope": "single",
        })
        assert status == 200, result

    # It must NOT reappear (the resurrection bug).
    assert _events_on(app, coach_id, second_date, "Recurring Class") == [], \
        "deleted occurrence resurrected on the parent recurrence"
    assert _events_on(app, coach_id, second_date, "Edited Occurrence") == [], \
        "deleted (edited) occurrence resurrected"

    # Other occurrences are untouched.
    assert _events_on(app, coach_id, first_start.date(), "Recurring Class"), \
        "first occurrence should remain"
    assert _events_on(app, coach_id, third_date, "Recurring Class"), \
        "third occurrence should remain"


def test_delete_single_non_materialized_occurrence_via_lesson_path(app, recurring_with_coach):
    """Deleting a NON-edited recurring occurrence (model="Lesson", scope="single")
    must also exclude the date — the latent split_lesson off-by-one that
    class-deletion PAD-10 never asserted."""
    coach_id, user_id, lesson_id, first_start = recurring_with_coach
    from padel_app.services.lesson_service import remove_class_service

    second_date = (first_start + timedelta(weeks=1)).date()
    assert _events_on(app, coach_id, second_date, "Recurring Class"), "precondition"

    with app.app_context():
        result, status = remove_class_service({
            "event": {
                "model": "Lesson",
                "originalId": lesson_id,
                "date": second_date.isoformat(),
            },
            "scope": "single",
        })
        assert status == 200, result

    assert _events_on(app, coach_id, second_date, "Recurring Class") == [], \
        "single occurrence resurrected via the Lesson path"
    # First and third occurrences remain.
    assert _events_on(app, coach_id, first_start.date(), "Recurring Class")
    assert _events_on(app, coach_id, (first_start + timedelta(weeks=2)).date(), "Recurring Class")


def test_delete_single_instance_of_non_recurring_lesson_still_deletes(app):
    """A materialized instance of a NON-recurring lesson is just removed (no
    recurrence to exclude, no lesson split)."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.services.lesson_service import (
        get_or_materialize_instance,
        remove_class_service,
    )

    with app.app_context():
        u = User(name="C2", username="rd_coach2", password="x")
        db.session.add(u)
        db.session.flush()
        coach = Coach(user_id=u.id)
        db.session.add(coach)
        db.session.flush()
        club = Club(name="RD2", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        start = datetime.utcnow().replace(microsecond=0) + timedelta(days=1)
        lesson = Lesson(
            title="Single Class",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=False,
            type="academy",
            max_players=4,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()
        db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
        db.session.commit()

        instance = get_or_materialize_instance(lesson, start.date())
        db.session.commit()
        instance_id = instance.id

        result, status = remove_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_id,
                "date": start.date().isoformat(),
            },
            "scope": "single",
        })
        assert status == 200
        assert LessonInstance.query.get(instance_id) is None
