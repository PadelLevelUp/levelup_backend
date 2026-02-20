from padel_app.models import (
    Coach,
    CoachLevel,
    EvaluationCategory,
    CoachPlayerNote,
    EvaluationEntry,
    Association_CoachPlayer,
)
from padel_app.tools.request_adapter import JsonRequestAdapter


def _apply_form(form, payload, element):
    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)
    element.update_with_dict(values)
    return element


def create_coach_service(data):
    coach = Coach()
    form = coach.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach.update_with_dict(values)
    coach.create()
    return coach


def create_coach_level_service(data):
    coach_level = CoachLevel()
    form = coach_level.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach_level.update_with_dict(values)
    coach_level.create()
    return coach_level


def upsert_coach_levels(coach, data):
    """Batch upsert coach levels from a list of entries."""
    for entry in data:
        payload = {
            "code": entry.get("code"),
            "label": entry.get("label"),
            "coach": coach.id,
            "display_order": entry.get("displayOrder"),
        }
        coach_level = (
            CoachLevel.query
            .filter(CoachLevel.coach_id == coach.id)
            .filter(CoachLevel.code == payload["code"])
            .first()
        )
        if coach_level:
            _apply_form(coach_level.get_edit_form(), payload, coach_level)
            coach_level.save()
        else:
            coach_level = CoachLevel()
            _apply_form(coach_level.get_create_form(), payload, coach_level)
            coach_level.create()


def upsert_evaluation_categories(coach, data):
    """Batch upsert evaluation categories from a list of entries."""
    for entry in data:
        payload = {
            'name': entry.get("name"),
            'scale_min': entry.get("scaleMin"),
            'scale_max': entry.get("scaleMax"),
            "coach": coach.id,
        }
        evaluation_category = (
            EvaluationCategory.query
            .filter(EvaluationCategory.coach_id == coach.id)
            .filter(EvaluationCategory.name == payload["name"])
            .first()
        )
        if evaluation_category:
            _apply_form(evaluation_category.get_edit_form(), payload, evaluation_category)
            evaluation_category.save()
        else:
            evaluation_category = EvaluationCategory()
            _apply_form(evaluation_category.get_create_form(), payload, evaluation_category)
            evaluation_category.create()


def add_coach_note_service(coach, data):
    """Creates a coach note (strength/weakness). Returns (result_dict, status_code)."""
    player_id = data.get("playerId")
    note_type = data.get("type")
    text = data.get("text", "").strip()

    if not text:
        return {"error": "text is required"}, 400

    if note_type not in ("strength", "weakness"):
        return {"error": "type must be 'strength' or 'weakness'"}, 400

    coach_player = (
        Association_CoachPlayer.query
        .filter_by(coach_id=coach.id, player_id=player_id)
        .first_or_404()
    )

    note = CoachPlayerNote()
    _apply_form(note.get_create_form(), {
        "coach_player": coach_player.id,
        "type": note_type,
        "text": text,
    }, note)
    note.create()

    return {"status": "ok", "id": note.id, "type": note_type, "text": note.text}, 200


def add_evaluation_entry_service(coach, data):
    """Records evaluation scores and notes for a player."""
    player_id = data.get("playerId")
    scores = data.get("scores", [])
    strengths = data.get("strengths", [])
    weaknesses = data.get("weaknesses", [])

    coach_player = (
        Association_CoachPlayer.query
        .filter_by(coach_id=coach.id, player_id=player_id)
        .first_or_404()
    )

    for score in scores:
        ev_payload = {
            "coach_player": coach_player.id,
            "category": score.get("categoryId"),
            "score": score.get("value"),
        }
        entry = EvaluationEntry()
        _apply_form(entry.get_create_form(), ev_payload, entry)
        entry.create()

    existing_strengths = {n.text for n in coach_player.strengths}
    for item in strengths:
        text = item.get("text") if isinstance(item, dict) else item
        if text in existing_strengths:
            continue
        note = CoachPlayerNote()
        _apply_form(note.get_create_form(), {
            "coach_player": coach_player.id,
            "type": "strength",
            "text": text,
        }, note)
        note.create()

    existing_weaknesses = {n.text for n in coach_player.weaknesses}
    for item in weaknesses:
        text = item.get("text") if isinstance(item, dict) else item
        if text in existing_weaknesses:
            continue
        note = CoachPlayerNote()
        _apply_form(note.get_create_form(), {
            "coach_player": coach_player.id,
            "type": "weakness",
            "text": text,
        }, note)
        note.create()

    return {"status": "ok", "playerId": player_id}
