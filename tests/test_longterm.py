"""The percentile for this calendar date over every year of record (USGS daily statistics)."""

from datetime import datetime, timedelta

import pytest
import requests

import streamgage as sg
from tests.conftest import FIXTURES, gage

STATS_TEXT = (FIXTURES / "daily_stats.rdb").read_text()  # four real Peace River gages, a few days each
ROWS = sg.parse_rdb(STATS_TEXT)


def real_row(site, month=11, day=5):
    return next(r for r in ROWS if r["site_no"] == site and (r["month_nu"], r["day_nu"]) == (str(month), str(day)))


ARCADIA = real_row("02296750")     # 95 years; min -0.07 .. max 8.57; median 2.44
FORT_MEADE = real_row("02294898")  # 53 years
DOVER_SINK = real_row("02294705")  # 18 years; the p05 and p95 columns are blank
TEN_YEARS = real_row("02295440")   # exactly 10 years; p05 and p95 blank


def stat_row(**over):
    """A synthetic statistics row; columns not given are blank."""
    row = {"site_no": "01234567", "month_nu": "11", "day_nu": "5", "begin_yr": "1990", "end_yr": "2026",
           "count_nu": "30", "max_va_yr": "2001", "min_va_yr": "1995",
           **{col: "" for col, _ in sg.STAT_COLUMNS}}
    row.update(over)
    return row


class TestLongTermPercentile:
    @pytest.mark.parametrize("stage, expected", [
        (2.44, 50.0),    # exactly the median
        (0.73, 5.0),     # exactly the 5th percentile
        (6.21, 95.0),    # exactly the 95th
        (3.0, 61.8),     # between the 50th (2.44) and 75th (3.63): 50 + 25 * 0.56 / 1.19
        (7.4, 97.5),     # between the 95th (6.21) and the maximum (8.57): 95 + 5 * 1.19 / 2.36
        (0.0, 0.4),      # between the minimum (-0.07) and the 5th (0.73): 5 * 0.07 / 0.80
    ])
    def test_interpolates_between_the_published_points(self, stage, expected):
        result = sg.long_term(stage, ARCADIA)
        assert result["status"] == "within"
        assert result["pct"] == pytest.approx(expected, abs=0.05)

    def test_fort_meade_is_the_88th(self):
        # 74.48 ft lies between the 80th (73.36) and 90th (74.77): 80 + 10 * 1.12 / 1.41
        assert sg.long_term(74.48, FORT_MEADE)["pct"] == pytest.approx(87.9, abs=0.05)

    def test_the_extremes_are_inside_the_record(self):
        top = sg.long_term(8.57, ARCADIA)
        assert (top["status"], top["pct"]) == ("within", 100.0)
        bottom = sg.long_term(-0.07, ARCADIA)
        assert (bottom["status"], bottom["pct"]) == ("within", 0.0)

    def test_higher_than_every_past_value_is_above_the_record(self):
        result = sg.long_term(16.15, ARCADIA)
        assert (result["status"], result["pct"]) == ("above", 100.0)
        assert sg.long_term(8.58, ARCADIA)["status"] == "above"

    def test_lower_than_every_past_value_is_below_the_record(self):
        result = sg.long_term(-0.08, ARCADIA)
        assert (result["status"], result["pct"]) == ("below", 0.0)

    def test_blank_percentile_columns_are_skipped(self):
        # Dover Sink has no p05 or p95: between the 90th (90.51) and the maximum (91.46)
        assert sg.long_term(91.0, DOVER_SINK)["pct"] == pytest.approx(95.2, abs=0.05)
        # and between the minimum (75.33) and the 10th (80.05), since the p05 is missing
        assert sg.long_term(77.0, DOVER_SINK)["pct"] == pytest.approx(3.5, abs=0.05)

    def test_a_flat_stretch_takes_the_middle_of_its_percentile_span(self):
        row = stat_row(min_va="0", p05_va="1", p10_va="1", p20_va="1", p25_va="2", p50_va="3", max_va="9")
        assert sg.long_term(1.0, row)["pct"] == pytest.approx((5 + 20) / 2)

    def test_a_flat_bottom_and_a_flat_top(self):
        row = stat_row(min_va="1", p05_va="1", p50_va="3", p95_va="5", max_va="5")
        assert sg.long_term(1.0, row)["pct"] == pytest.approx(2.5)    # shared by the 0th and 5th
        assert sg.long_term(5.0, row)["pct"] == pytest.approx(97.5)   # shared by the 95th and 100th
        assert sg.long_term(5.01, row)["status"] == "above"

    def test_the_percentile_never_goes_down_as_the_stage_goes_up(self):
        for row in (ARCADIA, FORT_MEADE, DOVER_SINK, TEN_YEARS):
            low, high = float(row["min_va"]), float(row["max_va"])
            sweep = [low - 1 + i * (high - low + 2) / 400 for i in range(401)]
            pcts = [sg.long_term(v, row)["pct"] for v in sweep]
            assert pcts == sorted(pcts) and pcts[0] == 0.0 and pcts[-1] == 100.0

    def test_the_percentile_is_rounded_to_a_tenth(self):
        assert sg.long_term(3.0, ARCADIA)["pct"] == 61.8


class TestLongTermRecord:
    def test_what_the_popup_shows_about_the_record(self):
        result = sg.long_term(2.0, ARCADIA)
        assert result["years"] == 95 and (result["first"], result["last"]) == (1932, 2026)
        assert (result["month"], result["day"]) == (11, 5)
        assert result["median"] == 2.44
        assert (result["high"], result["high_year"]) == (8.57, 2006)
        assert (result["low"], result["low_year"]) == (-0.07, 1943)

    def test_short_record_is_refused(self):
        assert sg.long_term(2.0, dict(ARCADIA, count_nu="9")) is None
        assert sg.long_term(2.0, dict(ARCADIA, count_nu="10")) is not None
        assert sg.MIN_STAT_YEARS == 10

    def test_the_ten_year_gage_is_accepted(self):
        result = sg.long_term(43.55, TEN_YEARS)
        assert result["years"] == 10 and result["pct"] == pytest.approx(50.0)

    @pytest.mark.parametrize("count", ["", "many", None])
    def test_an_unreadable_year_count_is_refused(self, count):
        assert sg.long_term(2.0, dict(ARCADIA, count_nu=count)) is None
        row = dict(ARCADIA)
        del row["count_nu"]
        assert sg.long_term(2.0, row) is None

    @pytest.mark.parametrize("missing", ["min_va", "max_va"])
    def test_without_the_minimum_or_maximum_there_is_nothing_to_bound_the_range(self, missing):
        assert sg.long_term(2.0, dict(ARCADIA, **{missing: ""})) is None

    @pytest.mark.parametrize("junk", ["-999999", "nan", "abc"])
    def test_no_data_values_count_as_blank(self, junk):
        row = dict(ARCADIA, p50_va=junk)
        result = sg.long_term(2.44, row)
        assert result["median"] is None
        # across the gap, between the 25th (1.57) and 75th (3.63): 25 + 50 * 0.87 / 2.06
        assert result["pct"] == pytest.approx(46.1, abs=0.05)

    def test_missing_years_and_median_are_left_out_not_invented(self):
        row = dict(ARCADIA, begin_yr="", end_yr="", max_va_yr="", min_va_yr="", p50_va="")
        result = sg.long_term(2.0, row)
        assert (result["first"], result["last"], result["high_year"], result["low_year"]) == (None,) * 4
        assert result["median"] is None

    def test_only_the_needed_columns_are_required(self):
        row = stat_row(min_va="1", max_va="9")  # a record with nothing between the extremes
        assert sg.long_term(5.0, row)["pct"] == pytest.approx(50.0)


class TestReadingDay:
    @pytest.mark.parametrize("when, expected", [
        ("2026-11-05T12:30:00.000-05:00", (11, 5)),
        ("2026-11-05T23:30:00.000-05:00", (11, 5)),  # 04:30 UTC on the 6th, but still the 5th here
        ("2026-11-05T00:15:00.000-05:00", (11, 5)),
        ("2028-02-29T09:00:00.000-05:00", (2, 29)),
        ("2026-01-01T00:00:00.000+00:00", (1, 1)),
    ])
    def test_uses_the_gages_own_calendar_day(self, when, expected):
        assert sg.reading_day(when) == expected


FIXTURE_SITES = ["02296750", "02294898", "02294705", "02295440"]


class TestFetchDailyStats:
    def test_picks_each_sites_own_day(self, route):
        route({sg.STAT_SERVICE: STATS_TEXT})
        found = sg.fetch_daily_stats({"02296750": (11, 5), "02294898": (11, 6)})
        assert set(found) == {"02296750", "02294898"}
        assert (found["02296750"]["month_nu"], found["02296750"]["day_nu"]) == ("11", "5")
        assert (found["02294898"]["month_nu"], found["02294898"]["day_nu"]) == ("11", "6")

    def test_a_leap_day_is_found(self, route):
        route({sg.STAT_SERVICE: STATS_TEXT})
        found = sg.fetch_daily_stats({"02296750": (2, 29)})
        assert found["02296750"]["day_nu"] == "29" and found["02296750"]["month_nu"] == "2"

    def test_a_site_the_service_has_nothing_for_is_absent(self, route):
        route({sg.STAT_SERVICE: STATS_TEXT})
        found = sg.fetch_daily_stats({"02296750": (11, 5), "02297345": (11, 5)})  # Platt: none
        assert set(found) == {"02296750"}

    def test_a_day_with_no_row_is_absent(self, route):
        route({sg.STAT_SERVICE: STATS_TEXT})
        assert sg.fetch_daily_stats({"02296750": (7, 4)}) == {}  # the fixture has only a few days

    def test_asks_for_every_statistic_of_gage_height_by_day(self, route):
        calls = route({sg.STAT_SERVICE: STATS_TEXT})
        sg.fetch_daily_stats({site: (11, 5) for site in FIXTURE_SITES})
        _, params = calls[0]
        assert params["statReportType"] == "daily" and params["statTypeCd"] == "all"
        assert params["parameterCd"] == "00065" and params["format"] == "rdb"
        assert params["sites"] == ",".join(sorted(FIXTURE_SITES))

    def test_batches_at_the_services_limit_of_ten_sites(self, route):
        sites = {f"{i:08d}": (11, 5) for i in range(25)}
        calls = route({sg.STAT_SERVICE: "site_no\n5s\n"})
        sg.fetch_daily_stats(sites)
        assert sorted(len(p["sites"].split(",")) for _, p in calls) == [5, 10, 10]
        assert sg.MAX_STAT_SITES == 10

    def test_no_sites_makes_no_request(self, route):
        calls = route({})
        assert sg.fetch_daily_stats({}) == {}
        assert calls == []

    def test_a_failed_batch_does_not_lose_the_others(self, route):
        sites = {f"{i:08d}": (11, 5) for i in range(12)}  # two batches: 00000000-9 and 10-11
        sites["02296750"] = (11, 5)                        # sorts after them: the second batch

        def respond(params):
            if "00000000" in params["sites"]:
                raise requests.ConnectionError("down")
            return STATS_TEXT
        route({sg.STAT_SERVICE: respond})
        assert set(sg.fetch_daily_stats(sites)) == {"02296750"}

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), requests.Timeout("slow"),
                                       requests.HTTPError("400")])
    def test_every_batch_failing_gives_no_statistics_not_an_error(self, route, error):
        route({sg.STAT_SERVICE: error})
        assert sg.fetch_daily_stats({"02296750": (11, 5)}) == {}

    def test_an_unexpected_response_gives_no_statistics_not_an_error(self, route):
        route({sg.STAT_SERVICE: "<html>Error report</html>"})
        assert sg.fetch_daily_stats({"02296750": (11, 5)}) == {}
        route({sg.STAT_SERVICE: "site_no\tmonth_nu\n5s\t2n\n02296750\tNov\n"})  # not a number
        assert sg.fetch_daily_stats({"02296750": (11, 5)}) == {}


class TestPercentileLabel:
    def lt(self, status="within", pct=88.0):
        return gage()["longterm"] | {"status": status, "pct": pct}

    def test_within_the_record(self):
        assert sg.percentile_label({"longterm": self.lt(pct=87.9), "pct": 100.0}) == "p88"

    def test_above_and_below_the_record(self):
        assert sg.percentile_label({"longterm": self.lt("above", 100.0), "pct": 100.0}) == "REC"
        assert sg.percentile_label({"longterm": self.lt("below", 0.0), "pct": 0.0}) == "LOW"

    def test_with_no_statistics_the_yearly_percentile_is_marked(self):
        assert sg.percentile_label({"longterm": None, "pct": 99.7}) == "p100*"

    def test_with_nothing_at_all(self):
        assert sg.percentile_label({"longterm": None, "pct": None}) == "n/a"
        assert sg.percentile_label({}) == "n/a"


class TestBuildGagesWithStatistics:
    @pytest.fixture
    def river(self, monkeypatch, route):
        sites = {"02296750": ("PEACE RIVER AT SR 70 AT ARCADIA, FL", 27.22, -81.88),
                 "02294898": ("PEACE RIVER AT FORT MEADE FL", 27.75, -81.78),
                 "02299999": ("PEACE RIVER NO STATISTICS FL", 27.0, -82.0)}
        monkeypatch.setattr(sg, "fetch_river", lambda anchor, km: ([], sites))
        def at(text):
            return datetime.fromisoformat(text).timestamp()
        monkeypatch.setattr(sg, "fetch_readings", lambda ids, as_of=None: {
            "02296750": [(at("2026-11-05T12:30:00-05:00"), 16.15)],   # above the record for Nov 5
            "02294898": [(at("2026-11-05T23:45:00-05:00"), 74.48)],   # still Nov 5 on its own clock
            "02299999": [(at("2026-11-05T12:00:00-05:00"), 3.0)]})
        monkeypatch.setattr(sg, "fetch_time_zones", lambda ids: {site: ("EST", True) for site in ids})
        monkeypatch.setattr(sg, "STALE_AFTER", timedelta(days=36500))  # count these old readings as current
        monkeypatch.setattr(sg, "fetch_history", lambda site, as_of=None: [])
        monkeypatch.setattr(sg, "fetch_catalog", lambda ids: {})
        monkeypatch.setattr(sg, "fetch_nws", lambda site, lat, lon, gauges: (
            {"lid": "ARCF1", "stages": {"major": 16}} if site == "02296750" else None))
        monkeypatch.setattr(sg, "fetch_forecast", lambda lid, stages: None)
        return route({sg.NWPS: {"gauges": []}, sg.STAT_SERVICE: STATS_TEXT})

    def test_every_gage_gets_its_long_term_percentile_or_none(self, river):
        by_site = {g["site"]: g for g in sg.build_gages("anchor", 322)[1]}
        assert by_site["02296750"]["longterm"]["status"] == "above"    # a circle gets it too
        assert by_site["02296750"]["method"] == "nws"
        assert by_site["02294898"]["longterm"]["pct"] == pytest.approx(87.9, abs=0.05)
        assert by_site["02299999"]["longterm"] is None

    def test_the_statistics_are_requested_once_for_every_reporting_gage(self, river):
        sg.build_gages("anchor", 322)
        stat_calls = [p for url, p in river if url == sg.STAT_SERVICE]
        assert len(stat_calls) == 1
        assert stat_calls[0]["sites"] == "02294898,02296750,02299999"

    def test_a_failed_statistics_request_leaves_the_rest_of_the_map_intact(self, river, monkeypatch):
        monkeypatch.setattr(sg, "fetch_daily_stats", lambda dates: {})
        gages = sg.build_gages("anchor", 322)[1]
        assert len(gages) == 3 and all(g["longterm"] is None for g in gages)


class TestMainWithLongTerm:
    def test_the_terminal_column_shows_the_long_term_percentile(self, make_page, capsys):
        above = gage("02294161", "Peace Creek Near Bartow FL",
                     longterm=gage()["longterm"] | {"status": "above", "pct": 100.0})
        make_page([gage(), above, gage("02299999", "No Stats FL", longterm=None, pct=99.7)])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].split()[3] == "p94"
        assert lines[1].split()[3] == "REC"
        assert lines[2].split()[3] == "p100*"

    def test_the_page_embeds_the_long_term_data(self, make_page):
        page = make_page([gage()])
        assert '"longterm": {"pct": 94.0, "status": "within"' in page
