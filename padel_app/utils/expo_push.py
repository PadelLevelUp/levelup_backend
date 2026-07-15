# Phase 5 — native (Expo) push sender.
# - Mirrors padel_app/utils/push_notifications.py (the existing VAPID/Web-Push
#   sender) in style: best-effort, never raises into the caller, logs and
#   swallows failures.
# - No `exponent_server_sdk` dependency in pyproject.toml, so this posts
#   directly to the raw Expo HTTP push API (https://exp.host/--/api/v2/push/send)
#   using `requests` (already a transitive dependency, importable in this venv).
# - Batches in groups of <=100 tokens per request (Expo's documented limit).
# - Parses receipts for a `DeviceNotRegistered` error and deletes the stale
#   DeviceToken row for that token (cleanup on the caller's behalf).
import logging

import requests

from padel_app.models import DeviceToken
from padel_app.sql_db import db


logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_BATCH_SIZE = 100


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def send_expo_push(tokens: list[str], title, body, data: dict | None = None) -> bool:
    """Best-effort Expo push send to one or more Expo push tokens.

    Never raises — logs and returns False on any failure so callers can treat
    this exactly like the existing web-push helper (fire-and-forget).
    Deletes DeviceToken rows for tokens Expo reports as no longer registered.
    """
    tokens = [t for t in (tokens or []) if t]
    if not tokens:
        return False

    data = data or {}
    any_success = False

    for batch in _chunks(tokens, _BATCH_SIZE):
        messages = [
            {
                "to": token,
                "title": title,
                "body": body,
                "data": data,
            }
            for token in batch
        ]
        try:
            response = requests.post(
                EXPO_PUSH_URL,
                json=messages,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to send Expo push notification batch: %s", exc)
            continue

        try:
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to parse Expo push response: %s", exc)
            continue

        receipts = payload.get("data", [])
        for token, receipt in zip(batch, receipts):
            status = receipt.get("status") if isinstance(receipt, dict) else None
            if status == "ok":
                any_success = True
                continue

            error_type = (receipt.get("details") or {}).get("error") if isinstance(receipt, dict) else None
            if error_type == "DeviceNotRegistered":
                logger.info(
                    "Deleting stale Expo device token (DeviceNotRegistered): %s", token
                )
                stale = DeviceToken.query.filter_by(token=token).first()
                if stale:
                    db.session.delete(stale)
                    db.session.commit()
            else:
                logger.warning(
                    "Expo push receipt error for token=%s: %s", token, receipt
                )

    return any_success


def send_expo_push_to_user(user_id, title, body, data: dict | None = None) -> bool:
    """Convenience wrapper: look up the user's registered device tokens and
    send. Best-effort — no-ops (and never raises) when the user has no
    registered devices, mirroring the semantics of send_push_notification's
    "no subscription -> return False" behaviour.
    """
    if not user_id:
        return False
    tokens = [
        row.token
        for row in DeviceToken.query.filter_by(user_id=user_id).all()
    ]
    if not tokens:
        return False
    return send_expo_push(tokens, title, body, data)
