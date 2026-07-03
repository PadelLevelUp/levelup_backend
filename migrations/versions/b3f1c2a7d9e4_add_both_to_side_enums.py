"""add 'both' value to player_side and vacancy_side enums

Adds a third value ("both") to the player and vacancy side Postgres enums so a
player can be marked as comfortable on either court side. Existing left/right
players are left untouched (no backfill).

PAD-15.

Revision ID: b3f1c2a7d9e4
Revises: 7a4c9e2d1f30
Create Date: 2026-07-03 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'b3f1c2a7d9e4'
down_revision = '7a4c9e2d1f30'
branch_labels = None
depends_on = None


def upgrade():
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # Postgres. End Alembic's implicit transaction first, then add the value.
    # IF NOT EXISTS makes this idempotent (Postgres 12+).
    op.execute('COMMIT')
    op.execute("ALTER TYPE player_side ADD VALUE IF NOT EXISTS 'both'")
    op.execute("ALTER TYPE vacancy_side ADD VALUE IF NOT EXISTS 'both'")


def downgrade():
    # Postgres does not support removing a value from an enum type without
    # recreating the type. Since no rows are backfilled to "both" by this
    # migration, the downgrade is a no-op (leaving the value in place is safe).
    pass
