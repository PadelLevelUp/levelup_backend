"""add standing waiting list

Revision ID: b1d2e3f4a5c6
Revises: a3f1c9e2b847
Create Date: 2026-03-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1d2e3f4a5c6'
down_revision = 'a3f1c9e2b847'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'standing_waiting_list_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=False),
        sa.Column('credits_total', sa.Integer(), nullable=False),
        sa.Column('credits_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['player_id'], ['players.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column(
        'waiting_list_entries',
        sa.Column('standing_entry_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_waiting_list_entries_standing_entry_id',
        'waiting_list_entries',
        'standing_waiting_list_entries',
        ['standing_entry_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint(
        'fk_waiting_list_entries_standing_entry_id',
        'waiting_list_entries',
        type_='foreignkey',
    )
    op.drop_column('waiting_list_entries', 'standing_entry_id')
    op.drop_table('standing_waiting_list_entries')
