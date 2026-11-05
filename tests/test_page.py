"""The generated page: template contract, valid JavaScript, and the logic that colors markers."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import streamgage as sg
from tests.conftest import gage, retired_gage

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
LOGIC_TESTS = Path(__file__).parent / "page_logic.js"


def inline_script(page):
    """The page's own <script> block (not the Leaflet one)."""
    return re.search(r"<script>\n(.*?)</script>", page, re.DOTALL)[1]


def test_template_placeholders_are_the_ones_main_substitutes():
    assert set(re.findall(r"__[A-Z_]+__", sg.HTML)) == {
        "__TITLE__", "__TITLE_JSON__", "__RIVER__", "__GAGES__", "__RETIRED__", "__GENERATED__"}


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


def test_the_diamond_modes_and_trend_glyphs_are_in_the_page(make_page):
    page = make_page([gage()])
    for label in ("Position in 7-day range", "Trend (last 6 h)", "Percentile of past year"):
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


def between(text, start, end):
    """The part of the template from `start` up to (not including) `end`; fail loudly if moved."""
    assert start in text and end in text, f"template markers moved: {start!r} .. {end!r}"
    return text[text.index(start):text.index(end)]


@needs_node
def test_page_logic(tmp_path):
    """Run the page's binning, coloring and formatting code under node, with assertions."""
    template = sg.HTML
    prelude = "\n".join([
        between(template, "const esc =", "const map ="),  # esc, tables, bins, color
        between(template, "function spark(g)", "function icon(g)"),  # sparklines, helpers, popup
        between(template, "function retiredPopup", "if (RETIRED.length) {"),  # retired popup
    ])
    script = tmp_path / "logic.js"
    script.write_text(prelude + "\n" + LOGIC_TESTS.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run([NODE, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "page logic ok" in result.stdout
