"""Datetime utilities. The codebase stores naive UTC datetimes in the DB
because SQLAlchemy's `DateTime` column is naive by default. Use these helpers
instead of the deprecated `datetime.utcnow()` so behaviour is consistent in
any host timezone."""

from datetime import datetime, timezone
from typing import Optional


def utcnow_naive() -> datetime:
    """Return the current time as a naive UTC datetime.

    Equivalent to the deprecated `datetime.utcnow()`, but uses a timezone-aware
    intermediate to avoid host-timezone drift.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as a UTC-aware ISO 8601 string.

    Datetimes are stored naive-UTC in the DB. Calling `.isoformat()` on those
    produces a string with no timezone offset (e.g. "2026-07-01T17:00:00"),
    which browsers' `new Date(...)` interpret as *local* time — displaying the
    wrong wall-clock (e.g. 1 hour behind in Lisbon summer time). Attaching the
    UTC offset here lets clients convert to the viewer's local timezone.

    A naive datetime is assumed to be UTC. An already-aware datetime is
    converted to UTC. Returns None for None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()
