from padel_app.models import (
    Coach,
    CoachLevel,
    EvaluationCategory,
    CoachPlayerNote,
    EvaluationEntry,
    Association_CoachPlayer,
)
from padel_app.services.level_ladder import (
    get_level_ladder,
    is_unordered,
    next_display_order,
    normalize_display_orders,
)
from padel_app.sql_db import db
from padel_app.tools.request_adapter import JsonRequestAdapter

# Ordering convention (specs/levels/spec.md rule 3): lower display_order =
# STRONGER level, so the first entry here is the top of a new coach's ladder.
# These are placeholders the coach renames and reorders in Settings.
DEFAULT_COACH_LEVELS = [
    {"code": "L1", "label": "Level 1", "display_order": 1},
    {"code": "L2", "label": "Level 2", "display_order": 2},
    {"code": "L3", "label": "Level 3", "display_order": 3},
]


def _apply_form(form, payload, element):
    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)
    element.update_with_dict(values)
    return element


def create_default_levels_for_coach(coach):
    """Create the three default skill levels (L1, L2, L3) for a coach.

    Idempotent: does nothing if the coach already has any levels.
    Returns the coach's levels.
    """
    if coach.levels:
        return coach.levels

    for entry in DEFAULT_COACH_LEVELS:
        db.session.add(
            CoachLevel(
                coach_id=coach.id,
                code=entry["code"],
                label=entry["label"],
                display_order=entry["display_order"],
            )
        )
    db.session.commit()
    return coach.levels


def create_coach_service(data):
    coach = Coach()
    form = coach.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach.update_with_dict(values)
    coach.create()
    create_default_levels_for_coach(coach)
    return coach


def _coach_id_from_payload(data):
    raw = data.get("coach") or data.get("coach_id") or data.get("coachId")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if isinstance(raw, CoachLevel):  # defensive; never expected
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return getattr(raw, "id", None)


def create_coach_level_service(data):
    data = dict(data or {})

    # PAD-70: a level created without an explicit position must be APPENDED to
    # the bottom of the ladder. The column default is 0, which the notification
    # engine would otherwise read as the coach's strongest level. Resolved
    # before the form runs — building the object first would attach it to the
    # session and make the lookup query autoflush a half-built row.
    order = data.get("display_order", data.get("displayOrder"))
    if is_unordered(order):
        coach_id = _coach_id_from_payload(data)
        if coach_id:
            data["display_order"] = next_display_order(coach_id)

    coach_level = CoachLevel()
    form = coach_level.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    coach_level.update_with_dict(values)
    coach_level.create()
    return coach_level


def upsert_coach_levels(coach, data):
    """Batch upsert coach levels from a list of entries.

    The submitted list IS the coach's ladder, top (strongest) first. Entries may
    carry an explicit ``displayOrder``; when they don't, their position in the
    list is used. Afterwards the whole ladder is renumbered to a contiguous
    ``1..N`` so gaps, duplicates and unset orders can never reach the
    notification engine (PAD-70).
    """
    for position, entry in enumerate(data, start=1):
        display_order = entry.get("displayOrder")
        if is_unordered(display_order):
            display_order = position
        payload = {
            "code": entry.get("code"),
            "label": entry.get("label"),
            "coach": coach.id,
            "display_order": display_order,
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

    normalize_display_orders(coach.id)
    db.session.commit()


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


def get_coach_levels(coach_id: int) -> list:
    """Fetch the coach's existing levels, ordered strongest → weakest.

    Each ``CoachLevel`` carries:
        - code (str): unique level identifier, e.g. "COMP", "ADV", "INT"
        - label (str): display name, e.g. "Competicao", "Avancado", "Intermedio"
        - display_order (int): ladder position — **lower = stronger**
          (specs/levels/spec.md rule 3), so ``display_order`` 1 is the coach's
          top level, not their beginners.

    Example ladder:
        [
            {"code": "COMP", "label": "Competicao", "display_order": 1},
            {"code": "ADV", "label": "Avancado", "display_order": 2},
            {"code": "INT", "label": "Intermedio", "display_order": 3},
            {"code": "INI", "label": "Iniciacao", "display_order": 4},
        ]
    """

    return get_level_ladder(coach_id)