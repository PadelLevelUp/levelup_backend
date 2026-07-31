"""
PAD-85 — Duplicate LessonInstance materialized when an occurrence's time
diverges from its parent lesson.

`get_or_materialize_instance(lesson, date)` used to look an existing instance up
by the PARENT lesson's time-of-day:

    LessonInstance.query.filter_by(
        lesson_id=lesson.id,
        start_datetime=datetime.combine(date, lesson.start_datetime.time()),
    )

If a single occurrence was edited to a different time ("this occurrence only"),
the materialized override's `start_datetime` no longer matches the parent's
time, so the lookup missed and a SECOND `LessonInstance` (with a fresh set of
unconfirmed Presence rows) was materialized for the same logical occurrence.

That is the reminder-double-send path behind PAD-69: the reminder runner calls
`get_or_materialize_instance` on every pass, so a follow-up pass would create a
duplicate instance and re-invite students who had already declined.

The lookup must be keyed on the occurrence identity (`lesson_id` +
`original_lesson_occurence_date`, mirroring `calendar_helpers`), robust to time
divergence — never on the parent lesson's time-of-day.

Covered spec: classes.instances (recurring occurrence materialization)
"""
import json
from datetime import datetime, timedelta

import pytest

from padel_app.sql_db import db


@pytest.fixture
def recurring_with_coach(app):
    """Coach + a weekly recurring lesson at 10:00. Returns
    (coach_id, lesson_id, first_start_datetime)."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.clubs import Club
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.lessons import Lesson

    with app.app_context():
        coach_user = User(name="Coach", username="pad85_coach", password="x")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name="PAD85 Club", description="c", location="x")
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
        return coach.id, lesson.id, start


def _instance_count_for_date(lesson_id, occ_date):
    """Count LessonInstance rows that represent the given logical occurrence,
    regardless of whether their time diverged from the parent."""
    from padel_app.models import LessonInstance
    from sqlalchemy import or_

    day_start = datetime.combine(occ_date, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    return (
        LessonInstance.query
        .filter(LessonInstance.lesson_id == lesson_id)
        .filter(
            or_(
                LessonInstance.original_lesson_occurence_date == occ_date,
                LessonInstance.start_datetime >= day_start,
            )
        )
        .filter(LessonInstance.start_datetime < day_end)
        .count()
    )


def test_time_diverged_occurrence_is_not_re_materialized(app, recurring_with_coach):
    """Editing one occurrence's time then running materialization again must
    reuse the same instance, never create a duplicate."""
    coach_id, lesson_id, first_start = recurring_with_coach
    from padel_app.models import Lesson
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    occ_date = (first_start + timedelta(weeks=1)).date()

    # "This occurrence only" edit that MOVES the time (10:00 -> 14:00).
    with app.app_context():
        result, status = edit_class_service({
            "event": {
                "model": "Lesson",
                "originalId": lesson_id,
                "date": occ_date.isoformat(),
            },
            "scope": "single",
            "updates": {"startTime": "14:00", "endTime": "15:00"},
        })
        db.session.commit()
        assert status == 201
        first_instance_id = result["id"]

    # Exactly one instance exists for this occurrence so far.
    with app.app_context():
        assert _instance_count_for_date(lesson_id, occ_date) == 1

    # A later code path (e.g. the reminder runner) materializes the same
    # occurrence again. It must return the SAME instance, not a second one.
    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        reused = get_or_materialize_instance(lesson, occ_date)
        db.session.commit()
        assert reused.id == first_instance_id, (
            "materialization returned a different instance for the same "
            "occurrence — a duplicate was created"
        )

    # No duplicate row was written.
    with app.app_context():
        assert _instance_count_for_date(lesson_id, occ_date) == 1, (
            "a second LessonInstance was materialized for the same occurrence"
        )


def test_materialize_with_standing_waiting_list_entry_does_not_close_transaction(
    app, recurring_with_coach
):
    """Regression for the 2026-07-27 prod incident: reminder_for_lesson_occurrence
    crashed with sqlalchemy.exc.ResourceClosedError ("This transaction is closed")
    for any lesson with an assigned coach, because
    `_sync_standing_entries_for_new_instance` called `WaitingListEntry(...).create()`
    — which issues a full `db.session.commit()` — from inside the caller's
    `db.session.begin_nested()` SAVEPOINT in `get_or_materialize_instance`. That
    commit ended the savepoint out from under the caller, so the caller's later
    `sp.commit()` raised ResourceClosedError.

    This only reproduces when there's an active StandingWaitingListEntry for the
    coach, since that's what drives `_sync_standing_entries_for_new_instance` to
    actually create a WaitingListEntry row.
    """
    coach_id, lesson_id, first_start = recurring_with_coach
    from padel_app.models import Lesson, Player, User
    from padel_app.models.standing_waiting_list_entry import StandingWaitingListEntry
    from padel_app.services.lesson_service import get_or_materialize_instance

    occ_date = (first_start + timedelta(weeks=3)).date()

    with app.app_context():
        puser = User(name="Waiter", username="pad85_waiter", password="x")
        db.session.add(puser)
        db.session.flush()
        player = Player(user_id=puser.id)
        db.session.add(player)
        db.session.flush()

        entry = StandingWaitingListEntry(
            coach_id=coach_id,
            player_id=player.id,
            credits_total=5,
            credits_used=0,
            expires_at=datetime.utcnow() + timedelta(days=30),
            is_active=True,
        )
        db.session.add(entry)
        db.session.commit()
        player_id = player.id

    # This must not raise sqlalchemy.exc.ResourceClosedError.
    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance = get_or_materialize_instance(lesson, occ_date)
        assert instance is not None
        assert instance.lesson_id == lesson_id
        instance_id = instance.id

        # The outer session/transaction must still be usable afterward — proof
        # the savepoint used by `_sync_standing_entries_for_new_instance` did not
        # close the caller's transaction.
        db.session.commit()
        from padel_app.models import LessonInstance

        assert LessonInstance.query.get(instance_id) is not None

    # A WaitingListEntry was actually synced for the standing entry (confirms
    # the code path under test executed, not just skipped).
    with app.app_context():
        from padel_app.models.waiting_list_entry import WaitingListEntry

        wle = WaitingListEntry.query.filter_by(
            lesson_instance_id=instance_id, player_id=player_id, is_active=True
        ).first()
        assert wle is not None, "standing entry was not fanned out to the new instance"


def test_reminder_reuses_instance_after_student_declined(app, recurring_with_coach):
    """Full PAD-69 shape: student declines on instance A, a follow-up
    materialization pass must not spawn instance B with a fresh (unconfirmed)
    Presence row for the same student."""
    coach_id, lesson_id, first_start = recurring_with_coach
    from padel_app.models import Lesson, LessonInstance, Presence, Player, User
    from padel_app.models.Association_PlayerLesson import Association_PlayerLesson
    from padel_app.services.lesson_service import (
        edit_class_service,
        get_or_materialize_instance,
    )

    occ_date = (first_start + timedelta(weeks=2)).date()

    # Attach a player to the parent lesson so materialization creates a Presence.
    with app.app_context():
        puser = User(name="Player", username="pad85_player", password="x")
        db.session.add(puser)
        db.session.flush()
        player = Player(user_id=puser.id)
        db.session.add(player)
        db.session.flush()
        db.session.add(Association_PlayerLesson(player_id=player.id, lesson_id=lesson_id))
        db.session.commit()
        player_id = player.id

    # First reminder pass materializes instance A (creates a Presence row).
    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        instance_a = get_or_materialize_instance(lesson, occ_date)
        db.session.commit()
        instance_a_id = instance_a.id

    # Student answers the reminder on instance A (confirmed=True marks answered).
    with app.app_context():
        presence = Presence.query.filter_by(
            lesson_instance_id=instance_a_id, player_id=player_id
        ).first()
        assert presence is not None, "materialized instance should have a Presence"
        presence.confirmed = True
        presence.save()
        db.session.commit()

    # A "this occurrence only" time edit moves instance A off the parent's time.
    with app.app_context():
        _, status = edit_class_service({
            "event": {
                "model": "LessonInstance",
                "originalId": instance_a_id,
                "date": occ_date.isoformat(),
            },
            "scope": "single",
            "updates": {"startTime": "14:00", "endTime": "15:00"},
        })
        db.session.commit()
        assert status == 200

    # Follow-up reminder pass re-materializes the same occurrence.
    with app.app_context():
        lesson = Lesson.query.get(lesson_id)
        reused = get_or_materialize_instance(lesson, occ_date)
        db.session.commit()
        assert reused.id == instance_a_id

    # The student's answer must survive: still exactly one Presence, confirmed.
    with app.app_context():
        presences = Presence.query.filter_by(player_id=player_id).all()
        instance_ids = {
            LessonInstance.query.get(p.lesson_instance_id).lesson_id
            for p in presences
        }
        assert instance_ids == {lesson_id}
        assert len(presences) == 1, (
            "a duplicate instance created a second (unconfirmed) Presence — "
            "student would be reminded again despite having answered"
        )
        assert presences[0].confirmed is True
