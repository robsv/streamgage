"""Shared test helpers. No test makes a network request.

The JSON files in tests/fixtures are real USGS and NWS responses, trimmed to a few days so they
stay small. Everything else is built in the tests themselves.
"""

import json
from pathlib import Path

import pytest

import streamgage as sg

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    """Load a recorded response from tests/fixtures by file name."""
    def load(name):
        return json.loads((FIXTURES / name).read_text())
    return load


@pytest.fixture
def route(monkeypatch):
    """Replace sg.get_json and sg.get_text with a router, so code under test never uses the network.

    Call route({"substring": response, ...}). A request goes to the response whose key is the
    longest substring of the URL. A response can be a dict, list or string (returned as is), an
    exception (raised), or a callable taking the request params. Any unrouted request fails the
    test. The returned list records every (url, params) request made.
    """
    def install(routes):
        calls = []

        def fake_get(url, **params):
            calls.append((url, params))
            for key in sorted(routes, key=len, reverse=True):
                if key in url:
                    resp = routes[key]
                    if isinstance(resp, Exception):
                        raise resp
                    return resp(params) if callable(resp) else resp
            raise AssertionError(f"unexpected request: {url} {params}")

        monkeypatch.setattr(sg, "get_json", fake_get)
        monkeypatch.setattr(sg, "get_text", fake_get)  # RDB (tab-separated) responses
        return calls
    return install


@pytest.fixture
def make_page(monkeypatch, tmp_path):
    """Render a real page through sg.main() with the network stages stubbed out.

    make_page(gages, title="Peace River", lines=(), retired=()) returns the HTML text. Use it to
    test what the tool writes, not just the functions behind it.
    """
    def render(gages, title="Peace River", lines=(), retired=(), extra_args=()):
        out = tmp_path / "page.html"
        monkeypatch.setattr(sg, "resolve_anchor", lambda *a, **k: "01234567")
        monkeypatch.setattr(sg, "build_gages", lambda anchor, km, draw=False, as_of=None: (list(lines), gages, list(retired)))
        monkeypatch.setattr("sys.argv", ["streamgage.py", title, "--out", str(out), *extra_args])
        sg.main()
        return out.read_text(encoding="utf-8")
    return render


def gage(site="02297345", name="Peace River At Platt FL", **overrides):
    """A gage dict shaped like build_gages() output: a diamond at the 100th percentile of the past
    year and the 94th for this date over all years."""
    base = {"site": site, "name": name, "lat": 27.08, "lon": -82.0, "stage": 2.89,
            "time": "2026-11-04T20:00:00.000-05:00", "method": "percentile", "pct": 100.0,
            "longterm": {"pct": 94.0, "status": "within", "years": 53, "first": 1973, "last": 2026,
                         "month": 11, "day": 4, "median": 2.27, "high": 2.9, "high_year": 2006,
                         "low": 0.4, "low_year": 2001},
            "recent": {"low": -0.77, "high": 2.89, "peak": 2.89, "peak_time": 1793840400000,
                       "range_pos": 100.0, "rate": 0.008277, "delta24": 1.48,
                       "series": [[1793836800000, 2.5], [1793840400000, 2.89]]}}
    base.update(overrides)
    return base


def retired_gage(site="02298300", name="Peace River At Punta Gorda FL", **overrides):
    """A retired-gage dict shaped like build_gages() output for a site that no longer reports."""
    base = {"site": site, "name": name, "lat": 26.95, "lon": -82.05, "status": "retired",
            "last": "2005-09-29", "first": "2002-09-30",
            "params": ["specific conductance", "stage", "water temperature"]}
    base.update(overrides)
    return base
