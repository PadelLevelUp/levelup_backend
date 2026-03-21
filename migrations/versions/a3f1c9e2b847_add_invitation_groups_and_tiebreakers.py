"""add invitation_groups and tiebreakers to notification_config

Revision ID: a3f1c9e2b847
Revises: c6f54b3fa846
Create Date: 2026-03-20

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a3f1c9e2b847'
down_revision = 'a20050262cd7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('notification_configs', sa.Column('invitation_groups', sa.JSON(), nullable=True))
    op.add_column('notification_configs', sa.Column('tiebreakers', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('notification_configs', 'tiebreakers')
    op.drop_column('notification_configs', 'invitation_groups')
