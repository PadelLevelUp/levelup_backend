"""add user abbreviation (PAD-81)

Adds the optional short badge label a user can set on their own profile.
NULL keeps the previous behaviour: the abbreviation is derived from the
initials of the user's name (see User.abbreviation_display), so no backfill
is needed.

Revision ID: a1c2e3d4b5f6
Revises: c5d6e7f8a9b0
Create Date: 2026-07-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c2e3d4b5f6'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('abbreviation', sa.String(length=8), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('abbreviation')
