"""add message reactions and reply/edit/delete fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reply_to_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('edited', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.create_foreign_key(
            'fk_messages_reply_to',
            'messages',
            ['reply_to_id'],
            ['id'],
            ondelete='SET NULL',
        )

    op.create_table(
        'message_reactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('emoji', sa.String(8), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_reaction'),
    )


def downgrade():
    op.drop_table('message_reactions')
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_constraint('fk_messages_reply_to', type_='foreignkey')
        batch_op.drop_column('is_deleted')
        batch_op.drop_column('edited')
        batch_op.drop_column('reply_to_id')
