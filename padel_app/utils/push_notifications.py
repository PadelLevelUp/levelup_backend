# Audit findings (Phase 1):
# - Message send flow: /api/app/message -> create_message_service().
# - SSE presence tracking is global-only (padel_app/realtime.py), without user-level connectivity state.
# - ORM stack is Flask-SQLAlchemy models + Alembic migrations.
# - API auth pattern is flask-jwt-extended.
# - Env loading is configured in levelup_backend/app.py for local dev (.env.local.dev).
import json
import logging
import os

from pywebpush import webpush, WebPushException

from padel_app.models import PushSubscription
from padel_app.sql_db import db


logger = logging.getLogger(__name__)


def send_push_notification(user_id, title, body, url="/"):
    subscription = PushSubscription.query.filter_by(user_id=user_id).first()
    if not subscription:
        return False

    vapid_public_key = os.getenv("VAPID_PUBLIC_KEY")
    vapid_private_key = os.getenv("VAPID_PRIVATE_KEY")
    vapid_claims_email = os.getenv("VAPID_CLAIMS_EMAIL")
    if not vapid_public_key or not vapid_private_key or not vapid_claims_email:
        logger.warning("VAPID env vars are not configured; skipping push notification")
        return False
    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url,
    })
    vapid_claims = {"sub": vapid_claims_email}

    try:
        webpush(
            subscription_info=json.loads(subscription.subscription_json),
            data=payload,
            vapid_private_key=vapid_private_key,
            vapid_claims=vapid_claims,
        )
        return True
    except WebPushException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (404, 410):
            logger.info(
                "Deleting invalid push subscription for user_id=%s (status=%s)",
                user_id,
                status_code,
            )
            db.session.delete(subscription)
            db.session.commit()
        else:
            logger.warning("Failed to send push notification for user_id=%s: %s", user_id, exc)
        return False
    except Exception as exc:
        logger.warning("Unexpected push notification failure for user_id=%s: %s", user_id, exc)
        return False
