from sqlalchemy import Column, Integer, String, Text, ForeignKey, Date, DateTime, Boolean, Enum
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Lesson(db.Model, model.Model):
    __tablename__ = "lessons"
    __table_args__ = {"extend_existing": True}

    page_title = "Lessons"
    model_name = "Lesson"

    id = Column(Integer, primary_key=True)

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    start_datetime = Column(DateTime, nullable=False)
    end_datetime = Column(DateTime, nullable=False)

    is_recurring = Column(Boolean, default=False, nullable=False)
    recurrence_rule = Column(Text, nullable=True)
    recurrence_end = Column(Date, nullable=True)
    
    type = Column(Enum("academy", "private", name="lesson_type"), nullable=False)

    default_level_id = Column(Integer, ForeignKey("coach_levels.id"))
    level = relationship("CoachLevel")
    max_players = Column(Integer, nullable=False)

    color = Column(String(10))
    status = Column(Enum("active", "ended", name="lesson_status"), default="active")
    notifications_enabled = Column(Boolean, default=True, nullable=False, server_default="1")

    # Many-to-many: Lesson <-> Coach
    coaches_relations = relationship(
        "Association_CoachLesson", back_populates="lesson", cascade="all, delete-orphan"
    )

    club_id = Column(
        Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False
    )
    club = relationship("Club", back_populates="lessons")

    @property
    def coaches(self):
        return [rel.coach for rel in self.coaches_relations]
    
    @property
    def name(self):
        return self.title

    # Many-to-many: Lesson <-> Player
    players_relations = relationship(
        "Association_PlayerLesson",
        back_populates="lesson",
        cascade="all, delete-orphan",
    )

    @property
    def players(self):
        return [rel.player for rel in self.players_relations]

    # One-to-many: Lesson -> LessonInstance
    instances = relationship(
        "LessonInstance", back_populates="lesson", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Lesson {self.title}>"

    def __str__(self):
        return self.title

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "title", "label": "Title"}
        columns = [
            {"field": "title", "label": "Title"},
            {"field": "start_datetime", "label": "Start"},
            {"field": "end_datetime", "label": "End"},
            {"field": "is_recurring", "label": "Recurring"},
        ]
        return searchable, columns

    @classmethod
    def get_create_form(cls):
        def get_field(name, type, label=None, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                type=type,
                label=label or name.capitalize(),
                **kwargs,
            )

        form = Form()
        info_block = Block(
            "info_block",
            fields=[
                get_field("title", type="Text", label="Title"),
                get_field("club", type="ManyToOne", label="Club", related_model="Club"),
                get_field("description", type="Text", label="Description"),
                get_field("type", type="Select", label="Type", options=["academy", "private"]),
                get_field("status", type="Select", label="Status", options=["active", "ended"]),
                get_field("color", type="Color", label="Color"),
                get_field("max_players", type="Integer", label="Max players"),
                get_field("level", type="ManyToOne", label="Level", related_model="CoachLevel"),
                get_field("start_datetime", type="DateTime", label="Start Time"),
                get_field("end_datetime", type="DateTime", label="End Time"),
                get_field("is_recurring", type="Boolean", label="Is Recurring"),
                get_field("recurrence_rule", type="Text", label="Recurrence Rule"),
                get_field("recurrence_end", type="Date", label="Recurrence End"),
                get_field(
                    "coaches_relations",
                    "OneToMany",
                    label="Coaches",
                    related_model="Association_CoachLesson",
                ),
                get_field(
                    "players_relations",
                    "OneToMany",
                    label="Players",
                    related_model="Association_PlayerLesson",
                ),
            ],
        )
        form.add_block(info_block)

        return form

    def data_for_instance(self):
        return {
            "lesson_id": self.id,
            "lesson": self.id,
            "original_lesson_occurence_date": self.start_datetime.date(),
            "start_datetime": self.start_datetime,
            "end_datetime": self.end_datetime,
            "overwrite_title": self.title,
            "title": self.title,
            "level": self.default_level_id,
            "level_id": self.default_level_id,
            "notifications_enabled": self.notifications_enabled,
            "status": "scheduled",
            "max_players": self.max_players,
            "player_ids": [
                rel.player_id
                for rel in self.players_relations
                if rel.player_id is not None
            ],
            "coach_ids": [
                rel.coach_id
                for rel in self.coaches_relations
                if rel.coach_id is not None
            ],
        }

    def to_instance_data(self):
        return self.data_for_instance()
