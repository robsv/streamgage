"""--as-of: the river as it was at a past moment."""

import re
from datetime import datetime, timedelta, timezone

import pytest

import streamgage as sg
from tests.conftest import continuous_csv, gage, readings

EST = timezone(timedelta(hours=-5))
MOMENT = datetime(2026, 11, 3, 12, 0, tzinfo=EST)  # Tuesday noon, Eastern Standard Time


class TestParseAsOf:
    @pytest.mark.parametrize("text", [
        "2026-11-03T12:00-05:00",
        "2026-11-03T12:00:00-05:00",
        "2026-11-03 12:00-05:00",
        "2026-11-03T17:00Z",
        "2026-11-03T17:00z",
        "2026-11-03T17:00:00+00:00",
        "  2026-11-03T12:00-05:00  ",
    ])
    def test_reads_the_same_instant_however_it_is_written(self, text):
        assert sg.parse_as_of(text) == MOMENT

    def test_keeps_the_offset_it_was_given(self):
        assert sg.parse_as_of("2026-11-03T12:00-05:00").utcoffset() == timedelta(hours=-5)
        assert sg.parse_as_of("2026-11-03T17:00Z").utcoffset() == timedelta(0)

    @pytest.mark.parametrize("text", ["2026-11-03T12:00", "2026-11-03 12:00:00", "2026-11-03"])
    def test_a_time_with_no_offset_is_refused_rather_than_guessed(self, text):
        with pytest.raises(ValueError, match="no UTC offset"):
            sg.parse_as_of(text)

    def test_the_message_shows_how_to_fix_it(self):
        with pytest.raises(ValueError) as exc:
            sg.parse_as_of("2026-11-03T12:00")
        assert "2026-11-03T12:00-05:00" in str(exc.value) and "Z" in str(exc.value)

    @pytest.mark.parametrize("text", ["last tuesday", "", "   ", "11/03/2026 12:00 EST", "2026-13-45T00:00Z"])
    def test_something_that_is_not_a_time(self, text):
        with pytest.raises(ValueError, match="cannot read"):
            sg.parse_as_of(text)

    def test_a_time_that_has_not_happened_yet_is_refused(self):
        now = datetime(2026, 11, 3, 16, 59, tzinfo=timezone.utc)  # one minute before the moment
        with pytest.raises(ValueError, match="in the future"):
            sg.parse_as_of("2026-11-03T12:00-05:00", now=now)
        with pytest.raises(ValueError, match="in the future"):
            sg.parse_as_of("2099-01-01T00:00Z")

    def test_the_present_and_the_past_are_fine(self):
        assert sg.parse_as_of("2026-11-03T12:00-05:00", now=datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc))
        assert sg.parse_as_of("2026-11-03T12:00-05:00", now=datetime(2030, 1, 1, tzinfo=timezone.utc))


class TestDescribeMoment:
    @pytest.mark.parametrize("when, expected", [
        (MOMENT, "2026-11-03 12:00 UTC-05:00"),
        (datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc), "2026-11-03 17:00 UTC+00:00"),
        (datetime(2026, 7, 1, 9, 5, tzinfo=timezone(timedelta(hours=5, minutes=30))), "2026-07-01 09:05 UTC+05:30"),
    ])
    def test_the_label_on_the_page(self, when, expected):
        assert sg.describe_moment(when) == expected


class TestRequestsForAPastMoment:
    def test_the_week_of_readings_ends_at_the_moment(self, route):
        calls = route({sg.CONTINUOUS_ITEMS: continuous_csv()})
        sg.fetch_readings(["02296750"], MOMENT)
        assert calls[0][1]["datetime"] == "2026-10-27T12:00-05:00/2026-11-03T12:00-05:00"

    def test_a_utc_moment_is_sent_with_its_own_offset(self, route):
        calls = route({sg.CONTINUOUS_ITEMS: continuous_csv()})
        sg.fetch_readings(["02296750"], datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc))
        assert calls[0][1]["datetime"].endswith("/2026-11-03T17:00+00:00")

    def test_recent_staleness_is_judged_against_the_moment_not_today(self):
        two_days_before = (MOMENT - timedelta(days=2)).timestamp()
        # two days old at that moment: fresh then, although it is long past by today's clock
        assert sg.latest_readings({"02296750": [(two_days_before, 9.5)]}, MOMENT) == {
            "02296750": (9.5, two_days_before)}

    def test_the_year_of_daily_values_ends_on_the_moments_date(self, route, fixture):
        calls = route({sg.DAILY_ITEMS: fixture("daily_three_gages.csv")})
        sg.fetch_histories(["02294161"], MOMENT)
        assert calls[0][1]["datetime"] == "2025-11-03/2026-11-03"

    def test_the_moments_own_date_is_used_not_the_utc_one(self, route, fixture):
        calls = route({sg.DAILY_ITEMS: fixture("daily_three_gages.csv")})
        sg.fetch_histories(["02294161"], datetime(2026, 11, 3, 22, 0, tzinfo=EST))  # 3 a.m. on the 4th in UTC
        assert calls[0][1]["datetime"].endswith("/2026-11-03")


@pytest.fixture
def river(monkeypatch):
    """Two gages as of MOMENT: Arcadia (NWS, with stages) and a diamond; records what it was asked."""
    asked = {"readings": [], "history": [], "forecast": [], "catalog_now": []}
    sites = {"02296750": ("PEACE RIVER AT SR 70 AT ARCADIA, FL", 27.22, -81.88),
             "02294898": ("PEACE RIVER AT FORT MEADE FL", 27.75, -81.78),
             "02299999": ("PEACE RIVER OLD GAGE FL", 27.0, -82.0)}
    monkeypatch.setattr(sg, "fetch_river", lambda anchor, km: ([], sites))

    def fetch_readings(ids, as_of=None):
        asked["readings"].append(as_of)
        end = (as_of or datetime.now(timezone.utc)).timestamp()
        return {"02296750": readings(16.0, end, slope=0.05), "02294898": readings(74.5, end, slope=0.05)}
    monkeypatch.setattr(sg, "fetch_readings", fetch_readings)
    monkeypatch.setattr(sg, "fetch_time_zones", lambda ids: {site: ("EST", True) for site in ids})
    monkeypatch.setattr(sg, "fetch_histories", lambda ids, as_of=None: asked["history"].append((list(ids), as_of))
                        or {site: [float(i) for i in range(40)] for site in ids})
    monkeypatch.setattr(sg, "fetch_nws", lambda site, lat, lon, gauges: (
        {"lid": "ARCF1", "stages": {"action": 9, "minor": 11, "moderate": 14, "major": 16}}
        if site == "02296750" else None))
    monkeypatch.setattr(sg, "fetch_forecast", lambda lid, stages: asked["forecast"].append(lid) or {"crest": 99})
    monkeypatch.setattr(sg, "fetch_daily_stats", lambda dates: {})
    old = {"02299999": [{"site_no": "02299999", "parm_cd": "00065", "data_type_cd": "dv",
                         "begin_date": "2000-01-01", "end_date": "2025-11-04"}]}
    monkeypatch.setattr(sg, "fetch_catalog", lambda ids: old)
    monkeypatch.setattr(sg, "get_json", lambda url, **p: {"gauges": []})
    return asked


class TestBuildGagesAsOf:
    def test_the_moment_reaches_every_fetch(self, river):
        sg.build_gages("anchor", 322, as_of=MOMENT)
        assert river["readings"] == [MOMENT]  # one batched request for every gage's readings
        assert river["history"] == [(["02294898"], MOMENT)]  # only the one diamond needs a year of history

    def test_without_a_moment_nothing_is_dated(self, river):
        sg.build_gages("anchor", 322)
        assert river["readings"] == [None] and river["history"] == [(["02294898"], None)]

    def test_there_are_no_forecasts_for_a_past_moment(self, river):
        gages = {g["site"]: g for g in sg.build_gages("anchor", 322, as_of=MOMENT)[1]}
        assert river["forecast"] == []  # never even asked
        arcadia = gages["02296750"]
        assert arcadia["method"] == "nws" and arcadia["forecast"] is None
        assert arcadia["category"] == "major" and arcadia["lid"] == "ARCF1"  # flood stages still apply

    def test_a_forecast_is_still_fetched_for_now(self, river):
        gages = {g["site"]: g for g in sg.build_gages("anchor", 322)[1]}
        assert river["forecast"] == ["ARCF1"] and gages["02296750"]["forecast"] == {"crest": 99}

    def test_the_trend_is_judged_at_that_moment_not_by_todays_clock(self, river):
        # the readings end at MOMENT, days in the past. Judged against now they would be stale and have no trend.
        gages = {g["site"]: g for g in sg.build_gages("anchor", 322, as_of=MOMENT)[1]}
        assert gages["02296750"]["recent"]["rate"] == pytest.approx(0.05)
        assert gages["02296750"]["recent"]["delta24"] == pytest.approx(0.05 * 24)

    def test_the_same_readings_are_not_current_when_judged_against_today(self, river, monkeypatch):
        # years old by today's clock, so no gage counts as reporting
        monkeypatch.setattr(sg, "fetch_readings", lambda ids, as_of=None: {
            "02296750": readings(16.0, MOMENT.timestamp(), slope=0.05)})
        with pytest.raises(SystemExit):
            sg.build_gages("anchor", 322)

    def test_the_time_is_given_on_the_gages_own_clock(self, river):
        gages = {g["site"]: g for g in sg.build_gages("anchor", 322, as_of=MOMENT)[1]}
        assert gages["02296750"]["time"] == "2026-11-03T12:00:00-05:00"

    def test_the_retired_test_uses_the_moments_date(self, river):
        # last data 2025-11-04: 364 days before MOMENT, so still just "offline" then. Today it is "retired".
        retired = sg.build_gages("anchor", 322, draw_retired=True, as_of=MOMENT)[2]
        assert [(g["site"], g["status"]) for g in retired] == [("02299999", "offline")]

    def test_the_summary_says_what_it_is_showing(self, river, capsys):
        sg.build_gages("anchor", 322, as_of=MOMENT)
        err = capsys.readouterr().err
        assert "Showing the river as of 2026-11-03 12:00 UTC-05:00" in err
        assert "no forecast rings" in err

    def test_the_summary_is_silent_about_it_for_the_present(self, river, capsys):
        sg.build_gages("anchor", 322)
        assert "Showing the river as of" not in capsys.readouterr().err


class TestMainAsOf:
    @pytest.fixture
    def captured(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        seen = {}
        monkeypatch.setattr(sg, "resolve_anchor", lambda *a, **k: "01234567")

        def build(anchor, km, draw=False, as_of=None):
            seen["as_of"] = as_of
            return [], [gage()], []
        monkeypatch.setattr(sg, "build_gages", build)
        return seen

    def run(self, monkeypatch, *args):
        monkeypatch.setattr("sys.argv", ["streamgage.py", "Peace River", *args])
        sg.main()

    def test_the_parsed_moment_reaches_build_gages(self, monkeypatch, captured):
        self.run(monkeypatch, "--as-of", "2026-11-03T17:00Z")
        assert captured["as_of"] == MOMENT

    def test_no_flag_means_now(self, monkeypatch, captured):
        self.run(monkeypatch)
        assert captured["as_of"] is None

    def test_the_default_file_name_carries_the_moment(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "--as-of", "2026-11-03T12:00-05:00")
        assert (tmp_path / "peace_river_2026-11-03_1200_gages.html").exists()
        assert not (tmp_path / "peace_river_gages.html").exists()

    def test_a_past_file_does_not_overwrite_the_current_one(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch)
        self.run(monkeypatch, "--as-of", "2026-11-03T12:00-05:00")
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "peace_river_2026-11-03_1200_gages.html", "peace_river_gages.html"]

    def test_an_explicit_out_wins(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "--as-of", "2026-11-03T12:00-05:00", "--out", str(tmp_path / "storm.html"))
        assert (tmp_path / "storm.html").exists()

    def test_the_page_says_when_it_is_from(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "--as-of", "2026-11-03T12:00-05:00", "--out", "p.html")
        page = (tmp_path / "p.html").read_text(encoding="utf-8")
        assert 'const AS_OF = "2026-11-03 12:00 UTC-05:00";' in page
        assert re.search(r'const GENERATED = "\d{4}-\d\d-\d\d \d\d:\d\d UTC"', page)  # when it was made, too

    def test_a_present_day_page_has_no_moment(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "--out", "p.html")
        assert "const AS_OF = null;" in (tmp_path / "p.html").read_text(encoding="utf-8")

    @pytest.mark.parametrize("text, message", [
        ("2026-11-03T12:00", "no UTC offset"),
        ("2099-01-01T00:00Z", "in the future"),
        ("last tuesday", "cannot read"),
    ])
    def test_a_bad_moment_is_an_argument_error(self, monkeypatch, captured, capsys, text, message):
        with pytest.raises(SystemExit) as exc:
            self.run(monkeypatch, "--as-of", text)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--as-of:" in err and message in err
        assert "as_of" not in captured  # it stopped before building anything

    def test_help_describes_it(self, monkeypatch, captured, capsys):
        with pytest.raises(SystemExit):
            self.run(monkeypatch, "--help")
        out = " ".join(capsys.readouterr().out.split())
        assert "--as-of TIME" in out and "UTC offset" in out and "no NWS forecasts" in out
