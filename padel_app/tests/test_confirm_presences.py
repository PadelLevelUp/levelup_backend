"""
PAD-64 — Attendance can be saved for a recurring class occurrence.

`confirm_presences_service` receives the ClassInstance payload the frontend
builds from `getClassInstance`. For a projected (recurring or single) Lesson
occurrence, `originalId` is a *Lesson* id and the calendar-event `id` is
`"lesson-<id>-<date>"`; for a materialized instance it is a *LessonInstance*
id with an `id` of `"lessoninstance-<id>"`.

The old branch keyed on `'parentClassId' in keys()`, but `parentClassId` is
added by `serialize_class_instance` whenever the detail endpoint resolves to
an instance — even when the frontend's event (and `originalId`) is still a
recurring Lesson (e.g. a stale calendar whose occurrence was materialized
after it loaded). That routed a Lesson id into `LessonInstance.get_or_404`
→ 404 "Failed to save attendance". The fix branches on the event id prefix.

Covered spec: attendance.presence
"""
from datetime import datetime, timedelta

import pytest
from werkzeug.exceptions import NotFound

from padel_app.sql_db import db


@pytest.fixture
def recurring_lesson(app):
    """A weekly recurring lesson with one enrolled player and no materialized
    instance yet. Returns (lesson_id, player_id, occurrence_date)."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_PlayerLesson import Association_PlayerLesson
    from padel_app.models.lessons import Lesson
    import json

    with app.app_context():
        coach_user = User(name="Coach", username="cp_coach", password="x")
        student_user = User(name="Student", username="cp_student", password="x")
        db.session.add_all([coach_user, student_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        player = Player(user_id=student_user.id)
        db.session.add_all([coach, player])
        db.session.flush()

        club = Club(name="CP Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        # Next week's occurrence at a fixed time.
        start = (datetime.utcnow().replace(hour=14, minute=0, second=0, microsecond=0)
                 + timedelta(days=7))
        lesson = Lesson(
            title="Recurring Class",
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            is_recurring=True,
            recurrence_rule=json.dumps(
                {"frequency": "weekly", "daysOfWeek": [(start.weekday() + 1) % 7]}
            ),
            recurrence_end=(start + timedelta(weeks=8)).date(),
            type="academy",
            max_players=4,
            status="active",
            club_id=club.id,
        )
        db.session.add(lesson)
        db.session.flush()

        db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
        db.session.add(Association_PlayerLesson(player_id=player.id, lesson_id=lesson.id))
        db.session.commit()

        return lesson.id, player.id, start.date().isoformat()


def test_confirm_presences_recurring_occurrence_materializes_and_records(app, recurring_lesson):
    """First-time save for a recurring occurrence: the service materializes the
    instance and records the presence (regression for the reported 404)."""
    lesson_id, player_id, date_str = recurring_lesson
    from padel_app.services.lesson_service import confirm_presences_service
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence

    class_instance_data = {
        "id": f"lesson-{lesson_id}-{date_str}",
        "originalId": str(lesson_id),
        "date": date_str,
        # No parentClassId — a fresh projected recurring occurrence.
    }

    with app.app_context():
        confirm_presences_service(
            class_instance_data,
            [{"playerId": player_id, "status": "present"}],
        )

        instance = (
            LessonInstance.query
            .filter_by(lesson_id=lesson_id, original_lesson_occurence_date=date_str)
            .first()
        )
        assert instance is not None, "occurrence should have been materialized"

        presence = Presence.query.filter_by(
            lesson_instance_id=instance.id, player_id=player_id
        ).first()
        assert presence is not None
        assert presence.status == "present"


def test_confirm_presences_stale_lesson_event_with_parent_class_id(app, recurring_lesson):
    """The reported bug: the payload carries `parentClassId` (added when the
    detail endpoint resolved to an instance) but `originalId` is still the
    Lesson id and the event id is `lesson-...`. The old code did
    `LessonInstance.get_or_404(<lesson id>)` → 404. The fix routes it through
    materialization and records the presence on the correct instance."""
    lesson_id, player_id, date_str = recurring_lesson
    from padel_app.services.lesson_service import confirm_presences_service
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence

    class_instance_data = {
        "id": f"lesson-{lesson_id}-{date_str}",
        "originalId": str(lesson_id),
        "date": date_str,
        # Present because serialize_class_instance resolved to an instance,
        # yet originalId is a Lesson id — the exact shape that used to 404.
        "parentClassId": str(lesson_id),
    }

    with app.app_context():
        confirm_presences_service(
            class_instance_data,
            [{"playerId": player_id, "status": "present"}],
        )

        instance = (
            LessonInstance.query
            .filter_by(lesson_id=lesson_id, original_lesson_occurence_date=date_str)
            .first()
        )
        assert instance is not None
        presence = Presence.query.filter_by(
            lesson_instance_id=instance.id, player_id=player_id
        ).first()
        assert presence is not None and presence.status == "present"


def test_confirm_presences_materialized_instance_event(app, recurring_lesson):
    """A genuine materialized-instance event (`id` = `lessoninstance-<id>`,
    `originalId` = instance id) still records on that instance."""
    lesson_id, player_id, date_str = recurring_lesson
    from padel_app.services.lesson_service import (
        confirm_presences_service,
        get_or_materialize_instance,
    )
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.presences import Presence
    from datetime import date as _date

    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        occ_date = _date.fromisoformat(date_str)
        instance = get_or_materialize_instance(lesson, occ_date)
        db.session.commit()
        instance_id = instance.id

        class_instance_data = {
            "id": f"lessoninstance-{instance_id}",
            "originalId": str(instance_id),
            "date": date_str,
            "parentClassId": str(lesson_id),
        }
        confirm_presences_service(
            class_instance_data,
            [{"playerId": player_id, "status": "absent", "justification": "justified"}],
        )

        presence = Presence.query.filter_by(
            lesson_instance_id=instance_id, player_id=player_id
        ).first()
        assert presence is not None
        assert presence.status == "absent"
        # No extra instance should have been created for the same occurrence.
        count = LessonInstance.query.filter_by(
            lesson_id=lesson_id, original_lesson_occurence_date=date_str
        ).count()
        assert count == 1
