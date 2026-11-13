"""Matching a USGS gage to its NWS gauge, flood stages, and the forecast crest."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

import streamgage as sg

SITE, LAT, LON = "02296750", 27.221944, -81.876111  # Peace River at Arcadia / NWS ARCF1
ARCF1 = {"lid": "ARCF1", "latitude": 27.221944, "longitude": -81.876111}
STAGES = {"action": 9, "minor": 11, "moderate": 14, "major": 16}


class TestFetchNws:
    def test_matches_the_colocated_gauge(self, route, fixture):
        route({"/gauges/ARCF1": fixture("nwps_gauge_arcf1.json")})
        assert sg.fetch_nws(SITE, LAT, LON, [ARCF1]) == {"lid": "ARCF1", "stages": STAGES}

    def test_a_gauge_beyond_the_radius_is_not_matched_and_not_fetched(self, route, fixture):
        calls = route({"/gauges/": fixture("nwps_gauge_arcf1.json")})
        far = dict(ARCF1, latitude=LAT + 0.01)  # about 1.1 km north
        assert sg.fetch_nws(SITE, LAT, LON, [far]) is None
        assert calls == []

    def test_a_gauge_just_inside_the_radius_is_matched(self, route, fixture):
        route({"/gauges/ARCF1": fixture("nwps_gauge_arcf1.json")})
        near = dict(ARCF1, latitude=LAT + 0.4 / 111.19)  # 0.4 km north
        assert sg.fetch_nws(SITE, LAT, LON, [near]) is not None

    def test_rejects_a_nearby_gauge_that_belongs_to_another_usgs_site(self, route, fixture):
        route({"/gauges/ARCF1": fixture("nwps_gauge_arcf1.json")})
        assert sg.fetch_nws("02299999", LAT, LON, [ARCF1]) is None

    def test_picks_the_nearest_of_several_gauges(self, route, fixture):
        calls = route({"/gauges/ARCF1": fixture("nwps_gauge_arcf1.json")})
        farther = {"lid": "OTHER1", "latitude": LAT + 0.2, "longitude": LON}
        assert sg.fetch_nws(SITE, LAT, LON, [farther, ARCF1])["lid"] == "ARCF1"
        assert [url.rsplit("/", 1)[-1] for url, _ in calls] == ["ARCF1"]

    def test_no_gauges(self, route):
        calls = route({})
        assert sg.fetch_nws(SITE, LAT, LON, []) is None
        assert calls == []

    def test_a_failed_request_is_raised_not_taken_for_no_gauge(self, route):
        route({"/gauges/ARCF1": requests.ConnectionError("down")})
        with pytest.raises(requests.ConnectionError):
            sg.fetch_nws(SITE, LAT, LON, [ARCF1])

    def test_missing_stages_are_dropped(self, route, fixture):
        detail = fixture("nwps_gauge_arcf1.json")
        detail["flood"]["categories"]["minor"]["stage"] = -9999  # NWS no-data sentinel
        detail["flood"]["categories"]["major"]["stage"] = "N/A"
        route({"/gauges/ARCF1": detail})
        assert sg.fetch_nws(SITE, LAT, LON, [ARCF1])["stages"] == {"action": 9, "moderate": 14}

    @pytest.mark.parametrize("flood", [None, {}, {"categories": None}, {"categories": {}}])
    def test_a_gauge_with_no_flood_stages_is_not_an_nws_gage(self, route, fixture, flood):
        detail = fixture("nwps_gauge_arcf1.json")
        detail["flood"] = flood
        route({"/gauges/ARCF1": detail})
        assert sg.fetch_nws(SITE, LAT, LON, [ARCF1]) is None

    def test_all_stages_missing_is_not_an_nws_gage(self, route, fixture):
        detail = fixture("nwps_gauge_arcf1.json")
        for category in detail["flood"]["categories"].values():
            category["stage"] = -999
        route({"/gauges/ARCF1": detail})
        assert sg.fetch_nws(SITE, LAT, LON, [ARCF1]) is None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def forecast(points, units="ft", now=None):
    """An NWPS forecast body with points given as (hours from now, stage)."""
    now = now or datetime.now(timezone.utc)
    return {"primaryUnits": units, "issuedTime": iso(now),
            "data": [{"validTime": iso(now + timedelta(hours=h)), "primary": v}
                     for h, v in points]}


class TestFetchForecast:
    def run(self, route, body):
        route({"/stageflow/forecast": body})
        return sg.fetch_forecast("ARCF1", STAGES)

    def test_crest_is_the_highest_remaining_point(self, route):
        result = self.run(route, forecast([(1, 12.0), (12, 16.5), (24, 15.0), (36, 13.0)]))
        assert result["crest"] == 16.5
        assert result["crest_category"] == "major"
        assert len(result["series"]) == 4

    def test_a_peak_that_has_already_passed_is_not_the_crest(self, route):
        result = self.run(route, forecast([(-12, 18.0), (-3, 12.0), (6, 13.0), (18, 12.5)]))
        assert result["crest"] == 13.0  # the 18.0 from 12 hours ago is history
        assert result["crest_category"] == "minor"  # 13 ft: above minor (11), below moderate (14)
        assert [v for _, v in result["series"]] == [12.0, 13.0, 12.5]

    def test_points_up_to_six_hours_old_are_kept(self, route):
        result = self.run(route, forecast([(-5.9, 20.0), (-6.5, 25.0), (5, 10.0)]))
        assert [v for _, v in result["series"]] == [20.0, 10.0]

    def test_crest_category_uses_the_stage_thresholds(self, route):
        assert self.run(route, forecast([(2, 8.0)]))["crest_category"] == "normal"
        assert self.run(route, forecast([(2, 11.0)]))["crest_category"] == "minor"

    def test_reports_the_issue_time_and_crest_time(self, route):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self.run(route, forecast([(1, 12.0), (5, 13.0)], now=now))
        assert result["issued"] == iso(now)
        assert result["crest_time"] == iso(now + timedelta(hours=5))

    def test_flow_forecasts_are_ignored_because_the_thresholds_are_in_feet(self, route):
        assert self.run(route, forecast([(2, 12.0)], units="kcfs")) is None

    def test_missing_units_are_ignored(self, route):
        body = forecast([(2, 12.0)])
        del body["primaryUnits"]
        assert self.run(route, body) is None

    def test_a_forecast_entirely_in_the_past_is_ignored(self, route):
        assert self.run(route, forecast([(-30, 12.0), (-20, 13.0)])) is None

    def test_no_data_sentinels_and_non_numbers_are_dropped(self, route):
        body = forecast([(1, -999), (2, None), (3, "high"), (4, 12.0)])
        assert [v for _, v in self.run(route, body)["series"]] == [12.0]

    def test_empty_forecast(self, route):
        assert self.run(route, forecast([])) is None
        assert self.run(route, {"primaryUnits": "ft"}) is None

    def test_a_failed_request_is_raised_not_taken_for_no_forecast(self, route):
        route({"/stageflow/forecast": requests.HTTPError("404")})
        with pytest.raises(requests.HTTPError):
            sg.fetch_forecast("ARCF1", STAGES)
