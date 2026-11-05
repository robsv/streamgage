"""build_gages() assembly and the command line (main), with every network stage stubbed."""

import json
import re
from datetime import datetime, timezone

import pytest

import streamgage as sg
from tests.conftest import gage

STAGES = {"action": 9, "minor": 11, "moderate": 14, "major": 16}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def readings():
    """Two days of 15-minute readings ending now, rising 0.05 ft/h."""
    end = datetime.now(timezone.utc).timestamp()
    return [(end - (192 - i) * 900, 2 + 0.05 * i / 4) for i in range(193)]


@pytest.fixture
def river(monkeypatch, route):
    """Two gages: Arcadia (has NWS stages and a forecast) and Platt (does not)."""
    sites = {"02296750": ("PEACE RIVER AT SR 70 AT ARCADIA, FL", 27.22, -81.88),
             "02297345": ("PEACE RIVER AT PLATT FL", 27.09, -82.0)}
    monkeypatch.setattr(sg, "fetch_river", lambda anchor, km: ([{"line": 1}], sites))
    monkeypatch.setattr(sg, "fetch_current",
                        lambda ids: {"02296750": (15.5, now_iso()), "02297345": (2.89, now_iso())})
    monkeypatch.setattr(sg, "fetch_recent", lambda site: readings())
    monkeypatch.setattr(sg, "fetch_nws", lambda site, lat, lon, gauges: (
        {"lid": "ARCF1", "stages": STAGES} if site == "02296750" else None))
    monkeypatch.setattr(sg, "fetch_forecast", lambda lid, stages: {
        "issued": "x", "series": [], "crest": 16.5, "crest_time": "t", "crest_category": "major"})
    monkeypatch.setattr(sg, "fetch_history", lambda site: [float(i) / 10 for i in range(40)])
    monkeypatch.setattr(sg, "fetch_catalog", lambda ids: {})
    return route({sg.NWPS: {"gauges": [{"lid": "ARCF1"}]}})


class TestBuildGages:
    def test_assembles_nws_and_percentile_gages(self, river):
        lines, gages, retired = sg.build_gages("02298300", 322)
        assert lines == [{"line": 1}]
        assert retired == []
        by_site = {g["site"]: g for g in gages}
        arcadia, platt = by_site["02296750"], by_site["02297345"]

        assert arcadia["method"] == "nws"
        assert arcadia["name"] == "Peace River At SR 70 At Arcadia, FL"
        assert arcadia["lid"] == "ARCF1" and arcadia["stages"] == STAGES
        assert arcadia["category"] == "moderate"  # 15.5 ft: above 14, below 16
        assert arcadia["forecast"]["crest_category"] == "major"
        assert "pct" not in arcadia

        assert platt["method"] == "percentile"
        assert platt["pct"] == 72.5  # 29 of the 40 stub history values (0.0 to 3.9) are <= 2.89
        assert "forecast" not in platt and "stages" not in platt

    def test_every_gage_carries_recent_stats(self, river):
        for g in sg.build_gages("02298300", 322)[1]:
            assert g["recent"]["rate"] == pytest.approx(0.05, abs=0.001)
            assert g["recent"]["range_pos"] == 100.0

    def test_gages_are_sorted_north_to_south(self, river):
        assert [g["lat"] for g in sg.build_gages("02298300", 322)[1]] == [27.22, 27.09]

    def test_requests_nws_gauges_for_the_bounding_box_of_the_reporting_gages(self, river):
        sg.build_gages("02298300", 322)
        _, params = river[0]
        assert params["bbox.xmin"] == pytest.approx(-82.0 - 0.02)
        assert params["bbox.xmax"] == pytest.approx(-81.88 + 0.02)
        assert params["bbox.ymin"] == pytest.approx(27.09 - 0.02)
        assert params["bbox.ymax"] == pytest.approx(27.22 + 0.02)
        assert params["srid"] == "EPSG_4326"

    def test_gages_not_reporting_are_left_out(self, river, monkeypatch):
        monkeypatch.setattr(sg, "fetch_current", lambda ids: {"02297345": (2.89, now_iso())})
        gages = sg.build_gages("02298300", 322)[1]
        assert [g["site"] for g in gages] == ["02297345"]

    def test_no_reporting_gages_exits(self, river, monkeypatch):
        monkeypatch.setattr(sg, "fetch_current", lambda ids: {})
        with pytest.raises(SystemExit) as exc:
            sg.build_gages("02298300", 322)
        assert "No gages on this river are reporting recent gage height" in str(exc.value)

    def test_a_gage_with_no_recent_readings_keeps_its_other_data(self, river, monkeypatch):
        monkeypatch.setattr(sg, "fetch_recent", lambda site: [])
        gages = sg.build_gages("02298300", 322)[1]
        assert len(gages) == 2
        assert all(g["recent"] is None for g in gages)
        assert {g["method"] for g in gages} == {"nws", "percentile"}

    def test_a_short_history_gives_no_percentile_but_still_a_gage(self, river, monkeypatch):
        monkeypatch.setattr(sg, "fetch_history", lambda site: [1.0] * 5)
        platt = next(g for g in sg.build_gages("02298300", 322)[1] if g["site"] == "02297345")
        assert platt["pct"] is None


class TestMainOutput:
    def test_writes_a_page_that_embeds_the_data(self, make_page):
        gages = [gage(), gage("02296750", "Peace River At SR 70 At Arcadia, FL")]
        page = make_page(gages, lines=[{"type": "Feature", "properties": {}}])
        embedded = json.loads(re.search(r"const GAGES = (.*?);\nconst RETIRED", page, re.DOTALL)[1])
        assert embedded == gages
        river = json.loads(re.search(r"const RIVER = (.*?);\nconst GAGES", page, re.DOTALL)[1])
        assert river == {"type": "FeatureCollection",
                         "features": [{"type": "Feature", "properties": {}}]}
        assert "<title>Peace River stream gages</title>" in page
        assert re.search(r'const GENERATED = "\d{4}-\d\d-\d\d \d\d:\d\d UTC"', page)

    def test_no_placeholder_is_left_in_the_page(self, make_page):
        assert re.findall(r"__[A-Z_]+__", make_page([gage()])) == []

    def test_the_page_is_written_as_utf8(self, make_page, tmp_path):
        make_page([gage()])
        raw = (tmp_path / "page.html").read_bytes()
        assert "▲".encode() in raw and "▬".encode() in raw  # the trend glyphs

    def test_hostile_gage_names_cannot_end_the_script_block(self, make_page):
        evil = "</script><img src=x onerror=alert(1)>"
        page = make_page([gage(name=evil)])
        assert page.count("</script>") == sg.HTML.count("</script>")
        embedded = json.loads(re.search(r"const GAGES = (.*?);\nconst RETIRED", page, re.DOTALL)[1])
        assert embedded[0]["name"] == evil  # still exact once parsed

    def test_hostile_title_is_escaped(self, make_page):
        page = make_page([gage()], title='<b>"Bad"</b> & Co')
        assert "<title>&lt;b&gt;&quot;Bad&quot;&lt;/b&gt; &amp; Co stream gages</title>" in page
        assert "<b>" not in page.split("<script>")[0]
        assert page.count("</script>") == sg.HTML.count("</script>")

    def test_placeholder_text_inside_a_name_is_not_substituted(self, make_page):
        page = make_page([gage(name="__TITLE__ __GENERATED__")])
        embedded = json.loads(re.search(r"const GAGES = (.*?);\nconst RETIRED", page, re.DOTALL)[1])
        assert embedded[0]["name"] == "__TITLE__ __GENERATED__"


class TestMainTerminalOutput:
    def test_one_line_per_gage_in_the_documented_format(self, make_page, capsys):
        make_page([gage()])
        assert capsys.readouterr().out == (
            "02297345     2.89 ft  p100      +0.008 ft/h  Peace River At Platt FL\n")

    def test_nws_gage_prints_category_and_forecast_crest(self, make_page, capsys):
        nws = gage("02296750", "Peace River At SR 70 At Arcadia, FL", method="nws",
                   category="moderate", stage=15.54,
                   forecast={"crest": 16.5, "crest_category": "major",
                             "crest_time": "2026-11-06T00:00:00Z"})
        make_page([nws])
        out = capsys.readouterr().out
        assert out.startswith("02296750    15.54 ft  moderate -> crest 16.5 ft major "
                              "2026-11-06T00:00:00Z +0.008 ft/h  Peace River At SR 70")

    def test_nws_gage_without_a_forecast(self, make_page, capsys):
        nws = gage(method="nws", category="normal", forecast=None)
        make_page([nws])
        assert re.match(r"\S+\s+2\.89 ft  normal    \+0\.008 ft/h  ", capsys.readouterr().out)

    def test_missing_percentile_and_trend_print_na(self, make_page, capsys):
        make_page([gage(pct=None, recent=None)])
        assert capsys.readouterr().out == (
            "02297345     2.89 ft  n/a               n/a  Peace River At Platt FL\n")

    def test_trend_unavailable_but_other_stats_present(self, make_page, capsys):
        make_page([gage(recent={"rate": None, "series": []})])
        assert "n/a  Peace River At Platt FL" in capsys.readouterr().out

    def test_falling_trend_has_a_minus_sign(self, make_page, capsys):
        make_page([gage(recent={"rate": -0.0864, "series": []})])
        assert "-0.086 ft/h" in capsys.readouterr().out


class TestCommandLine:
    @pytest.fixture
    def captured(self, monkeypatch, tmp_path):
        """Stub the network stages and record what main() passes to them."""
        monkeypatch.chdir(tmp_path)
        seen = {"resolve": [], "distance": []}

        def resolve(river, state=None, list_only=False):
            seen["resolve"].append((river, state, list_only))
            if list_only:
                raise SystemExit(0)
            return "01234567"

        def build(anchor, km, draw=False):
            seen["anchor"], seen["distance"], seen["draw_retired"] = anchor, km, draw
            return [], [gage()], []

        monkeypatch.setattr(sg, "resolve_anchor", resolve)
        monkeypatch.setattr(sg, "build_gages", build)
        return seen

    def run(self, monkeypatch, *args):
        monkeypatch.setattr("sys.argv", ["streamgage.py", *args])
        sg.main()

    @pytest.mark.parametrize("args, km", [
        ((), 322),                      # the default, 200 miles
        (("--distance", "10"), 16),
        (("--distance", "12.5"), 20),
        (("--distance", "0.5"), 1),
    ])
    def test_distance_is_in_miles(self, monkeypatch, captured, args, km):
        self.run(monkeypatch, "Peace River", *args)
        assert captured["distance"] == km

    def test_default_output_name_comes_from_the_river(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "Peace River")
        assert (tmp_path / "peace_river_gages.html").exists()

    def test_default_output_name_for_awkward_names(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "St. Mary's River")
        assert (tmp_path / "st_mary_s_river_gages.html").exists()

    def test_anchor_only_skips_the_name_lookup(self, monkeypatch, captured, tmp_path):
        self.run(monkeypatch, "--anchor", "02296750")
        assert captured["resolve"] == [] and captured["anchor"] == "02296750"
        page = (tmp_path / "usgs_02296750_gages.html").read_text(encoding="utf-8")
        assert "<title>USGS 02296750 stream gages</title>" in page

    def test_river_and_state_are_passed_to_the_lookup(self, monkeypatch, captured):
        self.run(monkeypatch, "Little River", "--state", "NC")
        assert captured["resolve"] == [("Little River", "NC", False)]
        assert captured["anchor"] == "01234567"

    def test_list_always_lists_even_with_an_anchor(self, monkeypatch, captured):
        with pytest.raises(SystemExit) as exc:
            self.run(monkeypatch, "Peace River", "--list", "--anchor", "02296750")
        assert exc.value.code == 0
        assert captured["resolve"] == [("Peace River", None, True)]
        assert "anchor" not in captured

    def test_explicit_output_path(self, monkeypatch, captured, tmp_path):
        target = tmp_path / "sub.html"
        self.run(monkeypatch, "Peace River", "--out", str(target))
        assert target.exists()

    def test_needs_a_river_or_an_anchor(self, monkeypatch, captured, capsys):
        with pytest.raises(SystemExit) as exc:
            self.run(monkeypatch)
        assert exc.value.code == 2
        assert "give a river name or --anchor" in capsys.readouterr().err

    def test_list_needs_a_river_name(self, monkeypatch, captured, capsys):
        with pytest.raises(SystemExit) as exc:
            self.run(monkeypatch, "--list", "--anchor", "02296750")
        assert exc.value.code == 2
        assert "--list needs a river name" in capsys.readouterr().err

    def test_help_lists_the_distance_unit(self, monkeypatch, captured, capsys):
        with pytest.raises(SystemExit) as exc:
            self.run(monkeypatch, "--help")
        assert exc.value.code == 0
        assert "miles to navigate each direction (default: 200)" in " ".join(
            capsys.readouterr().out.split())
