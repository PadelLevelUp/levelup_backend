"""add semi-automatic replacement approval

- notification_configs.invitation_mode (automatic | semi_automatic)
- vacancies.approval_status (not_required | pending | approved | dismissed)
- vacancies.invite_not_before
- replacement_approval_prompts table

Revision ID: 7a4c9e2d1f30
Revises: 362d13470558
Create Date: 2026-06-11 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '7a4c9e2d1f30'
down_revision = '362d13470558'
branch_labels = None
depends_on = None


VACANCY_APPROVAL_VALUES = ('not_required', 'pending', 'approved', 'dismissed')
PROMPT_STATUS_VALUES = ('pending', 'approved', 'dismissed', 'stale')

vacancy_approval_status = sa.Enum(
    *VACANCY_APPROVAL_VALUES, name='vacancy_approval_status'
)
prompt_status = sa.Enum(
    *PROMPT_STATUS_VALUES, name='replacement_approval_prompt_status'
)


def _enum_column_type(bind, values, name):
    """Reference an already-created named enum on PG (create_type=False
    prevents a duplicate CREATE TYPE); plain sa.Enum elsewhere (SQLite)."""
    if bind.dialect.name == 'postgresql':
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade():
    bind = op.get_bind()
    # Named PG enums must be created explicitly before being referenced by
    # add_column / create_table (no-op on SQLite).
    vacancy_approval_status.create(bind, checkfirst=True)
    prompt_status.create(bind, checkfirst=True)

    op.create_table(
        'replacement_approval_prompts',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=False),
        sa.Column('vacancy_id', sa.Integer(), nullable=False),
        sa.Column('bundle_id', sa.String(length=36), nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('declined_player_id', sa.Integer(), nullable=True),
        sa.Column('queue_snapshot', sa.JSON(), nullable=True),
        sa.Column('waiting_list_player_id', sa.Integer(), nullable=True),
        sa.Column(
            'status',
            _enum_column_type(bind, PROMPT_STATUS_VALUES, 'replacement_approval_prompt_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['declined_player_id'], ['players.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['waiting_list_player_id'], ['players.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vacancy_id'),
    )
    op.create_index(
        op.f('ix_replacement_approval_prompts_bundle_id'),
        'replacement_approval_prompts',
        ['bundle_id'],
        unique=False,
    )

    with op.batch_alter_table('notification_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'invitation_mode',
            sa.String(length=20),
            server_default='automatic',
            nullable=False,
        ))

    with op.batch_alter_table('vacancies', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'approval_status',
            _enum_column_type(bind, VACANCY_APPROVAL_VALUES, 'vacancy_approval_status'),
            server_default='not_required',
            nullable=False,
        ))
        batch_op.add_column(sa.Column('invite_not_before', sa.DateTime(), nullable=True))


def downgrade():
    bind = op.get_bind()

    with op.batch_alter_table('vacancies', schema=None) as batch_op:
        batch_op.drop_column('invite_not_before')
        batch_op.drop_column('approval_status')

    with op.batch_alter_table('notification_configs', schema=None) as batch_op:
        batch_op.drop_column('invitation_mode')

    op.drop_index(
        op.f('ix_replacement_approval_prompts_bundle_id'),
        table_name='replacement_approval_prompts',
    )
    op.drop_table('replacement_approval_prompts')

    prompt_status.drop(bind, checkfirst=True)
    vacancy_approval_status.drop(bind, checkfirst=True)
