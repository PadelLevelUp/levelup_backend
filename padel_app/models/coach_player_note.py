from sqlalchemy import Column, Integer, String, ForeignKey, Enum, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class CoachPlayerNote(db.Model, model.Model):
    __tablename__ = "coach_player_notes"
    __table_args__ = {"extend_existing": True}

    page_title = "Coach Player Notes"
    model_name = "CoachPlayerNote"

    id = Column(Integer, primary_key=True)

    coach_player_id = Column(
        Integer, ForeignKey("coach_in_player.id", ondelete="CASCADE"), nullable=False
    )
    coach_player = relationship("Association_CoachPlayer", back_populates="notes_list")

    type = Column(Enum("strength", "weakness", name="note_type"), nullable=False)
    text = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def name(self):
        return f"{self.type.capitalize()}: {self.text[:40]}"

    def __repr__(self):
        return f"<CoachPlayerNote {self.type} - {self.text[:40]}>"

    def __str__(self):
        return f"{self.type.capitalize()}: {self.text[:40]}"

    @property
    def display_name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "text", "label": "Note"}
        columns = [
            {"field": "coach_player", "label": "Coach ↔ Player"},
            {"field": "type", "label": "Type"},
            {"field": "text", "label": "Note"},
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
                    "coach_player",
                    "ManyToOne",
                    label="Coach ↔ Player",
                    related_model="Association_CoachPlayer",
                ),
                get_field(
                    "type",
                    "Select",
                    label="Type",
                    options=["strength", "weakness"],
                ),
                get_field("text", "Text", label="Note"),
            ],
        )
        form.add_block(info_block)

        return form
