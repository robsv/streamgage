"""Small pure functions: value parsing, percentile, flood category, names, distance."""

import math

import pytest

import streamgage as sg


class TestUsable:
    def test_parses_numbers(self):
        assert sg.usable("1.5") == 1.5
        assert sg.usable(2) == 2.0
        assert sg.usable("-3.25") == -3.25

    def test_zero_is_a_real_reading(self):
        assert sg.usable("0") == 0.0
        assert sg.usable(0) == 0.0

    @pytest.mark.parametrize("value", [None, "", "abc", "N/A", [], {}])
    def test_unparseable_is_none(self, value):
        assert sg.usable(value) is None

    @pytest.mark.parametrize("value", ["-999", "-9999", "-999999", -999999, -999.0])
    def test_no_data_sentinels_are_none(self, value):
        assert sg.usable(value) is None

    def test_just_above_the_sentinel_is_kept(self):
        assert sg.usable("-998.99") == -998.99

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", float("nan"), float("inf")])
    def test_non_finite_is_none(self, value):
        # json.dumps would write NaN, which is not valid JSON for the page
        assert sg.usable(value) is None


HISTORY = tuple(float(i) for i in range(1, 31))  # 1..30, the minimum usable length


class TestPercentile:
    def test_too_short_has_no_percentile(self):
        assert sg.percentile(HISTORY[:29], 10) is None
        assert sg.percentile([], 10) is None

    def test_minimum_length_is_enough(self):
        assert sg.percentile(HISTORY, 15) == 50.0

    def test_above_every_value_is_100(self):
        assert sg.percentile(HISTORY, 99) == 100.0

    def test_below_every_value_is_0(self):
        assert sg.percentile(HISTORY, 0.5) == 0.0

    def test_equal_values_count_as_at_or_below(self):
        assert sg.percentile(HISTORY, 30) == 100.0
        assert sg.percentile(HISTORY, 1) == pytest.approx(100 / 30)

    def test_threshold_matches_the_constant(self):
        assert sg.MIN_HISTORY_DAYS == 30


STAGES = {"action": 7.6, "minor": 8, "moderate": 9.2, "major": 10}


class TestFloodCategory:
    @pytest.mark.parametrize("stage, expected", [
        (5.0, "normal"), (7.59, "normal"),
        (7.6, "action"), (7.99, "action"),
        (8, "minor"), (9.19, "minor"),
        (9.2, "moderate"), (9.99, "moderate"),
        (10, "major"), (25, "major"),
    ])
    def test_thresholds_are_inclusive(self, stage, expected):
        assert sg.flood_category(stage, STAGES) == expected

    def test_missing_categories_are_skipped(self):
        assert sg.flood_category(9, {"minor": 8}) == "minor"
        assert sg.flood_category(5, {"minor": 8}) == "normal"

    def test_no_stages_is_normal(self):
        assert sg.flood_category(100, {}) == "normal"


class TestPrettyName:
    @pytest.mark.parametrize("raw, expected", [
        ("PEACE RIVER AT SR 70 AT ARCADIA, FL", "Peace River At SR 70 At Arcadia, FL"),
        ("PEACE RV DISTRIBUTARY AT DOVER SINK NR BARTOW FL",
         "Peace Rv Distributary At Dover Sink NR Bartow FL"),
        ("MYAKKA RIVER NEAR SARASOTA FL", "Myakka River Near Sarasota FL"),
        ("PEACE RIVER AT US 17 AT ZOLFO SPRINGS, FL", "Peace River At US 17 At Zolfo Springs, FL"),
    ])
    def test_keeps_road_and_state_abbreviations(self, raw, expected):
        assert sg.pretty_name(raw) == expected

    def test_empty(self):
        assert sg.pretty_name("") == ""


class TestHaversine:
    def test_same_point_is_zero(self):
        assert sg.haversine_km(27.0, -82.0, 27.0, -82.0) == 0.0

    def test_one_degree_of_latitude(self):
        assert sg.haversine_km(27.0, -82.0, 28.0, -82.0) == pytest.approx(111.19, abs=0.1)

    def test_is_symmetric(self):
        a = sg.haversine_km(27.1, -82.0, 27.9, -81.8)
        assert a == pytest.approx(sg.haversine_km(27.9, -81.8, 27.1, -82.0))

    def test_longitude_shrinks_with_latitude(self):
        at_equator = sg.haversine_km(0, 0, 0, 1)
        at_27 = sg.haversine_km(27, 0, 27, 1)
        assert at_27 == pytest.approx(at_equator * math.cos(math.radians(27)), rel=1e-3)


def test_miles_to_kilometers():
    assert round(200 * sg.KM_PER_MILE) == 322  # the default --distance
    assert round(1 * sg.KM_PER_MILE) == 2
    assert sg.KM_PER_MILE == pytest.approx(1.609344)
