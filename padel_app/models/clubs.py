from sqlalchemy import Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Club(db.Model, model.Model):
    __tablename__ = "clubs"
    __table_args__ = {"extend_existing": True}

    page_title = "Clubs"
    model_name = "Club"

    id = Column(Integer, primary_key=True)

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)

    # Club logo
    logo_id = Column(Integer, ForeignKey("images.id", ondelete="SET NULL"))
    logo = relationship("Image", foreign_keys=[logo_id])

    @property
    def logo_url(self):
        return self.logo.url() if self.logo else None

    # Many-to-many: Club ↔ Coaches
    coaches_relations = relationship(
        "Association_CoachClub", back_populates="club", cascade="all, delete-orphan"
    )

    @property
    def coaches(self):
        return [rel.coach for rel in self.coaches_relations]

    # Many-to-many: Club ↔ Players
    players_relations = relationship(
        "Association_PlayerClub", back_populates="club", cascade="all, delete-orphan"
    )

    @property
    def players(self):
        return [rel.player for rel in self.players_relations]

    # One-to-many: Club ↔ Lessons
    lessons = relationship(
        "Lesson", back_populates="club", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Club {self.name}>"

    def __str__(self):
        return self.name

    @property
    def display_name(self):
        return self.name

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Name"}
        columns = [
            {"field": "name", "label": "Name"},
            {"field": "location", "label": "Location"},
            {"field": "logo", "label": "Logo"},
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
        picture_block = Block(
            "picture_block",
            fields=[
                get_field("logo_id", "Picture", label="Club Logo"),
            ],
        )
        form.add_block(picture_block)

        info_block = Block(
            "info_block",
            fields=[
                get_field("name", "Text", label="Name"),
                get_field("description", "Text", label="Description"),
                get_field("location", "Text", label="Location"),
                get_field(
                    "coaches_relations",
                    "OneToMany",
                    label="Coaches",
                    related_model="Association_CoachClub",
                ),
                get_field(
                    "players_relations",
                    "OneToMany",
                    label="Players",
                    related_model="Association_PlayerClub",
                ),
                get_field(
                    "lessons", "OneToMany", label="Lessons", related_model="Lesson"
                ),
            ],
        )
        form.add_block(info_block)

        return form
