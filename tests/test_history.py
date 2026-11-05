"""The past-year history behind the percentile: which daily statistic is used."""

import pytest
import requests

import streamgage as sg


def values(data, stat):
    """Daily values of one statistic (00001 max, 00002 min, 00003 mean) from a recorded response."""
    for ts in data["value"]["timeSeries"]:
        if ts["name"].endswith(":" + stat):
            return [float(v["value"]) for v in ts["values"][0]["value"] if float(v["value"]) > -999]
    return []


class TestFetchHistory:
    def test_uses_the_daily_mean_when_the_site_publishes_several_statistics(self, route, fixture):
        # Regression: this site's response lists max (00001) first, then min, then mean. Taking
        # the first series compared the current stage against daily maxima.
        data = fixture("dv_mean_max_min.json")
        route({"/dv/": data})
        result = sg.fetch_history("02297350")
        assert result == values(data, "00003")
        assert result != values(data, "00001")
        assert max(values(data, "00001")) > max(result)  # the trap was real in this data

    def test_falls_back_to_the_max_min_midpoint_when_there_is_no_mean(self, route, fixture):
        # Platt publishes only a daily max and min for stage
        data = fixture("dv_max_min_only.json")
        route({"/dv/": data})
        hi, lo = values(data, "00001"), values(data, "00002")
        assert len(hi) == len(lo) == 40
        assert sg.fetch_history("02297345") == pytest.approx([(h + m) / 2 for h, m in zip(hi, lo)])

    def test_mean_only_site(self, route, fixture):
        data = fixture("dv_mean_only.json")
        route({"/dv/": data})
        assert sg.fetch_history("02294161") == values(data, "00003")

    def test_requests_the_year_of_gage_height_for_the_site(self, route, fixture):
        calls = route({"/dv/": fixture("dv_mean_only.json")})
        sg.fetch_history("02294161")
        _, params = calls[0]
        assert params["sites"] == "02294161"
        assert params["parameterCd"] == "00065"
        assert params["period"] == "P365D"

    def test_no_data_sentinels_are_dropped(self, route, fixture):
        data = fixture("dv_mean_only.json")
        data["value"]["timeSeries"][0]["values"][0]["value"][5]["value"] = "-999999"
        route({"/dv/": data})
        assert len(sg.fetch_history("02294161")) == 39

    def test_midpoint_skips_days_missing_from_either_series(self, route, fixture):
        data = fixture("dv_max_min_only.json")
        for ts in data["value"]["timeSeries"]:
            if ts["name"].endswith(":00002"):
                ts["values"][0]["value"][7]["value"] = "-999999"  # no minimum on one day
                del ts["values"][0]["value"][12]  # and a day absent altogether
        route({"/dv/": data})
        assert len(sg.fetch_history("02297345")) == 38

    def test_a_short_mean_does_not_beat_a_full_max_min_record(self, route, fixture):
        data = fixture("dv_mean_max_min.json")
        for ts in data["value"]["timeSeries"]:
            if ts["name"].endswith(":00003"):
                ts["values"][0]["value"] = ts["values"][0]["value"][:10]  # under 30 days
        route({"/dv/": data})
        hi, lo = values(data, "00001"), values(data, "00002")
        assert sg.fetch_history("02297350") == pytest.approx([(h + m) / 2 for h, m in zip(hi, lo)])

    def test_a_short_record_gives_no_percentile(self, route, fixture):
        data = fixture("dv_mean_only.json")
        data["value"]["timeSeries"][0]["values"][0]["value"] = (
            data["value"]["timeSeries"][0]["values"][0]["value"][:10])
        route({"/dv/": data})
        assert sg.percentile(sg.fetch_history("02294161"), 2.0) is None

    def test_no_series(self, route):
        route({"/dv/": {"value": {"timeSeries": []}}})
        assert sg.fetch_history("02294161") == []

    def test_only_empty_series(self, route, fixture):
        data = fixture("dv_mean_max_min.json")
        for ts in data["value"]["timeSeries"]:
            ts["values"][0]["value"] = []
        route({"/dv/": data})
        assert sg.fetch_history("02297350") == []

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), requests.Timeout("slow"),
                                       requests.HTTPError("500")])
    def test_request_failures_give_an_empty_history(self, route, error):
        route({"/dv/": error})
        assert sg.fetch_history("02294161") == []

    @pytest.mark.parametrize("body", [{}, {"value": {}}, {"value": {"timeSeries": [{}]}},
                                      {"value": {"timeSeries": [{"name": "x", "values": []}]}}])
    def test_malformed_responses_do_not_raise(self, route, body):
        route({"/dv/": body})
        assert sg.fetch_history("02294161") == []

    def test_statistic_codes(self):
        assert (sg.DAILY_MAX, sg.DAILY_MIN, sg.DAILY_MEAN) == ("00001", "00002", "00003")
