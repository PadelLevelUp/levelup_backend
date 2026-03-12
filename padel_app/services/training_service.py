from datetime import datetime as _dt
from flask import abort

from padel_app.sql_db import db
from padel_app.models.exercise import Exercise, ExerciseGroup
from padel_app.models.Association_CoachExercise import Association_CoachExercise
from padel_app.models.Association_CoachExerciseGroup import Association_CoachExerciseGroup
from padel_app.models.lesson_instance_training import LessonInstanceTraining


# ---------------------------------------------------------------------------
# Exercises
# ---------------------------------------------------------------------------

def get_exercises_for_coach(coach):
    """Returns all exercises the coach can access (owned + followed)."""
    relations = (
        Association_CoachExercise.query
        .filter_by(coach_id=coach.id)
        .all()
    )
    return [rel.exercise for rel in relations]


def get_exercise_for_coach(coach, exercise_id):
    """Returns a single exercise if the coach has access to it."""
    rel = (
        Association_CoachExercise.query
        .filter_by(coach_id=coach.id, exercise_id=int(exercise_id))
        .first_or_404()
    )
    return rel.exercise


def create_exercise_service(coach, data):
    exercise = Exercise(
        name=data["name"],
        description=data.get("description"),
        type=data["type"],
        custom_type=data.get("customType"),
        difficulty=data.get("difficulty", 3),
        level_ids=data.get("levelIds", []),
        diagram=data.get("diagram"),
        notes=data.get("notes"),
        owner_coach_id=coach.id,
    )
    db.session.add(exercise)
    db.session.flush()

    # Owner association
    rel = Association_CoachExercise(
        coach_id=coach.id,
        exercise_id=exercise.id,
        role="owner",
    )
    db.session.add(rel)
    db.session.commit()
    return exercise


def update_exercise_service(exercise_id, coach, data):
    rel = (
        Association_CoachExercise.query
        .filter_by(coach_id=coach.id, exercise_id=int(exercise_id), role="owner")
        .first()
    )
    if not rel:
        abort(403, "You don't have permission to edit this exercise")

    exercise = rel.exercise
    if "name" in data:
        exercise.name = data["name"]
    if "description" in data:
        exercise.description = data.get("description")
    if "type" in data:
        exercise.type = data["type"]
    if "customType" in data:
        exercise.custom_type = data.get("customType")
    if "difficulty" in data:
        exercise.difficulty = data["difficulty"]
    if "levelIds" in data:
        exercise.level_ids = data["levelIds"]
    if "diagram" in data:
        exercise.diagram = data.get("diagram")
    if "notes" in data:
        exercise.notes = data.get("notes")

    exercise.save()
    return exercise


def delete_exercise_service(exercise_id, coach):
    rel = (
        Association_CoachExercise.query
        .filter_by(coach_id=coach.id, exercise_id=int(exercise_id), role="owner")
        .first()
    )
    if not rel:
        abort(403, "You don't have permission to delete this exercise")

    # Deleting the exercise cascades the association row
    rel.exercise.delete()


# ---------------------------------------------------------------------------
# Exercise Groups
# ---------------------------------------------------------------------------

def get_exercise_groups_for_coach(coach):
    """Returns all exercise groups the coach can access (owned + followed)."""
    relations = (
        Association_CoachExerciseGroup.query
        .filter_by(coach_id=coach.id)
        .all()
    )
    return [rel.exercise_group for rel in relations]


def create_exercise_group_service(coach, data):
    group = ExerciseGroup(
        name=data["name"],
        description=data.get("description"),
        owner_coach_id=coach.id,
    )
    db.session.add(group)
    db.session.flush()

    # Link exercises
    exercise_ids = data.get("exerciseIds", [])
    if exercise_ids:
        exercises = Exercise.query.filter(
            Exercise.id.in_([int(i) for i in exercise_ids])
        ).all()
        group.exercises.extend(exercises)

    # Owner association
    rel = Association_CoachExerciseGroup(
        coach_id=coach.id,
        exercise_group_id=group.id,
        role="owner",
    )
    db.session.add(rel)
    db.session.commit()
    return group


def update_exercise_group_service(group_id, coach, data):
    rel = (
        Association_CoachExerciseGroup.query
        .filter_by(coach_id=coach.id, exercise_group_id=int(group_id), role="owner")
        .first()
    )
    if not rel:
        abort(403, "You don't have permission to edit this group")

    group = rel.exercise_group
    if "name" in data:
        group.name = data["name"]
    if "description" in data:
        group.description = data.get("description")

    # Replace exercise list when provided
    if "exerciseIds" in data:
        exercise_ids = data["exerciseIds"] or []
        exercises = (
            Exercise.query.filter(Exercise.id.in_([int(i) for i in exercise_ids])).all()
            if exercise_ids else []
        )
        group.exercises = exercises

    group.save()
    return group


def delete_exercise_group_service(group_id, coach):
    rel = (
        Association_CoachExerciseGroup.query
        .filter_by(coach_id=coach.id, exercise_group_id=int(group_id), role="owner")
        .first()
    )
    if not rel:
        abort(403, "You don't have permission to delete this group")

    rel.exercise_group.delete()


# ---------------------------------------------------------------------------
# Lesson Instance Training
# ---------------------------------------------------------------------------

def confirm_training_service(class_instance_data, exercise_ids):
    """Materialises a LessonInstance if needed, then replaces its training exercises."""
    from padel_app.models import LessonInstance, Lesson
    from padel_app.services.lesson_service import get_or_materialize_instance

    if 'parentClassId' in class_instance_data:
        instance = LessonInstance.query.get_or_404(int(class_instance_data['originalId']))
    else:
        lesson = Lesson.query.get_or_404(int(class_instance_data['originalId']))
        date = _dt.strptime(class_instance_data['date'], '%Y-%m-%d').date()
        instance = get_or_materialize_instance(lesson, date)

    LessonInstanceTraining.query.filter_by(lesson_instance_id=instance.id).delete()
    for exercise_id in exercise_ids:
        db.session.add(LessonInstanceTraining(
            lesson_instance_id=instance.id,
            exercise_id=int(exercise_id),
        ))
    db.session.commit()

    return LessonInstanceTraining.query.filter_by(lesson_instance_id=instance.id).all()
