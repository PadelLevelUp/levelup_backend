"""merge PAD-8 (seasons) and PAD-43 (late-cancellation) migration heads

Two feature branches (PAD-8 seasons, PAD-43 late-cancellation) each added a
migration off the same parent (b1c2d3e4f5a6), producing two Alembic heads. The
deploy entrypoint runs `flask db upgrade` (singular head), which fails with
"Multiple head revisions are present". This no-op merge revision unifies both
heads back into a single head so upgrades apply cleanly.

Revision ID: e1f2a3b4c5d6
Revises: c3d4e5f6a7b8, c9d1e2f3a4b5
Create Date: 2026-07-06 08:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = ('c3d4e5f6a7b8', 'c9d1e2f3a4b5')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
