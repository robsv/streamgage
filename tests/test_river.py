"""Finding the river: name lookup, anchor choice, the NLDI walk, and current readings."""

from datetime import datetime, timedelta, timezone

import pytest

import streamgage as sg


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
        filters = [params["filter"] for _, params in calls]
        # The filter is case-sensitive and USGS names are upper case in some states, mixed in others
        assert filters == ["monitoring_location_name LIKE 'Peace River %'",
                           "monitoring_location_name LIKE 'PEACE RIVER %'"]
        for _, params in calls:
            assert params["site_type_code"] == "ST"
            assert "state_name" not in params

    def test_apostrophes_are_escaped_in_the_filter(self, route):
        calls = route({sg.MONITORING: monitoring()})
        sg.find_candidates("St. Mary's River")
        assert all("''s" in params["filter"].lower() for _, params in calls)
        likes = [params["filter"].split(" LIKE ")[1] for _, params in calls]
        assert "'ST. MARY''S RIVER %'" in likes

    def test_finds_mixed_case_names(self, route):
        # Pennsylvania spells it "Shenango River at ...", Florida "PEACE RIVER AT ..."; the stub
        # returns the same rows for every variant, so they must not be listed twice
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


def iv_response(*rows):
    """An NWIS iv response from (site, [(dateTime, value), ...]) pairs."""
    series = [{"sourceInfo": {"siteCode": [{"value": site}]},
               "values": [{"value": [{"value": v, "dateTime": t} for t, v in readings]}]}
              for site, readings in rows]
    return {"value": {"timeSeries": series}}


def hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


class TestFetchCurrent:
    def test_returns_the_latest_reading_per_site(self, route):
        route({"/iv/": iv_response(("02296750", [(hours_ago(2), "14.0"), (hours_ago(1), "15.5")]))})
        stage, when = sg.fetch_current(["02296750"])["02296750"]
        assert stage == 15.5
        assert when.endswith("+00:00")

    def test_ignores_gages_whose_latest_reading_is_stale(self, route):
        route({"/iv/": iv_response(("fresh000", [(hours_ago(1), "2.0")]),
                                   ("stale000", [(hours_ago(24 * 3 + 1), "2.0")]))})
        assert list(sg.fetch_current(["fresh000", "stale000"])) == ["fresh000"]

    def test_a_reading_just_inside_three_days_counts(self, route):
        route({"/iv/": iv_response(("02296750", [(hours_ago(24 * 3 - 1), "2.0")]))})
        assert "02296750" in sg.fetch_current(["02296750"])

    def test_ignores_no_data_and_unparseable_latest_readings(self, route):
        route({"/iv/": iv_response(("a0000000", [(hours_ago(1), "-999999")]),
                                   ("b0000000", [(hours_ago(1), "")]),
                                   ("c0000000", [(hours_ago(1), "2.5")]))})
        assert list(sg.fetch_current(["a0000000", "b0000000", "c0000000"])) == ["c0000000"]

    def test_ignores_series_with_no_readings(self, route):
        route({"/iv/": iv_response(("02296750", []))})
        assert sg.fetch_current(["02296750"]) == {}

    def test_asks_only_for_active_gage_height(self, route):
        calls = route({"/iv/": iv_response()})
        sg.fetch_current(["02296750", "02296751"])
        _, params = calls[0]
        assert params["sites"] == "02296750,02296751"
        assert params["parameterCd"] == "00065"
        assert params["siteStatus"] == "active"

    def test_batches_requests_at_the_nwis_limit(self, route):
        sites = [f"{i:08d}" for i in range(250)]
        calls = route({"/iv/": iv_response()})
        sg.fetch_current(sites)
        sizes = [len(p["sites"].split(",")) for _, p in calls]
        assert sizes == [100, 100, 50]
        assert sg.MAX_SITES_PER_REQUEST == 100
