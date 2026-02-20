from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint, Enum, String
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Association_CoachPlayer(db.Model, model.Model):
    __tablename__ = "coach_in_player"
    __table_args__ = (
        UniqueConstraint("coach_id", "player_id", name="uq_coach_player"),
        {"extend_existing": True},
    )

    page_title = "Coach ↔ Player"
    model_name = "Association_CoachPlayer"

    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"))
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"))
    level_id = Column(Integer, ForeignKey("coach_levels.id"), nullable=True)

    coach = relationship("Coach", back_populates="players_relations")
    player = relationship("Player", back_populates="coaches_relations")
    level = relationship("CoachLevel", back_populates="coach_player_relations")
    
    side = Column(Enum("left", "right", name="player_side"), nullable=True)
    notes = Column(String(255), nullable=True)
    
    notes_list = relationship("CoachPlayerNote", back_populates="coach_player", cascade="all, delete-orphan")
    evaluations = relationship("EvaluationEntry", back_populates="coach_player", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<CoachPlayer {self.coach.name} - {self.player.name}>"

    def __str__(self):
        return f"{self.coach.name} - {self.player.name}"

    @property
    def name(self):
        return f"{self.coach.name} - {self.player.name}"
    
    @property
    def strengths(self):
        return [n for n in self.notes_list if n.type == "strength"]

    @property
    def weaknesses(self):
        return [n for n in self.notes_list if n.type == "weakness"]

    @property
    def current_evaluations(self):
        seen = {}
        for entry in sorted(self.evaluations, key=lambda e: e.evaluated_at, reverse=True):
            if entry.category_id not in seen:
                seen[entry.category_id] = entry
        return list(seen.values())

    @classmethod
    def display_all_info(cls):
        searchable_column = {"field": "coach", "label": "Coach"}
        table_columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "player", "label": "Player"},
        ]
        return searchable_column, table_columns

    @classmethod
    def get_create_form(cls):
        def get_field(name, label, type, options=None, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                label=label,
                type=type,
                options=options,
                **kwargs,
            )

        form = Form()
        info_block = Block(
            "info_block",
            fields=[
                get_field("coach", "Coach", "ManyToOne", related_model="Coach"),
                get_field("player", "Player", "ManyToOne", related_model="Player"),
                get_field("level", "CoachLevel", "ManyToOne", related_model="CoachLevel"),
                get_field("notes", label="Notes", type="Text"),
                get_field("side", label="Side", type="Select" , options=["left", "right"]),
            ],
        )
        form.add_block(info_block)

        return form
