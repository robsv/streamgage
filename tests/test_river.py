"""Finding the river: name lookup, anchor choice, the NLDI walk, and current readings."""

from datetime import datetime, timedelta, timezone

import pytest

import streamgage as sg
from tests.conftest import continuous_csv


def monitoring(*rows):
    """A monitoring-locations response from (number, name, state, drainage area) tuples."""
    return {"features": [{"properties": {"monitoring_location_number": n,
                                         "monitoring_location_name": name,
                                         "state_name": state, "drainage_area": area}}
                         for n, name, state, area in rows]}


class TestFindCandidates:
    def test_returns_the_largest_basin_first(self, route):
        route({sg.MONITORING: monitoring(
            ("02294650", "PEACE RIVER AT SR 60 AT BARTOW, FL", "Florida", 390.0),
            ("02298300", "PEACE RIVER AT PUNTA GORDA FL", "Florida", 1367.0),
            ("02296750", "PEACE RIVER AT SR 70 AT ARCADIA, FL", "Florida", 1367.5))})
        assert [c["site"] for c in sg.find_candidates("Peace River")] == [
            "02296750", "02298300", "02294650"]

    def test_skips_sites_that_are_not_eight_digits(self, route):
        route({sg.MONITORING: monitoring(
            ("02296750", "PEACE RIVER AT ARCADIA", "Florida", 1367.0),
            ("272330081543200", "PEACE RIVER TEST SITE", "Florida", None))})
        assert [c["site"] for c in sg.find_candidates("Peace River")] == ["02296750"]

    def test_gages_without_a_basin_area_sort_last(self, route):
        route({sg.MONITORING: monitoring(
            ("02290001", "PEACE RIVER NO AREA", "Florida", None),
            ("02290002", "PEACE RIVER SMALL", "Florida", 5.0))})
        assert [c["site"] for c in sg.find_candidates("Peace River")] == ["02290002", "02290001"]

    def test_builds_the_name_filter(self, route):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates("  Peace River ")
        # The filter is case-sensitive and USGS names are upper case in some states, mixed in others,
        # so both spellings go in one query
        assert len(calls) == 1
        _, params = calls[0]
        assert params["filter"] == ("monitoring_location_name LIKE 'Peace River %' "
                                    "OR monitoring_location_name LIKE 'PEACE RIVER %'")
        assert params["filter-lang"] == "cql-text"
        assert params["site_type_code"] == "ST"
        assert "state_name" not in params

    @pytest.mark.parametrize("name, spellings", [
        ("Peace River", ["Peace River", "PEACE RIVER"]),                  # typed as USGS mixes it: two spellings
        ("PEACE RIVER", ["PEACE RIVER", "Peace River"]),                  # typed in capitals: still both
        ("mckenzie river", ["mckenzie river", "Mckenzie River", "MCKENZIE RIVER"]),  # three differ
    ])
    def test_every_spelling_is_asked_for_in_a_single_request(self, route, name, spellings):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates(name)
        assert len(calls) == 1
        likes = calls[0][1]["filter"].split(" OR ")
        assert likes == [f"monitoring_location_name LIKE '{s} %'" for s in spellings]

    def test_a_state_does_not_add_a_request(self, route):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates("Little River", "NC")
        assert len(calls) == 1 and calls[0][1]["state_name"] == "North Carolina"

    def test_apostrophes_are_escaped_in_every_spelling(self, route):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates("St. Mary's River")
        likes = calls[0][1]["filter"].split(" OR ")
        # as typed, str.title() (which capitalizes after an apostrophe: an odd but harmless extra
        # spelling), and upper case; the apostrophe is doubled in each
        assert likes == ["monitoring_location_name LIKE 'St. Mary''s River %'",
                         "monitoring_location_name LIKE 'St. Mary''S River %'",
                         "monitoring_location_name LIKE 'ST. MARY''S RIVER %'"]

    def test_finds_mixed_case_names(self, route):
        # Pennsylvania spells it "Shenango River at ...", Florida "PEACE RIVER AT ...": a name typed
        # in lower case still finds the mixed-case gage
        route({sg.MONITORING: monitoring(
            ("03103500", "Shenango River at Sharpsville, PA", "Pennsylvania", 584.0))})
        assert [c["site"] for c in sg.find_candidates("shenango river")] == ["03103500"]

    @pytest.mark.parametrize("state, expected", [
        ("FL", "Florida"), ("fl", "Florida"), ("Florida", "Florida"),
        ("north carolina", "North Carolina"), ("NC", "North Carolina"), ("PR", "Puerto Rico"),
    ])
    def test_state_names_are_expanded(self, route, state, expected):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates("Little River", state)
        assert calls[0][1]["state_name"] == expected


class TestResolveAnchor:
    PEACE = monitoring(
        ("02298300", "PEACE RIVER AT PUNTA GORDA FL", "Florida", 1367.0),
        ("02294650", "PEACE RIVER AT SR 60 AT BARTOW, FL", "Florida", 390.0))

    def test_no_match_exits_with_advice(self, route):
        route({sg.MONITORING: monitoring()})
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Nowhere River")
        assert "No USGS stream gages found with a name starting 'Nowhere River'" in str(exc.value)
        assert "--anchor" in str(exc.value)

    def test_no_match_mentions_the_state(self, route):
        route({sg.MONITORING: monitoring()})
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Nowhere River", "FL")
        assert "in FL" in str(exc.value)

    def test_several_states_ask_for_state(self, route):
        route({sg.MONITORING: monitoring(
            ("02100000", "LITTLE RIVER AT NC", "North Carolina", 100.0),
            ("02200000", "LITTLE RIVER AT GA", "Georgia", 50.0))})
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Little River")
        message = str(exc.value)
        assert "matches gages in 2 states: Georgia, North Carolina" in message
        assert "--state GA" in message

    def test_picks_the_largest_basin_the_walker_knows(self, route, monkeypatch, capsys):
        route({sg.MONITORING: self.PEACE})
        monkeypatch.setattr(sg, "nldi_knows", lambda site: True)
        assert sg.resolve_anchor("Peace River") == "02298300"
        assert "Using 02298300 Peace River At Punta Gorda Fl (Florida)" in capsys.readouterr().err

    def test_skips_a_basin_nldi_does_not_know(self, route, monkeypatch):
        route({sg.MONITORING: self.PEACE})
        monkeypatch.setattr(sg, "nldi_knows", lambda site: site == "02294650")
        assert sg.resolve_anchor("Peace River") == "02294650"

    def test_skips_gages_with_no_basin_area_without_asking_nldi(self, route, monkeypatch):
        route({sg.MONITORING: monitoring(("02290001", "PEACE RIVER NO AREA", "Florida", None))})
        asked = []
        monkeypatch.setattr(sg, "nldi_knows", lambda site: asked.append(site) or True)
        with pytest.raises(SystemExit):
            sg.resolve_anchor("Peace River")
        assert asked == []

    def test_none_known_to_nldi_exits_with_advice(self, route, monkeypatch):
        route({sg.MONITORING: self.PEACE})
        monkeypatch.setattr(sg, "nldi_knows", lambda site: False)
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Peace River")
        assert "none are in the NLDI network" in str(exc.value)

    def test_list_prints_the_candidates_and_exits_zero(self, route, capsys):
        route({sg.MONITORING: self.PEACE})
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Peace River", list_only=True)
        assert exc.value.code == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("02298300") and "1367.0 sq mi" in lines[0]
        assert lines[1].startswith("02294650") and lines[1].endswith(
            "PEACE RIVER AT SR 60 AT BARTOW, FL")

    def test_list_works_across_states(self, route, capsys):
        route({sg.MONITORING: monitoring(
            ("02100000", "LITTLE RIVER AT NC", "North Carolina", 100.0),
            ("02200000", "LITTLE RIVER AT GA", "Georgia", None))})
        with pytest.raises(SystemExit) as exc:
            sg.resolve_anchor("Little River", list_only=True)
        assert exc.value.code == 0
        assert "n/a" in capsys.readouterr().out


def nldi_site(site, mainstem, lon=-82.0, lat=27.0, name=None, comid=None):
    ident = site if site.startswith("USGS-") else f"USGS-{site}"
    props = {"identifier": ident, "name": name or f"GAGE {site}"}
    if mainstem is not None:
        props["mainstem"] = mainstem
    if comid is not None:
        props["comid"] = comid
    return {"properties": props, "geometry": {"coordinates": [lon, lat]}}


def flowline(comid):
    return {"properties": {"nhdplus_comid": comid}, "geometry": {"type": "LineString"}}


class TestFetchRiver:
    ANCHOR = "02298300"
    MAIN = "https://geoconnex.us/ref/mainstems/2514196"
    OTHER = "https://geoconnex.us/ref/mainstems/999"

    def routes(self, um_sites, dm_sites, um_lines=(), dm_lines=()):
        base = f"{sg.NLDI}/USGS-{self.ANCHOR}"
        return {
            base: {"features": [{"properties": {"mainstem": self.MAIN}}]},
            f"{base}/navigation/UM/nwissite": {"features": list(um_sites)},
            f"{base}/navigation/DM/nwissite": {"features": list(dm_sites)},
            f"{base}/navigation/UM/flowlines": {"features": list(um_lines)},
            f"{base}/navigation/DM/flowlines": {"features": list(dm_lines)},
        }

    def test_collects_gages_with_lat_lon_in_the_right_order(self, route):
        route(self.routes(
            [nldi_site("02296750", self.MAIN, lon=-81.876, lat=27.222, name="PEACE R AT ARCADIA")],
            []))
        _, sites = sg.fetch_river(self.ANCHOR, 322)
        assert sites == {"02296750": ("PEACE R AT ARCADIA", 27.222, -81.876)}

    def test_passes_the_distance_to_every_walk(self, route):
        calls = route(self.routes([nldi_site("02296750", self.MAIN)],
                                  [nldi_site("02298300", self.MAIN)]))
        sg.fetch_river(self.ANCHOR, 322)
        distances = [p["distance"] for url, p in calls if "/navigation/" in url]
        assert distances and set(distances) == {322}

    def test_drops_gages_on_a_different_mainstem(self, route):
        route(self.routes([nldi_site("02296750", self.MAIN), nldi_site("02296751", self.OTHER)],
                          []))
        _, sites = sg.fetch_river(self.ANCHOR, 322)
        assert list(sites) == ["02296750"]

    def test_keeps_gages_with_no_mainstem_information(self, route):
        route(self.routes([nldi_site("02296750", None)], []))
        assert "02296750" in sg.fetch_river(self.ANCHOR, 322)[1]

    def test_skips_miscellaneous_sites_with_long_numbers(self, route):
        route(self.routes([nldi_site("272330081543200", self.MAIN),
                           nldi_site("02296750", self.MAIN)], []))
        assert list(sg.fetch_river(self.ANCHOR, 322)[1]) == ["02296750"]

    def test_merges_upstream_and_downstream_gages(self, route):
        route(self.routes([nldi_site("02296750", self.MAIN)], [nldi_site("02298300", self.MAIN)]))
        assert set(sg.fetch_river(self.ANCHOR, 322)[1]) == {"02296750", "02298300"}

    def test_flowlines_are_deduplicated_by_comid(self, route):
        route(self.routes([nldi_site("02296750", self.MAIN)], [nldi_site("02298300", self.MAIN)],
                          um_lines=[flowline(1), flowline(2)], dm_lines=[flowline(2), flowline(3)]))
        lines, _ = sg.fetch_river(self.ANCHOR, 322)
        assert sorted(f["properties"]["nhdplus_comid"] for f in lines) == [1, 2, 3]

    def test_without_paths_nothing_is_recorded_and_nothing_extra_is_asked(self, route):
        calls = route(self.routes([nldi_site("02296750", self.MAIN)], [nldi_site("02298300", self.MAIN)],
                                  um_lines=[flowline(1)], dm_lines=[flowline(2)]))
        sg.fetch_river(self.ANCHOR, 322)
        assert len(calls) == 5  # the anchor's mainstem, then the site walk and the flowline walk each way

    def test_paths_record_every_flowline_by_comid_without_new_requests(self, route):
        routes = self.routes([nldi_site("02296750", self.MAIN)], [nldi_site("02298300", self.MAIN)],
                             um_lines=[flowline(1), flowline(2)], dm_lines=[flowline(2), flowline(3)])
        calls = route(routes)
        paths = {}
        sg.fetch_river(self.ANCHOR, 322, paths)
        assert sorted(paths) == [1, 2, 3]
        assert paths[2]["properties"]["nhdplus_comid"] == 2
        assert len(calls) == 5  # the same requests as without paths

    def test_paths_include_the_stretch_past_the_mouth_that_is_not_drawn(self, route):
        # nothing downstream on this river, so the walk is not drawn, but it is where this river
        # runs into another, so the paths keep it (at the cost of one request)
        calls = route(self.routes([nldi_site("02296750", self.MAIN)], [],
                                  um_lines=[flowline(1)], dm_lines=[flowline(98), flowline(99)]))
        paths = {}
        lines, _ = sg.fetch_river(self.ANCHOR, 322, paths)
        assert [f["properties"]["nhdplus_comid"] for f in lines] == [1]  # still only the river itself
        assert sorted(paths) == [1, 98, 99]
        assert any("/DM/flowlines" in url for url, _ in calls)

    def test_comids_record_the_flowline_each_kept_gage_sits_on(self, route):
        routes = self.routes([nldi_site("02296750", self.MAIN, comid=111), nldi_site("02296751", self.OTHER, comid=222)],
                             [nldi_site("02298300", self.MAIN, comid=333), nldi_site("02298301", self.MAIN)])
        route(routes)
        comids = {}
        sg.fetch_river(self.ANCHOR, 322, comids=comids)
        # a gage on another mainstem is not kept, so it is not recorded; one with no comid is None
        assert comids == {"02296750": 111, "02298300": 333, "02298301": None}

    def test_comids_cost_no_extra_requests(self, route):
        calls = route(self.routes([nldi_site("02296750", self.MAIN, comid=1)], [nldi_site("02298300", self.MAIN, comid=2)]))
        sg.fetch_river(self.ANCHOR, 322, comids={})
        assert len(calls) == 5

    def test_a_downstream_walk_with_no_gages_draws_no_line(self, route):
        # nothing downstream on this river: the walk ran into another river, so don't draw it
        calls = route(self.routes([nldi_site("02296750", self.MAIN)], [],
                                  um_lines=[flowline(1)], dm_lines=[flowline(99)]))
        lines, _ = sg.fetch_river(self.ANCHOR, 322)
        assert [f["properties"]["nhdplus_comid"] for f in lines] == [1]
        assert not any("/DM/flowlines" in url for url, _ in calls)


def utc(dt):
    """A time as the continuous-values API writes it: UTC, with a space before the clock."""
    return dt.astimezone(timezone.utc).isoformat(sep=" ")


def hours_ago(h):
    return utc(datetime.now(timezone.utc) - timedelta(hours=h))


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


class TestFetchReadings:
    def test_parses_the_recorded_response(self, route, fixture):
        text = fixture("continuous_two_days.csv")
        route({sg.CONTINUOUS_ITEMS: text})
        got = sg.fetch_readings(["02297345", "02296750"])
        assert set(got) == {"02297345", "02296750"}
        first = text.splitlines()[1].split(",")
        assert got["02297345"][0] == (epoch(first[4]), float(first[5]))  # the UTC offset is honored
        for pts in got.values():
            assert len(pts) == 193 and pts == sorted(pts)
            assert all(isinstance(t, float) and isinstance(v, float) for t, v in pts)

    def test_asks_for_a_week_of_gage_height_for_every_site_at_once(self, route):
        calls = route({sg.CONTINUOUS_ITEMS: continuous_csv()})
        sg.fetch_readings(["02296750", "02297345"])
        _, params = calls[0]
        assert params["monitoring_location_id"] == "USGS-02296750,USGS-02297345"
        assert params["parameter_code"] == "00065"
        assert params["f"] == "csv" and params["skipGeometry"] == "true"
        start, end = (datetime.fromisoformat(t) for t in params["datetime"].split("/"))
        assert end - start == timedelta(days=sg.RECENT_DAYS)
        assert abs(end - datetime.now(timezone.utc)) < timedelta(minutes=2)

    def test_drops_blank_no_data_and_unreadable_rows(self, route):
        route({sg.CONTINUOUS_ITEMS: continuous_csv(("02296750", [
            (hours_ago(4), "2.0"), (hours_ago(3), ""), (hours_ago(2), "-999999"), ("not a time", "2.5"),
            (hours_ago(1), "3.0")]))})
        assert [v for _, v in sg.fetch_readings(["02296750"])["02296750"]] == [2.0, 3.0]

    def test_readings_out_of_order_are_sorted(self, route):
        route({sg.CONTINUOUS_ITEMS: continuous_csv(("02296750", [(hours_ago(1), "3.0"), (hours_ago(2), "2.0")]))})
        assert [v for _, v in sg.fetch_readings(["02296750"])["02296750"]] == [2.0, 3.0]

    def test_sites_with_no_usable_readings_are_left_out(self, route):
        route({sg.CONTINUOUS_ITEMS: continuous_csv(("a0000000", [(hours_ago(1), "")]),
                                             ("b0000000", [(hours_ago(1), "2.5")]))})
        assert list(sg.fetch_readings(["a0000000", "b0000000", "c0000000"])) == ["b0000000"]

    def test_a_site_with_two_series_keeps_its_primary_one(self, route, fixture):
        primary, backup = "0f28e254c7124e918bfaafea26f787aa", "0aa2529d5cdd4157be802289961cb68d"  # Point State Park
        route({sg.TIME_SERIES: fixture("time_series_gage_height.json"), sg.CONTINUOUS_ITEMS: continuous_csv(
            ("03085152", [(hours_ago(2), "17.1")], primary),
            ("03085152", [(hours_ago(2), "16.0"), (hours_ago(1), "16.1")], backup))})  # newer, but a backup
        assert [v for _, v in sg.fetch_readings(["03085152"])["03085152"]] == [17.1]

    def test_with_no_primary_series_the_one_without_a_label_is_kept(self, route, fixture):
        plain, labeled = "ed44dd5f0da047f1bb4e46a472bcc926", "21f192e045344e1ba3bff1b8350ce213"  # below Berlin Dam
        route({sg.TIME_SERIES: fixture("time_series_gage_height.json"), sg.CONTINUOUS_ITEMS: continuous_csv(
            ("03090500", [(hours_ago(1), "1.23")], labeled), ("03090500", [(hours_ago(1), "1.37")], plain))})
        assert [v for _, v in sg.fetch_readings(["03090500"])["03090500"]] == [1.37]

    def test_the_series_catalog_is_asked_only_about_sites_with_several_series(self, route):
        calls = route({sg.TIME_SERIES: {"features": []}, sg.CONTINUOUS_ITEMS: continuous_csv(
            ("02296750", [(hours_ago(1), "4.0")], "a"), ("02296750", [(hours_ago(1), "4.1")], "b"),
            ("02297345", [(hours_ago(1), "2.0")]))})
        sg.fetch_readings(["02296750", "02297345"])
        assert [p["monitoring_location_id"] for url, p in calls if url == sg.TIME_SERIES] == ["USGS-02296750"]

    def test_without_the_series_catalog_the_newest_series_is_kept(self, route):
        route({sg.TIME_SERIES: sg.requests.ConnectionError("down"), sg.CONTINUOUS_ITEMS: continuous_csv(
            ("02296750", [(hours_ago(30), "9.0"), (hours_ago(29), "9.1"), (hours_ago(28), "9.2")], "old"),
            ("02296750", [(hours_ago(2), "4.0"), (hours_ago(1), "4.1")], "new"))})
        assert [v for _, v in sg.fetch_readings(["02296750"])["02296750"]] == [4.0, 4.1]

    def test_sites_go_in_batches(self, route):
        sites = [f"{i:08d}" for i in range(250)]
        calls = route({sg.CONTINUOUS_ITEMS: continuous_csv()})
        sg.fetch_readings(sites)
        sizes = sorted((len(p["monitoring_location_id"].split(",")) for _, p in calls), reverse=True)
        assert sizes == [100, 100, 50] and sg.READINGS_SITES_PER_REQUEST == 100

    def test_a_batch_that_fills_a_page_is_split_and_asked_again(self, route, monkeypatch):
        monkeypatch.setattr(sg, "PAGE_ROWS", 3)

        def one_reading_per_site(params):
            ids = [s.removeprefix("USGS-") for s in params["monitoring_location_id"].split(",")]
            return continuous_csv(*((site, [(hours_ago(1), "2.0")]) for site in ids))
        calls = route({sg.CONTINUOUS_ITEMS: one_reading_per_site})
        got = sg.fetch_readings(["a0000000", "b0000000", "c0000000", "d0000000"])
        assert len(got) == 4
        assert sorted(len(p["monitoring_location_id"].split(",")) for _, p in calls) == [2, 2, 4]

    def test_a_failed_request_is_raised(self, route):
        route({sg.CONTINUOUS_ITEMS: sg.requests.ConnectionError("usgs down")})
        with pytest.raises(sg.requests.ConnectionError):
            sg.fetch_readings(["02296750"])


class TestLatestReadings:
    NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def at(self, **age):
        return (self.NOW - timedelta(**age)).timestamp()

    def test_gives_the_newest_reading_and_its_time(self):
        pts = [(self.at(hours=2), 14.0), (self.at(hours=1), 15.5)]
        assert sg.latest_readings({"02296750": pts}, self.NOW) == {"02296750": (15.5, self.at(hours=1))}

    @pytest.mark.parametrize("age, kept", [
        (timedelta(days=3), True),                       # exactly three days old
        (timedelta(days=3, minutes=1), False),
        (timedelta(0), True),
    ])
    def test_a_gage_whose_newest_reading_is_over_three_days_old_is_not_reporting(self, age, kept):
        pts = [((self.NOW - age).timestamp(), 2.0)]
        assert bool(sg.latest_readings({"02296750": pts}, self.NOW)) is kept

    def test_without_a_moment_the_age_is_judged_against_now(self):
        now = datetime.now(timezone.utc)
        fresh, stale = (now - timedelta(hours=1)).timestamp(), (now - timedelta(days=4)).timestamp()
        assert list(sg.latest_readings({"fresh000": [(fresh, 2.0)], "stale000": [(stale, 2.0)]})) == ["fresh000"]

    def test_a_site_with_no_readings_is_not_reporting(self):
        assert sg.latest_readings({"02296750": []}, self.NOW) == {}


class TestFetchTimeZones:
    def test_reads_the_recorded_response(self, route, fixture):
        calls = route({sg.MONITORING: fixture("monitoring_time_zones.json")})
        zones = sg.fetch_time_zones(["02296750", "02297345", "09380000"])
        assert zones == {"02296750": ("EST", True), "02297345": ("EST", True), "09380000": ("MST", False)}
        _, params = calls[0]
        assert params["filter"] == "monitoring_location_number IN ('02296750','02297345','09380000')"

    def test_sites_go_in_batches(self, route):
        calls = route({sg.MONITORING: {"features": []}})
        sg.fetch_time_zones([f"{i:08d}" for i in range(250)])
        assert [p["filter"].count("'") // 2 for _, p in calls] == [100, 100, 50]

    @pytest.mark.parametrize("response", [sg.requests.ConnectionError("down"), {"no": "features"}])
    def test_a_failure_gives_no_zones_instead_of_stopping_the_run(self, route, response):
        route({sg.MONITORING: response})
        assert sg.fetch_time_zones(["02296750"]) == {}


class TestLocalTime:
    @pytest.mark.parametrize("instant, zone, expected", [
        ("2026-11-05T17:30Z", ("EST", True), "2026-11-05T12:30:00-05:00"),     # standard time
        ("2026-07-04T12:00Z", ("EST", True), "2026-07-04T08:00:00-04:00"),     # daylight time
        ("2026-07-04T12:00Z", ("CST", True), "2026-07-04T07:00:00-05:00"),
        ("2026-07-04T19:00Z", ("MST", False), "2026-07-04T12:00:00-07:00"),    # Arizona keeps standard time
        ("2026-07-04T22:00Z", ("HST", False), "2026-07-04T12:00:00-10:00"),
        ("2026-07-04T12:00Z", ("AST", False), "2026-07-04T08:00:00-04:00"),    # Puerto Rico
        ("2026-03-08T06:59Z", ("EST", True), "2026-03-08T01:59:00-05:00"),     # a minute before the change
        ("2026-03-08T07:00Z", ("EST", True), "2026-03-08T03:00:00-04:00"),     # clocks go forward at 2 a.m.
        ("2026-11-01T05:59Z", ("EST", True), "2026-11-01T01:59:00-04:00"),
        ("2026-11-01T06:00Z", ("EST", True), "2026-11-01T01:00:00-05:00"),     # and back at 2 a.m.
        ("2026-07-04T12:00Z", ("XYZ", True), "2026-07-04T12:00:00+00:00"),     # a zone it doesn't know
        ("2026-07-04T12:00Z", None, "2026-07-04T12:00:00+00:00"),
    ])
    def test_puts_the_reading_on_the_gages_own_clock(self, instant, zone, expected):
        assert sg.local_time(epoch(instant.replace("Z", "+00:00")), zone) == expected

    def test_a_late_evening_reading_keeps_its_own_calendar_day(self):
        when = sg.local_time(epoch("2026-11-06T04:30:00+00:00"), ("EST", True))  # 11:30 pm on the 5th
        assert sg.reading_day(when) == (11, 5)
