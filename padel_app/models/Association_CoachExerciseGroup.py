from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint, Enum
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Association_CoachExerciseGroup(db.Model, model.Model):
    """
    Links a Coach to an ExerciseGroup.
    role='owner'    → the coach created the group and can edit/delete it.
    role='follower' → the coach has access (read-only) via sharing.
    """
    __tablename__ = "coach_exercise_group"
    __table_args__ = (
        UniqueConstraint("coach_id", "exercise_group_id", name="uq_coach_exercise_group"),
        {"extend_existing": True},
    )

    page_title = "Coach ↔ Exercise Group"
    model_name = "Association_CoachExerciseGroup"

    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"))
    exercise_group_id = Column(Integer, ForeignKey("exercise_groups.id", ondelete="CASCADE"))
    role = Column(
        Enum("owner", "follower", name="coach_exercise_group_role"),
        nullable=False,
        default="owner",
    )

    coach = relationship("Coach", back_populates="exercise_group_relations")
    exercise_group = relationship("ExerciseGroup", back_populates="coaches_relations")

    def __repr__(self):
        return f"<CoachExerciseGroup coach={self.coach_id} group={self.exercise_group_id} role={self.role}>"

    @property
    def name(self):
        return f"{self.coach_id} - {self.exercise_group_id}"

    @classmethod
    def display_all_info(cls):
        searchable_column = {"field": "coach", "label": "Coach"}
        table_columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "exercise_group", "label": "Exercise Group"},
            {"field": "role", "label": "Role"},
        ]
        return searchable_column, table_columns

    @classmethod
    def get_create_form(cls):
        def get_field(name, label, type, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                label=label,
                type=type,
                **kwargs,
            )

        form = Form()
        info_block = Block(
            "info_block",
            fields=[
                get_field("coach_id", "Coach", "ManyToOne", related_model="Coach"),
                get_field("exercise_group_id", "Exercise Group", "ManyToOne", related_model="ExerciseGroup"),
                get_field("role", "Role", "Select", options=["owner", "follower"]),
            ],
        )
        form.add_block(info_block)
        return form
