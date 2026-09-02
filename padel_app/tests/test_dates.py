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


# ===========================================================================
# club_day_start_utc — PAD-144
#
# A "day" the coach reads off their own calendar is a CLUB-LOCAL day, but every
# instant in the DB is naive UTC. The boundary must therefore be *derived* in
# Europe/Lisbon and *converted back* to naive UTC. The discriminating evidence
# that a real conversion happens (rather than a constant offset, or nothing at
# all) is that the same UTC instant maps to a different local day in summer
# than in winter.
# ===========================================================================


def test_club_day_start_winter_matches_utc_midnight():
    """January is WET (UTC+0), so the local day boundary *is* UTC midnight."""
    from padel_app.utils.dates import club_day_start_utc

    assert club_day_start_utc(datetime(2025, 1, 14, 23, 30)) == datetime(2025, 1, 14, 0, 0)


def test_club_day_start_summer_is_an_hour_before_utc_midnight():
    """August is WEST (UTC+1): 00:00 local == 23:00 UTC the previous day."""
    from padel_app.utils.dates import club_day_start_utc

    # 10:00 UTC on the 14th is 11:00 local on the 14th; that local day starts
    # at 23:00 UTC on the 13th.
    assert club_day_start_utc(datetime(2025, 8, 14, 10, 0)) == datetime(2025, 8, 13, 23, 0)


def test_club_day_start_late_utc_instant_belongs_to_the_next_local_day():
    """23:30 UTC in summer is 00:30 local the NEXT day — the reported bug.

    An invitation sent at this instant must count against the new local day's
    quota, not the previous one.
    """
    from padel_app.utils.dates import club_day_start_utc

    # 23:30 UTC on the 14th == 00:30 local on the 15th, whose day starts at
    # 23:00 UTC on the 14th.
    assert club_day_start_utc(datetime(2025, 8, 14, 23, 30)) == datetime(2025, 8, 14, 23, 0)


def test_same_utc_instant_lands_on_different_local_days_in_summer_and_winter():
    """The discriminating assertion: a naive-UTC boundary makes these agree.

    Both instants are 23:30 UTC. In summer that is already the next local day,
    so the boundary is *later* than the instant's own UTC midnight; in winter it
    is the same local day. A regression to `.replace(hour=0, ...)` returns the
    instant's UTC midnight in both cases and fails this test.
    """
    from padel_app.utils.dates import club_day_start_utc

    summer = club_day_start_utc(datetime(2025, 8, 14, 23, 30))
    winter = club_day_start_utc(datetime(2025, 1, 14, 23, 30))

    # Summer: boundary is 30 minutes BEFORE the instant (same local day, the
    # 15th, which began at 23:00 UTC on the 14th).
    assert summer == datetime(2025, 8, 14, 23, 0)
    # Winter: boundary is 23.5 hours before the instant (the 14th local).
    assert winter == datetime(2025, 1, 14, 0, 0)

    # Naive-UTC regression would put both at midnight of their own UTC date.
    assert summer != datetime(2025, 8, 14, 0, 0)


def test_club_day_start_accepts_a_days_offset():
    """`days_offset` walks CALENDAR days, not 24-hour blocks."""
    from padel_app.utils.dates import club_day_start_utc

    # 10:00 UTC 14 Aug == 11:00 local 14 Aug. Tomorrow local is the 15th,
    # which starts at 23:00 UTC on the 14th.
    assert club_day_start_utc(
        datetime(2025, 8, 14, 10, 0), days_offset=1
    ) == datetime(2025, 8, 14, 23, 0)


def test_club_day_start_offset_crosses_the_dst_transition_by_calendar_day():
    """Across the WET->WEST change the local day is still 1 calendar day on.

    2025-03-30 is the spring-forward date. Walking from the 29th to the 30th
    must land on the 30th's local midnight (00:00 WET == 00:00 UTC), not on
    "24 hours later", which would drift the boundary.
    """
    from padel_app.utils.dates import club_day_start_utc

    result = club_day_start_utc(datetime(2025, 3, 29, 12, 0), days_offset=1)

    assert result == datetime(2025, 3, 30, 0, 0)
    assert result.tzinfo is None  # still naive UTC for DB comparison
