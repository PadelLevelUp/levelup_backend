# Audit findings (Phase 1):
# - Message creation is handled in services/messaging_service.py and routed via /api/app/message.
# - SSE subscriptions are global in-memory queues, not user-scoped.
# - Auth uses flask-jwt-extended on API routes.
