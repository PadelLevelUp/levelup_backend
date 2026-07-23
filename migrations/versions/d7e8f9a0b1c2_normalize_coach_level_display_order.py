"""normalize coach_levels.display_order to a contiguous 1..N per coach (PAD-70)

Existing coaches can have levels with ``display_order`` NULL or 0 — the column
is nullable and defaults to 0, so any level created through a path that did not
supply an order (single-level POST, spreadsheet/AI import, rows predating the
column) landed there. The convention is "lower display_order = stronger level",
so those levels sorted ahead of every explicitly ordered one and the invitation
engine read them as the coach's strongest level. That is how a "5-" student got
invited as being one level above a "4" vacancy.

This renumbers every coach's ladder to a contiguous 1..N, preserving the order
the app already displayed: explicitly ordered levels first (ascending), then any
unordered ones appended at the bottom, ties broken by id.

Revision ID: d7e8f9a0b1c2
Revises: c5d6e7f8a9b0
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa


revision = 'd7e8f9a0b1c2'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, coach_id, display_order FROM coach_levels")
    ).fetchall()

    by_coach = {}
    for row in rows:
        by_coach.setdefault(row[1], []).append((row[0], row[2]))

    for levels in by_coach.values():
        # (unordered last, explicit order asc, id asc) — mirrors
        # padel_app/services/level_ladder.ladder_sort_key
        ordered = sorted(
            levels,
            key=lambda item: (
                1 if (item[1] is None or item[1] <= 0) else 0,
                0 if (item[1] is None or item[1] <= 0) else item[1],
                item[0],
            ),
        )
        for position, (level_id, current) in enumerate(ordered, start=1):
            if current != position:
                conn.execute(
                    sa.text(
                        "UPDATE coach_levels SET display_order = :pos WHERE id = :id"
                    ),
                    {"pos": position, "id": level_id},
                )


def downgrade():
    # Renumbering is not reversible — the previous NULL/0/duplicate values are
    # not recoverable, and the normalized values are a strict improvement.
    pass
