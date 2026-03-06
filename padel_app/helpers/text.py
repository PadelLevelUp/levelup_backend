"""
Text normalisation, fuzzy matching, date/time/status parsing utilities.
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def is_empty(val: Any) -> bool:
    return val is None or (isinstance(val, str) and val.strip() == "")


def is_numeric(val: Any) -> bool:
    if isinstance(val, (int, float)):
        return True
    if val is None:
        return False
    try:
        float(str(val))
        return True
    except (ValueError, TypeError):
        return False


def normalize_text(text: str) -> str:
    """Accent-insensitive lowercase normalisation."""
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.strip().lower())


def deep_copy_rows(rows: list[dict]) -> list[dict]:
    return copy.deepcopy(rows)


def deduplicate_rows(existing: list[dict], new_rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for row in existing + new_rows:
        key = json.dumps(row, default=str, sort_keys=True)
        seen[key] = row
    return list(seen.values())


def merge_table_rows(target: dict[str, list], new_tables: dict[str, list]) -> None:
    """Merge new_tables into target, deduplicating rows per table."""
    for name, rows in new_tables.items():
        if isinstance(rows, list) and rows:
            target[name] = deduplicate_rows(target.get(name, []), rows)


# ---------------------------------------------------------------------------
# Category matching
# ---------------------------------------------------------------------------

_COL_N = re.compile(r"^col_\d+$", re.IGNORECASE)
_PURE_NUM = re.compile(r"^[\d.,]+$")
_GARBAGE_NAMES = frozenset({"none", "null", "nan", "n/a", "na", "-"})


def is_garbage_category(name: str) -> bool:
    return (
        is_numeric(name)
        or len(name) < 2
        or bool(_COL_N.match(name))
        or bool(_PURE_NUM.match(name))
        or name.lower() in _GARBAGE_NAMES
    )


def fuzzy_match_category(name: str, known: dict[str, str]) -> str | None:
    """Match against known categories: exact norm → substring → SequenceMatcher."""
    if not known:
        return None
    norm = normalize_text(name)
    if norm in known:
        return known[norm]
    if len(norm) >= 4:
        for kn, canonical in known.items():
            if len(kn) >= 4 and (norm in kn or kn in norm):
                return canonical
    best_score, best = 0.0, None
    for kn, canonical in known.items():
        if abs(len(norm) - len(kn)) > max(2, len(norm) * 0.3):
            continue
        score = SequenceMatcher(None, norm, kn).ratio()
        if score > best_score:
            best_score, best = score, canonical
    return best if best_score >= 0.8 else None


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"(\d{1,2}:\d{2})")


def normalize_date(val: Any) -> str | None:
    if is_empty(val):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    m = _DATE_RE.match(str(val).strip())
    return m.group(1) if m else None


def normalize_time(val: Any) -> str | None:
    if is_empty(val):
        return None
    if hasattr(val, "strftime"):
        return val.strftime("%H:%M")
    m = _TIME_RE.match(str(val).strip())
    return m.group(1).zfill(5) if m else None


# ---------------------------------------------------------------------------
# Attendance status
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "present": ("present", None), "presente": ("present", None),
    "p": ("present", None), "✓": ("present", None), "✔": ("present", None),
    "x": ("present", None), "1": ("present", None), "sim": ("present", None),
    "yes": ("present", None), "y": ("present", None),
    "absent": ("absent", None), "ausente": ("absent", None),
    "a": ("absent", None), "0": ("absent", None),
    "não": ("absent", None), "nao": ("absent", None),
    "no": ("absent", None), "n": ("absent", None),
    "f": ("absent", None), "falta": ("absent", None), "fault": ("absent", None),
    "fj": ("absent", "justified"), "fj.": ("absent", "justified"),
    "falta justificada": ("absent", "justified"),
    "justified": ("absent", "justified"), "j": ("absent", "justified"),
    "fi": ("absent", "unjustified"), "fi.": ("absent", "unjustified"),
    "fnj": ("absent", "unjustified"),
    "falta injustificada": ("absent", "unjustified"),
    "falta nao justificada": ("absent", "unjustified"),
    "unjustified": ("absent", "unjustified"),
}

_STATUS_PREFIXES: list[tuple[str, str, str | None]] = [
    ("pres", "present", None),
    ("aus", "absent", None), ("abs", "absent", None),
    ("falta j", "absent", "justified"),
    ("falta i", "absent", "unjustified"), ("falta n", "absent", "unjustified"),
    ("falta", "absent", None),
    ("fj", "absent", "justified"), ("fi", "absent", "unjustified"),
]


def normalize_status(raw: str) -> tuple[str | None, str | None]:
    s = raw.strip().lower()
    result = _STATUS_MAP.get(s)
    if result:
        return result
    for prefix, status, just in _STATUS_PREFIXES:
        if s.startswith(prefix):
            return status, just
    return None, None


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------


def has_meaningful_text(val: Any) -> bool:
    if is_empty(val):
        return False
    cleaned = re.sub(r"[\s\-\u2014\u2013_.,;:!?/\\|*#@()[\]{}\"']+", "", str(val).strip())
    return len(cleaned) >= 2


def clean_text_value(val: str) -> str:
    text = val.strip()
    text = re.sub(r"^[-*\u2022\u00b7>]\s+", "", text)
    return text.strip(" ;,")