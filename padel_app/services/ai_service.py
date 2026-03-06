"""
AI-powered Excel import analysis service for padel coaching.

Pipeline:
    1. parse_excel              → sheet → raw rows
    2. pick_relevant_sheets     → LLM filters irrelevant sheets
    3. detect_table_segments    → find multiple tables within a sheet
    4. column-map all segments  → LLM maps columns to target schemas
    5. ordered validation chain → business rules enforced programmatically
    6. stream_import_analysis   → orchestrate and yield SSE events

Design: LLM handles ambiguous tasks (column mapping, level matching, name
validation). Business rules are enforced deterministically in validators.
"""
from __future__ import annotations

import copy
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Generator

from padel_app.helpers.llm import call_llm, log_timing, logger, parse_json
from padel_app.helpers.parsing import (
    TableSegment,
    detect_table_segments,
    parse_excel,
    pick_relevant_sheets,
)
from padel_app.helpers.text import (
    clean_text_value,
    deduplicate_rows,
    deep_copy_rows,
    fuzzy_match_category,
    has_meaningful_text,
    is_empty,
    is_garbage_category,
    is_numeric,
    merge_table_rows,
    normalize_date,
    normalize_status,
    normalize_text,
    normalize_time,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALL_IMPORTABLE_TABLES: list[str] = [
    "Players", "Classes", "Players in Classes", "Presences",
    "Evaluations", "Strengths", "Weaknesses",
]
_AUTO_TABLES: frozenset[str] = frozenset({"Coach Levels", "Evaluation Categories"})

_TABLE_FIELDS: dict[str, str] = {
    "Coach Levels": "code, label, display_order",
    "Evaluation Categories": "name, scale_min, scale_max",
    "Players": "name, email, phone, level_code, side",
    "Classes": "title, type (academy/private), is_recurring, day (YYYY-MM-DD), start_time (HH:MM), end_time (HH:MM), max_players. NOTE: if only one time/hour column exists, map it to start_time (end_time will be inferred).",
    "Players in Classes": "lesson_title, player_name",
    "Presences": "lesson_title, date (YYYY-MM-DD), player_name, status (P/present/A/absent/FJ/FI), justification, start_time (HH:MM), end_time (HH:MM). NOTE: if only one time/hour column exists, map it to start_time.",
    "Evaluations": "player_name, date (YYYY-MM-DD), category_name, score",
    "Strengths": "player_name, strengths",
    "Weaknesses": "player_name, weaknesses",
}


# ---------------------------------------------------------------------------
# Category registry
# ---------------------------------------------------------------------------


class CategoryRegistry:
    """Single source of truth for evaluation category tracking."""

    def __init__(self) -> None:
        self._by_norm: dict[str, str] = {}
        self._rows: list[dict] = []

    @property
    def known(self) -> dict[str, str]:
        return dict(self._by_norm)

    @property
    def rows(self) -> list[dict]:
        return list(self._rows)

    def register(self, name: str, scale_min: float = 0, scale_max: float = 10) -> bool:
        norm = normalize_text(name)
        if norm in self._by_norm or fuzzy_match_category(name, self._by_norm):
            return False
        self._by_norm[norm] = name
        self._rows.append({"name": name, "scale_min": scale_min, "scale_max": scale_max})
        return True

    def bulk_register(self, cat_rows: list[dict]) -> None:
        for row in cat_rows:
            name = row.get("name")
            if not is_empty(name):
                self.register(str(name).strip(), row.get("scale_min", 0), row.get("scale_max", 10))

    def discover_from_evaluations(self, raw_evals: list[dict]) -> int:
        added = 0
        for ev in raw_evals:
            cat = ev.get("category_name")
            if is_empty(cat):
                continue
            cat_str = str(cat).strip()
            if not is_garbage_category(cat_str) and self.register(cat_str):
                added += 1
        return added

    def merge_discovered(self, updated: dict[str, str]) -> None:
        for norm, canonical in updated.items():
            if norm not in self._by_norm and not is_garbage_category(canonical):
                self._by_norm[norm] = canonical
                self._rows.append({"name": canonical, "scale_min": 0, "scale_max": 10})

    def deduplicate(self) -> None:
        seen: dict[str, str] = {}
        deduped: list[dict] = []
        for row in self._rows:
            name = row.get("name", "")
            if fuzzy_match_category(name, seen):
                continue
            seen[normalize_text(name)] = name
            deduped.append(row)
        self._rows = deduped
        self._by_norm = {normalize_text(r["name"]): r["name"] for r in deduped}


# ---------------------------------------------------------------------------
# Step 4 — Column mapping (LLM)
# ---------------------------------------------------------------------------


def _column_profile(rows: list[dict]) -> dict[str, list]:
    result: dict[str, list] = {}
    for header in (rows[0].keys() if rows else []):
        seen: list[str] = []
        for row in rows:
            v = row.get(header)
            if v is not None and str(v) not in seen:
                seen.append(str(v))
            if len(seen) >= 15:
                break
        result[header] = seen
    return result


def _infer_column_mapping(segment: TableSegment, active_tables: set[str]) -> dict:
    t = time.perf_counter()
    profile = _column_profile(segment.data_rows)
    tables_section = "\n".join(
        f'- "{t_}": {_TABLE_FIELDS[t_]}' for t_ in _TABLE_FIELDS if t_ in active_tables
    )
    prompt = f"""You are a data import assistant for a padel coaching app.

I have a table segment from Excel sheet "{segment.sheet_name}" with {len(segment.data_rows)} rows.
Column names and up to 15 unique sample values per column:

{json.dumps(profile, default=str, indent=2)}

Map this to ONE OR MORE of the following tables.
Return a JSON object with key "mappings" containing an array of mapping objects.

STANDARD mapping (for flat tables):
- "table_name": one of the table names below
- "columns": object mapping target_field to source_column_name (or null)
- "value_mappings": object mapping target_field to {{original_value: normalised_value}}

PIVOT mapping (for evaluation/scoring sheets where columns ARE categories):
- "table_name": "Evaluations"
- "pivot": true
- "player_column": "<column containing player names>"
- "date_column": "<column containing dates, or null>"
- "category_columns": ["<col1>", ...] — ONLY columns with numeric scores

Also include a SEPARATE mapping for Evaluation Categories with pivot:
- "table_name": "Evaluation Categories"
- "extract_from_pivot": true
- "category_names": ["<same category column names>"]

ONLY map to these tables:
{tables_section}

If this segment contains level or category data, include mappings with
"extract_unique_from" naming the source column.

Example standard: {{"mappings": [{{"table_name": "Players", "columns": {{"name": "Nome"}}, "value_mappings": {{}}}}]}}
Example pivot: {{"mappings": [{{"table_name": "Evaluations", "pivot": true, "player_column": "Aluno", "date_column": null, "category_columns": ["Tecnica", "Tatica"]}}]}}"""

    content = call_llm(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024, json_mode=True, label="column_map",
        segment=segment.label, rows=len(segment.data_rows),
    )
    result = parse_json(content, f"column_map({segment.label})")
    log_timing("column_map", t, segment=segment.label, tables=len(result.get("mappings", [])))
    return result


def _apply_standard_mapping(rows: list[dict], mapping: dict) -> tuple[str, list[dict]]:
    table_name: str = mapping.get("table_name", "")
    columns: dict = mapping.get("columns", {})
    value_mappings: dict = mapping.get("value_mappings", {})

    if mapping.get("extract_from_pivot"):
        return table_name, [
            {"name": str(n).strip(), "scale_min": 0, "scale_max": 10}
            for n in mapping.get("category_names", [])
            if str(n).strip() and not is_numeric(str(n)) and len(str(n).strip()) >= 2
        ]

    if extract_col := mapping.get("extract_unique_from"):
        seen: set[str] = set()
        out = []
        for row in rows:
            val = row.get(extract_col)
            if val is not None:
                vs = str(val).strip()
                if vs and vs not in seen:
                    seen.add(vs)
                    rec = {f: vs for f, sc in columns.items() if sc}
                    if table_name == "Coach Levels":
                        rec.setdefault("display_order", len(out) + 1)
                    out.append(rec)
        return table_name, out

    out = []
    for row in rows:
        rec: dict = {}
        for field, src in columns.items():
            if not src:
                continue
            val = row.get(src)
            if hasattr(val, "strftime"):
                val = val.strftime("%Y-%m-%d")
            elif hasattr(val, "isoformat"):
                val = str(val)
            elif val is not None and not isinstance(val, (int, float, bool)):
                val = str(val)
            if field in value_mappings and val is not None:
                val = value_mappings[field].get(str(val), val)
            rec[field] = val
        out.append(rec)
    return table_name, out


def _apply_pivot_mapping(rows: list[dict], mapping: dict) -> tuple[str, list[dict]]:
    player_col = mapping.get("player_column", "")
    date_col = mapping.get("date_column")
    cat_cols = mapping.get("category_columns", [])
    if not player_col or not cat_cols:
        return mapping.get("table_name", "Evaluations"), []

    out = []
    for row in rows:
        player = row.get(player_col)
        if is_empty(player):
            continue
        name = str(player).strip()
        date_val = normalize_date(row.get(date_col)) if date_col else None
        for cc in cat_cols:
            score = row.get(cc)
            if is_empty(score) or not is_numeric(score):
                continue
            out.append({"player_name": name, "date": date_val, "category_name": str(cc).strip(), "score": float(score)})
    return mapping.get("table_name", "Evaluations"), out


def _process_segment(segment: TableSegment, active_tables: set[str]) -> dict[str, list[dict]]:
    t = time.perf_counter()
    try:
        resp = _infer_column_mapping(segment, active_tables)
    except ValueError:
        logger.exception("[AI] Column mapping failed: '%s'", segment.label)
        return {}

    mappings = resp.get("mappings", [])
    if not mappings and "table_name" in resp:
        mappings = [resp]

    result: dict[str, list[dict]] = {}
    for m in mappings:
        tbl, rows = (_apply_pivot_mapping(segment.data_rows, m) if m.get("pivot")
                     else _apply_standard_mapping(segment.data_rows, m))
        if tbl and rows:
            result.setdefault(tbl, []).extend(rows)

    log_timing("process_segment", t, segment=segment.label, records=sum(len(v) for v in result.values()))
    return result


# ---------------------------------------------------------------------------
# Step 5 — Validators
# ---------------------------------------------------------------------------


def _map_coach_levels(raw_levels, raw_players, existing_levels):
    t = time.perf_counter()
    raw_values: set[str] = set()
    for r in raw_levels:
        for f in ("code", "label"):
            v = r.get(f)
            if not is_empty(v):
                raw_values.add(str(v).strip())
    for p in raw_players:
        lc = p.get("level_code")
        if not is_empty(lc):
            raw_values.add(str(lc).strip())

    if not raw_values or not existing_levels:
        log_timing("map_levels", t, status="empty")
        return [], {}

    existing_info = [{"code": l.get("code"), "label": l.get("label")} for l in existing_levels]
    existing_codes = {l.get("code") for l in existing_levels}

    prompt = f"""You are a data import assistant for a padel coaching app.

Map level values from Excel to existing DB levels.

Excel values: {json.dumps(sorted(raw_values))}
DB levels: {json.dumps(existing_info, indent=2)}

RULES:
- Map by meaning/similarity (e.g. "Iniciacao" -> "INI").
- Map to null if no match. Multiple values CAN map to the same code.
- ONLY use codes from the DB list.

Return: {{"mapping": {{"<excel_value>": "<db_code or null>"}}}}"""

    content = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=1024, json_mode=True, label="map_levels")
    raw_map = parse_json(content, "map_levels").get("mapping", {})
    level_map = {k: v for k, v in raw_map.items() if v and v in existing_codes}
    matched = [l for l in existing_levels if l.get("code") in set(level_map.values())]
    log_timing("map_levels", t, raw=len(raw_values), mapped=len(level_map))
    return matched, level_map


_VALID_SIDES = frozenset({"left", "right", "both", "esquerda", "direita", "ambos"})
_SIDE_NORMALIZE = {
    "left": "left", "esquerda": "left", "esq": "left", "l": "left",
    "right": "right", "direita": "right", "dir": "right", "r": "right",
    "both": "both", "ambos": "both", "ambas": "both",
}


def _validate_players(rows, level_mapping):
    rows = deep_copy_rows(rows)
    clean = []
    for row in rows:
        if is_empty(row.get("name")) and is_empty(row.get("email")):
            continue
        raw_lv = row.get("level_code")
        if raw_lv and level_mapping:
            row["level_code"] = level_mapping.get(str(raw_lv))
        # Validate side: must be a known enum value, otherwise null it out.
        raw_side = row.get("side")
        if not is_empty(raw_side):
            side_lower = str(raw_side).strip().lower()
            row["side"] = _SIDE_NORMALIZE.get(side_lower)
        else:
            row["side"] = None
        clean.append(row)

    if len(clean) < 2:
        return clean

    by_name: dict[str, dict] = {}
    unnamed: list[dict] = []
    for row in clean:
        name = row.get("name")
        if is_empty(name):
            unnamed.append(row)
            continue
        key = normalize_text(str(name))
        if key not in by_name:
            by_name[key] = row
        else:
            for col, val in row.items():
                if val is not None and (not isinstance(val, str) or val.strip()):
                    if is_empty(by_name[key].get(col)):
                        by_name[key][col] = val

    result = list(by_name.values()) + unnamed
    if len(result) < len(clean):
        logger.info("[AI] Player dedup: %d -> %d", len(clean), len(result))
    return result


def _validate_eval_categories(rows, raw_evaluations):
    rows = deep_copy_rows(rows)
    candidates = []
    for row in rows:
        name = row.get("name")
        if is_empty(name):
            continue
        ns = str(name).strip()
        if not is_garbage_category(ns) and ns not in candidates:
            candidates.append(ns)

    if not candidates:
        return []

    score_ranges: dict[str, tuple[float, float]] = {}
    for ev in raw_evaluations:
        cat, score = ev.get("category_name"), ev.get("score")
        if is_empty(cat) or not is_numeric(score):
            continue
        cn = normalize_text(str(cat))
        s = float(score)
        lo, hi = score_ranges.get(cn, (s, s))
        score_ranges[cn] = (min(lo, s), max(hi, s))

    return [
        {"name": n, "scale_min": score_ranges.get(normalize_text(n), (0, 10))[0],
         "scale_max": score_ranges.get(normalize_text(n), (0, 10))[1]}
        for n in candidates
    ]


_VALID_CLASS_TYPES = frozenset({"academy", "private"})
_DEFAULT_START = "10:00"
_DEFAULT_END = "11:30"
_DEFAULT_DURATION_MINUTES = 90


def _infer_end_time(start: str) -> str:
    """Given a start time HH:MM, add 1h30 to get end time."""
    try:
        h, m = int(start[:2]), int(start[3:5])
        total = h * 60 + m + _DEFAULT_DURATION_MINUTES
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"
    except (ValueError, IndexError):
        return _DEFAULT_END


def _validate_classes(rows):
    rows = deep_copy_rows(rows)
    clean = []
    for row in rows:
        day = normalize_date(row.get("day"))
        if not day:
            continue
        start = normalize_time(row.get("start_time"))
        end = normalize_time(row.get("end_time"))
        # If only start_time, infer end as +1h30.
        if start and not end:
            end = _infer_end_time(start)
        # If no times at all, use defaults.
        if not start:
            start = _DEFAULT_START
            end = _DEFAULT_END
        # Sanity: times must look like HH:MM, not dates that slipped through.
        if len(start) > 5 or len(end) > 5:
            continue
        row["day"], row["start_time"], row["end_time"] = day, start, end
        # Validate class type — always ensure a valid value.
        raw_type = row.get("type")
        if not is_empty(raw_type) and str(raw_type).strip().lower() in _VALID_CLASS_TYPES:
            row["type"] = str(raw_type).strip().lower()
        else:
            row["type"] = "academy"
        if is_empty(row.get("title")):
            row["title"] = f"Class {day} {start}-{end}"
        else:
            row["title"] = str(row["title"]).strip()
        clean.append(row)
    return clean


def _validate_presences(rows, known_players):
    rows = deep_copy_rows(rows)
    clean, dropped = [], 0
    for row in rows:
        player = row.get("player_name")
        if is_empty(player) or str(player).strip().lower() not in known_players:
            dropped += 1; continue
        raw_st = row.get("status")
        if is_empty(raw_st):
            dropped += 1; continue
        status, just = normalize_status(str(raw_st))
        if status is None:
            dropped += 1; continue
        row["status"] = status
        existing_just = row.get("justification")
        if just and is_empty(existing_just):
            row["justification"] = just
        elif not is_empty(existing_just):
            jl = str(existing_just).strip().lower()
            if jl in ("justified", "unjustified", "justificada", "injustificada"):
                row["justification"] = "justified" if "just" in jl and "in" not in jl else "unjustified"
        date = normalize_date(row.get("date"))
        if not date:
            dropped += 1; continue
        row["date"] = date
        for tf in ("start_time", "end_time"):
            row[tf] = normalize_time(row.get(tf))
        # Infer missing times: start only → end = +1h30. Neither → defaults.
        if row["start_time"] and not row["end_time"]:
            row["end_time"] = _infer_end_time(row["start_time"])
        if not row["start_time"]:
            row["start_time"] = _DEFAULT_START
            row["end_time"] = _DEFAULT_END
        clean.append(row)
    return clean, dropped


def _validate_evaluations(rows, known_players, known_categories):
    rows = deep_copy_rows(rows)
    cats = dict(known_categories)
    clean, dropped = [], 0
    for row in rows:
        player = row.get("player_name")
        if is_empty(player) or str(player).strip().lower() not in known_players:
            dropped += 1; continue
        cat = row.get("category_name")
        if is_empty(cat):
            dropped += 1; continue
        cs = str(cat).strip()
        if is_garbage_category(cs):
            dropped += 1; continue
        canonical = fuzzy_match_category(cs, cats)
        if canonical:
            row["category_name"] = canonical
        else:
            cats[normalize_text(cs)] = cs
        score = row.get("score")
        if not is_numeric(score):
            dropped += 1; continue
        row["score"] = float(score)
        clean.append(row)
    return clean, dropped, cats


def _validate_players_in_classes(rows, known_players, known_titles):
    rows = deep_copy_rows(rows)
    clean, dropped = [], 0
    for row in rows:
        p = row.get("player_name")
        t = row.get("lesson_title")
        if is_empty(p) or str(p).strip().lower() not in known_players:
            dropped += 1; continue
        if is_empty(t) or str(t).strip().lower() not in known_titles:
            dropped += 1; continue
        clean.append(row)
    return clean, dropped


def _validate_player_linked(rows, known_players, text_field=None):
    rows = deep_copy_rows(rows)
    clean, dropped = [], 0
    for row in rows:
        p = row.get("player_name")
        if is_empty(p) or str(p).strip().lower() not in known_players:
            dropped += 1; continue
        if text_field:
            if not has_meaningful_text(row.get(text_field)):
                dropped += 1; continue
            row[text_field] = clean_text_value(str(row[text_field]))
        clean.append(row)
    return clean, dropped


def _validate_names_with_llm(names):
    if not names:
        return set()
    t = time.perf_counter()
    prompt = f"""I have a list of values that should be padel player names.
Some may be data errors. Return: {{"valid_names": ["name1", ...]}}
Values: {json.dumps(sorted(names))}"""
    content = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=2048, json_mode=True, label="validate_names")
    valid = set(parse_json(content, "validate_names").get("valid_names", []))
    log_timing("validate_names", t, input=len(names), valid=len(valid))
    return valid


def _discover_players(raw_analysis, known_names):
    refs: set[str] = set()
    for tbl in ("Presences", "Evaluations", "Strengths", "Weaknesses", "Players in Classes"):
        for row in raw_analysis.get(tbl, []):
            n = row.get("player_name")
            if not is_empty(n):
                refs.add(str(n).strip())
    unknown = {n for n in refs if n.lower() not in known_names}
    if not unknown:
        return []
    valid = _validate_names_with_llm(unknown)
    return [{"name": n, "email": None, "phone": None, "level_code": None, "side": None} for n in valid]


def _build_lesson_instances(presences, existing_classes, known_players):
    t = time.perf_counter()
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in presences:
        d, s = p.get("date", ""), p.get("start_time", "")
        groups[f"{d}_{s}" if s else d].append(p)

    existing_by_key = {}
    for c in existing_classes:
        day, start, title = c.get("day", ""), c.get("start_time", ""), c.get("title", "")
        if day:
            existing_by_key[f"{day}_{start}" if start else day] = title

    mapping, unmatched = {}, []
    for key in groups:
        if key in existing_by_key:
            mapping[key] = existing_by_key[key]
        else:
            unmatched.append(key)

    if not unmatched:
        return [], mapping

    summaries = []
    for key in unmatched:
        pl = groups[key]
        d = pl[0].get("date", "")
        players = {str(p["player_name"]).strip() for p in pl if not is_empty(p.get("player_name"))}
        dow = ""
        try: dow = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError): pass
        summaries.append({"key": key, "date": d, "day_of_week": dow,
                         "start_time": pl[0].get("start_time"), "end_time": pl[0].get("end_time"),
                         "player_count": len(players), "player_names": sorted(players)[:5]})

    prompt = f"""You are a data import assistant for a padel coaching app.
Generate lesson titles for {len(summaries)} groups.
RULES: 1-2 players → "Private Lesson", 3-4 → "Small Group", 5+ → "Academy Class"
+ date (YYYY-MM-DD) + time if available (e.g. "09:00-10:00").
Each title MUST be unique — always include the date.
Example: "Academy Class 2025-03-15 09:00-10:00", "Private Lesson 2025-03-16"
Groups: {json.dumps(summaries, indent=2)}
Return: {{"lessons": [{{"key": "<key>", "title": "<title>", "type": "academy"|"private"}}]}}"""

    try:
        content = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=1024, json_mode=True, label="lesson_names")
        llm_data = {ld["key"]: ld for ld in parse_json(content, "lesson_names").get("lessons", []) if ld.get("key")}
    except Exception:
        logger.exception("[AI] Lesson naming failed"); llm_data = {}

    new_classes = []
    for gs in summaries:
        key = gs["key"]
        llm = llm_data.get(key, {})
        title = llm.get("title") or _fallback_title(gs)
        # Safety: title MUST contain the date for uniqueness.
        if gs["date"] not in title:
            title = f"{title} {gs['date']}"
        ltype = llm.get("type", "private" if gs["player_count"] <= 2 else "academy")
        mapping[key] = title
        gs_start = gs.get("start_time") or _DEFAULT_START
        gs_end = gs.get("end_time") or (
            _infer_end_time(gs_start) if gs.get("start_time") else _DEFAULT_END
        )
        new_classes.append({"title": title, "type": ltype, "day": gs["date"],
                           "start_time": gs_start, "end_time": gs_end,
                           "is_recurring": False, "max_players": None})

    log_timing("build_lessons", t, matched=len(mapping) - len(unmatched), created=len(new_classes))
    return new_classes, mapping


def _fallback_title(gs):
    pc = gs["player_count"]
    date = gs.get("date", "")
    start = gs.get("start_time")
    end = gs.get("end_time")
    # Build suffix: always include date, add times if available.
    time_part = f" {start}-{end}" if start and end else (f" {start}" if start else "")
    suffix = f"{date}{time_part}"
    if pc <= 2: return f"Private Lesson {suffix}"
    if pc <= 4: return f"Small Group {suffix}"
    return f"Academy Class {suffix}"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ImportPipeline:
    """Holds pipeline state explicitly. Each validation step is a method."""

    def __init__(self, file_bytes: bytes, coach_id: int, active_tables: set[str]):
        self.file_bytes = file_bytes
        self.coach_id = coach_id
        self.active_tables = active_tables
        self.analysis: dict[str, list] = {}
        self.drop_counts: dict[str, int] = {}
        self.level_mapping: dict[str, str] = {}
        self.known_players: set[str] = set()
        self.known_classes: set[str] = set()
        self.categories = CategoryRegistry()

    def _drops(self, table: str, n: int):
        if n: self.drop_counts[table] = self.drop_counts.get(table, 0) + n

    def _rebuild_players(self, source=None):
        src = source or self.analysis.get("Players", [])
        self.known_players = {str(p["name"]).strip().lower() for p in src if not is_empty(p.get("name"))}

    def _rebuild_classes(self):
        self.known_classes = {str(c["title"]).strip().lower() for c in self.analysis.get("Classes", []) if not is_empty(c.get("title"))}

    def parse(self):
        return parse_excel(self.file_bytes)

    def filter_sheets(self, sheets):
        desc = ", ".join(sorted(self.active_tables | _AUTO_TABLES))
        relevant = pick_relevant_sheets(sheets, desc)
        return {k: v for k, v in sheets.items() if k in relevant} or sheets

    def detect_segments(self, selected):
        segs = []
        for name, rows in selected.items():
            segs.extend(detect_table_segments(name, rows))
        return segs

    def map_segments(self, segments):
        raw: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=min(len(segments), 4)) as pool:
            futs = {pool.submit(_process_segment, s, self.active_tables): s for s in segments}
            for f in as_completed(futs):
                try: merge_table_rows(raw, f.result())
                except Exception: logger.exception("[AI] Segment failed: '%s'", futs[f].label)
        return raw

    def validate_coach_levels(self, raw):
        try:
            from .coach_service import get_coach_levels
            existing = get_coach_levels(self.coach_id)
        except (ImportError, Exception) as e:
            logger.warning("[AI] Could not fetch coach levels: %s", e); existing = []
        levels, self.level_mapping = _map_coach_levels(raw.get("Coach Levels", []), raw.get("Players", []), existing)
        if levels: self.analysis["Coach Levels"] = levels

    def validate_players(self, raw):
        if "Players" not in self.active_tables: return
        clean = _validate_players(raw.get("Players", []), self.level_mapping)
        self._rebuild_players(clean)
        new = _discover_players(raw, self.known_players)
        if new: clean = deduplicate_rows(clean, new)
        self.analysis["Players"] = clean
        self._rebuild_players()

    def validate_eval_categories(self, raw):
        if "Evaluation Categories" not in self.active_tables: return
        raw_cats = raw.get("Evaluation Categories", [])
        raw_evals = raw.get("Evaluations", [])
        clean = _validate_eval_categories(raw_cats, raw_evals)
        if clean: self.analysis["Evaluation Categories"] = clean
        self._drops("Evaluation Categories", len(raw_cats) - len(clean))
        self.categories.bulk_register(clean)
        added = self.categories.discover_from_evaluations(raw_evals)
        if added: logger.info("[AI] Discovered %d categories from raw evaluations", added)
        self.analysis["Evaluation Categories"] = self.categories.rows

    def validate_classes(self, raw):
        if "Classes" not in self.active_tables: return
        raw_cls = raw.get("Classes", [])
        clean = _validate_classes(raw_cls)
        if clean: self.analysis["Classes"] = clean
        self._drops("Classes", len(raw_cls) - len(clean))
        self._rebuild_classes()

    def validate_players_in_classes(self, raw):
        if "Players in Classes" not in self.active_tables: return
        clean, dropped = _validate_players_in_classes(raw.get("Players in Classes", []), self.known_players, self.known_classes)
        if clean: self.analysis["Players in Classes"] = clean
        self._drops("Players in Classes", dropped)

    def validate_presences(self, raw):
        if "Presences" not in self.active_tables: return
        clean, dropped = _validate_presences(raw.get("Presences", []), self.known_players)
        self._drops("Presences", dropped)
        if not clean: return

        new_cls, mapping = _build_lesson_instances(clean, self.analysis.get("Classes", []), self.known_players)
        if new_cls:
            self.analysis["Classes"] = deduplicate_rows(self.analysis.get("Classes", []), new_cls)
            self._rebuild_classes()
        if mapping:
            for p in clean:
                d, s = p.get("date", ""), p.get("start_time", "")
                key = f"{d}_{s}" if s else d
                if key in mapping: p["lesson_title"] = mapping[key]
        self.analysis["Presences"] = clean

    def revalidate_players_in_classes(self, raw):
        if "Players in Classes" not in self.active_tables: return
        raw_pic = raw.get("Players in Classes", [])
        if not raw_pic: return
        self._rebuild_classes()
        clean, dropped = _validate_players_in_classes(raw_pic, self.known_players, self.known_classes)
        if clean: self.analysis["Players in Classes"] = clean
        if dropped: self.drop_counts["Players in Classes"] = dropped

    def validate_evaluations(self, raw):
        if "Evaluations" not in self.active_tables: return
        clean, dropped, updated = _validate_evaluations(raw.get("Evaluations", []), self.known_players, self.categories.known)
        if clean: self.analysis["Evaluations"] = clean
        self._drops("Evaluations", dropped)
        self.categories.merge_discovered(updated)
        self.categories.deduplicate()
        if self.categories.rows: self.analysis["Evaluation Categories"] = self.categories.rows

    def validate_strengths(self, raw):
        if "Strengths" not in self.active_tables: return
        clean, dropped = _validate_player_linked(raw.get("Strengths", []), self.known_players, text_field="strengths")
        if clean: self.analysis["Strengths"] = clean
        self._drops("Strengths", dropped)

    def validate_weaknesses(self, raw):
        if "Weaknesses" not in self.active_tables: return
        clean, dropped = _validate_player_linked(raw.get("Weaknesses", []), self.known_players, text_field="weaknesses")
        if clean: self.analysis["Weaknesses"] = clean
        self._drops("Weaknesses", dropped)

    def results(self) -> dict[str, list]:
        return {k: v for k, v in self.analysis.items() if v}


# ---------------------------------------------------------------------------
# SSE entry point
# ---------------------------------------------------------------------------


def stream_import_analysis(
    file_bytes: bytes,
    coach_id: int,
    requested_tables: list[str] | None = None,
) -> Generator[str, None, None]:
    def _ev(payload): return f"data: {json.dumps(payload)}\n\n"

    active = (set(requested_tables) & set(ALL_IMPORTABLE_TABLES) if requested_tables
              else set(ALL_IMPORTABLE_TABLES)) | _AUTO_TABLES
    pipe = ImportPipeline(file_bytes, coach_id, active)

    try:
        t0 = time.perf_counter()
        yield _ev({"type": "phase", "phase": "uploading"})
        yield _ev({"type": "thinking", "text": f"Importing: {', '.join(sorted(active - _AUTO_TABLES))}"})

        yield _ev({"type": "thinking", "text": "Parsing Excel..."})
        t = time.perf_counter()
        sheets = pipe.parse()
        ms = log_timing("stream.parse", t, sheets=len(sheets))
        yield _ev({"type": "thinking", "text": f"Parsed {len(sheets)} sheet(s) in {ms:.0f}ms."})
        if not sheets:
            yield _ev({"type": "error", "message": "No usable sheets found."}); return
        yield _ev({"type": "progress", "value": 10})

        yield _ev({"type": "phase", "phase": "processing"})
        t = time.perf_counter()
        selected = pipe.filter_sheets(sheets)
        log_timing("stream.filter", t, selected=len(selected))
        yield _ev({"type": "progress", "value": 18})

        t = time.perf_counter()
        segments = pipe.detect_segments(selected)
        log_timing("stream.segments", t, segments=len(segments))
        yield _ev({"type": "thinking", "text": f"{len(segments)} segment(s) found."})
        yield _ev({"type": "progress", "value": 22})

        yield _ev({"type": "phase", "phase": "analyzing"})
        yield _ev({"type": "thinking", "text": f"Mapping {len(segments)} segment(s) via AI..."})
        t = time.perf_counter()
        raw = pipe.map_segments(segments)
        ms = log_timing("stream.mapping", t, segments=len(segments))
        yield _ev({"type": "thinking", "text": f"Mapped in {ms:.0f}ms. Raw: {', '.join(f'{k}({len(v)})' for k,v in raw.items())}"})
        yield _ev({"type": "progress", "value": 45})

        yield _ev({"type": "phase", "phase": "validating"})
        pipe.validate_coach_levels(raw);           yield _ev({"type": "progress", "value": 50})
        pipe.validate_players(raw);                yield _ev({"type": "progress", "value": 55})
        pipe.validate_eval_categories(raw)
        pipe.validate_classes(raw);                yield _ev({"type": "progress", "value": 65})
        pipe.validate_players_in_classes(raw)
        pipe.validate_presences(raw);              yield _ev({"type": "progress", "value": 75})
        pipe.revalidate_players_in_classes(raw)
        pipe.validate_evaluations(raw)
        pipe.validate_strengths(raw)
        pipe.validate_weaknesses(raw);             yield _ev({"type": "progress", "value": 85})

        final = pipe.results()
        yield _ev({"type": "progress", "value": 90})
        total_rec = sum(len(v) for v in final.values())
        total_drop = sum(pipe.drop_counts.values())
        yield _ev({"type": "thinking", "text": f"Done — {total_rec} records, {total_drop} dropped."})
        if pipe.drop_counts:
            yield _ev({"type": "thinking", "text": f"Drops: {', '.join(f'{k}: {v}' for k,v in pipe.drop_counts.items())}"})
        yield _ev({"type": "tables", "tables": final})
        yield _ev({"type": "done"})
        log_timing("stream.total", t0, tables=len(final), records=total_rec, dropped=total_drop)

    except ValueError as exc:
        logger.error("[AI] Validation error: %s", exc)
        yield _ev({"type": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("[AI] stream_import_analysis failed")
        yield _ev({"type": "error", "message": str(exc)})