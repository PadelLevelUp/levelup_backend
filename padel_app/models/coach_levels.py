from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class CoachLevel(db.Model, model.Model):
    __tablename__ = "coach_levels"
    __table_args__ = {"extend_existing": True}

    page_title = "Coach Levels"
    model_name = "CoachLevel"

    id = Column(Integer, primary_key=True)

    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    coach = relationship("Coach", back_populates="levels")

    label = Column(String(100), nullable=False)  # e.g. "A1", "Beginner", "Pro"
    code = Column(String(10), nullable=False)
    
    coach_player_relations = relationship(
        "Association_CoachPlayer", 
        back_populates="level",
        cascade="all, delete-orphan"
    )
    
    display_order = Column(Integer, default=0)
    
    @property
    def name(self):
        return self.label

    def __repr__(self):
        return f"<CoachLevel {self.coach.name}: {self.label}>"

    def __str__(self):
        return f"{self.coach.name} - {self.label}"

    @property
    def display_name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "label", "label": "Level"}
        columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "label", "label": "Level"},
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
                    "coach", "ManyToOne", label="Coach", related_model="Coach"
                ),
                get_field("label", "Text", label="Level label"),
                get_field("code", "Text", label="Level code"),
                get_field("display_order", "Integer", label="Display order"),
            ],
        )
        form.add_block(info_block)

        return form
