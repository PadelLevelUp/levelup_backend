"""
PAD-94 — responding to a reminder must be IDEMPOTENT.

Reported from production (2026-07-21): one student tapped **No** on a class
reminder 8 times in ~62 seconds. Every tap ran ``respond_to_reminder`` in full,
so the student received 8 separate ``reminder_declined`` system messages and the
invitation engine was re-driven 8 times, fanning out 8 duplicate "a spot opened"
invitations to the same two replacement candidates.

Fix: re-submitting the answer already on record for a (player, instance) is a
no-op that returns ``{"action": ..., "duplicate": True}`` — no second system
message, no vacancy churn, no re-fan-out. Changing the answer is never
suppressed, and a genuinely NEW reminder is always answerable.

Run:
    pytest padel_app/tests/test_reminder_response_idempotency.py -v
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from padel_app.sql_db import db

from padel_app.tests.test_notification_reminder_flow import (
    PATCHES,
    _config_with_repeat,
    _enable_auto_notify,
    _seed_coach_and_student,
    _seed_instance,
    _seed_replacement_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _system_messages(coach_user_id, student_user_id):
    """Plain-text system messages (reminder_confirmed / reminder_declined) sent
    to the student in their direct conversation with the coach."""
    from padel_app.models.messages import Message
    from padel_app.services.notification_service import (
        _get_or_create_direct_conversation,
    )

    conv = _get_or_create_direct_conversation(coach_user_id, student_user_id)
    return Message.query.filter_by(
        conversation_id=conv.id,
        message_type="text",
    ).all()


def _invitation_events(instance_id):
    from padel_app.models.notification_event import NotificationEvent

    return NotificationEvent.query.filter_by(lesson_instance_id=instance_id).all()


def _seed_declinable_class(app):
    """A class 12h out (inside the default 24h-before invitation window, so the
    decline fans out invitations immediately) with 3 replacement candidates."""
    ids = _seed_coach_and_student(app)
    instance_id = _seed_instance(
        app, ids["coach_id"], ids["student_id"], start_offset_hours=12
    )
    _config_with_repeat(app, ids["coach_id"], count=3, hours=2)
    _enable_auto_notify(app, ids["coach_id"])
    candidate_ids = _seed_replacement_candidates(app, ids["coach_id"], 3)
    return ids, instance_id, candidate_ids


# ---------------------------------------------------------------------------
# TestReminderResponseIdempotency
# ---------------------------------------------------------------------------

class TestReminderResponseIdempotency:

    def test_repeated_no_produces_one_decline_and_one_invitation_round(self, app):
        """The headline regression: three "No" taps == one decline, one vacancy,
        one round of invitations."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.vacancy import Vacancy

        ids, instance_id, _ = _seed_declinable_class(app)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)

                first = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0
                )
                events_after_first = len(_invitation_events(instance_id))

                second = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0
                )
                third = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0
                )

            # Exactly ONE reminder_declined message — not three.
            assert len(_system_messages(ids["coach_user_id"], ids["student_user_id"])) == 1

            # Exactly ONE vacancy for the student's spot.
            vacancies = Vacancy.query.filter_by(lesson_instance_id=instance_id).all()
            assert len(vacancies) == 1

            # Exactly ONE round of invitations — and the first round really did
            # fan out, so this is not a vacuously-true assertion.
            events = _invitation_events(instance_id)
            assert events_after_first > 0
            assert len(events) == events_after_first
            invited_ids = [e.player_id for e in events]
            assert len(invited_ids) == len(set(invited_ids)), (
                "a replacement candidate was invited more than once"
            )

            # The first answer is processed for real; the repeats are no-ops.
            assert first == {"action": "declined"}
            assert second == {"action": "declined", "duplicate": True}
            assert third == {"action": "declined", "duplicate": True}

    def test_eight_rapid_no_taps_match_the_production_incident(self, app):
        """The exact production shape: 8 taps in ~1 minute, still one decline."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.vacancy import Vacancy

        ids, instance_id, _ = _seed_declinable_class(app)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                results = [
                    respond_to_reminder(
                        instance_id, "no", ids["student_user_id"],
                        now=t0 + timedelta(seconds=9 * i),
                    )
                    for i in range(8)
                ]

            assert results[0] == {"action": "declined"}
            assert all(r.get("duplicate") is True for r in results[1:])
            assert len(_system_messages(ids["coach_user_id"], ids["student_user_id"])) == 1
            assert Vacancy.query.filter_by(lesson_instance_id=instance_id).count() == 1
            invited_ids = [e.player_id for e in _invitation_events(instance_id)]
            assert len(invited_ids) == len(set(invited_ids))

    def test_repeated_yes_produces_one_confirmation_message(self, app):
        """Same guarantee on the confirm side."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.presences import Presence

        ids, instance_id, _ = _seed_declinable_class(app)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                first = respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"], now=t0
                )
                second = respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"], now=t0
                )

            assert first == {"action": "confirmed"}
            assert second == {"action": "confirmed", "duplicate": True}
            assert len(_system_messages(ids["coach_user_id"], ids["student_user_id"])) == 1

            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.confirmed is True
            assert presence.status is None

    def test_changing_the_answer_is_never_suppressed(self, app):
        """yes → no is a real change: it must free the spot as usual."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.vacancy import Vacancy
        from padel_app.models.presences import Presence

        ids, instance_id, _ = _seed_declinable_class(app)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                respond_to_reminder(instance_id, "yes", ids["student_user_id"], now=t0)
                changed = respond_to_reminder(
                    instance_id, "no", ids["student_user_id"], now=t0
                )

            assert changed == {"action": "declined"}
            assert Vacancy.query.filter_by(lesson_instance_id=instance_id).count() == 1
            presence = Presence.query.filter_by(
                lesson_instance_id=instance_id, player_id=ids["student_id"]
            ).first()
            assert presence.status == "absent"
            # One confirm + one decline message.
            assert len(_system_messages(ids["coach_user_id"], ids["student_user_id"])) == 2

    def test_new_reminder_is_answerable_again_with_the_same_answer(self, app):
        """A fresh reminder (PAD-49 rule 9) is a new question. Answering it the
        same way is NOT a duplicate — the student is confirming again on a
        message that is still live."""
        from padel_app.services.notification_service import (
            respond_to_reminder,
            send_class_reminders,
        )
        from padel_app.models.messages import Message

        ids, instance_id, _ = _seed_declinable_class(app)

        with app.app_context():
            t0 = datetime.utcnow()
            with patch(PATCHES[0]), patch(PATCHES[1]):
                send_class_reminders(instance_id, now=t0)
                first = respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"], now=t0
                )
                # A second reminder goes out anyway (e.g. the coach re-notifies).
                _resend_reminder(ids, instance_id)
                second = respond_to_reminder(
                    instance_id, "yes", ids["student_user_id"],
                    now=t0 + timedelta(hours=2),
                )

            assert first == {"action": "confirmed"}
            assert second.get("duplicate") is not True
            # Both reminder messages are badged as responded.
            reminders = Message.query.filter_by(
                message_type="notification_reminder"
            ).all()
            assert len(reminders) == 2
            assert all(
                m.msg_metadata.get("responded") or m.msg_metadata.get("superseded")
                for m in reminders
            )


def _resend_reminder(ids, instance_id):
    """Insert a second live reminder message directly, mimicking a fresh
    reminder pass for a student who already answered the first one."""
    from padel_app.models.messages import Message
    from padel_app.services.notification_service import (
        _get_or_create_direct_conversation,
    )

    conv = _get_or_create_direct_conversation(
        ids["coach_user_id"], ids["student_user_id"]
    )
    msg = Message(
        text="Reminder: your class is coming up.",
        sender_id=ids["coach_user_id"],
        conversation_id=conv.id,
        message_type="notification_reminder",
        msg_metadata={
            "lessonInstanceId": instance_id,
            "instanceId": instance_id,
            "reminderNumber": 2,
        },
    )
    msg.create()
    db.session.commit()
    return msg
