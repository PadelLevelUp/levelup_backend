"""
PAD-93 — blast-radius of the `Field.set_boolean_value` coercion defect.

PAD-69 fixed the coercion itself (`True == "true"` was False, so every JSON
boolean was persisted as False). This module covers the *collateral damage*
that fix does not repair on its own:

1. Boolean form fields whose write path now behaves differently — and must not
   regress — once real booleans survive the form layer:
     * `users.is_admin` / `is_superadmin` — a JSON payload must never be able
       to grant admin through the app-facing user services.
     * `conversations.is_group` — the source expression counted the creator,
       so every 1-on-1 DM would now be flagged as a group.
     * `calendar_blocks.blocks_auto_invitations` — the edit payload never
       carries it, so the form was silently clearing student availability
       blockers (PAD-28).
2. Two datetime columns on `conversation_participants` mislabelled as
   `"Boolean"` fields, plus a phantom `validated` Boolean on `Conversation`
   that has no backing column.
3. The historical backfill rules (migration `a7b8c9d0e1f2`), asserted as data
   invariants against seeded "corrupt" rows.

Run:
    pytest padel_app/tests/test_pad93_boolean_blast_radius.py -v
"""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# 1a. users.is_admin / is_superadmin — privilege escalation guard
# ---------------------------------------------------------------------------

class TestUserPrivilegeFlagsAreNotFormSettable:
    """`POST /api/app/user` and friends are unauthenticated. Now that real
    booleans survive the form layer, `{"is_admin": true}` would actually grant
    admin — the service layer must strip the flags."""

    def test_create_user_cannot_grant_admin(self, app):
        from padel_app.models import User
        from padel_app.services.user_service import create_user_service

        with app.app_context():
            user = create_user_service({
                "name": "Mallory",
                "username": "mallory",
                "email": "mallory@test.com",
                "is_admin": True,
                "is_superadmin": True,
            })
            db.session.commit()

            fresh = User.query.get(user.id)
            assert fresh.is_admin is not True
            assert fresh.is_superadmin is not True

    def test_edit_user_cannot_grant_admin(self, app):
        from padel_app.models import User
        from padel_app.services.user_service import edit_user_service

        with app.app_context():
            user = User(name="Bob", username="bob93", email="bob93@test.com",
                        password="hashed", status="active")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

            edit_user_service(user_id, {"name": "Bob", "is_admin": True})
            db.session.commit()

            assert User.query.get(user_id).is_admin is not True

    def test_edit_user_cannot_revoke_admin_either(self, app):
        """The guard is symmetric: the app-facing form owns neither direction."""
        from padel_app.models import User
        from padel_app.services.user_service import edit_user_service

        with app.app_context():
            user = User(name="Root", username="root93", email="root93@test.com",
                        password="hashed", status="active", is_admin=True)
            db.session.add(user)
            db.session.commit()
            user_id = user.id

            edit_user_service(user_id, {"name": "Root", "is_admin": False})
            db.session.commit()

            assert User.query.get(user_id).is_admin is True

    def test_activate_user_cannot_grant_admin(self, app):
        from padel_app.models import User
        from padel_app.services.user_service import activate_user_service

        with app.app_context():
            user = User(name="Pending", username="pending93",
                        email="pending93@test.com", status="inactive")
            db.session.add(user)
            db.session.commit()
            user_id = user.id

            activate_user_service(user_id, {"password": "Str0ngPass!",
                                            "is_superadmin": True})
            db.session.commit()

            fresh = User.query.get(user_id)
            assert fresh.status == "active"
            assert fresh.is_superadmin is not True


# ---------------------------------------------------------------------------
# 1b. conversations.is_group — the expression itself was wrong
# ---------------------------------------------------------------------------

def _seed_user(app, username):
    from padel_app.models import User

    user = User(name=username, username=username, email=f"{username}@test.com",
                password="hashed", status="active")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_coach(app, username):
    """A student may start a conversation with any active coach."""
    from padel_app.models.coaches import Coach

    user = _seed_user(app, username)
    coach = Coach(user_id=user.id)
    db.session.add(coach)
    db.session.flush()
    return user


class TestConversationIsGroup:

    def test_one_to_one_conversation_is_not_a_group(self, app):
        from padel_app.services.messaging_service import create_conversation_service

        with app.app_context():
            student = _seed_user(app, "pad93-alice")
            coach = _seed_coach(app, "pad93-bob")
            db.session.commit()

            conv, _ = create_conversation_service(
                {"otherParticipants": [coach.id]}, student
            )
            db.session.commit()

            assert conv.is_group is False

    def test_three_way_conversation_is_a_group(self, app):
        from padel_app.services.messaging_service import create_conversation_service

        with app.app_context():
            student = _seed_user(app, "pad93-alice2")
            coach_a = _seed_coach(app, "pad93-bob2")
            coach_b = _seed_coach(app, "pad93-carol2")
            db.session.commit()

            conv, _ = create_conversation_service(
                {"otherParticipants": [coach_a.id, coach_b.id]}, student
            )
            db.session.commit()

            assert conv.is_group is True


# ---------------------------------------------------------------------------
# 1c. calendar_blocks.blocks_auto_invitations must survive an event edit
# ---------------------------------------------------------------------------

class TestBlockerFlagSurvivesEventEdit:

    def test_edit_event_does_not_clear_blocks_auto_invitations(self, app):
        from padel_app.models import CalendarBlock
        from padel_app.services.calendar_service import edit_event_service

        with app.app_context():
            user = _seed_user(app, "pad93-blocker")
            db.session.commit()

            start = datetime(2026, 8, 3, 18, 0)
            block = CalendarBlock(
                user_id=user.id,
                type="unavailable",
                title="Busy",
                start_datetime=start,
                end_datetime=start + timedelta(hours=2),
                is_recurring=False,
                blocks_auto_invitations=True,
            )
            db.session.add(block)
            db.session.commit()
            block_id = block.id

            edit_event_service(block_id, user.id, {
                "type": "unavailable",
                "title": "Still busy",
                "date": "2026-08-03",
                "startTime": "19:00",
                "endTime": "21:00",
                "isRecurring": False,
            })
            db.session.commit()

            fresh = CalendarBlock.query.get(block_id)
            assert fresh.title == "Still busy"
            assert fresh.blocks_auto_invitations is True


# ---------------------------------------------------------------------------
# 2. Mislabelled / phantom Boolean form fields
# ---------------------------------------------------------------------------

class TestFormFieldDeclarations:

    def test_conversation_participant_timestamps_are_not_booleans(self, app):
        from padel_app.models.conversation_participants import ConversationParticipant

        with app.app_context():
            fields = {f.name: f for f in
                      ConversationParticipant.get_create_form().fields}

        assert fields["joined_at"].type == "DateTime"
        assert fields["last_read_at"].type == "DateTime"

    def test_conversation_participant_edit_form_round_trips(self, app):
        """`get_edit_form()` reads every declared field off the instance — with
        the old Boolean declaration a datetime was fed into a Boolean field."""
        from padel_app.models.conversation_participants import ConversationParticipant
        from padel_app.models.conversations import Conversation

        with app.app_context():
            user = _seed_user(app, "pad93-participant")
            conv = Conversation(participant_key=str(user.id), is_group=False)
            db.session.add(conv)
            db.session.flush()

            participant = ConversationParticipant(
                conversation_id=conv.id, user_id=user.id
            )
            db.session.add(participant)
            db.session.commit()

            form = participant.get_edit_form()
            values = {f.name: f.value for f in form.fields}

        assert isinstance(values["joined_at"], datetime)

    def test_conversation_form_has_no_phantom_validated_field(self, app):
        """`Conversation` has no `validated` column — the field made
        `get_edit_form()` raise AttributeError."""
        from padel_app.models.conversations import Conversation

        with app.app_context():
            names = [f.name for f in Conversation.get_create_form().fields]
            assert "validated" not in names

            conv = Conversation(participant_key="9,10", is_group=False)
            db.session.add(conv)
            db.session.commit()

            # Must not raise.
            conv.get_edit_form()

    def test_every_boolean_form_field_has_a_boolean_column(self, app):
        """Guard rail: a Boolean field pointed at a non-Boolean (or missing)
        column is how PAD-93's mislabelled fields got in."""
        from sqlalchemy import Boolean

        from padel_app import models  # noqa: F401  (registers the mappers)
        from padel_app.model import Model

        offenders = []
        with app.app_context():
            for mapper in db.Model.registry.mappers:
                cls = mapper.class_
                if not issubclass(cls, Model) or not hasattr(cls, "__table__"):
                    continue
                try:
                    form = cls.get_create_form()
                except (NotImplementedError, TypeError, ValueError):
                    # ValueError: a handful of models declare fields with types
                    # that aren't in Field.valid_types (e.g. DeviceToken uses
                    # "String"), so their form can't even be built. Separate
                    # pre-existing bug — out of scope for PAD-93.
                    continue
                columns = cls.__table__.columns
                for field in form.fields:
                    if field.type != "Boolean":
                        continue
                    column = columns.get(field.name)
                    if column is None:
                        offenders.append(f"{cls.__name__}.{field.name} (no column)")
                    elif not isinstance(column.type, Boolean):
                        offenders.append(
                            f"{cls.__name__}.{field.name} ({column.type})"
                        )

        assert offenders == [], f"Boolean fields on non-Boolean columns: {offenders}"


# ---------------------------------------------------------------------------
# 3. Backfill rules (migration a7b8c9d0e1f2), asserted as data invariants
# ---------------------------------------------------------------------------

BACKFILL_IS_RECURRING = (
    "UPDATE lessons SET is_recurring = TRUE "
    "WHERE recurrence_rule IS NOT NULL AND recurrence_rule <> '' "
    "  AND (is_recurring = FALSE OR is_recurring IS NULL)"
)

BACKFILL_VALIDATED = (
    "UPDATE presences SET validated = TRUE "
    "WHERE status = 'present' AND (validated = FALSE OR validated IS NULL)"
)


def _seed_corrupt_fixtures(app):
    """Rows shaped exactly like the mis-persisted production data."""
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.lesson_instances import LessonInstance
    from padel_app.models.players import Player
    from padel_app.models.presences import Presence

    club = Club(name="PAD93 Club", description="", location="Lisbon")
    db.session.add(club)
    db.session.flush()

    start = datetime(2026, 6, 1, 10, 0)
    rule = json.dumps({"frequency": "weekly", "daysOfWeek": [1]})

    recurring = Lesson(title="Recurring (corrupt)", start_datetime=start,
                       end_datetime=start + timedelta(hours=1),
                       is_recurring=False, recurrence_rule=rule,
                       type="academy", max_players=4, color="#000000",
                       status="active", club_id=club.id)
    one_off = Lesson(title="One-off", start_datetime=start,
                     end_datetime=start + timedelta(hours=1),
                     is_recurring=False, recurrence_rule=None,
                     type="academy", max_players=4, color="#000000",
                     status="active", club_id=club.id)
    db.session.add_all([recurring, one_off])
    db.session.flush()

    instance = LessonInstance(lesson_id=recurring.id, start_datetime=start,
                              end_datetime=start + timedelta(hours=1),
                              max_players=4, status="scheduled")
    db.session.add(instance)
    db.session.flush()

    players = []
    for i in range(3):
        user = _seed_user(app, f"pad93-player-{i}")
        player = Player(user_id=user.id)
        db.session.add(player)
        db.session.flush()
        players.append(player)

    present = Presence(player_id=players[0].id, lesson_instance_id=instance.id,
                       status="present", validated=False)
    absent = Presence(player_id=players[1].id, lesson_instance_id=instance.id,
                      status="absent", justification="justified", validated=False)
    unmarked = Presence(player_id=players[2].id, lesson_instance_id=instance.id,
                        status=None, validated=False)
    db.session.add_all([present, absent, unmarked])
    db.session.commit()

    return {
        "recurring_id": recurring.id,
        "one_off_id": one_off.id,
        "present_id": present.id,
        "absent_id": absent.id,
        "unmarked_id": unmarked.id,
    }


class TestBackfillRules:

    def test_is_recurring_backfill_only_touches_rows_with_a_rule(self, app):
        from padel_app.models.lessons import Lesson

        with app.app_context():
            ids = _seed_corrupt_fixtures(app)
            db.session.execute(text(BACKFILL_IS_RECURRING))
            db.session.commit()

            assert Lesson.query.get(ids["recurring_id"]).is_recurring is True
            assert Lesson.query.get(ids["one_off_id"]).is_recurring is False

    def test_validated_backfill_only_touches_status_present(self, app):
        from padel_app.models.presences import Presence

        with app.app_context():
            ids = _seed_corrupt_fixtures(app)
            db.session.execute(text(BACKFILL_VALIDATED))
            db.session.commit()

            assert Presence.query.get(ids["present_id"]).validated is True
            # status='absent' is ambiguous (coach-marked vs. student decline)
            # and status=NULL was never marked — both stay untouched.
            assert Presence.query.get(ids["absent_id"]).validated is False
            assert Presence.query.get(ids["unmarked_id"]).validated is False

    def test_backfill_is_idempotent(self, app):
        from padel_app.models.lessons import Lesson
        from padel_app.models.presences import Presence

        with app.app_context():
            ids = _seed_corrupt_fixtures(app)
            for _ in range(2):
                db.session.execute(text(BACKFILL_IS_RECURRING))
                db.session.execute(text(BACKFILL_VALIDATED))
                db.session.commit()

            assert Lesson.query.get(ids["recurring_id"]).is_recurring is True
            assert Presence.query.get(ids["present_id"]).validated is True


# ---------------------------------------------------------------------------
# 4. The end-to-end write path the backfill exists for
# ---------------------------------------------------------------------------

class TestRecurringClassNowPersistsTheFlag:

    def test_add_class_service_persists_is_recurring(self, app):
        """The corruption source: `add_class_service` sends a real boolean."""
        from padel_app.models.clubs import Club
        from padel_app.models.coaches import Coach
        from padel_app.services.lesson_service import add_class_service

        with app.app_context():
            user = _seed_user(app, "pad93-coach")
            coach = Coach(user_id=user.id)
            club = Club(name="PAD93 Club 2", description="", location="Lisbon")
            db.session.add_all([coach, club])
            db.session.commit()

            lesson = add_class_service(
                {
                    "name": "Weekly academy",
                    "classType": "academy",
                    "maxPlayers": 4,
                    "date": "2026-08-03",
                    "startTime": "10:00",
                    "endTime": "11:00",
                    "isRecurring": True,
                    "recurrenceRule": {"frequency": "weekly", "daysOfWeek": [1]},
                    "endDate": "2026-12-31",
                },
                coach,
                club,
            )
            db.session.commit()

            assert lesson.is_recurring is True
            assert lesson.recurrence_rule is not None

    def test_add_class_service_non_recurring_stays_false(self, app):
        from padel_app.models.clubs import Club
        from padel_app.models.coaches import Coach
        from padel_app.services.lesson_service import add_class_service

        with app.app_context():
            user = _seed_user(app, "pad93-coach-2")
            coach = Coach(user_id=user.id)
            club = Club(name="PAD93 Club 3", description="", location="Lisbon")
            db.session.add_all([coach, club])
            db.session.commit()

            lesson = add_class_service(
                {
                    "name": "One-off private",
                    "classType": "private",
                    "maxPlayers": 2,
                    "date": "2026-08-04",
                    "startTime": "10:00",
                    "endTime": "11:00",
                    "isRecurring": False,
                },
                coach,
                club,
            )
            db.session.commit()

            assert lesson.is_recurring is False
