from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Player(db.Model, model.Model):
    __tablename__ = "players"
    __table_args__ = {"extend_existing": True}

    page_title = "Players"
    model_name = "Player"

    id = Column(Integer, primary_key=True)
    
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="player")

    # Relations to lessons
    lessons_relations = relationship(
        "Association_PlayerLesson",
        back_populates="player",
        cascade="all, delete-orphan",
    )
    
    @property
    def name(self):
        return self.user.name

    @property
    def lessons(self):
        return [rel.lesson for rel in self.lessons_relations]
    
    presences = relationship(
        "Presence",
        back_populates="player",
        cascade="all, delete-orphan",
    )

    lesson_instances_relations = relationship(
        "Association_PlayerLessonInstance",
        back_populates="player",
        cascade="all, delete-orphan",
    )

    @property
    def lesson_instances(self):
        return [rel.lesson_instance for rel in self.lesson_instances_relations]

    clubs_relations = relationship(
        "Association_PlayerClub", back_populates="player", cascade="all, delete-orphan"
    )

    @property
    def clubs(self):
        return [rel.club for rel in self.clubs_relations]

    coaches_relations = relationship(
        "Association_CoachPlayer", back_populates="player", cascade="all, delete-orphan"
    )

    @property
    def coaches(self):
        return [rel.coach for rel in self.coaches_relations]

    # Level history
    level_history = relationship(
        "PlayerLevelHistory", 
        back_populates="player", 
        cascade="all, delete-orphan",
        order_by="desc(PlayerLevelHistory.assigned_at)"
    )
    
    @property
    def level(self):
        if self.level_history:
            return self.level_history[0]
        return None

    def __repr__(self):
        return f"<Player {self.name}>"

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
                get_field("user", type="ManyToOne", label="User", related_model="User"),
                get_field(
                    "lessons_relations",
                    type="OneToMany",
                    label="Lessons",
                    related_model="Association_PlayerLesson",
                ),
                get_field(
                    "lesson_instances_relations",
                    type="OneToMany",
                    label="Lesson Instances",
                    related_model="Association_PlayerLessonInstance",
                ),
                get_field(
                    "level_history",
                    type="OneToMany",
                    label="Level History",
                    related_model="PlayerLevelHistory",
                ),
            ],
        )
        form.add_block(info_block)

        return form

    def coach_player_info(self, coach_id):
        rel = next((r for r in self.coaches_relations if r.coach_id == coach_id), None)
        return {
            "id": f"p-{self.id}_c-{coach_id}",
            "coachId": coach_id,
            "playerId": self.id,
            "levelId": str(rel.level_id) if rel.level_id else None,
            "notes": rel.notes,
            "name": self.user.name,        
            "email": self.user.email,        
            "phone": self.user.phone,        
            "username": self.user.username,
            "side": rel.side,
            "userId": self.user_id,
            "isActive": self.user.status == 'active' 
        }
