from sqlalchemy import Boolean, Column, Integer, ForeignKey, DateTime, Enum, Text, String, Date
from sqlalchemy.orm import relationship


from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class LessonInstance(db.Model, model.Model):
    __tablename__ = "lesson_instances"
    __table_args__ = {"extend_existing": True}

    page_title = "Lesson Instances"
    model_name = "LessonInstance"

    id = Column(Integer, primary_key=True)

    lesson_id = Column(
        Integer, ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    lesson = relationship("Lesson", back_populates="instances")

    original_lesson_occurence_date = Column(Date)
    start_datetime = Column(DateTime, nullable=False)
    end_datetime = Column(DateTime, nullable=False)
    overwrite_title = Column(String(255), nullable=True)
    
    level_id = Column(Integer, ForeignKey("coach_levels.id"))
    level = relationship("CoachLevel")

    notifications_enabled = Column(Boolean, default=True, nullable=False, server_default="1")

    status = Column(
        Enum(
            "scheduled",
            "canceled",
            "rescheduled",
            "completed",
            name="lesson_instance_status",
        ),
        default="scheduled",
        nullable=False,
    )
    
    notes = Column(Text, nullable=True)
    max_players = Column(Integer, nullable=False)
    overridden_fields = Column(Text)

    presences = relationship(
        "Presence",
        back_populates="lesson_instance",
        cascade="all, delete-orphan",
    )
    
    # Many-to-many: LessonInstance <-> Player
    players_relations = relationship(
        "Association_PlayerLessonInstance",
        back_populates="lesson_instance",
        cascade="all, delete-orphan",
    )
    
    coaches_relations = relationship(
        "Association_CoachLessonInstance", 
        back_populates="lesson_instance", 
        cascade="all, delete-orphan"
    )
    
    @property
    def title(self):
        return self.overwrite_title or self.lesson.title

    @property
    def players(self):
        return [rel.player for rel in self.players_relations]

    def __repr__(self):
        return f"<LessonInstance {self.id} {self.title} {self.start_datetime.strftime('%Y-%m-%d %H:%M')}>"

    def __str__(self):
        return f"<LessonInstance {self.id} {self.title} {self.start_datetime.strftime('%Y-%m-%d %H:%M')}>"

    @property
    def name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "lesson", "label": "Lesson"}
        columns = [
            {"field": "lesson", "label": "Lesson"},
            {"field": "start_datetime", "label": "Start"},
            {"field": "end_datetime", "label": "End"},
            {"field": "status", "label": "Status"},
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
                get_field(
                    "lesson", "ManyToOne", label="Lesson", related_model="Lesson"
                ),
                get_field(
                    "level", "ManyToOne", label="Level", related_model="CoachLevel"
                ),
                get_field("start_datetime", "DateTime", label="Start Time"),
                get_field("end_datetime", "DateTime", label="End Time"),
                get_field("original_lesson_occurence_date", "Date", label="Original lesson occurence date"),
                get_field("notes", "Text", label="Notes"),
                get_field("overwrite_title", "Text", label="Titulo novo"),
                get_field("max_players", "Integer", label="Max players"),
                get_field(
                    "status",
                    "Select",
                    label="Status",
                    options=["scheduled", "canceled", "rescheduled", "completed"],
                ),
                get_field(
                    "players_relations",
                    "OneToMany",
                    label="Players",
                    related_model="Association_PlayerLessonInstance",
                ),
            ],
        )
        form.add_block(info_block)

        return form
