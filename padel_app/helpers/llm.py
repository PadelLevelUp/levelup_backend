"""
LLM client wrapper with retry logic, JSON parsing, and timing helpers.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from openai import AuthenticationError, OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
_MODEL = os.environ.get("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free")
_REASONING = os.environ.get("OPENROUTER_REASONING_ENABLED", "true").lower() in {
    "1", "true", "yes", "on",
}

_MAX_RETRIES = 2
_RETRY_DELAY_S = 1.0
_NON_RETRYABLE = (AuthenticationError, TypeError, ValueError)

client = OpenAI(base_url=_BASE_URL, api_key=_API_KEY)
logger = logging.getLogger("ai_import")

if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def log_timing(step: str, start: float, **meta: Any) -> float:
    ms = (time.perf_counter() - start) * 1000
    if meta:
        meta_str = ", ".join(f"{k}={v}" for k, v in meta.items())
        logger.info("[AI TIMER] %s took %.2fms (%s)", step, ms, meta_str)
    else:
        logger.info("[AI TIMER] %s took %.2fms", step, ms)
    return ms


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def call_llm(
    *,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0,
    json_mode: bool = False,
    label: str = "llm_call",
    **extra_meta: Any,
) -> str:
    """Call OpenAI-compatible API with retry. Raises immediately on auth errors."""
    kwargs: dict = {
        "model": _MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "extra_body": {"reasoning": {"enabled": _REASONING}},
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            t = time.perf_counter()
            resp = client.chat.completions.create(**kwargs)
            log_timing(f"{label}.api", t, model=_MODEL, attempt=attempt, **extra_meta)
            return resp.choices[0].message.content.strip()
        except _NON_RETRYABLE:
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("[AI] %s attempt %d failed: %s", label, attempt, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def parse_json(raw: str, label: str = "llm") -> dict:
    """Parse LLM output as a JSON object."""
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(f"Expected JSON object, got {type(result).__name__}")
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("[AI] %s bad JSON: %s\nRaw: %.500s", label, exc, raw)
        raise ValueError(f"LLM {label} returned invalid JSON: {exc}") from exc