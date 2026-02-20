from sqlalchemy import Column, Integer, Float, String, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class EvaluationEntry(db.Model, model.Model):
    __tablename__ = "evaluation_entries"
    __table_args__ = {"extend_existing": True}

    page_title = "Evaluation Entries"
    model_name = "EvaluationEntry"

    id = Column(Integer, primary_key=True)

    coach_player_id = Column(
        Integer, ForeignKey("coach_in_player.id", ondelete="CASCADE"), nullable=False
    )
    coach_player = relationship("Association_CoachPlayer", back_populates="evaluations")

    category_id = Column(
        Integer, ForeignKey("evaluation_categories.id", ondelete="CASCADE"), nullable=False
    )
    category = relationship("EvaluationCategory", back_populates="entries")

    score = Column(Float, nullable=False)
    comment = Column(String(500), nullable=True)
    evaluated_at = Column(DateTime, default=datetime.utcnow)

    @property
    def name(self):
        return f"{self.category.name}: {self.score} ({self.evaluated_at.strftime('%Y-%m-%d')})"

    def __repr__(self):
        return f"<EvaluationEntry {self.category.name} - {self.score}>"

    def __str__(self):
        return f"{self.category.name}: {self.score}"

    @property
    def display_name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "category", "label": "Category"}
        columns = [
            {"field": "coach_player", "label": "Coach ↔ Player"},
            {"field": "category", "label": "Category"},
            {"field": "score", "label": "Score"},
            {"field": "evaluated_at", "label": "Evaluated At"},
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
                    "category",
                    "ManyToOne",
                    label="Category",
                    related_model="EvaluationCategory",
                ),
                get_field("score", "Float", label="Score"),
                get_field("comment", "Text", label="Comment"),
                get_field("evaluated_at", "DateTime", label="Evaluated at"),
            ],
        )
        form.add_block(info_block)

        return form
