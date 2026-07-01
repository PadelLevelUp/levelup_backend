"""Unit tests for padel_app.utils.dates.

Regression coverage for PAD-33: chat timestamps were serialized without a
timezone offset, causing clients to render them 1 hour behind local time.
`to_utc_iso` must always emit a UTC-aware ISO 8601 string.
"""
from datetime import datetime, timezone, timedelta


def test_to_utc_iso_attaches_utc_offset_to_naive_datetime():
    from padel_app.utils.dates import to_utc_iso

    result = to_utc_iso(datetime(2026, 7, 1, 17, 0, 0))

    assert result is not None
    # Must carry an explicit UTC offset (naive is assumed to be UTC).
    assert result.endswith("+00:00")
    assert result.startswith("2026-07-01T17:00:00")


def test_to_utc_iso_converts_aware_datetime_to_utc():
    from padel_app.utils.dates import to_utc_iso

    # 18:00 at UTC+1 == 17:00 UTC.
    aware = datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    result = to_utc_iso(aware)

    assert result == "2026-07-01T17:00:00+00:00"


def test_to_utc_iso_returns_none_for_none():
    from padel_app.utils.dates import to_utc_iso

    assert to_utc_iso(None) is None
