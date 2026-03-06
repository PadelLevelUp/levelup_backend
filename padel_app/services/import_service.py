"""
Bulk import service.

All functions are idempotent: they use find-or-create logic so re-running the
same rows produces no duplicates. Each function returns::

    {"imported": N, "errors": [{"row": i, "error": "..."}, ...]}

where ``imported`` counts newly created records and ``errors`` collects any
per-row failures so that a bad row never aborts the entire batch.
"""
from datetime import datetime, date
import re

from psycopg2.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from padel_app.sql_db import db
from padel_app.models import (
    CoachLevel,
    CoachPlayerNote,
    EvaluationCategory,
    EvaluationEntry,
    User,
    Player,
    Association_CoachPlayer,
    Association_PlayerLesson,
    Presence,
    PlayerLevelHistory,
)
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.tools.calendar_tools import build_datetime
from padel_app.services.player_service import create_player_helper
from padel_app.services.lesson_service import (
    create_lesson_helper,
    get_or_materialize_instance,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_form(form, payload, element):
    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)
    element.update_with_dict(values)
    return element


def _ok(imported, errors):
    return {"imported": imported, "errors": errors}


def _sanitize_email_part(value):
    text = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "", text)
    return cleaned or "player"


def _build_fake_email(name, row_idx):
    tokens = [t for t in re.split(r"\s+", str(name or "").strip()) if t]
    first = _sanitize_email_part(tokens[0]) if tokens else "player"
    second = _sanitize_email_part(tokens[1]) if len(tokens) > 1 else f"player{row_idx + 1}"
    return f"{first}_{second}@email.com"


def _resolve_player_email(row, row_idx):
    email = str(row.get("email") or "").strip().lower()
    if email:
        return email

    base_email = _build_fake_email(row.get("name"), row_idx)
    if not User.query.filter_by(email=base_email).first():
        return base_email

    local, domain = base_email.split("@", 1)
    suffix = 2
    while True:
        candidate = f"{local}_{suffix}@{domain}"
        if not User.query.filter_by(email=candidate).first():
            return candidate
        suffix += 1


def _coerce_import_datetime(value):
    if isinstance(value, datetime):
        return value

    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    if value is None:
        return datetime.utcnow()

    text = str(value).strip()
    if not text:
        return datetime.utcnow()

    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.utcnow()


# ---------------------------------------------------------------------------
# Coach levels
# ---------------------------------------------------------------------------

def bulk_create_coach_levels(rows, coach):
    """Find-or-create coach levels. Uniqueness key: (coach, code)."""
    imported = 0
    errors = []

    for i, row in enumerate(rows):
        try:
            code = row.get("code")
            if not code:
                errors.append({"row": i, "error": "Missing required field: code"})
                continue

            existing = (
                CoachLevel.query
                .filter_by(coach_id=coach.id, code=code)
                .first()
            )
            if existing:
                continue

            payload = {
                "code": code,
                "label": row.get("label"),
                "coach": coach.id,
                "display_order": row.get("display_order"),
            }
            level = CoachLevel()
            _apply_form(level.get_create_form(), payload, level)
            level.create()
            imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Evaluation categories
# ---------------------------------------------------------------------------

def bulk_create_evaluation_categories(rows, coach):
    """Find-or-create evaluation categories. Uniqueness key: (coach, name)."""
    imported = 0
    errors = []

    for i, row in enumerate(rows):
        try:
            name = row.get("name")
            if not name:
                errors.append({"row": i, "error": "Missing required field: name"})
                continue

            existing = (
                EvaluationCategory.query
                .filter_by(coach_id=coach.id, name=name)
                .first()
            )
            if existing:
                continue

            payload = {
                "name": name,
                "scale_min": row.get("scale_min"),
                "scale_max": row.get("scale_max"),
                "coach": coach.id,
            }
            category = EvaluationCategory()
            _apply_form(category.get_create_form(), payload, category)
            category.create()
            imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

def bulk_create_players(rows, coach, club):
    """
    Find-or-create players.

    Uniqueness key: user email.

    Missing emails are auto-generated as:
        firstname_secondname@email.com

    - If no user with that email exists → create User + Player +
      Association_CoachPlayer via create_player_helper.
    - If a user with that email exists but has no Player record → create
      Player + Association_CoachPlayer.
    - If the user/player already exists and the coach↔player link already
      exists → skip (idempotent).
    - If the user/player exists but is not yet linked to this coach → create
      only the association.

    Level is matched by ``level_code`` against the coach's existing levels.
    """
    imported = 0
    errors = []

    levels_by_code = {lvl.code: lvl for lvl in coach.levels}

    for i, row in enumerate(rows):
        try:
            email = _resolve_player_email(row, i)

            level_code = row.get("level_code")
            level = levels_by_code.get(level_code) if level_code else None

            existing_user = User.query.filter_by(email=email).first()

            if existing_user:
                # Resolve or create the Player record for this user.
                player = existing_user.player
                if not player:
                    player = Player(user_id=existing_user.id)
                    player.create()

                # Check if the coach↔player association already exists.
                rel = Association_CoachPlayer.query.filter_by(
                    coach_id=coach.id,
                    player_id=player.id,
                ).first()
                if rel:
                    continue  # Already fully linked — nothing to do.

                # Create only the missing association.
                rel_payload = {
                    "coach": coach.id,
                    "player": player.id,
                    "level": level.id if level else None,
                    "side": row.get("side"),
                    "notes": None,
                }
                rel = Association_CoachPlayer()
                _apply_form(rel.get_create_form(), rel_payload, rel)
                rel.create()

                if level:
                    PlayerLevelHistory(
                        coach_id=coach.id,
                        player_id=player.id,
                        level_id=level.id,
                    ).create()

            else:
                # Full creation: User + Player + Association_CoachPlayer.
                # Derive a username from the email prefix when not supplied.
                username = email.split("@")[0]
                payload = {
                    "coach": coach.id,
                    "level": level.id if level else None,
                    "side": row.get("side"),
                    "notes": None,
                    "user": {
                        "name": row.get("name"),
                        "email": email,
                        "phone": row.get("phone"),
                        "username": username,
                    },
                }
                create_player_helper(payload)

            imported += 1

        except IntegrityError as e:
            db.session.rollback()
            if isinstance(e.orig, UniqueViolation) and "username" in str(e.orig):
                name = row.get("name", "Unknown")
                errors.append({"row": i, "error": f"Player '{name}' already exists with a different email — skipped."})
            else:
                errors.append({"row": i, "data": row, "error": str(e.orig)})
        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

def bulk_create_lessons(rows, coach, club):
    """
    Find-or-create lessons. Uniqueness key: (coach, title).

    ``day`` is expected to be a date string in YYYY-MM-DD format used as the
    lesson's anchor start date. ``start_time`` and ``end_time`` are HH:MM
    strings.
    """
    if club is None:
        return _ok(0, [{"error": "Coach has no associated club — cannot import classes"}])

    imported = 0
    errors = []

    # Build an in-memory set of existing lesson titles for this coach.
    existing_titles = {rel.lesson.title for rel in coach.lessons_relations}

    for i, row in enumerate(rows):
        try:
            title = row.get("title")
            if not title:
                errors.append({"row": i, "error": "Missing required field: title"})
                continue

            if title in existing_titles:
                continue

            day = row.get("day")
            if not day:
                errors.append({"row": i, "error": "Missing required field: day"})
                continue

            payload = {
                "title": title,
                "type": row.get("type") or "academy",
                "status": "active",
                "is_recurring": row.get("is_recurring", False),
                "start_datetime": build_datetime(day, row.get("start_time")),
                "end_datetime": build_datetime(day, row.get("end_time")),
                "max_players": row.get("max_players") or 5,
                "color": row.get("color") or "#3B82F6",
                "club": club.id,
                "coach": coach.id,
            }
            create_lesson_helper(payload)
            existing_titles.add(title)  # Prevent duplicate within the same batch.
            imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Player↔lesson associations
# ---------------------------------------------------------------------------

def bulk_create_player_lesson_associations(rows, coach):
    """
    Find-or-create Association_PlayerLesson records.

    Uniqueness key: (lesson, player). Rows reference lesson by title and
    player by name — both are resolved within the coach's existing records.
    If either cannot be found the row is skipped and recorded as an error.
    """
    imported = 0
    errors = []

    # Build lookups scoped to this coach.
    lessons_by_title = {}
    for rel in coach.lessons_relations:
        lessons_by_title.setdefault(rel.lesson.title, rel.lesson)

    players_by_name = {}
    for rel in coach.players_relations:
        players_by_name.setdefault(rel.player.user.name, rel.player)

    for i, row in enumerate(rows):
        try:
            lesson_title = row.get("lesson_title")
            player_name = row.get("player_name")

            lesson = lessons_by_title.get(lesson_title)
            if not lesson:
                errors.append({"row": i, "error": f"Lesson not found: {lesson_title!r}"})
                continue

            player = players_by_name.get(player_name)
            if not player:
                errors.append({"row": i, "error": f"Player not found: {player_name!r}"})
                continue

            existing = Association_PlayerLesson.query.filter_by(
                lesson_id=lesson.id,
                player_id=player.id,
            ).first()
            if existing:
                continue

            Association_PlayerLesson(
                lesson_id=lesson.id,
                player_id=player.id,
            ).create()
            imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Presences
# ---------------------------------------------------------------------------

def bulk_create_presences(rows, coach):
    """
    Find-or-create Presence records.

    Uniqueness key: (lesson_instance, player). The lesson instance is
    materialised on demand for the given date (same logic as confirming
    presences in the app). If a presence already exists its status and
    justification are updated so the import is always idempotent.

    Row keys: lesson_title, date (YYYY-MM-DD), player_name, status,
    justification.
    """
    imported = 0
    errors = []

    lessons_by_title = {}
    for rel in coach.lessons_relations:
        lessons_by_title.setdefault(rel.lesson.title, rel.lesson)

    players_by_name = {}
    for rel in coach.players_relations:
        players_by_name.setdefault(rel.player.user.name, rel.player)

    for i, row in enumerate(rows):
        try:
            lesson_title = row.get("lesson_title")
            player_name = row.get("player_name")
            date_str = row.get("date")

            lesson = lessons_by_title.get(lesson_title)
            if not lesson:
                errors.append({"row": i, "error": f"Lesson not found: {lesson_title!r}"})
                continue

            player = players_by_name.get(player_name)
            if not player:
                errors.append({"row": i, "error": f"Player not found: {player_name!r}"})
                continue

            if not date_str:
                errors.append({"row": i, "error": "Missing required field: date"})
                continue

            date = datetime.strptime(date_str, "%Y-%m-%d").date()
            instance = get_or_materialize_instance(lesson, date)

            existing = Presence.query.filter_by(
                lesson_instance_id=instance.id,
                player_id=player.id,
            ).first()

            status = row.get("status") or None
            justification = row.get("justification") or None

            if existing:
                # Update status/justification on re-import.
                existing.status = status
                existing.justification = justification
                existing.validated = True
                existing.save()
            else:
                Presence(
                    player_id=player.id,
                    lesson_instance_id=instance.id,
                    status=status,
                    justification=justification,
                    invited=True,
                    confirmed=True,
                    validated=True,
                ).create()

            imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Evaluation entries
# ---------------------------------------------------------------------------

def bulk_create_evaluation_entries(rows, coach):
    """
    Create evaluation entries for one or more categories per row.

    Supports two formats:

    *Wide format* (default): player_name, date (YYYY-MM-DD), <category_name>: <score>, ...
    Every key that is not ``player_name`` or ``date`` is treated as a category
    name whose value is the numeric score. Blank / None values are skipped.

    *Normalized format* (AI output): player_name, date (YYYY-MM-DD), category_name, score
    Detected automatically when rows contain ``category_name`` and ``score`` keys.
    Each row produces exactly one entry.

    ``imported`` counts the number of rows that produced at least one new entry.
    """
    imported = 0
    errors = []

    # coach_player relations indexed by player display name.
    coach_players_by_name = {}
    for rel in coach.players_relations:
        coach_players_by_name.setdefault(rel.player.user.name, rel)

    categories_by_name = {c.name: c for c in coach.evaluation_categories}

    # Detect format from the first row.
    is_normalized = bool(rows) and ("category_name" in rows[0] and "score" in rows[0])

    reserved_keys = {"player_name", "date"}

    for i, row in enumerate(rows):
        try:
            player_name = row.get("player_name")
            coach_player = coach_players_by_name.get(player_name)
            if not coach_player:
                errors.append({"row": i, "error": f"Player not found: {player_name!r}"})
                continue

            evaluated_at = _coerce_import_datetime(row.get("date"))

            if is_normalized:
                # Each row = one (category_name, score) pair.
                category_name = row.get("category_name")
                value = row.get("score")

                if not category_name or value is None or value == "":
                    errors.append({"row": i, "error": "Missing category_name or score"})
                    continue

                category = categories_by_name.get(category_name)
                if not category:
                    errors.append({"row": i, "error": f"Category not found: {category_name!r}"})
                    continue

                try:
                    score = float(value)
                except (ValueError, TypeError):
                    errors.append({"row": i, "error": f"Invalid score: {value!r}"})
                    continue

                ev_payload = {
                    "coach_player": coach_player.id,
                    "category": category.id,
                    "score": score,
                    "evaluated_at": evaluated_at,
                }
                entry = EvaluationEntry()
                _apply_form(entry.get_create_form(), ev_payload, entry)
                entry.create()
                imported += 1

            else:
                # Wide format: non-reserved keys are category names.
                row_imported = 0
                for key, value in row.items():
                    if key in reserved_keys or value is None or value == "":
                        continue

                    category = categories_by_name.get(key)
                    if not category:
                        errors.append({"row": i, "error": f"Category not found: {key!r}"})
                        continue

                    try:
                        score = float(value)
                    except (ValueError, TypeError):
                        errors.append({"row": i, "error": f"Invalid score for {key!r}: {value!r}"})
                        continue

                    ev_payload = {
                        "coach_player": coach_player.id,
                        "category": category.id,
                        "score": score,
                        "evaluated_at": evaluated_at,
                    }
                    entry = EvaluationEntry()
                    _apply_form(entry.get_create_form(), ev_payload, entry)
                    entry.create()
                    row_imported += 1

                if row_imported > 0:
                    imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)


# ---------------------------------------------------------------------------
# Coach player notes (strengths / weaknesses)
# ---------------------------------------------------------------------------

def bulk_create_coach_notes(rows, coach, note_type):
    """
    Find-or-create CoachPlayerNote records of a given type.

    ``note_type`` must be ``"strength"`` or ``"weakness"``.

    The AI returns one row per player with a comma-separated string in the
    key matching ``note_type`` (e.g. ``{"player_name": "Ana", "strengths":
    "Boa direita, boa mobilidade"}``). Each comma-separated item becomes one
    note. Duplicates (same coach_player + type + text) are silently skipped.
    """
    imported = 0
    errors = []

    coach_players_by_name = {}
    for rel in coach.players_relations:
        coach_players_by_name.setdefault(rel.player.user.name, rel)

    # The column name in the AI row.
    col_by_type = {
        "strength": "strengths",
        "weakness": "weaknesses",
    }
    col = col_by_type.get(note_type, f"{note_type}s")

    for i, row in enumerate(rows):
        try:
            player_name = row.get("player_name")
            coach_player = coach_players_by_name.get(player_name)
            if not coach_player:
                errors.append({"row": i, "error": f"Player not found: {player_name!r}"})
                continue

            raw = (
                row.get(col)
                or row.get(note_type)
                or row.get(f"{note_type}s")
                or ""
            )
            texts = [t.strip() for t in str(raw).split(",") if t.strip()]
            if not texts:
                continue

            existing_texts = {
                n.text
                for n in coach_player.notes_list
                if n.type == note_type
            }

            row_imported = 0
            for text in texts:
                if text in existing_texts:
                    continue
                note = CoachPlayerNote()
                _apply_form(note.get_create_form(), {
                    "coach_player": coach_player.id,
                    "type": note_type,
                    "text": text,
                }, note)
                note.create()
                existing_texts.add(text)
                row_imported += 1

            if row_imported > 0:
                imported += 1

        except Exception as e:
            db.session.rollback()
            errors.append({"row": i, "data": row, "error": str(e)})

    return _ok(imported, errors)
