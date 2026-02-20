from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Coach(db.Model, model.Model):
    __tablename__ = "coaches"
    __table_args__ = {"extend_existing": True}

    page_title = "Coach"
    model_name = "Coach"

    id = Column(Integer, primary_key=True)
    
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="coach")

    # One-to-many to Club
    clubs_relations = relationship(
        "Association_CoachClub", back_populates="coach", cascade="all, delete-orphan",
        order_by="desc(Association_CoachClub.created_at)",
    )
    
    evaluation_categories = relationship("EvaluationCategory", back_populates="coach", cascade="all, delete-orphan")
    
    @property
    def name(self):
        return self.user.name

    @property
    def clubs(self):
        return [rel.club for rel in self.clubs_relations]
    
    @property
    def current_club(self):
        return self.clubs[-1] if self.clubs else None

    # Many-to-many: Lessons
    lessons_relations = relationship(
        "Association_CoachLesson",
        back_populates="coach",
        cascade="all, delete-orphan",
    )

    @property
    def lessons(self):
        return [rel.lesson for rel in self.lessons_relations]

    # Relations to lesson instances
    lesson_instances_relations = relationship(
        "Association_CoachLessonInstance",
        back_populates="coach",
        cascade="all, delete-orphan",
    )

    @property
    def lesson_instances(self):
        return [rel.lesson_instance for rel in self.lesson_instances_relations]

    # Many-to-many: Players
    players_relations = relationship(
        "Association_CoachPlayer",
        back_populates="coach",
        cascade="all, delete-orphan",
    )

    @property
    def players(self):
        return [rel.player for rel in self.players_relations]

    levels = relationship(
        "CoachLevel", back_populates="coach", cascade="all, delete-orphan"
    )

    # Player levels tracked by this coach
    player_levels = relationship(
        "PlayerLevelHistory", back_populates="coach", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Coach {self.name}>"

    def __str__(self):
        return self.name

    @property
    def name_str(self):
        return self.name

    @classmethod
    def display_all_info(cls):
        searchable_column = {"field": "name", "label": "Name"}
        table_columns = [
            {"field": "name", "label": "Name"},
            {"field": "club", "label": "Club"},
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
                get_field("user", "User", "ManyToOne", related_model="User"),
            ],
        )
        form.add_block(info_block)

        return form
