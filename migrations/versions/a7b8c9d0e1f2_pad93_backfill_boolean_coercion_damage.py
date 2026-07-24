"""PAD-93 backfill: repair booleans mis-persisted as False by the form layer

Until PAD-69, ``Field.set_boolean_value`` compared the incoming value against
the *string* ``"true"``. Every JSON payload reaches the form layer through
``JsonRequestAdapter``, which puts real Python booleans into ``request.form``,
so ``True == "true"`` was False and **every Boolean form field fed a real
boolean was written as False for the entire life of the app**.

PAD-69 fixed the coercion. This migration repairs the historical rows it left
behind — but ONLY the two columns whose intended value can be *proved* from
other data still in the database. Everything else is deliberately left alone
(see "Not backfilled" below) rather than guessed at.

--------------------------------------------------------------------------
Backfilled
--------------------------------------------------------------------------

1. ``lessons.is_recurring``

   ``UPDATE lessons SET is_recurring = TRUE
    WHERE recurrence_rule IS NOT NULL AND is_recurring IS NOT TRUE``

   ``recurrence_rule`` is only ever written when the caller asked for a
   recurring class (``lesson_service.add_class_service`` populates it inside
   ``if data.get("isRecurring")``; ``edit_lesson_helper`` only rewrites an
   already-present rule). It is a Text column, so it was never routed through
   the broken Boolean path and survived intact. Production on 2026-07-23:
   0/34 lessons flagged recurring, 33 of them carrying a recurrence rule.

2. ``presences.validated``

   ``UPDATE presences SET validated = TRUE
    WHERE status = 'present' AND validated IS NOT TRUE``

   ``add_presences`` (the coach's "mark attendance" action) is the only code
   path in the app that can write ``status = 'present'`` — every other writer
   of ``presences.status`` sets ``'absent'`` (``notification_service`` on a
   student decline, ``ai_service`` on import). And ``add_presences``
   unconditionally intends ``validated = True``. So ``status = 'present'``
   proves the coach validated that row. Production: 0/3117 validated, which
   inflates the dashboard's ``pending_validations`` KPI
   (``helpers/dashboard/kpis.py`` counts ``Presence.validated == False``) with
   every presence ever recorded.

--------------------------------------------------------------------------
Not backfilled (cannot be reconstructed — documented rather than guessed)
--------------------------------------------------------------------------

* ``presences.validated`` where ``status = 'absent'`` — indistinguishable
  between a coach marking someone absent and a student declining a reminder
  (``notification_service.respond_to_reminder`` writes the same status).
* ``presences.invited`` / ``confirmed`` / ``late_cancellation`` — the reminder
  flow sets these by direct attribute assignment, so they are mostly correct;
  the rows the old ``add_presences`` clobbered leave no trace of their prior
  value. Harmless in practice: reminder passes stop at class start.
* ``lessons.recurs_until_season_end`` — an edit through the form could have
  cleared it and nothing else records the original intent.
* ``conversations.is_group`` — the write path's own expression was wrong
  (it counted the creator, so every 1-on-1 DM would have been flagged a
  group); backfilling would propagate that second bug. Fixed at the source in
  ``messaging_service`` instead. The column is not read anywhere.
* ``users.is_admin`` / ``is_superadmin`` — never intended True from any app
  payload; backfilling privileges would be unsafe by construction.
* ``calendar_blocks.is_recurring`` — ``calendar_service._build_payload`` sends
  the *string* ``"true"``/``"false"``, so this column was never corrupted.
* ``calendar_blocks.blocks_auto_invitations`` — no historical rows carry it
  (0/4 in production); the clobbering path is fixed in ``calendar_service``.

--------------------------------------------------------------------------
Reversibility
--------------------------------------------------------------------------

``downgrade()`` applies the exact inverse UPDATE with the same WHERE clause,
restoring the pre-migration state for the rows this migration touched. Note it
is predicate-based, not row-id-based: if the app runs long enough after the
upgrade for new rows to satisfy the same predicate legitimately, a downgrade
would also reset those. That is the intended semantics — "undo the backfill"
means "put these columns back to the pre-fix value".

Revision ID: a7b8c9d0e1f2
Revises: 62236317a46e
Create Date: 2026-07-24 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = '62236317a46e'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # 1. lessons.is_recurring — provable from the surviving recurrence_rule.
    conn.execute(sa.text(
        "UPDATE lessons "
        "SET is_recurring = TRUE "
        "WHERE recurrence_rule IS NOT NULL "
        "  AND recurrence_rule <> '' "
        "  AND (is_recurring = FALSE OR is_recurring IS NULL)"
    ))

    # 2. presences.validated — status='present' can only come from the coach's
    #    attendance-marking path, which always intends validated=True.
    conn.execute(sa.text(
        "UPDATE presences "
        "SET validated = TRUE "
        "WHERE status = 'present' "
        "  AND (validated = FALSE OR validated IS NULL)"
    ))


def downgrade():
    conn = op.get_bind()

    conn.execute(sa.text(
        "UPDATE presences "
        "SET validated = FALSE "
        "WHERE status = 'present' "
        "  AND validated = TRUE"
    ))

    conn.execute(sa.text(
        "UPDATE lessons "
        "SET is_recurring = FALSE "
        "WHERE recurrence_rule IS NOT NULL "
        "  AND recurrence_rule <> '' "
        "  AND is_recurring = TRUE"
    ))
