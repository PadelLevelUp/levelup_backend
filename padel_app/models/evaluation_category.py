from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class EvaluationCategory(db.Model, model.Model):
    __tablename__ = "evaluation_categories"
    __table_args__ = {"extend_existing": True}

    page_title = "Evaluation Categories"
    model_name = "EvaluationCategory"

    id = Column(Integer, primary_key=True)

    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    coach = relationship("Coach", back_populates="evaluation_categories")

    name = Column(String(100), nullable=False)
    scale_min = Column(Integer, default=1)
    scale_max = Column(Integer, default=10)

    entries = relationship(
        "EvaluationEntry", back_populates="category", cascade="all, delete-orphan"
    )

    @property
    def display_name(self):
        return f"{self.name} ({self.scale_min}–{self.scale_max})"

    def __repr__(self):
        return f"<EvaluationCategory {self.coach.name}: {self.name}>"

    def __str__(self):
        return f"{self.coach.name} - {self.name}"

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Category"}
        columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "name", "label": "Category"},
            {"field": "scale_min", "label": "Scale Min"},
            {"field": "scale_max", "label": "Scale Max"},
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
                get_field("coach", "ManyToOne", label="Coach", related_model="Coach"),
                get_field("name", "Text", label="Category name"),
                get_field("scale_min", "Integer", label="Scale min"),
                get_field("scale_max", "Integer", label="Scale max"),
            ],
        )
        form.add_block(info_block)

        return form
    
    def frontend_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'scaleMin': self.scale_min,
            'scaleMax': self.scale_max,
        }
