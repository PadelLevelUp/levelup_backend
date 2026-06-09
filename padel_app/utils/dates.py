"""Datetime utilities. The codebase stores naive UTC datetimes in the DB
because SQLAlchemy's `DateTime` column is naive by default. Use these helpers
instead of the deprecated `datetime.utcnow()` so behaviour is consistent in
any host timezone."""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return the current time as a naive UTC datetime.

    Equivalent to the deprecated `datetime.utcnow()`, but uses a timezone-aware
    intermediate to avoid host-timezone drift.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
