from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint, Enum
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Association_CoachExercise(db.Model, model.Model):
    """
    Links a Coach to an Exercise.
    role='owner'    → the coach created the exercise and can edit/delete it.
    role='follower' → the coach has access (read-only) via sharing.
    """
    __tablename__ = "coach_exercise"
    __table_args__ = (
        UniqueConstraint("coach_id", "exercise_id", name="uq_coach_exercise"),
        {"extend_existing": True},
    )

    page_title = "Coach ↔ Exercise"
    model_name = "Association_CoachExercise"

    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"))
    exercise_id = Column(Integer, ForeignKey("exercises.id", ondelete="CASCADE"))
    role = Column(
        Enum("owner", "follower", name="coach_exercise_role"),
        nullable=False,
        default="owner",
    )

    coach = relationship("Coach", back_populates="exercise_relations")
    exercise = relationship("Exercise", back_populates="coaches_relations")

    def __repr__(self):
        return f"<CoachExercise coach={self.coach_id} exercise={self.exercise_id} role={self.role}>"

    @property
    def name(self):
        return f"{self.coach_id} - {self.exercise_id}"

    @classmethod
    def display_all_info(cls):
        searchable_column = {"field": "coach", "label": "Coach"}
        table_columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "exercise", "label": "Exercise"},
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
                get_field("exercise_id", "Exercise", "ManyToOne", related_model="Exercise"),
                get_field("role", "Role", "Select", options=["owner", "follower"]),
            ],
        )
        form.add_block(info_block)
        return form
