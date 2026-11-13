"""The past-year history behind the percentile: which daily statistic is used."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
import requests

import streamgage as sg

SITES = ["02297350", "02297345", "02294161"]  # mean, max and min; max and min only; mean only


def parse(text):
    return list(csv.DictReader(io.StringIO(text)))


def write(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def values(rows, site, stat):
    """One statistic's daily values for a site (00001 max, 00002 min, 00003 mean), by date."""
    return {r["time"]: float(r["value"]) for r in rows
            if r["monitoring_location_id"] == f"USGS-{site}" and r["statistic_id"] == stat}


@pytest.fixture
def daily(fixture):
    return parse(fixture("daily_three_gages.csv"))


class TestFetchHistories:
    def test_uses_the_daily_mean_when_the_site_publishes_several_statistics(self, route, daily):
        # Regression: a site with all three must not be compared against its daily maxima
        route({sg.DAILY_ITEMS: write(daily)})
        got = sg.fetch_histories(SITES)["02297350"]
        assert sorted(got) == sorted(values(daily, "02297350", "00003").values())
        assert max(values(daily, "02297350", "00001").values()) > max(got)  # the trap was real in this data

    def test_falls_back_to_the_max_min_midpoint_when_there_is_no_mean(self, route, daily):
        # Platt publishes only a daily max and min for stage
        route({sg.DAILY_ITEMS: write(daily)})
        hi, lo = values(daily, "02297345", "00001"), values(daily, "02297345", "00002")
        assert len(hi) == len(lo) == 40
        got = sg.fetch_histories(SITES)["02297345"]
        assert sorted(got) == pytest.approx(sorted((hi[d] + lo[d]) / 2 for d in hi))

    def test_mean_only_site(self, route, daily):
        route({sg.DAILY_ITEMS: write(daily)})
        assert sorted(sg.fetch_histories(SITES)["02294161"]) == sorted(values(daily, "02294161", "00003").values())

    def test_asks_for_a_year_of_daily_gage_height_for_all_the_sites_at_once(self, route, daily):
        calls = route({sg.DAILY_ITEMS: write(daily)})
        sg.fetch_histories(SITES)
        (_, params), = calls
        assert params["monitoring_location_id"] == "USGS-02297350,USGS-02297345,USGS-02294161"
        assert params["parameter_code"] == "00065"
        assert params["statistic_id"] == "00001,00002,00003"
        start, end = params["datetime"].split("/")
        assert end == datetime.now(timezone.utc).date().isoformat()
        assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(days=365)

    def test_sites_go_in_batches(self, route):
        calls = route({sg.DAILY_ITEMS: "monitoring_location_id,statistic_id,time,value\n"})
        sg.fetch_histories([f"{i:08d}" for i in range(100)])
        assert sorted(len(p["monitoring_location_id"].split(",")) for _, p in calls) == [20, 40, 40]

    def test_blank_and_no_data_values_are_dropped(self, route, daily):
        rows = [r for r in daily if r["monitoring_location_id"] == "USGS-02294161"]
        rows[5]["value"], rows[6]["value"] = "-999999", ""
        route({sg.DAILY_ITEMS: write(rows)})
        assert len(sg.fetch_histories(["02294161"])["02294161"]) == 38

    def test_midpoint_skips_days_missing_from_either_series(self, route, daily):
        rows = [r for r in daily if r["monitoring_location_id"] == "USGS-02297345"]
        minimums = [r for r in rows if r["statistic_id"] == "00002"]
        minimums[7]["value"] = "-999999"  # no minimum on one day
        rows.remove(minimums[12])  # and a day absent altogether
        route({sg.DAILY_ITEMS: write(rows)})
        assert len(sg.fetch_histories(["02297345"])["02297345"]) == 38

    def test_a_short_mean_does_not_beat_a_full_max_min_record(self, route, daily):
        rows = [r for r in daily if r["monitoring_location_id"] == "USGS-02297350"]
        means = [r for r in rows if r["statistic_id"] == "00003"]
        rows = [r for r in rows if r not in means[10:]]  # under 30 days of means
        route({sg.DAILY_ITEMS: write(rows)})
        hi, lo = values(rows, "02297350", "00001"), values(rows, "02297350", "00002")
        assert sorted(sg.fetch_histories(["02297350"])["02297350"]) == pytest.approx(
            sorted((hi[d] + lo[d]) / 2 for d in hi))

    def test_a_short_record_gives_no_percentile(self, route, daily):
        rows = [r for r in daily if r["monitoring_location_id"] == "USGS-02294161"][:10]
        route({sg.DAILY_ITEMS: write(rows)})
        assert sg.percentile(sg.fetch_histories(["02294161"])["02294161"], 2.0) is None

    def test_a_site_with_no_daily_values_has_no_history(self, route, daily):
        route({sg.DAILY_ITEMS: write(daily)})
        assert "09999999" not in sg.fetch_histories([*SITES, "09999999"])

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), requests.Timeout("slow"),
                                       requests.HTTPError("500")])
    def test_a_failed_request_gives_no_histories_instead_of_stopping_the_run(self, route, error):
        route({sg.DAILY_ITEMS: error})
        assert sg.fetch_histories(SITES) == {}

    def test_statistic_codes(self):
        assert (sg.DAILY_MAX, sg.DAILY_MIN, sg.DAILY_MEAN) == ("00001", "00002", "00003")


class TestDailyHistory:
    def test_means_in_any_order_are_kept_whole(self):
        means = {f"2026-10-{d:02d}": float(d) for d in range(1, 31)}
        assert sorted(sg.daily_history({"00003": means})) == sorted(means.values())

    def test_nothing_at_all(self):
        assert sg.daily_history({}) == []
