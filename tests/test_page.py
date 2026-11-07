"""The generated page: template contract, valid JavaScript, and the logic that colors markers."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import streamgage as sg
from tests.conftest import confluence, gage, retired_gage

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
LOGIC_TESTS = Path(__file__).parent / "page_logic.js"


def inline_script(page):
    """The page's own <script> block (not the Leaflet one)."""
    return re.search(r"<script>\n(.*?)</script>", page, re.DOTALL)[1]


def test_template_placeholders_are_the_ones_main_substitutes():
    assert set(re.findall(r"__[A-Z_]+__", sg.HTML)) == {
        "__TITLE__", "__TITLE_JSON__", "__RIVER__", "__GAGES__", "__RETIRED__", "__CONFLUENCES__",
        "__AS_OF__", "__GENERATED__"}


def test_leaflet_is_pinned_with_integrity_hashes():
    tags = re.findall(r"<(?:link|script)[^>]*unpkg\.com[^>]*>", sg.HTML)
    assert len(tags) == 2
    for tag in tags:
        assert "leaflet@1.9.4" in tag
        assert re.search(r'integrity="sha256-[A-Za-z0-9+/]{43}="', tag)
        assert 'crossorigin=""' in tag


def test_the_retired_layer_is_wired_into_the_page(make_page):
    page = make_page([gage()], retired=[retired_gage()])
    assert "layersCtl.addOverlay(retiredLayer" in page  # a checkbox in the layers control
    assert "No longer reporting (retired or offline)" in page  # the legend entry
    assert "const RETIRED = [{" in page


def test_the_confluence_layer_is_wired_into_the_page(make_page):
    page = make_page([gage()], confluences=[confluence()])
    assert "layersCtl.addOverlay(confluenceLayer" in page  # a checkbox in the layers control
    assert "</span>River confluence</div>" in page  # the legend entry
    assert "const CONFLUENCES = [{" in page
    assert "Peace River" in page and "Horse Creek Near Nocatee FL" in page


def test_a_single_river_page_has_no_confluences(make_page):
    assert "const CONFLUENCES = [];" in make_page([gage()])


def test_the_cresting_rule_is_in_the_template_with_its_documented_thresholds():
    consts = dict(re.findall(r"const (STEADY_BAND|CREST_MIN_AGE_H|CREST_MAX_AGE_H|CREST_MIN_RANGE_POS) = ([\d.]+);",
                             sg.HTML))
    assert consts == {"STEADY_BAND": "0.02", "CREST_MIN_AGE_H": "0.25", "CREST_MAX_AGE_H": "6",
                      "CREST_MIN_RANGE_POS": "95"}


def test_the_legend_explains_the_crest_glyph_only_when_a_gage_has_it():
    assert "if (GAGES.some(isCresting)) h += crestLegendLine();" in sg.HTML


def test_the_marker_uses_the_shared_glyph_builder():
    assert "glyph = glyphFor(g)" in sg.HTML


def test_the_diamond_modes_and_trend_glyphs_are_in_the_page(make_page):
    page = make_page([gage()])
    for label in ("Position in 7-day range", "Trend (last 6 h)", "Percentile for this date (all years)"):
        assert label in page
    assert page.count('name="mode"') == 1  # the radio group is built from MODES in JavaScript


@needs_node
def test_embedded_script_is_valid_javascript(make_page, tmp_path):
    script = tmp_path / "page.js"
    script.write_text(inline_script(make_page([gage()])), encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@needs_node
def test_embedded_script_is_valid_with_hostile_data(make_page, tmp_path):
    evil = gage(name="</script><img src=x onerror=alert(1)>", site="1\"><b>")
    old = retired_gage(name="</script><b>", site="2\"><i>", params=["</script>"])
    script = tmp_path / "page.js"
    script.write_text(inline_script(make_page([evil], title="a</script>b", retired=[old])),
                      encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@needs_node
def test_embedded_script_is_valid_with_hostile_confluence_names(make_page, tmp_path):
    evil = confluence(rivers=("</script><img src=x onerror=alert(1)>", "<b>x</b>"))
    page = make_page([gage()], title="a</script>b + c", confluences=[evil])
    assert "</script><img" not in inline_script(page)  # the data cannot end the script block
    script = tmp_path / "page.js"
    script.write_text(inline_script(page), encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def between(text, start, end):
    """The part of the template from `start` up to (not including) `end`; fail loudly if moved."""
    assert start in text and end in text, f"template markers moved: {start!r} .. {end!r}"
    return text[text.index(start):text.index(end)]


@needs_node
def test_page_logic(tmp_path):
    """Run the page's binning, coloring and formatting code under node, with assertions."""
    template = sg.HTML
    prelude = "\n".join([
        "let AS_OF = null;",  # set by the page from --as-of; tests change it
        between(template, "const esc =", "const map ="),  # esc, tables, bins, color
        between(template, "function spark(g)", "function icon(g)"),  # sparklines, helpers, popup
        between(template, "function retiredPopup", "if (RETIRED.length) {"),  # retired popup
        between(template, "function confluencePopup", "if (CONFLUENCES.length) {"),  # confluence popup
    ])
    script = tmp_path / "logic.js"
    script.write_text(prelude + "\n" + LOGIC_TESTS.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "page logic ok" in result.stdout
