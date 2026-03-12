from sqlalchemy import Column, Integer, ForeignKey

from padel_app.sql_db import db


class LessonInstanceTraining(db.Model):
    __tablename__ = "lesson_instance_training"

    lesson_instance_id = Column(
        Integer,
        ForeignKey("lesson_instances.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    exercise_id = Column(
        Integer,
        ForeignKey("exercises.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
