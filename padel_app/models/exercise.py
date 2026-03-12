from sqlalchemy import Column, Integer, String, Text, ForeignKey, Enum, JSON, Table
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


# Secondary table linking exercises to groups (pure junction, no extra columns)
exercise_group_exercises = Table(
    "exercise_group_exercises",
    db.Model.metadata,
    Column("exercise_group_id", Integer, ForeignKey("exercise_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("exercise_id", Integer, ForeignKey("exercises.id", ondelete="CASCADE"), primary_key=True),
)


class Exercise(db.Model, model.Model):
    __tablename__ = "exercises"
    __table_args__ = {"extend_existing": True}

    page_title = "Exercises"
    model_name = "Exercise"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    type = Column(
        Enum(
            "attack", "defense", "serve", "return", "volley",
            "transition", "warm_up", "footwork", "custom",
            name="exercise_type",
        ),
        nullable=False,
    )
    custom_type = Column(String(100), nullable=True)
    # difficulty: 1 (Beginner) to 5 (Expert)
    difficulty = Column(Integer, nullable=False, default=3)
    # JSON list of CoachLevel IDs this exercise targets
    level_ids = Column(JSON, nullable=True, default=list)
    # Full CourtDiagram JSON ({elements: [...]})
    diagram = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)

    owner_coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    owner_coach = relationship(
        "Coach", foreign_keys=[owner_coach_id], back_populates="owned_exercises"
    )

    # Coach access relations (owner + followers)
    coaches_relations = relationship(
        "Association_CoachExercise",
        back_populates="exercise",
        cascade="all, delete-orphan",
    )

    # Many-to-many: Exercise <-> ExerciseGroup
    groups = relationship(
        "ExerciseGroup",
        secondary=exercise_group_exercises,
        back_populates="exercises",
    )

    def __repr__(self):
        return f"<Exercise {self.name}>"

    def __str__(self):
        return self.name

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Name"}
        columns = [
            {"field": "name", "label": "Name"},
            {"field": "type", "label": "Type"},
            {"field": "difficulty", "label": "Difficulty"},
        ]
        return searchable, columns

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
                get_field("name", "Name", "Text"),
                get_field("description", "Description", "Text"),
                get_field(
                    "type", "Type", "Select",
                    options=["attack", "defense", "serve", "return", "volley",
                             "transition", "warm_up", "footwork", "custom"],
                ),
                get_field("custom_type", "Custom Type", "Text"),
                get_field("difficulty", "Difficulty (1-5)", "Integer"),
                get_field("notes", "Notes", "Text"),
            ],
        )
        form.add_block(info_block)
        return form


class ExerciseGroup(db.Model, model.Model):
    __tablename__ = "exercise_groups"
    __table_args__ = {"extend_existing": True}

    page_title = "Exercise Groups"
    model_name = "ExerciseGroup"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    owner_coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    owner_coach = relationship(
        "Coach", foreign_keys=[owner_coach_id], back_populates="owned_exercise_groups"
    )

    # Coach access relations (owner + followers)
    coaches_relations = relationship(
        "Association_CoachExerciseGroup",
        back_populates="exercise_group",
        cascade="all, delete-orphan",
    )

    # Many-to-many: ExerciseGroup <-> Exercise
    exercises = relationship(
        "Exercise",
        secondary=exercise_group_exercises,
        back_populates="groups",
    )

    def __repr__(self):
        return f"<ExerciseGroup {self.name}>"

    def __str__(self):
        return self.name

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Name"}
        columns = [
            {"field": "name", "label": "Name"},
            {"field": "description", "label": "Description"},
        ]
        return searchable, columns

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
                get_field("name", "Name", "Text"),
                get_field("description", "Description", "Text"),
            ],
        )
        form.add_block(info_block)
        return form
