from sqlalchemy import Column, Integer, String, Date, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Season(db.Model, model.Model):
    __tablename__ = "seasons"
    __table_args__ = {"extend_existing": True}

    page_title = "Seasons"
    model_name = "Season"

    id = Column(Integer, primary_key=True)

    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    coach = relationship("Coach", back_populates="seasons")

    name = Column(String(120), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)

    @property
    def display_name(self):
        return f"{self.name} ({self.start_date} - {self.end_date})"

    def __repr__(self):
        return f"<Season {self.coach.name}: {self.name}>"

    def __str__(self):
        return f"{self.coach.name} - {self.name}"

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Season"}
        columns = [
            {"field": "coach", "label": "Coach"},
            {"field": "name", "label": "Season"},
            {"field": "start_date", "label": "Start"},
            {"field": "end_date", "label": "End"},
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
                get_field("name", "Text", label="Season name"),
                get_field("start_date", "Date", label="Start date"),
                get_field("end_date", "Date", label="End date"),
            ],
        )
        form.add_block(info_block)

        return form

    def frontend_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "startDate": self.start_date.isoformat() if self.start_date else None,
            "endDate": self.end_date.isoformat() if self.end_date else None,
        }
