"""add training exercises and groups

Revision ID: a2b3c4d5e6f7
Revises: 30dba1e0df17
Create Date: 2026-03-12 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2b3c4d5e6f7'
down_revision = '30dba1e0df17'
branch_labels = None
depends_on = None


def upgrade():
    # Exercises
    op.create_table(
        'exercises',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'type',
            sa.Enum(
                'attack', 'defense', 'serve', 'return', 'volley',
                'transition', 'warm_up', 'footwork', 'custom',
                name='exercise_type',
            ),
            nullable=False,
        ),
        sa.Column('custom_type', sa.String(100), nullable=True),
        sa.Column('difficulty', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('level_ids', sa.JSON(), nullable=True),
        sa.Column('diagram', sa.JSON(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('owner_coach_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['owner_coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Exercise Groups
    op.create_table(
        'exercise_groups',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('owner_coach_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['owner_coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Exercise Group ↔ Exercise junction
    op.create_table(
        'exercise_group_exercises',
        sa.Column('exercise_group_id', sa.Integer(), nullable=False),
        sa.Column('exercise_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['exercise_group_id'], ['exercise_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['exercise_id'], ['exercises.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('exercise_group_id', 'exercise_id'),
    )

    # Coach ↔ Exercise access (owner / follower)
    op.create_table(
        'coach_exercise',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=True),
        sa.Column('exercise_id', sa.Integer(), nullable=True),
        sa.Column(
            'role',
            sa.Enum('owner', 'follower', name='coach_exercise_role'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['exercise_id'], ['exercises.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('coach_id', 'exercise_id', name='uq_coach_exercise'),
    )

    # Coach ↔ ExerciseGroup access (owner / follower)
    op.create_table(
        'coach_exercise_group',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=True),
        sa.Column('exercise_group_id', sa.Integer(), nullable=True),
        sa.Column(
            'role',
            sa.Enum('owner', 'follower', name='coach_exercise_group_role'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['exercise_group_id'], ['exercise_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('coach_id', 'exercise_group_id', name='uq_coach_exercise_group'),
    )


def downgrade():
    op.drop_table('coach_exercise_group')
    op.drop_table('coach_exercise')
    op.drop_table('exercise_group_exercises')
    op.drop_table('exercise_groups')
    op.drop_table('exercises')
    op.execute("DROP TYPE IF EXISTS coach_exercise_group_role")
    op.execute("DROP TYPE IF EXISTS coach_exercise_role")
    op.execute("DROP TYPE IF EXISTS exercise_type")
