"""Mapping several rivers at once, and finding where they meet."""

import json
import re
from pathlib import Path

import pytest
import requests

import streamgage as sg
from tests.conftest import gage
from tests.test_river import monitoring


def seg(comid, *pts, kind="LineString"):
    """A flowline from (lon, lat) points, drawn in the direction the water flows."""
    coords = [list(p) for p in pts]
    return {"type": "Feature", "properties": {"nhdplus_comid": comid},
            "geometry": {"type": kind, "coordinates": coords if kind == "LineString" else [coords]}}


def chain(comid, *pts):
    return {comid: seg(comid, *pts)}


JOIN = (-80.0, 40.9)  # where A and B meet
A_ONLY = seg(1, (-80.0, 41.0), JOIN)
B_ONLY = seg(2, (-80.1, 40.95), JOIN)
SHARED = [seg(3, JOIN, (-80.0, 40.8)), seg(4, (-80.0, 40.8), (-80.0, 40.7))]


def paths(*features):
    return {f["properties"]["nhdplus_comid"]: f for f in features}


class TestFindConfluences:
    def test_two_rivers_that_join_meet_where_their_shared_stretch_starts(self):
        found = sg.find_confluences({"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)})
        assert found == [{"lat": 40.9, "lon": -80.0, "rivers": ["A", "B"]}]

    def test_rivers_that_never_share_a_flowline_do_not_meet(self):
        assert sg.find_confluences({"A": paths(A_ONLY), "B": paths(B_ONLY)}) == []

    def test_the_far_end_of_a_walk_is_not_a_confluence(self):
        # B's walk starts partway down A's path, so B has no segment of its own arriving at the
        # shared stretch: B is not a tributary of A, its walk just overlaps A's
        assert sg.find_confluences({"A": paths(A_ONLY, *SHARED), "B": paths(*SHARED)}) == []

    def test_the_far_end_of_a_walk_is_not_a_confluence_whichever_river_it_is(self):
        # the same, with the rivers the other way round: here it is A whose walk starts mid-stretch
        assert sg.find_confluences({"A": paths(*SHARED), "B": paths(B_ONLY, *SHARED)}) == []

    def test_a_river_that_is_a_continuation_of_the_other_is_not_a_confluence(self):
        # Mahoning and Beaver in NHD: B is the same channel carried on past the mouth
        assert sg.find_confluences({"A": paths(A_ONLY, *SHARED), "B": paths(A_ONLY, *SHARED)}) == []

    def test_three_rivers_joining_at_one_place_are_one_confluence(self):
        c_only = seg(5, (-79.9, 40.95), JOIN)
        found = sg.find_confluences({"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED),
                                     "C": paths(c_only, *SHARED)})
        assert found == [{"lat": 40.9, "lon": -80.0, "rivers": ["A", "B", "C"]}]

    def test_a_tributary_meeting_another_river_part_way_down(self):
        # A is the long river and B joins it at JOIN; each path covers its own channel and the
        # stretch of A downstream of the join
        a = paths(seg(10, (-80.0, 41.2), JOIN), *SHARED)
        b = paths(B_ONLY, *SHARED)
        assert [c["rivers"] for c in sg.find_confluences({"A": a, "B": b})] == [["A", "B"]]

    def test_a_river_that_only_passes_through_is_not_named(self):
        # Joshua joins the Peace upstream of where Horse does; its walk then runs down the Peace
        # past Horse's mouth. It must not be listed as meeting Horse there.
        horse_mouth = (-80.0, 40.5)
        peace_up = seg(30, (-80.0, 41.0), horse_mouth)          # the Peace above Horse's mouth
        peace_far = seg(31, (-80.0, 41.5), (-80.0, 41.0))       # and further up, above Joshua's
        joshua_own = seg(32, (-79.9, 41.05), (-80.0, 41.0))
        horse_own = seg(33, (-80.1, 40.55), horse_mouth)
        below = [seg(34, horse_mouth, (-80.0, 40.2)), seg(35, (-80.0, 40.2), (-80.0, 40.0))]
        peace_farther = seg(36, (-80.0, 42.0), (-80.0, 41.5))
        found = sg.find_confluences({
            "Peace": paths(peace_farther, peace_far, peace_up, *below),
            "Horse": paths(horse_own, *below),
            "Joshua": paths(joshua_own, peace_up, *below)})
        by_place = {round(c["lat"], 2): c["rivers"] for c in found}
        assert by_place == {40.5: ["Peace", "Horse"], 41.0: ["Peace", "Joshua"]}

    def test_a_side_channel_rejoining_mid_stretch_is_not_a_new_meeting(self):
        # A and B already share the stretch from JOIN down; a bit of side channel of each rejoins it
        # part way along. The rivers were together already, so that node is not where they meet.
        mid = (-80.0, 40.8)
        a = paths(A_ONLY, SHARED[0], SHARED[1], seg(50, (-80.05, 40.85), mid))
        b = paths(B_ONLY, SHARED[0], SHARED[1], seg(51, (-79.95, 40.85), mid))
        found = sg.find_confluences({"A": a, "B": b})
        assert [(c["lat"], c["lon"]) for c in found] == [(40.9, -80.0)]

    def test_rivers_filed_as_one_channel_are_all_named_where_a_third_joins(self):
        # Mahoning and Beaver are one NLDI channel, and the Shenango joins it: all three are named,
        # because the two that share a channel never meet each other to count as "already joined"
        mahoning = paths(A_ONLY, *SHARED)
        beaver = paths(A_ONLY, *SHARED[:1])  # shorter, as a walk from a gage nearer the mouth is
        found = sg.find_confluences({"Mahoning": mahoning, "Beaver": beaver, "Shenango": paths(B_ONLY, *SHARED)})
        assert [c["rivers"] for c in found] == [["Mahoning", "Beaver", "Shenango"]]

    def test_a_confluence_left_with_one_river_is_not_reported(self):
        # B is pruned as passing through A at the second place, because it already met A at the first,
        # and A alone is not a meeting
        first = (-80.0, 41.0)
        a = paths(seg(40, (-80.0, 42.0), first), seg(41, first, (-80.0, 40.5)),
                  seg(42, (-80.0, 40.5), (-80.0, 40.0)), seg(43, (-80.0, 40.0), (-80.0, 39.0)))
        b = paths(seg(44, (-79.9, 41.1), first), seg(41, first, (-80.0, 40.5)))
        c = paths(seg(45, (-80.1, 40.6), (-80.0, 40.5)), seg(42, (-80.0, 40.5), (-80.0, 40.0)))
        found = sg.find_confluences({"A": a, "B": b, "C": c})
        assert all(len(f["rivers"]) > 1 for f in found)

    def test_separate_confluences_are_kept_and_sorted_north_to_south(self):
        south = (-81.0, 39.0)
        a = paths(A_ONLY, *SHARED, seg(21, (-81.0, 39.2), south), seg(22, south, (-81.0, 38.9)))
        b = paths(B_ONLY, *SHARED, seg(23, (-81.1, 39.1), south), seg(22, south, (-81.0, 38.9)))
        found = sg.find_confluences({"A": a, "B": b})
        assert [(c["lat"], c["lon"]) for c in found] == [(40.9, -80.0), (39.0, -81.0)]

    def test_multilinestring_flowlines_are_handled(self):
        a_only = seg(1, (-80.0, 41.0), JOIN, kind="MultiLineString")
        found = sg.find_confluences({"A": paths(a_only, *SHARED), "B": paths(B_ONLY, *SHARED)})
        assert len(found) == 1

    def test_nothing_to_compare_with_one_river(self):
        assert sg.find_confluences({"A": paths(A_ONLY, *SHARED)}) == []


def recorded(region, only=None):
    """(paths, drainage areas) recorded from NLDI and the USGS for the rivers of a region, in the shapes
    fetch_river and fetch_drainage_areas give. The flowlines are trimmed to each segment's comid and end
    points; `only` limits it to some of the rivers."""
    data = json.loads((Path(__file__).parent / "fixtures" / "nldi_paths.json").read_text())[region]
    names = [n for n in data["rivers"] if only is None or n in only]
    return ({name: {c: seg(c, (lon0, lat0), (lon1, lat1)) for c, lon0, lat0, lon1, lat1 in data["rivers"][name]}
             for name in names},
            {name: data["areas"][name] for name in names})


def meetings(found):
    """[((lat, lon) to 2 places, rivers)] for a list of confluences."""
    return [((round(c["lat"], 2), round(c["lon"], 2)), c["rivers"]) for c in found]


class TestRecordedRivers:
    """The rules were tuned on real flowlines, so keep real ones as a check that they still hold."""

    def test_pennsylvania_names_all_three_at_the_start_of_the_beaver(self):
        assert meetings(sg.find_confluences(*recorded("pennsylvania"))) == [
            ((40.96, -80.38), ["Shenango River", "Mahoning River", "Beaver River"])]

    def test_the_florida_creeks_each_meet_the_peace_where_they_join_it(self):
        assert meetings(sg.find_confluences(*recorded("florida"))) == [
            ((27.16, -81.90), ["Peace River", "Joshua Creek"]),
            ((27.10, -81.99), ["Peace River", "Horse Creek"]),
            ((26.98, -81.99), ["Peace River", "Shell Creek"])]

    def test_the_myakka_never_meets_the_peace(self):
        assert all("Myakka River" not in c["rivers"] for c in sg.find_confluences(*recorded("florida")))

    def test_the_order_the_rivers_are_given_in_does_not_change_where_they_meet(self):
        paths, areas = recorded("florida")
        flipped = dict(reversed(list(paths.items())))
        places = lambda found: sorted((round(c["lat"], 3), round(c["lon"], 3)) for c in found)
        assert places(sg.find_confluences(flipped, areas)) == places(sg.find_confluences(paths, areas))

    def test_leaving_a_river_out_leaves_the_others_confluences(self):
        paths, areas = recorded("florida")
        del paths["Horse Creek"]
        assert [c["rivers"] for c in sg.find_confluences(paths, areas)] == [["Peace River", "Joshua Creek"],
                                                                            ["Peace River", "Shell Creek"]]

    def test_the_susquehanna_is_named_at_every_junction_not_its_longer_walking_tributary(self):
        # the Susquehanna gage is far downstream, so its walk is shorter than the West Branch's;
        # by path length the West Branch would be named at the Juniata and the Swatara
        paths, areas = recorded("susquehanna")
        assert meetings(sg.find_confluences(paths, areas)) == [
            ((40.88, -76.80), ["Susquehanna River", "West Branch Susquehanna River"]),  # Northumberland
            ((40.40, -77.02), ["Susquehanna River", "Juniata River"]),                  # Duncannon
            ((40.18, -76.73), ["Susquehanna River", "Swatara Creek"])]                  # Middletown

    def test_without_areas_the_longer_walk_wins_and_the_wrong_river_is_named(self):
        # what the areas are for: the places are right either way, the names are not
        paths, _ = recorded("susquehanna")
        found = meetings(sg.find_confluences(paths))
        assert [place for place, _ in found] == [(40.88, -76.80), (40.40, -77.02), (40.18, -76.73)]
        assert found[1][1] == ["West Branch Susquehanna River", "Juniata River"]

    def test_pittsburgh_has_the_four_junctions_where_they_are(self):
        found = sg.find_confluences(*recorded("pittsburgh"))
        assert [place for place, _ in meetings(found)] == [
            (40.69, -80.29),  # the Beaver joins the Ohio at Rochester
            (40.68, -79.67),  # the Kiskiminetas joins the Allegheny at Freeport
            (40.44, -80.02),  # the Allegheny and the Monongahela make the Ohio at Pittsburgh
            (40.36, -79.87)]  # the Youghiogheny joins the Monongahela at McKeesport

    def test_with_the_ohio_listed_it_is_named_where_the_beaver_joins_it(self):
        found = dict(meetings(sg.find_confluences(*recorded("pittsburgh"))))
        assert found[(40.69, -80.29)] == ["Ohio River", "Beaver River"]
        assert found[(40.36, -79.87)] == ["Monongahela River", "Youghiogheny River"]
        assert "Monongahela River" in found[(40.44, -80.02)]

    def test_without_the_ohio_the_bigger_basin_carries_through_to_the_beaver(self):
        # the Beaver really meets the Ohio, which is not listed; the Allegheny (the bigger of the two
        # rivers that make it) stands in. By path length it would have been the Monongahela.
        rivers = ["Allegheny River", "Monongahela River", "Youghiogheny River", "Kiskiminetas River",
                  "Beaver River"]
        found = dict(meetings(sg.find_confluences(*recorded("pittsburgh", rivers))))
        assert found[(40.69, -80.29)] == ["Allegheny River", "Beaver River"]
        assert found[(40.44, -80.02)] == ["Allegheny River", "Monongahela River"]
        assert found[(40.68, -79.67)] == ["Allegheny River", "Kiskiminetas River"]


class TestBuildGagesPaths:
    def test_the_paths_dict_reaches_fetch_river_and_comes_back_filled(self, monkeypatch):
        seen = []

        def fetch_river(anchor, km, paths=None):
            seen.append(paths)
            if paths is not None:
                paths.update({1: "a flowline"})
            return [], {}

        monkeypatch.setattr(sg, "fetch_river", fetch_river)
        mine = {}
        with pytest.raises(SystemExit):  # no sites, so no gage reports: it stops after the walk
            sg.build_gages("anchor", 322, paths=mine)
        assert seen == [mine] and mine == {1: "a flowline"}

    def test_the_comids_dict_reaches_fetch_river_and_comes_back_filled(self, monkeypatch):
        seen = []

        def fetch_river(anchor, km, paths=None, comids=None):
            seen.append((paths, comids))
            comids.update({"02296750": 111})
            return [], {}

        monkeypatch.setattr(sg, "fetch_river", fetch_river)
        mine = {}
        with pytest.raises(SystemExit):
            sg.build_gages("anchor", 322, comids=mine)
        assert seen == [(None, mine)] and mine == {"02296750": 111}

    def test_without_paths_fetch_river_is_called_as_it_always_was(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sg, "fetch_river", lambda *args, **kw: calls.append((args, kw)) or ([], {}))
        with pytest.raises(SystemExit):
            sg.build_gages("anchor", 322)
        assert calls == [(("anchor", 322), {})]


class TestDownstreamOf:
    def test_follows_the_shared_stretch_on_down_from_the_node(self):
        features = paths(*SHARED, seg(5, (-80.0, 40.7), (-80.0, 40.6)))
        assert sg.downstream_of(features, JOIN) == {3, 4, 5}
        assert sg.downstream_of(features, (-80.0, 40.8)) == {4, 5}  # from a node part way along

    def test_leaves_out_what_lies_upstream_of_the_node(self):
        features = paths(seg(9, (-80.0, 41.5), JOIN), *SHARED)
        assert sg.downstream_of(features, JOIN) == {3, 4}

    def test_a_fork_is_followed_down_both_branches(self):
        # two flowlines leave the same node (a split channel): both branches count as below it
        fork = seg(8, JOIN, (-80.1, 40.8)), seg(9, JOIN, (-79.9, 40.8)), seg(10, (-79.9, 40.8), (-79.9, 40.7))
        assert sg.downstream_of(paths(*fork), JOIN) == {8, 9, 10}

    def test_a_node_the_stretch_does_not_start_at_gives_nothing(self):
        assert sg.downstream_of(paths(*SHARED), (-70.0, 30.0)) == set()


class TestConfluenceBelow:
    def test_the_stretch_below_a_confluence_is_listed_only_when_asked_for(self):
        net = {"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)}
        assert "below" not in sg.find_confluences(net)[0]
        assert sg.find_confluences(net, detail=True)[0]["below"] == [3, 4]

    def test_a_shared_stretch_above_the_meeting_is_not_below_it(self):
        # Mahoning and Beaver are one channel: the Shenango meets it at JOIN, and the part of the
        # channel above JOIN, which the Beaver's walk also covers, must not count as below
        upstream = seg(70, (-80.0, 41.5), (-80.0, 41.0))
        a_only = seg(71, (-80.0, 41.0), JOIN)
        mahoning = paths(upstream, a_only, *SHARED)
        beaver = paths(upstream, a_only, *SHARED[:1])
        found = sg.find_confluences({"Mahoning": mahoning, "Beaver": beaver, "Shenango": paths(B_ONLY, *SHARED)},
                                    detail=True)
        assert 70 not in found[0]["below"] and 71 not in found[0]["below"] and {3, 4} <= set(found[0]["below"])

    def test_every_pair_that_meets_at_a_place_adds_its_stretch(self):
        c_only = seg(5, (-79.9, 40.95), JOIN)
        net = {"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED), "C": paths(c_only, *SHARED)}
        assert sg.find_confluences(net, detail=True)[0]["below"] == [3, 4]

    def test_a_confluence_found_by_several_pairs_keeps_the_longest_stretch_whichever_came_first(self):
        # C's path shares only the first segment below the join with A, and A and B share two, so
        # the pair that registers the place first (A and C) knows less than the pair that merges into it
        c_only = seg(5, (-79.9, 40.95), JOIN)
        net = {"A": paths(A_ONLY, *SHARED), "C": paths(c_only, SHARED[0]), "B": paths(B_ONLY, *SHARED)}
        found = sg.find_confluences(net, detail=True)
        assert [c["rivers"] for c in found] == [["A", "C", "B"]]
        assert found[0]["below"] == [3, 4]

    def test_a_stretch_the_two_rivers_also_share_somewhere_else_is_not_below_the_meeting(self):
        # both walks start on the same flowline far away (it has no arrival of its own, so it is
        # not a meeting): it is shared, but it is not downstream of the join
        elsewhere = seg(80, (-75.0, 45.0), (-75.0, 44.9))
        net = {"A": paths(elsewhere, A_ONLY, *SHARED), "B": paths(elsewhere, B_ONLY, *SHARED)}
        found = sg.find_confluences(net, detail=True)
        assert [(c["lat"], c["lon"]) for c in found] == [(40.9, -80.0)]
        assert found[0]["below"] == [3, 4]

    def test_on_the_recorded_pittsburgh_rivers_the_ohio_gage_is_below_and_the_allegheny_one_is_not(self):
        rivers = ["Allegheny River", "Monongahela River", "Youghiogheny River", "Kiskiminetas River", "Beaver River"]
        found = {round(c["lat"], 2): set(c["below"])
                 for c in sg.find_confluences(*recorded("pittsburgh", rivers), detail=True)}
        pittsburgh, rochester = found[40.44], found[40.69]
        assert 3821269 in pittsburgh      # the Ohio River at Emsworth Dam: below the Allegheny and Monongahela
        assert 11050716 not in pittsburgh  # the Allegheny River at Pittsburgh: on the Allegheny's own side
        assert 3821269 not in rochester   # ...but upstream of where the Beaver joins the Ohio


class TestNearestGagesAboveTheMeeting:
    def meeting(self):
        return {"lat": 40.9, "lon": -80.0, "rivers": ["A", "B"], "below": [3, 4]}

    def test_a_gage_below_the_meeting_is_passed_over_for_one_on_the_rivers_own_side(self):
        by_river = {"A": [gage("near", "Below", lat=40.89, lon=-80.0), gage("far", "Above", lat=41.4, lon=-80.0)],
                    "B": [gage("b", "B gage", lat=40.95, lon=-80.1)]}
        comids = {"A": {"near": 3, "far": 99}, "B": {"b": 98}}
        out = sg.nearest_gages(self.meeting(), by_river, comids)
        assert [(n["river"], n["site"]) for n in out] == [("A", "far"), ("B", "b")]
        assert all("below" not in n for n in out)

    def test_when_every_gage_is_below_the_nearest_is_used_and_marked(self):
        by_river = {"A": [gage("x", "X", lat=40.8, lon=-80.0), gage("y", "Y", lat=40.5, lon=-80.0)], "B": []}
        out = sg.nearest_gages(self.meeting(), by_river, {"A": {"x": 3, "y": 4}})
        assert out == [{"river": "A", "site": "x", "name": "X", "miles": pytest.approx(6.9, abs=0.1), "below": True}]

    def test_a_gage_with_no_known_flowline_counts_as_above(self):
        by_river = {"A": [gage("x", "X", lat=40.89, lon=-80.0)], "B": []}
        out = sg.nearest_gages(self.meeting(), by_river, {"A": {"x": None}})
        assert out[0]["site"] == "x" and "below" not in out[0]

    def test_without_flowlines_every_gage_counts_as_above(self):
        by_river = {"A": [gage("x", "X", lat=40.89, lon=-80.0)], "B": []}
        assert "below" not in sg.nearest_gages(self.meeting(), by_river)[0]


class TestFetchDrainageAreas:
    def test_one_request_gives_every_sites_area(self, route):
        calls = route({sg.MONITORING: monitoring(("01577505", "SUSQUEHANNA", "Pennsylvania", 27000.0),
                                                 ("01553500", "WEST BRANCH", "Pennsylvania", 6847.0))})
        assert sg.fetch_drainage_areas(["01577505", "01553500"]) == {"01577505": 27000.0, "01553500": 6847.0}
        assert len(calls) == 1
        assert calls[0][1]["filter"] == "monitoring_location_number IN ('01577505','01553500')"
        assert calls[0][1]["filter-lang"] == "cql-text"

    def test_sites_with_no_area_are_left_out(self, route):
        route({sg.MONITORING: monitoring(("1", "A", "S", None), ("2", "B", "S", 0), ("3", "C", "S", 5.0))})
        assert sg.fetch_drainage_areas(["1", "2", "3"]) == {"3": 5.0}


class TestWhichRiverCarriesThrough:
    """The pass-through rule keeps the bigger of two rivers that have already met."""

    def network(self):
        # TRIB joins MAIN at M1; further down, OTHER joins at M2. MAIN's walk is the shorter one (it was
        # anchored far downstream), TRIB's is longer (anchored near its mouth, walked far up).
        m1, m2 = (-80.0, 41.0), (-80.0, 40.5)
        shared_below_m1 = [seg(61, m1, m2), seg(62, m2, (-80.0, 40.0))]
        main = paths(seg(60, (-80.0, 41.5), m1), *shared_below_m1)
        trib = paths(seg(63, (-79.9, 41.1), m1), seg(64, (-79.8, 41.3), (-79.9, 41.1)),
                     seg(65, (-79.7, 41.5), (-79.8, 41.3)), *shared_below_m1)
        other = paths(seg(66, (-80.1, 40.6), m2), shared_below_m1[1])
        return {"Main": main, "Trib": trib, "Other": other}

    def at_second_confluence(self, found):
        return next(c["rivers"] for c in found if round(c["lat"], 1) == 40.5)

    def test_the_bigger_basin_is_named_even_when_its_walk_was_shorter(self):
        net = self.network()
        assert len(net["Main"]) < len(net["Trib"])  # by path length alone Trib would win
        found = sg.find_confluences(net, {"Main": 27000.0, "Trib": 6800.0, "Other": 500.0})
        assert self.at_second_confluence(found) == ["Main", "Other"]

    def test_without_areas_the_longer_walk_is_used(self):
        assert self.at_second_confluence(sg.find_confluences(self.network())) == ["Trib", "Other"]

    def test_with_an_area_missing_for_one_river_the_walks_are_compared(self):
        found = sg.find_confluences(self.network(), {"Main": 27000.0, "Trib": None, "Other": 500.0})
        assert self.at_second_confluence(found) == ["Trib", "Other"]

    def test_areas_do_not_change_where_the_rivers_meet(self):
        net = self.network()
        places = lambda found: [(c["lat"], c["lon"]) for c in found]
        assert places(sg.find_confluences(net)) == places(sg.find_confluences(net, {"Main": 1.0, "Trib": 2.0, "Other": 3.0}))


class TestNearestGages:
    def test_distances_are_in_miles(self):
        # a gage one mile due north: 1.609344 km is 1.609344 / 111.195 degrees of latitude
        c = {"lat": 40.0, "lon": -80.0, "rivers": ["A"]}
        one_mile_north = 40.0 + 1.609344 / 111.195
        out = sg.nearest_gages(c, {"A": [gage("1", "A", lat=one_mile_north, lon=-80.0)]})
        assert out[0]["miles"] == 1.0
        assert "km" not in out[0]

    def test_picks_the_closest_gage_on_each_river(self):
        c = {"lat": 40.9, "lon": -80.0, "rivers": ["A", "B", "C"]}
        by_river = {"A": [gage("1", "Far", lat=41.5, lon=-80.0), gage("2", "Near", lat=40.95, lon=-80.0)],
                    "B": [gage("3", "Only", lat=40.9, lon=-80.1)], "C": []}
        out = sg.nearest_gages(c, by_river)
        assert [(n["river"], n["site"]) for n in out] == [("A", "2"), ("B", "3")]  # C has no gage
        assert out[0]["miles"] == pytest.approx(3.5, abs=0.1)  # about 5.6 km


@pytest.fixture
def stubbed(monkeypatch):
    """build_gages returns one gage per anchor and, like the real one, records A's and B's flowlines
    (a shared stretch below their join) in the `paths` it is given."""
    built = []
    flowlines = {"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)}

    def build_gages(anchor, km, draw=False, as_of=None, paths=None, comids=None):
        built.append(anchor)
        if anchor == "EMPTY":
            raise SystemExit("No gages on this river are reporting recent gage height (parameter 00065).")
        if paths is not None:
            paths.update(flowlines.get(anchor, {}))
        lat = {"A": 41.0, "B": 40.95, "C": 40.0}.get(anchor, 40.0)
        shared_site = gage("SHARED01", "At the join", lat=40.9, lon=-80.0)
        return ([seg(hash(anchor) % 1000 + 100, (-80, lat), (-80, lat - .01)), A_ONLY],
                [gage(f"{anchor}00001", f"{anchor} gage", lat=lat), shared_site],
                [{"site": f"{anchor}RET", "lat": 40.0, "lon": -80.0}])

    monkeypatch.setattr(sg, "build_gages", build_gages)
    return built


class TestBuildRivers:
    def test_one_river_is_just_build_gages(self, stubbed):
        _, gages, _, confluences = sg.build_rivers(["A"], ["A"], 322)
        assert stubbed == ["A"] and confluences == []
        assert len(gages) == 2

    def test_rivers_are_merged_without_duplicates(self, stubbed):
        lines, gages, retired, _ = sg.build_rivers(["A", "B"], ["A", "B"], 322, True)
        assert sorted(g["site"] for g in gages) == ["A00001", "B00001", "SHARED01"]  # shared gage once
        assert [g["lat"] for g in gages] == sorted((g["lat"] for g in gages), reverse=True)
        comids = [f["properties"]["nhdplus_comid"] for f in lines]
        assert len(comids) == len(set(comids)) and 1 in comids  # A_ONLY comes back from both rivers
        assert sorted(r["site"] for r in retired) == ["ARET", "BRET"]

    def test_confluences_name_the_rivers_and_their_nearest_gages(self, stubbed):
        *_, confluences = sg.build_rivers(["A", "B"], ["Alpha River", "Beta River"], 322)
        assert len(confluences) == 1
        assert confluences[0]["rivers"] == ["Alpha River", "Beta River"]
        # the gage at the join is closest to both
        assert [n["site"] for n in confluences[0]["nearest"]] == ["SHARED01", "SHARED01"]

    def test_a_river_with_nothing_to_show_is_skipped_with_a_warning(self, stubbed, capsys):
        _, _, _, confluences = sg.build_rivers(["A", "EMPTY", "B"], ["A", "Dry Creek", "B"], 322)
        assert "skipping Dry Creek" in capsys.readouterr().err
        assert len(confluences) == 1 and confluences[0]["rivers"] == ["A", "B"]

    def test_the_flowlines_come_from_the_build_and_only_the_drainage_areas_are_asked_for(self, stubbed, route):
        calls = route({sg.MONITORING: monitoring(("A", "A AT X", "S", 100.0), ("B", "B AT X", "S", 50.0))})
        *_, confluences = sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert len(confluences) == 1
        assert [url for url, _ in calls] == [sg.MONITORING]  # no flowline is downloaded a second time

    def test_a_gage_below_the_meeting_is_not_the_nearest_on_its_river(self, monkeypatch):
        # the gage at the join (on the shared stretch, flowline 3) is the closest to both rivers, but it is
        # below the meeting, so each river's own gage is named instead; "below" is not left on the result
        flow = {"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)}

        def build_gages(anchor, km, draw=False, as_of=None, paths=None, comids=None):
            paths.update(flow[anchor])
            comids.update({"SHARED01": 3, f"{anchor}1": 99})
            return [], [gage("SHARED01", "At the join", lat=40.9, lon=-80.0),
                        gage(f"{anchor}1", f"{anchor} up", lat=41.2 if anchor == "A" else 41.1, lon=-80.0)], []

        monkeypatch.setattr(sg, "build_gages", build_gages)
        monkeypatch.setattr(sg, "fetch_drainage_areas", lambda sites: {})
        *_, confluences = sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert [n["site"] for n in confluences[0]["nearest"]] == ["A1", "B1"]
        assert "below" not in confluences[0] and all("below" not in n for n in confluences[0]["nearest"])

    def test_every_river_is_given_its_own_comids_to_fill(self, monkeypatch):
        given = []

        def build_gages(anchor, km, draw=False, as_of=None, paths=None, comids=None):
            given.append(comids)
            return [], [gage(f"{anchor}1", anchor)], []

        monkeypatch.setattr(sg, "build_gages", build_gages)
        sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert len(given) == 2 and all(c == {} for c in given) and given[0] is not given[1]

    def test_the_rivers_drainage_areas_reach_the_confluence_rules(self, stubbed, route, monkeypatch):
        route({sg.MONITORING: monitoring(("A", "A AT X", "S", 100.0), ("B", "B AT X", "S", 50.0))})
        seen = {}
        real = sg.find_confluences
        monkeypatch.setattr(sg, "find_confluences", lambda paths, areas=None, detail=False: seen.update(areas=areas) or real(paths, areas, detail))
        sg.build_rivers(["A", "B"], ["Alpha", "Beta"], 322)
        assert seen["areas"] == {"Alpha": 100.0, "Beta": 50.0}

    def test_without_drainage_areas_the_confluences_are_still_found(self, stubbed, route, capsys):
        route({sg.MONITORING: requests.ConnectionError("usgs down")})
        *_, confluences = sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert len(confluences) == 1

    def test_every_river_is_given_its_own_paths_to_fill(self, monkeypatch):
        given = []

        def build_gages(anchor, km, draw=False, as_of=None, paths=None, comids=None):
            given.append(paths)
            return [], [gage(f"{anchor}1", anchor)], []

        monkeypatch.setattr(sg, "build_gages", build_gages)
        sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert len(given) == 2 and all(p == {} for p in given) and given[0] is not given[1]

    def test_all_rivers_empty_exits(self, stubbed):
        with pytest.raises(SystemExit, match="None of these rivers"):
            sg.build_rivers(["EMPTY", "EMPTY"], ["X", "Y"], 322)

    def test_a_failed_confluence_lookup_still_gives_the_map(self, stubbed, monkeypatch, capsys):
        def boom(paths, areas=None, detail=False):
            raise ValueError("a flowline with no geometry")
        monkeypatch.setattr(sg, "find_confluences", boom)
        _, gages, _, confluences = sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert confluences == [] and len(gages) == 3
        assert "Could not look for confluences" in capsys.readouterr().err

    def test_a_river_that_fails_on_the_network_is_skipped(self, stubbed, monkeypatch, capsys):
        real = sg.build_gages

        def flaky(anchor, *a, **k):
            if anchor == "B":
                raise requests.HTTPError("429 Client Error: Too Many Requests")
            return real(anchor, *a, **k)

        monkeypatch.setattr(sg, "build_gages", flaky)
        _, gages, _, confluences = sg.build_rivers(["A", "B", "C"], ["A", "B", "C"], 322)
        assert "skipping B: 429" in capsys.readouterr().err
        assert {g["site"] for g in gages} >= {"A00001", "C00001"} and confluences == []

    def test_other_exits_are_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(sg, "build_gages", lambda *a, **k: (_ for _ in ()).throw(SystemExit(2)))
        with pytest.raises(SystemExit):
            sg.build_rivers(["A", "B"], ["A", "B"], 322)


class TestCommandLine:
    @pytest.fixture
    def run(self, monkeypatch, tmp_path):
        seen = {}

        def build_rivers(anchors, labels, km, draw=False, as_of=None):
            seen.update(anchors=anchors, labels=labels)
            return [], [gage()], [], seen.get("confluences", [])

        monkeypatch.setattr(sg, "resolve_anchor", lambda name, state=None, list_only=False: f"id:{name}")
        monkeypatch.setattr(sg, "build_rivers", build_rivers)

        def go(*argv, confluences=()):
            seen["confluences"] = list(confluences)
            out = tmp_path / "out.html"
            monkeypatch.setattr("sys.argv", ["streamgage.py", *argv, "--out", str(out)])
            sg.main()
            return out.read_text(encoding="utf-8"), seen
        return go

    def test_several_names_are_each_resolved_and_titled(self, run):
        page, seen = run("Shenango River", "Mahoning River", "--state", "PA")
        assert seen["anchors"] == ["id:Shenango River", "id:Mahoning River"]
        assert seen["labels"] == ["Shenango River", "Mahoning River"]
        assert "<title>Shenango River + Mahoning River stream gages</title>" in page

    def test_repeated_anchors_need_no_names(self, run):
        _, seen = run("--anchor", "03104515", "--anchor", "03099695")
        assert seen["anchors"] == ["03104515", "03099695"]
        assert seen["labels"] == ["USGS 03104515", "USGS 03099695"]

    def test_a_single_anchor_keeps_the_river_name(self, run):
        _, seen = run("Peace River", "--anchor", "02296750")
        assert seen["anchors"] == ["02296750"] and seen["labels"] == ["Peace River"]

    @pytest.mark.parametrize("argv", [("A River", "B River", "--anchor", "1"),
                                      ("A River", "--anchor", "1", "--anchor", "2")])
    def test_anchors_that_do_not_match_the_names_are_an_error(self, run, capsys, argv):
        with pytest.raises(SystemExit):
            run(*argv)
        assert "--anchor goes with one river name" in capsys.readouterr().err

    def test_a_name_given_twice_is_one_river(self, run):
        _, seen = run("Peace River", " Peace River")
        assert seen["anchors"] == ["id:Peace River"] and seen["labels"] == ["Peace River"]

    def test_the_same_site_given_twice_is_one_river(self, run):
        _, seen = run("--anchor", "03104515", "--anchor", "03104515", "--anchor", "03099695")
        assert seen["anchors"] == ["03104515", "03099695"]

    def test_two_names_for_one_site_are_one_river(self, run, monkeypatch):
        monkeypatch.setattr(sg, "resolve_anchor", lambda name, state=None, list_only=False: "02296750")
        _, seen = run("Peace River", "Peace R")
        assert seen["anchors"] == ["02296750"] and seen["labels"] == ["Peace River"]

    def test_the_page_embeds_the_confluences(self, run):
        c = {"lat": 40.9, "lon": -80.0, "rivers": ["A", "B"], "nearest": []}
        page, _ = run("A", "B", confluences=[c])
        embedded = json.loads(re.search(r"const CONFLUENCES = (.*?);\n", page)[1])
        assert embedded == [c]

    def test_without_confluences_the_page_has_an_empty_list(self, run):
        page, _ = run("A")
        assert "const CONFLUENCES = [];" in page

    def test_confluences_are_listed_in_the_terminal_output(self, run, capsys):
        run("A", "B", confluences=[{"lat": 40.9, "lon": -80.0, "rivers": ["A", "B"],
                                    "nearest": [{"river": "A", "site": "1", "name": "n", "miles": 2.5}]}])
        out = capsys.readouterr().out
        assert "Confluences (1):" in out and "A + B" in out and "A: 1 2.5 mi" in out

    def test_list_covers_every_name(self, monkeypatch, capsys):
        listed = []
        def resolve(name, state=None, list_only=False):
            listed.append(name)
            raise SystemExit(0)  # what the real one does after printing the candidates

        monkeypatch.setattr(sg, "resolve_anchor", resolve)
        monkeypatch.setattr("sys.argv", ["streamgage.py", "A River", "B River", "--list"])
        with pytest.raises(SystemExit) as exc:
            sg.main()
        assert listed == ["A River", "B River"] and exc.value.code == 0


SIX = ["Allegheny River", "Monongahela River", "Youghiogheny River", "Kiskiminetas River", "Beaver River",
       "Ohio River"]
SITES = ["03049828", "03085140", "03083900", "03048600", "03107500", "03108500"]


class TestFileStem:
    def test_one_river_keeps_its_whole_name(self):
        assert sg.file_stem(["Peace River"], ["02298300"]) == "peace_river"
        assert sg.file_stem(["St. Mary's River"], ["1"]) == "st_mary_s_river"

    def test_a_few_rivers_drop_the_word_river(self):
        assert sg.file_stem(["Shenango River", "Mahoning River"], ["1", "2"]) == "shenango_mahoning"
        assert sg.file_stem(SIX[:3], SITES[:3]) == "allegheny_monongahela_youghiogheny"

    def test_only_a_trailing_river_is_dropped_and_other_words_stay(self):
        assert sg.file_stem(["Peace River", "Horse Creek"], ["1", "2"]) == "peace_horse_creek"
        assert sg.file_stem(["River Raisin", "Peace River"], ["1", "2"]) == "river_raisin_peace"
        assert sg.file_stem(["River", "Peace River"], ["1", "2"]) == "river_peace"  # nothing left otherwise
        assert sg.file_stem(["peace RIVER", "horse creek"], ["1", "2"]) == "peace_horse_creek"
        assert sg.file_stem(["Peace River Fork", "Horse Creek"], ["1", "2"]) == "peace_river_fork_horse_creek"

    def test_a_name_that_is_nothing_but_river_once_it_is_dropped_keeps_its_word(self):
        assert sg.file_stem(["- River", "Peace River"], ["1", "2"]) == "river_peace"

    def test_anchor_labels_are_kept_whole(self):
        assert sg.file_stem(["USGS 03104515", "USGS 03099695"], ["03104515", "03099695"]) == \
            "usgs_03104515_usgs_03099695"

    def test_three_rivers_is_the_most_that_are_all_named(self):
        assert sg.MAX_NAMES_IN_FILE == 3
        assert "plus" not in sg.file_stem(SIX[:3], SITES[:3])
        assert "_plus_1_" in sg.file_stem(SIX[:4], SITES[:4])

    def test_more_than_that_gives_the_first_three_a_count_and_a_hash(self):
        stem = sg.file_stem(SIX, SITES)
        assert re.fullmatch(r"allegheny_monongahela_youghiogheny_plus_3_[0-9a-f]{6}", stem)

    def test_the_hash_is_the_same_for_the_same_rivers_and_different_for_others(self):
        same = sg.file_stem(SIX, SITES) == sg.file_stem(list(SIX), list(SITES))
        other_site = sg.file_stem(SIX, SITES[:-1] + ["03108501"])
        other_last = sg.file_stem(SIX[:-1] + ["Muskingum River"], SITES[:-1] + ["03150000"])
        assert same and other_site != sg.file_stem(SIX, SITES) != other_last

    def test_the_hash_tells_apart_sets_that_share_their_first_three(self):
        a = sg.file_stem(SIX[:3] + ["A River", "B River"], SITES[:3] + ["1", "2"])
        b = sg.file_stem(SIX[:3] + ["A River", "B River"], SITES[:3] + ["1", "3"])
        assert a.rsplit("_", 1)[0] == b.rsplit("_", 1)[0] and a != b

    def test_six_rivers_make_a_short_file_name(self):
        assert len(sg.file_stem(SIX, SITES) + "_gages.html") < 65  # it was 106


class TestFileNames:
    """What main() calls the file when --out is not given."""

    @pytest.fixture
    def run(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sg, "resolve_anchor", lambda name, state=None, list_only=False: f"id:{name}")
        monkeypatch.setattr(sg, "build_rivers", lambda anchors, labels, km, draw=False, as_of=None: (
            [], [gage()], [], []))

        def go(*argv):
            monkeypatch.setattr("sys.argv", ["streamgage.py", *argv])
            sg.main()
            return sorted(p.name for p in tmp_path.glob("*.html"))
        return go

    def test_two_rivers(self, run):
        assert run("Shenango River", "Mahoning River") == ["shenango_mahoning_gages.html"]

    def test_six_rivers(self, run):
        names = ["Allegheny River", "Monongahela River", "Youghiogheny River", "Kiskiminetas River",
                 "Beaver River", "Ohio River"]
        (name,) = run(*names)
        assert re.fullmatch(r"allegheny_monongahela_youghiogheny_plus_3_[0-9a-f]{6}_gages\.html", name)

    def test_one_river_is_named_as_it_always_was(self, run):
        assert run("Peace River") == ["peace_river_gages.html"]

    def test_the_moment_still_comes_last(self, run):
        assert run("Shenango River", "Mahoning River", "--as-of", "2026-11-03T12:00-05:00") == \
            ["shenango_mahoning_2026-11-03_1200_gages.html"]

    def test_the_page_title_still_lists_every_river(self, run, tmp_path):
        run("Shenango River", "Mahoning River")
        page = (tmp_path / "shenango_mahoning_gages.html").read_text(encoding="utf-8")
        assert "<title>Shenango River + Mahoning River stream gages</title>" in page

    def test_name_gives_the_file_and_the_title(self, run, tmp_path):
        assert run("Shenango River", "Mahoning River", "Beaver River", "--name", "Pittsburgh rivers") == \
            ["pittsburgh_rivers_gages.html"]
        page = (tmp_path / "pittsburgh_rivers_gages.html").read_text(encoding="utf-8")
        assert "<title>Pittsburgh rivers stream gages</title>" in page
        assert 'const TITLE = "Pittsburgh rivers";' in page

    def test_name_is_trimmed_and_works_for_one_river(self, run, tmp_path):
        assert run("Peace River", "--name", "  The Peace  ") == ["the_peace_gages.html"]
        page = (tmp_path / "the_peace_gages.html").read_text(encoding="utf-8")
        assert "<title>The Peace stream gages</title>" in page and 'const TITLE = "The Peace";' in page

    def test_name_keeps_the_moment_too(self, run):
        assert run("Peace River", "--name", "Arcadia", "--as-of", "2026-11-03T12:00-05:00") == \
            ["arcadia_2026-11-03_1200_gages.html"]

    def test_out_decides_the_file_and_name_still_decides_the_title(self, run, tmp_path):
        assert run("Peace River", "--name", "Arcadia", "--out", str(tmp_path / "x.html")) == ["x.html"]
        assert "<title>Arcadia stream gages</title>" in (tmp_path / "x.html").read_text(encoding="utf-8")

    @pytest.mark.parametrize("bad", ["", "  ", "!!!", "..."])
    def test_a_name_with_no_letters_or_digits_is_refused(self, run, capsys, bad):
        with pytest.raises(SystemExit):
            run("Peace River", "--name", bad)
        assert "--name needs at least one letter or digit" in capsys.readouterr().err
