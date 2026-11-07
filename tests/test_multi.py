"""Mapping several rivers at once, and finding where they meet."""

import json
import re
import threading
import time
from pathlib import Path

import pytest
import requests

import streamgage as sg
from tests.conftest import gage


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


def recorded(region):
    """Flowline paths recorded from NLDI (trimmed to each segment's comid and end points) for a set
    of rivers, in the shape fetch_paths returns."""
    data = json.loads((Path(__file__).parent / "fixtures" / "nldi_paths.json").read_text())[region]
    return {name: {c: seg(c, (lon0, lat0), (lon1, lat1)) for c, lon0, lat0, lon1, lat1 in rows}
            for name, rows in data.items()}


class TestRecordedRivers:
    """The rules were tuned on real flowlines, so keep real ones as a check that they still hold."""

    def places(self, found):
        return [((round(c["lat"], 2), round(c["lon"], 2)), c["rivers"]) for c in found]

    def test_pennsylvania_names_all_three_at_the_start_of_the_beaver(self):
        found = sg.find_confluences(recorded("pennsylvania"))
        assert self.places(found) == [((40.96, -80.38),
                                       ["Shenango River", "Mahoning River", "Beaver River"])]

    def test_the_florida_creeks_each_meet_the_peace_where_they_join_it(self):
        found = sg.find_confluences(recorded("florida"))
        assert self.places(found) == [((27.16, -81.90), ["Peace River", "Joshua Creek"]),
                                      ((27.10, -81.99), ["Peace River", "Horse Creek"]),
                                      ((26.98, -81.99), ["Peace River", "Shell Creek"])]

    def test_the_myakka_never_meets_the_peace(self):
        assert all("Myakka River" not in c["rivers"] for c in sg.find_confluences(recorded("florida")))

    def test_the_order_the_rivers_are_given_in_does_not_change_where_they_meet(self):
        paths = recorded("florida")
        flipped = dict(reversed(list(paths.items())))
        assert (sorted((round(c["lat"], 3), round(c["lon"], 3)) for c in sg.find_confluences(flipped))
                == sorted((round(c["lat"], 3), round(c["lon"], 3)) for c in sg.find_confluences(paths)))

    def test_leaving_a_river_out_leaves_the_others_confluences(self):
        paths = recorded("florida")
        del paths["Horse Creek"]
        assert [c["rivers"] for c in sg.find_confluences(paths)] == [["Peace River", "Joshua Creek"],
                                                                     ["Peace River", "Shell Creek"]]


class TestFetchPaths:
    def test_keeps_every_flowline_from_both_walks_once(self, route):
        calls = route({"/UM/flowlines": {"features": [A_ONLY]},
                       "/DM/flowlines": {"features": [A_ONLY, *SHARED]}})
        got = sg.fetch_paths("03104515", 322)
        assert sorted(got) == [1, 3, 4]
        assert {url.split("/")[-2] for url, _ in calls} == {"UM", "DM"}
        assert all(params["distance"] == 322 for _, params in calls)


class TestNearestGages:
    def test_picks_the_closest_gage_on_each_river(self):
        c = {"lat": 40.9, "lon": -80.0, "rivers": ["A", "B", "C"]}
        by_river = {"A": [gage("1", "Far", lat=41.5, lon=-80.0), gage("2", "Near", lat=40.95, lon=-80.0)],
                    "B": [gage("3", "Only", lat=40.9, lon=-80.1)], "C": []}
        out = sg.nearest_gages(c, by_river)
        assert [(n["river"], n["site"]) for n in out] == [("A", "2"), ("B", "3")]  # C has no gage
        assert out[0]["km"] == pytest.approx(5.6, abs=0.1)


@pytest.fixture
def stubbed(monkeypatch):
    """build_gages returns one gage per anchor; fetch_paths gives A and B a shared stretch."""
    built = []

    def build_gages(anchor, km, draw=False, as_of=None):
        built.append(anchor)
        if anchor == "EMPTY":
            raise SystemExit("No gages on this river are reporting recent gage height (parameter 00065).")
        lat = {"A": 41.0, "B": 40.95, "C": 40.0}.get(anchor, 40.0)
        shared_site = gage("SHARED01", "At the join", lat=40.9, lon=-80.0)
        return ([seg(hash(anchor) % 1000 + 100, (-80, lat), (-80, lat - .01)), A_ONLY],
                [gage(f"{anchor}00001", f"{anchor} gage", lat=lat), shared_site],
                [{"site": f"{anchor}RET", "lat": 40.0, "lon": -80.0}])

    monkeypatch.setattr(sg, "build_gages", build_gages)
    monkeypatch.setattr(sg, "fetch_paths", lambda anchor, km: {
        "A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)}.get(anchor, {}))
    return built


class TestBuildRivers:
    def test_one_river_is_just_build_gages(self, stubbed):
        lines, gages, retired, confluences = sg.build_rivers(["A"], ["A"], 322)
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
        lines, gages, _, confluences = sg.build_rivers(["A", "EMPTY", "B"], ["A", "Dry Creek", "B"], 322)
        assert "skipping Dry Creek" in capsys.readouterr().err
        assert len(confluences) == 1 and confluences[0]["rivers"] == ["A", "B"]

    def test_all_rivers_empty_exits(self, stubbed):
        with pytest.raises(SystemExit, match="None of these rivers"):
            sg.build_rivers(["EMPTY", "EMPTY"], ["X", "Y"], 322)

    def test_rivers_keep_the_order_given_even_when_their_lookups_finish_out_of_order(self, stubbed, monkeypatch):
        by_anchor = {"A": paths(A_ONLY, *SHARED), "B": paths(B_ONLY, *SHARED)}
        started = threading.Event()

        def slow_first(anchor, km):
            if anchor == "A":
                started.wait(1)  # A finishes after B has already finished
                time.sleep(0.05)
            else:
                started.set()
            return by_anchor[anchor]

        monkeypatch.setattr(sg, "fetch_paths", slow_first)
        *_, confluences = sg.build_rivers(["A", "B"], ["Alpha", "Beta"], 322)
        assert [c["rivers"] for c in confluences] == [["Alpha", "Beta"]]
        assert [n["river"] for n in confluences[0]["nearest"]] == ["Alpha", "Beta"]

    def test_a_failed_confluence_lookup_still_gives_the_map(self, stubbed, monkeypatch, capsys):
        def boom(anchor, km):
            raise requests.ConnectionError("nldi down")
        monkeypatch.setattr(sg, "fetch_paths", boom)
        lines, gages, _, confluences = sg.build_rivers(["A", "B"], ["A", "B"], 322)
        assert confluences == [] and len(gages) == 3
        assert "Could not look for confluences" in capsys.readouterr().err

    def test_a_river_that_fails_on_the_network_is_skipped(self, stubbed, monkeypatch, capsys):
        real = sg.build_gages

        def flaky(anchor, *a, **k):
            if anchor == "B":
                raise requests.HTTPError("429 Client Error: Too Many Requests")
            return real(anchor, *a, **k)

        monkeypatch.setattr(sg, "build_gages", flaky)
        lines, gages, _, confluences = sg.build_rivers(["A", "B", "C"], ["A", "B", "C"], 322)
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
                                    "nearest": [{"river": "A", "site": "1", "name": "n", "km": 2.5}]}])
        out = capsys.readouterr().out
        assert "Confluences (1):" in out and "A + B" in out and "A: 1 2.5 km" in out

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
