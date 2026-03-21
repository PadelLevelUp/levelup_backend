from sqlalchemy import Column, Integer, String, ForeignKey, Enum, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model

from padel_app.tools.input_tools import Block, Field, Form


class Presence(db.Model, model.Model):
    __tablename__ = "presences"
    __table_args__ = {"extend_existing": True}
    
    page_title = "Presences"
    model_name = "Presence"

    id = Column(Integer, primary_key=True)

    lesson_instance_id = Column(
        Integer, ForeignKey("lesson_instances.id", ondelete="CASCADE"),
        nullable=False
    )
    player_id = Column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False
    )
    
    player = relationship("Player", back_populates="presences")
    lesson_instance = relationship("LessonInstance", back_populates="presences")

    status = Column(Enum("present", "absent", name="lesson_presence_status"), nullable=True)
    justification = Column(Enum("justified", "unjustified", name="lesson_presence_justification"), nullable=True)

    invited = Column(Boolean, default=False)
    confirmed = Column(Boolean, default=False)
    validated = Column(Boolean, default=False)
    
    @property
    def name(self):
        return f"<Presence {self.id}"

    __table_args__ = (
        UniqueConstraint(
            "player_id", "lesson_instance_id",
            name="uq_presence_player_lesson_instance"
        ),
    )

    @classmethod
    def get_create_form(cls):
        def get_field(name, label, type, required=False, options=None, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                label=label,
                type=type,
                required=required,
                options=options,
                **kwargs,
            )

        form = Form()

        info_block = Block(
            "info_block",
            fields=[
                get_field("lesson_instance", "Lesson instance", "ManyToOne", related_model="LessonInstance", required=True),
                get_field("player", "Player", "ManyToOne", related_model="Player", required=True),
                get_field("status", "Status", "Select", options=["present", "absent"]),
                get_field("justification", "Justification", "Select", options=["justified", "unjustified"]),
                get_field("invited", "Invited", "Boolean"),
                get_field("confirmed", "Confirmed", "Boolean"),
                get_field("validated", "Validated", "Boolean"),
            ],
        )
        form.add_block(info_block)
        
        return form

