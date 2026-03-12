def serialize_exercise(exercise):
    return {
        "id": str(exercise.id),
        "name": exercise.name,
        "description": exercise.description,
        "type": exercise.type,
        "customType": exercise.custom_type,
        "difficulty": exercise.difficulty,
        "levelIds": exercise.level_ids or [],
        "diagram": exercise.diagram,
        "notes": exercise.notes,
        "createdAt": exercise.created_at.isoformat() if exercise.created_at else None,
        "updatedAt": exercise.updated_at.isoformat() if exercise.updated_at else None,
    }


def serialize_exercise_group(group):
    return {
        "id": str(group.id),
        "name": group.name,
        "description": group.description,
        "exerciseIds": [str(ex.id) for ex in group.exercises],
        "createdAt": group.created_at.isoformat() if group.created_at else None,
        "updatedAt": group.updated_at.isoformat() if group.updated_at else None,
    }
