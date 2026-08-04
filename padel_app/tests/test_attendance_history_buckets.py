"""PAD-114 — bucketing rules for the attendance history (spec `attendance.history`).

Pure-logic coverage: no app or DB fixture needed. These are the rules the chart's
x-axis is built from, and they are exactly the kind of thing that regresses
silently — an off-by-one here turns "1Y" into 13 bars or drops December.
"""

from datetime import date, datetime

from padel_app.services.attendance_history_service import (
    _bucket_series,
    default_range,
    pick_granularity,
)


class TestPickGranularity:
    """Rule 4: <= 31 days -> day, <= ~18 months -> month, longer -> year."""

    def test_current_week_is_daily(self):
        assert pick_granularity(datetime(2026, 8, 3), datetime(2026, 8, 9)) == "day"

    def test_a_full_month_is_still_daily(self):
        assert pick_granularity(datetime(2026, 1, 1), datetime(2026, 1, 31)) == "day"

    def test_just_over_a_month_switches_to_monthly(self):
        assert pick_granularity(datetime(2026, 1, 1), datetime(2026, 2, 2)) == "month"

    def test_current_year_is_monthly(self):
        assert pick_granularity(datetime(2026, 1, 1), datetime(2026, 12, 31)) == "month"

    def test_multi_year_span_is_yearly(self):
        assert pick_granularity(datetime(2024, 8, 5), datetime(2026, 8, 4)) == "year"


class TestBucketSeries:
    """Rule 5: contiguous and gap-filled — empty periods are still buckets."""

    def test_week_has_seven_daily_buckets(self):
        buckets = _bucket_series(date(2026, 8, 3), date(2026, 8, 9), "day")
        assert len(buckets) == 7
        assert buckets[0] == date(2026, 8, 3)
        assert buckets[-1] == date(2026, 8, 9)

    def test_year_has_twelve_monthly_buckets(self):
        buckets = _bucket_series(date(2026, 1, 1), date(2026, 12, 31), "month")
        assert len(buckets) == 12
        assert buckets[0] == date(2026, 1, 1)
        assert buckets[-1] == date(2026, 12, 1)

    def test_monthly_buckets_roll_over_the_year_boundary(self):
        buckets = _bucket_series(date(2025, 11, 5), date(2026, 2, 3), "month")
        assert buckets == [
            date(2025, 11, 1),
            date(2025, 12, 1),
            date(2026, 1, 1),
            date(2026, 2, 1),
        ]

    def test_february_month_length_is_respected(self):
        buckets = _bucket_series(date(2025, 2, 1), date(2025, 2, 28), "day")
        assert len(buckets) == 28

    def test_yearly_buckets_are_contiguous(self):
        buckets = _bucket_series(date(2024, 8, 5), date(2026, 8, 4), "year")
        assert buckets == [date(2024, 1, 1), date(2025, 1, 1), date(2026, 1, 1)]


class TestDefaultRange:
    """Rule 6: the default window is the current calendar month."""

    def test_defaults_to_the_current_month(self):
        start, end = default_range(datetime(2026, 8, 4, 13, 30))
        assert start == datetime(2026, 8, 1, 0, 0)
        assert end == datetime(2026, 8, 31, 23, 59, 59)

    def test_handles_a_28_day_february(self):
        start, end = default_range(datetime(2025, 2, 14))
        assert start.day == 1
        assert end.day == 28
