"""Sites that are not on the map: what they are, the summary line, and the --retired layer."""

import collections
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
import requests

import streamgage as sg
from tests.conftest import FIXTURES, gage, retired_gage

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def row(parm="00065", kind="dv", begin="2000-01-01", end="2005-09-29", site="01234567"):
    """One line of a site catalog: a data series a site has."""
    return {"site_no": site, "parm_cd": parm, "data_type_cd": kind, "begin_date": begin,
            "end_date": end}


def days_ago(n):
    return (NOW - timedelta(days=n)).date().isoformat()


# five real Peace River sites: retired (Punta Gorda, Nocatee), field visits, water quality, and live (Arcadia)
CATALOG_SITES = ["02294648", "02295977", "02296750", "02297105", "02298300"]


def catalog_routes():
    return {sg.TIME_SERIES: (FIXTURES / "series_catalog.csv").read_text(),
            sg.FIELD_MEASUREMENTS: (FIXTURES / "field_measurements.csv").read_text(),
            sg.SAMPLE_SITES: json.loads((FIXTURES / "sample_sites.json").read_text())}


@pytest.fixture
def catalog_rows(route):
    """The recorded catalog of the five sites, as fetch_catalog builds it."""
    route(catalog_routes())
    return collections.defaultdict(list, sg.fetch_catalog(CATALOG_SITES))


class TestClassifySite:
    def test_a_stage_record_that_ended_years_ago_is_retired(self, catalog_rows):
        category, info = sg.classify_site(catalog_rows["02298300"], NOW)  # Punta Gorda
        assert category == "retired"
        assert info == {"last": "2005-09-29", "first": "2002-09-30",
                        "params": ["specific conductance", "stage", "water temperature"]}

    def test_nocatee_is_retired_and_reports_both_stage_and_discharge(self, catalog_rows):
        category, info = sg.classify_site(catalog_rows["02297105"], NOW)
        assert (category, info["last"], info["params"]) == (
            "retired", "2016-10-03", ["discharge", "stage"])

    def test_only_field_visits(self, catalog_rows):
        assert sg.classify_site(catalog_rows["02294648"], NOW) == ("field_visits", {})

    def test_only_water_quality_samples(self, catalog_rows):
        assert {r["data_type_cd"] for r in catalog_rows["02295977"]} == {"qw"}  # not in the series catalog at all
        assert sg.classify_site(catalog_rows["02295977"], NOW) == ("water_quality", {})

    def test_no_catalog_rows_at_all(self):
        assert sg.classify_site([], NOW) == ("no_data", {})

    def test_recent_discharge_without_stage_is_discharge_only(self, catalog_rows):
        # Arcadia has live discharge; the caller only asks about sites not reporting stage
        assert sg.classify_site(catalog_rows["02296750"], NOW)[0] == (
            "discharge_only")

    def test_discharge_that_stopped_is_not_discharge_only(self):
        assert sg.classify_site([row("00060", end=days_ago(10))], NOW)[0] == "offline"

    @pytest.mark.parametrize("age, expected", [
        (1, "offline"), (60, "offline"), (364, "offline"), (365, "offline"), (366, "retired"),
        (4000, "retired"),
    ])
    def test_offline_versus_retired_is_a_year_since_the_last_data(self, age, expected):
        assert sg.classify_site([row(end=days_ago(age))], NOW)[0] == expected

    def test_last_and_first_come_from_the_stage_and_flow_series(self):
        rows = [row("00065", begin="2003-02-01", end="2004-05-05"),
                row("00060", begin="2001-06-01", end="2002-01-01"),
                row("00010", begin="1990-01-01", end="2010-01-01")]  # water temperature
        category, info = sg.classify_site(rows, NOW)
        assert (category, info["first"], info["last"]) == ("retired", "2001-06-01", "2004-05-05")
        assert info["params"] == ["discharge", "stage", "water temperature"]

    def test_unknown_parameters_are_left_out_of_the_measured_list(self):
        rows = [row("00065"), row("99999")]
        assert sg.classify_site(rows, NOW)[1]["params"] == ["stage"]

    def test_a_water_temperature_logger_is_not_a_gage(self):
        category, _ = sg.classify_site([row("00010", end=days_ago(10))], NOW)
        assert category == "other"

    def test_series_with_no_end_date_are_ignored(self):
        assert sg.classify_site([row(end="")], NOW)[0] == "other"

    @pytest.mark.parametrize("kinds, expected", [
        (["sv"], "field_visits"), (["qw"], "water_quality"), (["sv", "qw"], "field_visits"),
        (["pk"], "other"), (["ad", "pk"], "other"),
    ])
    def test_sites_without_a_continuous_record(self, kinds, expected):
        rows = [row("00060", kind=k) for k in kinds]
        assert sg.classify_site(rows, NOW)[0] == expected

    def test_now_defaults_to_the_clock(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        assert sg.classify_site([row(end=yesterday)])[0] == "offline"
        assert sg.classify_site([row(end="2005-09-29")])[0] == "retired"


class TestFetchCatalog:
    def test_rows_take_the_legacy_catalog_shape(self, catalog_rows):
        punta_gorda = catalog_rows["02298300"]
        assert {"parm_cd": "00065", "data_type_cd": "dv", "begin_date": "2002-09-30",
                "end_date": "2005-09-29"} in punta_gorda
        assert {r["data_type_cd"] for r in catalog_rows["02297105"]} == {"uv", "dv", "other"}  # other: annual peaks

    def test_a_series_with_no_dates_holds_no_data_and_is_left_out(self, catalog_rows):
        assert "00950" in (FIXTURES / "series_catalog.csv").read_text()  # Arcadia lists one, with no dates
        assert not any(r["parm_cd"] == "00950" for r in catalog_rows["02296750"])
        # the ditch's only series is such an empty one, so its field visits decide what it is
        assert {r["data_type_cd"] for r in catalog_rows["02294648"]} == {"sv"}

    def test_an_old_record_the_catalog_lists_without_dates_is_no_data(self, route):
        # Ohio River at Bellaire: daily discharge to 1958 in the legacy catalog; the new one lists the
        # series with no dates, and has none of its values
        route({sg.TIME_SERIES: "monitoring_location_id,parameter_code,computation_period_identifier,begin,end\n"
                               "USGS-03113600,00060,Daily,,\n",
               sg.FIELD_MEASUREMENTS: "monitoring_location_id\n", sg.SAMPLE_SITES: {"features": []}})
        assert sg.classify_site(sg.fetch_catalog(["03113600"]).get("03113600", []), NOW)[0] == "no_data"

    def test_a_reservoir_with_a_lake_level_record_is_other_whatever_its_visits(self, route):
        # Berlin Lake: a live lake-level record (62615) and field measurements
        calls = route({sg.TIME_SERIES: "monitoring_location_id,parameter_code,computation_period_identifier,begin,end\n"
                                       "USGS-03090000,62615,Points,2007-10-01 04:00:00+00:00,2026-11-04 04:00:00+00:00\n"})
        assert sg.classify_site(sg.fetch_catalog(["03090000"])["03090000"], NOW)[0] == "other"
        assert [url for url, _ in calls] == [sg.TIME_SERIES]  # no visits or samples asked about

    def test_asks_for_every_series_of_every_site(self, route):
        calls = route(catalog_routes())
        sg.fetch_catalog(CATALOG_SITES)
        (url, params), *_ = calls
        assert url == sg.TIME_SERIES
        assert params["monitoring_location_id"] == ",".join(f"USGS-{s}" for s in CATALOG_SITES)
        assert "parameter_code" not in params

    def test_visits_and_samples_are_asked_only_about_sites_with_no_continuous_record(self, route):
        calls = route(catalog_routes())
        sg.fetch_catalog(CATALOG_SITES)
        asked = {url: p for url, p in calls}
        # Punta Gorda, Nocatee and Arcadia have stage records; the ditch was visited, so only Brownville is left
        assert asked[sg.FIELD_MEASUREMENTS]["monitoring_location_id"] == "USGS-02294648,USGS-02295977"
        assert asked[sg.SAMPLE_SITES]["monitoringLocationIdentifier"] == [
            "USGS-02295977"]

    def test_a_catalog_of_gages_needs_no_more_requests(self, route):
        calls = route({sg.TIME_SERIES: (FIXTURES / "series_catalog.csv").read_text()})
        sg.fetch_catalog(["02297105", "02298300"])
        assert [url for url, _ in calls] == [sg.TIME_SERIES]

    def test_series_requests_go_in_batches(self, route):
        calls = route({sg.TIME_SERIES: "monitoring_location_id\n", sg.FIELD_MEASUREMENTS: "monitoring_location_id\n",
                       sg.SAMPLE_SITES: {"features": []}})
        sg.fetch_catalog([f"{i:08d}" for i in range(250)])
        sizes = sorted(len(p["monitoring_location_id"].split(",")) for url, p in calls if url == sg.TIME_SERIES)
        assert sizes == [50, 100, 100]

    def test_no_sites_makes_no_request(self, route):
        calls = route({})
        assert sg.fetch_catalog([]) == {}
        assert calls == []

    def test_a_failed_request_is_raised(self, route):
        route({sg.TIME_SERIES: requests.ConnectionError("down")})
        with pytest.raises(requests.ConnectionError):
            sg.fetch_catalog(CATALOG_SITES)


PEACE_RIVER_COUNTS = {"retired": 7, "field_visits": 13, "water_quality": 5, "no_data": 4}


class TestDescribeSites:
    def test_the_peace_river_breakdown_with_the_hint(self):
        assert sg.describe_sites(45, 16, PEACE_RIVER_COUNTS, 7, False) == (
            "45 sites on river: 16 reporting stage; 29 not (7 retired, 13 field visits only, "
            "5 water quality only, 4 no data); --retired draws the 7 that no longer report")

    def test_when_drawing_it_says_so_instead_of_hinting(self):
        msg = sg.describe_sites(45, 16, PEACE_RIVER_COUNTS, 7, True)
        assert msg.endswith("; drawing the 7 that no longer report")
        assert "--retired" not in msg

    def test_no_hint_when_there_is_nothing_to_draw(self):
        counts = {"field_visits": 3, "no_data": 1}
        msg = sg.describe_sites(10, 6, counts, 0, False)
        assert msg == "10 sites on river: 6 reporting stage; 4 not (3 field visits only, 1 no data)"

    def test_zero_categories_are_left_out_and_order_is_fixed(self):
        counts = {"no_data": 1, "offline": 2, "discharge_only": 1}
        msg = sg.describe_sites(8, 4, counts, 2, False)
        assert "(1 discharge only, 2 offline, 1 no data)" in msg

    def test_everything_reporting(self):
        assert sg.describe_sites(16, 16, {}, 0, False) == "16 sites on river: 16 reporting stage"

    def test_without_site_details(self):
        assert sg.describe_sites(45, 16, None, 0, False) == (
            "45 sites on river: 16 reporting stage; 29 not (site details unavailable)")

    def test_every_category_has_a_label(self):
        keys = {key for key, _ in sg.CATEGORY_LABELS}
        assert keys == {"discharge_only", "offline", "retired", "field_visits", "water_quality",
                        "other", "no_data"}


SITES = {"A0000001": ("RIVER AT NORTH FL", 27.5, -81.8), "B0000002": ("RIVER AT SOUTH FL", 27.0, -82.0),
         "C0000003": ("RIVER OLD GAGE NEAR MIDDLE FL", 27.2, -81.9),
         "D0000004": ("RIVER OFFLINE GAGE FL", 27.4, -81.85),
         "E0000005": ("RIVER FIELD SITE FL", 27.1, -81.95), "F0000006": ("RIVER EMPTY SITE FL", 27.3, -81.9)}


@pytest.fixture
def river(monkeypatch):
    """Two reporting gages and four that are not: retired, offline, field visits, no data."""
    monkeypatch.setattr(sg, "fetch_river", lambda anchor, km: ([], SITES))
    when = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr(sg, "fetch_readings", lambda ids, as_of=None: {"A0000001": [(when, 2.0)],
                                                                       "B0000002": [(when, 3.0)]})
    monkeypatch.setattr(sg, "fetch_time_zones", lambda ids: {})
    monkeypatch.setattr(sg, "fetch_histories", lambda ids, as_of=None: {})
    monkeypatch.setattr(sg, "fetch_nws", lambda *a: None)
    monkeypatch.setattr(sg, "fetch_daily_stats", lambda dates: {})
    monkeypatch.setattr(sg, "get_json", lambda url, **p: {"gauges": []})
    asked = []
    recent = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()

    def fake_catalog(ids):
        asked.append(list(ids))
        return {"C0000003": [row(site="C0000003", begin="1990-01-01", end="2001-09-30")],
                "D0000004": [row("00060", site="D0000004", begin="2010-01-01", end=recent)],
                "E0000005": [row(kind="sv", site="E0000005")]}
    monkeypatch.setattr(sg, "fetch_catalog", fake_catalog)
    return asked


class TestBuildGagesWithRetired:
    def test_only_sites_not_reporting_are_looked_up(self, river):
        sg.build_gages("anchor", 322)
        assert len(river) == 1 and sorted(river[0]) == ["C0000003", "D0000004", "E0000005", "F0000006"]

    def test_the_summary_line_breaks_down_where_the_sites_went(self, river, capsys):
        sg.build_gages("anchor", 322)
        err = capsys.readouterr().err
        assert ("6 sites on river: 2 reporting stage; 4 not (1 offline, 1 retired, "
                "1 field visits only, 1 no data); --retired draws the 2 that no longer report") in err

    def test_retired_gages_are_only_returned_when_asked_for(self, river):
        assert sg.build_gages("anchor", 322)[2] == []
        assert len(sg.build_gages("anchor", 322, draw_retired=True)[2]) == 2

    def test_asking_changes_the_summary_line(self, river, capsys):
        sg.build_gages("anchor", 322, draw_retired=True)
        assert "drawing the 2 that no longer report" in capsys.readouterr().err

    def test_retired_gage_details(self, river):
        retired = sg.build_gages("anchor", 322, draw_retired=True)[2]
        old = next(g for g in retired if g["site"] == "C0000003")
        assert old == {"site": "C0000003", "name": "River Old Gage Near Middle FL", "lat": 27.2,
                       "lon": -81.9, "status": "retired", "last": "2001-09-30",
                       "first": "1990-01-01", "params": ["stage"]}
        offline = next(g for g in retired if g["site"] == "D0000004")
        assert offline["status"] == "offline" and offline["params"] == ["discharge"]

    def test_retired_gages_are_sorted_north_to_south(self, river):
        retired = sg.build_gages("anchor", 322, draw_retired=True)[2]
        assert [g["lat"] for g in retired] == [27.4, 27.2]

    def test_field_visit_and_empty_sites_are_never_drawn(self, river):
        retired = sg.build_gages("anchor", 322, draw_retired=True)[2]
        assert {g["site"] for g in retired}.isdisjoint({"E0000005", "F0000006"})

    def test_the_mapped_gages_do_not_change(self, river):
        _, gages, _ = sg.build_gages("anchor", 322, draw_retired=True)
        assert [g["site"] for g in gages] == ["A0000001", "B0000002"]

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), requests.HTTPError("500"),
                                       KeyError("site_no"), ValueError("bad")])
    def test_a_failed_catalog_does_not_stop_the_run(self, river, monkeypatch, capsys, error):
        def broken(ids):
            raise error
        monkeypatch.setattr(sg, "fetch_catalog", broken)
        _, gages, retired = sg.build_gages("anchor", 322, draw_retired=True)
        assert len(gages) == 2 and retired == []
        assert "4 not (site details unavailable; retired gages cannot be drawn this time)" in capsys.readouterr().err

    def test_without_retired_gages_asked_for_a_failed_catalog_says_only_that(self, river, monkeypatch, capsys):
        def broken(ids):
            raise requests.ConnectionError("down")
        monkeypatch.setattr(sg, "fetch_catalog", broken)
        sg.build_gages("anchor", 322)
        assert "4 not (site details unavailable)\n" in capsys.readouterr().err

    def test_everything_reporting_makes_no_catalog_request(self, river, monkeypatch):
        sites = {k: v for k, v in SITES.items() if k in ("A0000001", "B0000002")}
        monkeypatch.setattr(sg, "fetch_river", lambda anchor, km: ([], sites))
        sg.build_gages("anchor", 322)
        assert river == []


class TestMainWithRetired:
    def test_the_flag_reaches_build_gages(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(sg, "resolve_anchor", lambda *a, **k: "01234567")
        monkeypatch.setattr(sg, "build_gages",
                            lambda anchor, km, draw=False, as_of=None: seen.update(draw=draw) or ([], [gage()], []))
        for args, expected in (([], False), (["--retired"], True)):
            monkeypatch.setattr("sys.argv", ["streamgage.py", "River", "--out", str(tmp_path / "x.html"),
                                             *args])
            sg.main()
            assert seen["draw"] is expected

    def test_the_page_embeds_the_retired_gages(self, make_page):
        retired = [retired_gage(), retired_gage("02297105", "Peace River At Nocatee FL", lat=27.0)]
        page = make_page([gage()], retired=retired)
        embedded = json.loads(re.search(r"const RETIRED = (.*?);\nconst GENERATED", page, re.DOTALL)[1])
        assert embedded == retired

    def test_without_retired_gages_the_page_has_an_empty_list(self, make_page):
        assert "const RETIRED = [];" in make_page([gage()])

    def test_retired_gages_are_listed_in_the_terminal_output(self, make_page, capsys):
        make_page([gage()], retired=[retired_gage(), retired_gage("02295203", "Peace River At SH 664A",
                                                                  last="2003-09-29", status="offline")])
        out = capsys.readouterr().out
        assert "No longer reporting stage (2):" in out
        assert "02298300  last data 2005-09-29  retired  Peace River At Punta Gorda FL" in out
        assert "02295203  last data 2003-09-29  offline  Peace River At SH 664A" in out

    def test_no_retired_section_when_there_are_none(self, make_page, capsys):
        make_page([gage()])
        assert "No longer reporting" not in capsys.readouterr().out

    def test_hostile_retired_names_cannot_end_the_script_block(self, make_page):
        evil = retired_gage(name="</script><img src=x onerror=alert(1)>")
        page = make_page([gage()], retired=[evil])
        assert page.count("</script>") == sg.HTML.count("</script>")

    def test_the_flag_is_documented_in_help(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["streamgage.py", "--help"])
        with pytest.raises(SystemExit):
            sg.main()
        assert "--retired" in capsys.readouterr().out
