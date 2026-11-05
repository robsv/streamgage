"""Trend, range, peak and sparkline statistics from the last 7 days of readings."""

from datetime import datetime, timezone

import pytest
import requests

import streamgage as sg

NOW = 1_800_000_000.0  # a fixed "now", in epoch seconds
HOUR = 3600


def line(hours, slope, step=900, end=NOW, base=2.0):
    """Readings every `step` seconds for `hours` hours ending at `end`, rising `slope` ft/h."""
    n = int(hours * HOUR / step)
    return [(end - (n - i) * step, base + slope * i * step / HOUR) for i in range(n + 1)]


class TestSlopePerHour:
    def test_rising_line(self):
        pts = [(i * 900.0, 2 + 0.04 * i) for i in range(10)]  # 0.04 ft per 15 minutes
        assert sg.slope_per_hour(pts) == pytest.approx(0.16)

    def test_falling_line_is_negative(self):
        pts = [(i * 900.0, 5 - 0.01 * i) for i in range(10)]
        assert sg.slope_per_hour(pts) == pytest.approx(-0.04)

    def test_flat_is_zero(self):
        assert sg.slope_per_hour([(i * 900.0, 3.0) for i in range(8)]) == 0.0

    def test_degenerate_input_is_zero(self):
        assert sg.slope_per_hour([]) == 0.0
        assert sg.slope_per_hour([(0.0, 1.0)]) == 0.0
        assert sg.slope_per_hour([(5.0, 1.0), (5.0, 9.0)]) == 0.0  # same timestamp

    def test_fit_resists_one_noisy_reading(self):
        pts = [(i * 900.0, 3.0) for i in range(24)]
        pts[12] = (pts[12][0], 3.3)  # one spike in an otherwise flat 6 hours
        assert abs(sg.slope_per_hour(pts)) < 0.02  # a two-point difference would say 0.4 ft/h


class TestRecentStatsGates:
    def test_too_few_readings(self):
        assert sg.recent_stats(line(48, 0.1, step=4 * HOUR)[:11], now=NOW) is None

    def test_enough_readings_but_too_short_a_span(self):
        pts = line(3, 0.1, step=600)  # 19 readings over 3 hours
        assert len(pts) >= sg.MIN_RECENT_POINTS
        assert sg.recent_stats(pts, now=NOW) is None

    def test_just_over_a_day_is_enough(self):
        assert sg.recent_stats(line(24, 0.1), now=NOW) is not None

    def test_empty(self):
        assert sg.recent_stats([], now=NOW) is None

    def test_thresholds_match_the_documented_values(self):
        assert sg.MIN_RECENT_POINTS == 12
        assert sg.MIN_RECENT_SPAN_HOURS == 24
        assert sg.TREND_HOURS == 6
        assert sg.TREND_MAX_AGE_HOURS == 3
        assert sg.MIN_RANGE_FT == 0.1


class TestRecentStatsValues:
    def test_steadily_rising_gage(self):
        stats = sg.recent_stats(line(48, 0.05), now=NOW)
        assert stats["rate"] == pytest.approx(0.05)
        assert stats["delta24"] == pytest.approx(0.05 * 24)
        assert stats["range_pos"] == 100.0  # the latest reading is the peak
        assert stats["peak"] == stats["high"]
        assert stats["peak_time"] == round(NOW * 1000)
        assert stats["low"] == pytest.approx(2.0)

    def test_steadily_falling_gage(self):
        stats = sg.recent_stats(line(48, -0.05, base=6.0), now=NOW)
        assert stats["rate"] == pytest.approx(-0.05)
        assert stats["delta24"] == pytest.approx(-0.05 * 24)
        assert stats["range_pos"] == 0.0

    def test_flat_gage_has_no_range_position(self):
        stats = sg.recent_stats(line(48, 0.0), now=NOW)
        assert stats["range_pos"] is None
        assert stats["rate"] == pytest.approx(0.0)
        assert stats["low"] == stats["high"]

    def test_range_narrower_than_the_noise_floor_is_flat(self):
        stats = sg.recent_stats(line(48, 0.001), now=NOW)  # 0.048 ft over two days
        assert stats["range_pos"] is None

    def test_crested_gage_sits_mid_range(self):
        # up 2 ft over the first day, back down 1 ft over the second: ends 50% of the way up
        pts = [(NOW - 48 * HOUR + i * 900, 2 + 2 * i / 96) for i in range(97)]
        pts += [(NOW - 24 * HOUR + i * 900, 4 - i / 96) for i in range(1, 97)]
        stats = sg.recent_stats(pts, now=NOW)
        assert stats["range_pos"] == pytest.approx(100 * (3 - 2) / (4 - 2))
        assert stats["peak"] == pytest.approx(4.0)
        assert stats["peak_time"] == round((NOW - 24 * HOUR) * 1000)
        assert stats["rate"] < 0

    def test_ties_for_the_peak_report_the_latest(self):
        pts = line(48, 0.0, base=3.0)
        pts[10] = (pts[10][0], 5.0)
        pts[60] = (pts[60][0], 5.0)
        assert sg.recent_stats(pts, now=NOW)["peak_time"] == round(pts[60][0] * 1000)


class TestRecentStatsFreshness:
    def test_stale_last_reading_has_no_trend_or_24h_change(self):
        pts = line(48, 0.05, end=NOW - 5 * HOUR)
        stats = sg.recent_stats(pts, now=NOW)
        assert stats["rate"] is None
        assert stats["delta24"] is None
        assert stats["range_pos"] == 100.0  # the range itself is still meaningful

    def test_reading_just_inside_the_limit_still_trends(self):
        pts = line(48, 0.05, end=NOW - 3 * HOUR + 1)
        assert sg.recent_stats(pts, now=NOW)["rate"] == pytest.approx(0.05)

    def test_reading_just_past_the_limit_does_not(self):
        pts = line(48, 0.05, end=NOW - 3 * HOUR - 1)
        assert sg.recent_stats(pts, now=NOW)["rate"] is None

    def test_now_defaults_to_the_clock(self):
        recent = line(48, 0.05, end=datetime.now(timezone.utc).timestamp())
        assert sg.recent_stats(recent)["rate"] == pytest.approx(0.05)
        long_ago = 1_000_000_000.0  # September 2001
        assert sg.recent_stats(line(48, 0.05, end=long_ago))["rate"] is None


class TestRecentStatsTrendWindow:
    def test_needs_four_readings_in_the_window(self):
        # one reading every 3 hours: only 3 fall inside the last 6 hours (the window includes
        # its start, so every 2 hours would give exactly 4)
        assert sg.recent_stats(line(48, 0.05, step=3 * HOUR), now=NOW)["rate"] is None
        assert sg.recent_stats(line(48, 0.05, step=2 * HOUR), now=NOW)["rate"] is not None

    def test_needs_the_readings_to_span_three_hours(self):
        pts = line(30, 0.05, end=NOW - 8 * HOUR)  # a day or more of readings, then a gap...
        pts += [(NOW - 45 * 60 + i * 900, 3.0) for i in range(4)]  # ...and 4 in the last 45 min
        stats = sg.recent_stats(pts, now=NOW)
        assert stats is not None  # enough readings and span overall
        assert stats["rate"] is None  # but the last 6 hours only span 45 minutes

    def test_uses_only_the_last_six_hours(self):
        pts = line(42, 0.5, end=NOW - 6 * HOUR, base=2.0)  # steep rise, ending 6 h ago...
        top = pts[-1][1]
        pts += [(NOW - 6 * HOUR + i * 900, top - 0.01 * i) for i in range(1, 25)]  # ...then falls
        stats = sg.recent_stats(pts, now=NOW)
        assert stats["rate"] == pytest.approx(-0.04)


class TestRecentStatsDelta24:
    def test_gap_around_the_24h_mark_gives_none(self):
        pts = [p for p in line(48, 0.05)
               if not (NOW - 26 * HOUR < p[0] < NOW - 22 * HOUR)]
        assert sg.recent_stats(pts, now=NOW)["delta24"] is None

    def test_reading_within_an_hour_of_the_mark_is_used(self):
        pts = [p for p in line(48, 0.05) if not (NOW - 24.5 * HOUR < p[0] < NOW - 23.5 * HOUR)]
        stats = sg.recent_stats(pts, now=NOW)
        assert stats["delta24"] == pytest.approx(0.05 * 24, abs=0.05)


class TestRecentStatsSeries:
    def test_keeps_the_last_reading_of_each_clock_hour(self):
        pts = line(48, 0.05)
        stats = sg.recent_stats(pts, now=NOW)
        by_hour = {}
        for t, v in pts:
            by_hour[int(t // HOUR)] = [round(t * 1000), round(v, 2)]
        assert stats["series"] == list(by_hour.values())
        assert 48 <= len(stats["series"]) <= 50

    def test_series_is_chronological_and_ends_at_the_latest_reading(self):
        series_ = sg.recent_stats(line(48, 0.05), now=NOW)["series"]
        times = [t for t, _ in series_]
        assert times == sorted(times)
        assert times[-1] == round(NOW * 1000)

    def test_series_is_much_smaller_than_the_raw_readings(self):
        pts = line(168, 0.01)  # a full week at 15 minutes
        assert len(sg.recent_stats(pts, now=NOW)["series"]) < len(pts) / 3


class TestFetchRecent:
    def test_parses_the_recorded_response(self, route, fixture):
        calls = route({"/iv/": fixture("iv_two_days.json")})
        pts = sg.fetch_recent("02297345")
        raw = fixture("iv_two_days.json")["value"]["timeSeries"][0]["values"][0]["value"]
        assert len(pts) == len([v for v in raw if float(v["value"]) > -999])
        assert pts == sorted(pts)
        first = datetime.fromisoformat(raw[0]["dateTime"]).timestamp()
        assert pts[0][0] == first  # offsets are honored, not read as local time
        assert all(isinstance(t, float) and isinstance(v, float) for t, v in pts)
        _, params = calls[0]
        assert params["sites"] == "02297345"
        assert params["parameterCd"] == "00065"
        assert params["period"] == "P7D"

    def test_drops_no_data_sentinels(self, route, fixture):
        data = fixture("iv_two_days.json")
        values = data["value"]["timeSeries"][0]["values"][0]["value"]
        values[3]["value"] = "-999999"
        route({"/iv/": data})
        assert len(sg.fetch_recent("02297345")) == len(values) - 1

    def test_unsorted_response_is_sorted(self, route, fixture):
        data = fixture("iv_two_days.json")
        data["value"]["timeSeries"][0]["values"][0]["value"].reverse()
        route({"/iv/": data})
        pts = sg.fetch_recent("02297345")
        assert pts == sorted(pts)

    def test_no_series(self, route):
        route({"/iv/": {"value": {"timeSeries": []}}})
        assert sg.fetch_recent("02297345") == []

    def test_request_failure(self, route):
        route({"/iv/": requests.ConnectionError("down")})
        assert sg.fetch_recent("02297345") == []

    def test_http_error(self, route):
        route({"/iv/": requests.HTTPError("503")})
        assert sg.fetch_recent("02297345") == []

    @pytest.mark.parametrize("body", [{}, {"value": {}}, {"value": {"timeSeries": [{}]}}])
    def test_malformed_response_does_not_raise(self, route, body):
        route({"/iv/": body})
        assert sg.fetch_recent("02297345") == []

    def test_bad_timestamp_does_not_raise(self, route):
        body = {"value": {"timeSeries": [{"values": [{"value": [
            {"value": "1.0", "dateTime": "not a time"}]}]}]}}
        route({"/iv/": body})
        assert sg.fetch_recent("02297345") == []
