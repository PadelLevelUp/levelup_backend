"""
PAD-117 — the SAVEPOINT guard in `get_or_materialize_instance` must absorb
transaction-closing failures, not just plain ones.

`get_or_materialize_instance()` fans standing waiting list entries out to a
newly materialized instance inside a SAVEPOINT. The comment above it states the
intent: "Use a SAVEPOINT so any DB failure ... doesn't poison the outer
transaction and break unrelated operations". It only half delivered:

    try:
        sp = db.session.begin_nested()      # <-- INSIDE the try
        _sync_standing_entries_for_new_instance(instance, coach_id)
        sp.commit()
    except Exception:
        sp.rollback()                       # <-- can raise the SAME error

Two failure shapes, only one of which was contained:

    | inner failure shape        | before PAD-117                          |
    | -------------------------- | --------------------------------------- |
    | plain exception            | 200 — correctly absorbed                |
    | commits, *then* raises     | 500 ResourceClosedError — escaped       |

When the guarded block commits, it ends the caller's savepoint out from under
it, so `sp.commit()` raises `ResourceClosedError` — and then `sp.rollback()` in
the handler raises the very same error, so the guard re-raised instead of
containing. That escaped to the coach as an HTTP 500 from
`POST /api/app/notify/send_reminders` (the "Lembrar" button), which is the
PAD-108 symptom.

Separately, `sp = db.session.begin_nested()` sat INSIDE the `try`, so a failure
opening the savepoint made the handler's `sp.rollback()` raise
`UnboundLocalError`, masking the real cause.

Reaching this code at all needs BOTH:
  * an occurrence that has never been materialized (so the calendar serializes
    it as `model: "Lesson"` and the notify endpoint materializes on demand), and
  * an active StandingWaitingListEntry for the coach (otherwise the guarded
    block does no work and cannot fail).

These tests assert on EFFECTS, not just status. A 200 with no reminder actually
delivered is the "worse than the 500" outcome the ticket warns about: the
request would look fine while running the rest of its work on a dead session.

Covered spec: classes.instances rules 5-7

Run:
    pytest padel_app/tests/test_pad117_savepoint_containment.py -v
"""
import json
import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from padel_app.sql_db import db


PATCHES = [
    "padel_app.services.notification_service.publish",
    "padel_app.services.notification_service.send_push_notification",
]


@contextmanager
def _patched_io():
    with ExitStack() as stack:
        for target in PATCHES:
            stack.enter_context(patch(target))
        yield


@pytest.fixture
def world(app):
    """Coach with an ACTIVE standing waiting list entry + a weekly recurring
    lesson with one enrolled player and zero materialized instances.

    Returns a dict of ids plus the date of an occurrence that has never been
    materialized.
    """
    from padel_app.models.users import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.Association_CoachClub import Association_CoachClub
    from padel_app.models.Association_CoachLesson import Association_CoachLesson
    from padel_app.models.Association_PlayerLesson import Association_PlayerLesson
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer
    from padel_app.models.standing_waiting_list_entry import StandingWaitingListEntry

    with app.app_context():
        coach_user = User(name="PAD117 Coach", username="pad117_coach",
                          email="pad117_coach@test.com", password="x")
        student_user = User(name="PAD117 Student", username="pad117_student",
                            email="pad117_student@test.com", password="x")
        waiter_user = User(name="PAD117 Waiter", username="pad117_waiter",
                           email="pad117_waiter@test.com", password="x")
        db.session.add_all([coach_user, student_user, waiter_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        student = Player(user_id=student_user.id)
        waiter = Player(user_id=waiter_user.id)
        db.session.add_all([student, waiter])
        db.session.flush()

        db.session.add_all([
            Association_CoachPlayer(coach_id=coach.id, player_id=student.id),
            Association_CoachPlayer(coach_id=coach.id, player_id=waiter.id),
        ])

        club = Club(name="PAD117 Club", description="c", location="x")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        # Weekly recurring lesson starting tomorrow at 10:00. No LessonInstance
        # rows are created — every occurrence is unmaterialized.
        start = (datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)
                 + timedelta(days=1))
        lesson = Lesson(
            title="PAD117 Recurring Class",
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
        db.session.add_all([
            Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id),
            Association_PlayerLesson(player_id=student.id, lesson_id=lesson.id),
        ])

        # The gating condition: without an ACTIVE standing entry the guarded
        # block is a no-op and none of this is reachable.
        db.session.add(StandingWaitingListEntry(
            coach_id=coach.id,
            player_id=waiter.id,
            credits_total=5,
            credits_used=0,
            expires_at=datetime.utcnow() + timedelta(days=90),
            is_active=True,
        ))
        db.session.commit()

        return {
            "coach_id": coach.id,
            "coach_user_id": coach_user.id,
            "lesson_id": lesson.id,
            "student_id": student.id,
            "student_user_id": student_user.id,
            # One week out: never materialized, and comfortably in the future so
            # send_class_reminders does not skip it as a past class.
            "occurrence_date": (start + timedelta(weeks=1)).date(),
        }


def _sync_target():
    """The symbol the guarded block imports at call time."""
    return "padel_app.services.notification_service._sync_standing_entries_for_new_instance"


def _commit_then_raise(instance, coach_id):
    """The PAD-113 failure shape: ends the caller's transaction, THEN fails."""
    db.session.commit()
    raise RuntimeError("PAD117 injected failure after commit")


def _plain_raise(instance, coach_id):
    """The shape that was already contained before PAD-117."""
    raise ValueError("PAD117 injected plain failure")


def _post_send_reminders(app, client, world):
    from flask_jwt_extended import create_access_token

    app.config["JWT_SECRET_KEY"] = "test-secret"
    with app.app_context():
        token = create_access_token(identity=str(world["coach_user_id"]))

    return client.post(
        "/api/app/notify/send_reminders",
        json={
            "model": "Lesson",
            "originalId": world["lesson_id"],
            "date": world["occurrence_date"].isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _reminder_messages_for(instance_id):
    """The actual delivered effect: reminder Messages carrying this instance."""
    from padel_app.models import Message

    msgs = Message.query.filter_by(message_type="notification_reminder").all()
    return [
        m for m in msgs
        if m.msg_metadata and m.msg_metadata.get("instanceId") == instance_id
    ]


def _materialized_instance_id(lesson_id, occ_date):
    from padel_app.models import LessonInstance

    inst = (
        LessonInstance.query
        .filter_by(lesson_id=lesson_id, original_lesson_occurence_date=occ_date)
        .first()
    )
    return inst.id if inst else None


# ---------------------------------------------------------------------------
# The regression: commit-then-raise must be contained
# ---------------------------------------------------------------------------

def test_commit_then_raise_inside_savepoint_is_contained(app, client, world, caplog):
    """A guarded-block failure that CLOSES the transaction must not escape as a
    500, and the reminder must still actually be delivered."""
    caplog.set_level(logging.ERROR)

    with _patched_io(), patch(_sync_target(), side_effect=_commit_then_raise):
        res = _post_send_reminders(app, client, world)

    assert res.status_code == 200, (
        f"savepoint failure escaped as {res.status_code} instead of being "
        f"contained: {res.data[:400]!r}"
    )

    with app.app_context():
        instance_id = _materialized_instance_id(world["lesson_id"],
                                                world["occurrence_date"])
        # The instance was committed BEFORE the guarded block ran, so it must
        # survive the containment (spec classes.instances rule 7).
        assert instance_id is not None, "materialized instance was lost"

        # The point of the assertion: a 200 with nothing behind it is worse than
        # the 500. The request must have kept a usable session and finished its
        # real work.
        delivered = _reminder_messages_for(instance_id)
        assert len(delivered) == 1, (
            "request returned 200 but delivered no reminder — the session was "
            "left dead after containment"
        )

    # The containment is a swallowed bug (a callee violated the no-commit
    # contract); it has to be diagnosable.
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "transaction-closing failure was contained silently — no ERROR logged"
    )


# ---------------------------------------------------------------------------
# Pin the shape that already worked, so the fix does not regress it
# ---------------------------------------------------------------------------

def test_plain_exception_inside_savepoint_is_still_contained(app, client, world):
    """The plain-exception row of the ticket's table: was already 200, must stay
    200 — and must still deliver."""
    with _patched_io(), patch(_sync_target(), side_effect=_plain_raise):
        res = _post_send_reminders(app, client, world)

    assert res.status_code == 200, res.data[:400]

    with app.app_context():
        instance_id = _materialized_instance_id(world["lesson_id"],
                                                world["occurrence_date"])
        assert instance_id is not None
        assert len(_reminder_messages_for(instance_id)) == 1


# ---------------------------------------------------------------------------
# Happy path: nothing injected — containment must not change normal behaviour
# ---------------------------------------------------------------------------

def test_healthy_sync_still_materializes_and_fans_out(app, client, world):
    """With no injected failure the standing entry must still fan out — proving
    the guard did not start swallowing real work."""
    from padel_app.models.waiting_list_entry import WaitingListEntry

    with _patched_io():
        res = _post_send_reminders(app, client, world)

    assert res.status_code == 200, res.data[:400]

    with app.app_context():
        instance_id = _materialized_instance_id(world["lesson_id"],
                                                world["occurrence_date"])
        assert instance_id is not None
        assert len(_reminder_messages_for(instance_id)) == 1
        entries = WaitingListEntry.query.filter_by(
            lesson_instance_id=instance_id, is_active=True
        ).all()
        assert len(entries) == 1, "standing entry did not fan out to the new instance"


# ---------------------------------------------------------------------------
# The masking bug: begin_nested() must not be inside the try
# ---------------------------------------------------------------------------

def test_failure_opening_savepoint_surfaces_its_own_cause(app, world):
    """If opening the SAVEPOINT itself fails, the caller must see THAT failure —
    never an UnboundLocalError from a handler referencing a savepoint that was
    never assigned."""
    from padel_app.models import Lesson
    from padel_app.services.lesson_service import get_or_materialize_instance

    class _SavepointBoom(Exception):
        pass

    with app.app_context():
        lesson = Lesson.query.get(world["lesson_id"])
        occ_date = world["occurrence_date"]

        with patch.object(db.session, "begin_nested", side_effect=_SavepointBoom("no savepoint")):
            with pytest.raises(Exception) as exc_info:
                get_or_materialize_instance(lesson, occ_date)

    assert not isinstance(exc_info.value, UnboundLocalError), (
        "the guard masked the real cause with UnboundLocalError"
    )
    assert isinstance(exc_info.value, _SavepointBoom), (
        f"expected the injected cause to surface, got {exc_info.value!r}"
    )
