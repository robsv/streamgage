#!/usr/bin/env python3
"""Draw an interactive map of a river with USGS gages colored by current stage.

Data sources (all public, no API key):
  * USGS NLDI           - river flowlines and gages along the main stem
  * USGS Water Services - current gage height (00065) and 365 days of daily values
  * NOAA NWPS           - NWS flood categories (action/minor/moderate/major)

Gages with NWS flood stages are colored by flood category (circles). Gages
without them (diamonds) can be recolored in the page by position within the
last 7 days' range (default), by 6-hour trend, or by the percentile of the
current stage within the past year of daily values. Every gage gets a
rising/falling arrow and a 7-day sparkline.

Examples:
    python streamgage.py "Peace River"
    python streamgage.py "Myakka River" --state FL
    python streamgage.py "Peace River" --list         # show candidate gages and exit
    python streamgage.py "Peace River" --anchor 02296750   # skip the name lookup
"""

import argparse
import html
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite"
NWIS = "https://waterservices.usgs.gov/nwis"
SITE_SERVICE = f"{NWIS}/site/"  # site catalog: what each site has ever measured
NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"
GAGE_HEIGHT = "00065"
DAILY_MAX, DAILY_MIN, DAILY_MEAN = "00001", "00002", "00003"  # NWIS statistic codes
MIN_HISTORY_DAYS = 30  # fewer daily values than this and there is no yearly percentile
RETIRED_AFTER = timedelta(days=365)  # no data for this long: "retired" rather than "offline"
CONTINUOUS = ("uv", "dv")  # catalog data types: real-time (15-minute) and daily values
STAGE_OR_FLOW = (GAGE_HEIGHT, "00060")  # a "gage" here means stage or discharge, not just water quality
PARAM_NAMES = {GAGE_HEIGHT: "stage", "00060": "discharge", "00010": "water temperature",
               "00095": "specific conductance", "00300": "dissolved oxygen", "00400": "pH",
               "63680": "turbidity"}
# the order and wording of the "not on the map" breakdown
CATEGORY_LABELS = (("discharge_only", "discharge only"), ("offline", "offline"), ("retired", "retired"),
                   ("field_visits", "field visits only"), ("water_quality", "water quality only"),
                   ("other", "other data only"), ("no_data", "no data"))
MONITORING = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations/items"
NO_DATA = -999  # NWIS/NWPS report missing values as -999, -9999 or -999999
MAX_SITES_PER_REQUEST = 100  # NWIS limit
STALE_AFTER = timedelta(days=3)  # ignore gages whose latest stage reading is older than this
FLOOD_CATEGORIES = ("action", "minor", "moderate", "major")  # the ones the page knows how to color
MATCH_RADIUS_KM = 0.5  # max distance between a USGS site and its NWS gauge
KM_PER_MILE = 1.609344  # --distance is in miles; the NLDI API takes whole kilometers
RECENT_DAYS = 7  # window for range position, peak and the recent sparkline
TREND_HOURS = 6  # window for the rate-of-change fit
MIN_RECENT_POINTS = 12  # fewer instantaneous readings than this and the recent stats are skipped
MIN_RANGE_FT = 0.1  # a 7-day range narrower than this is sensor noise, so no range position
MIN_RECENT_SPAN_HOURS = 24  # a record shorter than this (new or returning gage) has no meaningful range
TREND_MAX_AGE_HOURS = 3  # a last reading older than this is not "current", so no trend or 24 h change

session = requests.Session()
session.headers["User-Agent"] = "streamgage-map/1.0"


def get_json(url, **params):
    r = session.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico",
}


def find_candidates(river, state=None):
    """Stream gages whose USGS name starts with the river name, as dicts, largest basin first."""
    name = river.strip().upper().replace("'", "''")
    params = {"f": "json", "limit": 1000, "site_type_code": "ST",
              "filter": f"monitoring_location_name LIKE '{name} %'", "filter-lang": "cql-text"}
    if state:
        params["state_name"] = STATES.get(state.upper(), state.title())
    feats = get_json(MONITORING, **params)["features"]
    out = []
    for f in feats:
        p = f["properties"]
        if len(p["monitoring_location_number"]) != 8:  # skip miscellaneous/project sites
            continue
        out.append({"site": p["monitoring_location_number"], "name": p["monitoring_location_name"],
                    "state": p["state_name"], "area": p["drainage_area"]})
    return sorted(out, key=lambda c: -(c["area"] or 0))


def nldi_knows(site):
    return session.get(f"{NLDI}/USGS-{site}", timeout=60).status_code == 200


def resolve_anchor(river, state=None, list_only=False):
    """Turn a river name into (anchor site number, title). Exits with a message if ambiguous."""
    cands = find_candidates(river, state)
    if not cands:
        sys.exit(f"No USGS stream gages found with a name starting '{river}'"
                 + (f" in {state}" if state else "")
                 + ". Try the official spelling (e.g. 'Myakka River'), or pass --anchor.")
    states = sorted({c["state"] for c in cands})
    if list_only:
        for c in cands:
            area = f"{c['area']:>8.1f} sq mi" if c["area"] else f"{'n/a':>8s}       "
            print(f"{c['site']}  {area}  {c['state']:<14s} {c['name']}")
        sys.exit(0)
    if len(states) > 1:
        sys.exit(f"'{river}' matches gages in {len(states)} states: {', '.join(states)}.\n"
                 f"Re-run with --state (e.g. --state {next((k for k, v in STATES.items() if v == states[0]), states[0])}).")
    for c in cands:  # largest basin first: it sits at the downstream end, so the walk upstream
        if c["area"] and nldi_knows(c["site"]):  # covers the whole river
            print(f"Using {c['site']} {c['name'].title()} ({c['state']}) as the anchor", file=sys.stderr)
            return c["site"]
    sys.exit(f"Found gages for '{river}' but none are in the NLDI network; pass --anchor.")


def fetch_river(anchor, distance_km):
    """Return (flowline features, {site_id: (name, lat, lon)}) up and downstream of anchor.

    Only gages on the anchor's own mainstem are kept, so gages on a river that the
    downstream walk runs into (past the mouth/confluence) are not shown as part of this one.
    """
    base = f"{NLDI}/USGS-{anchor}/navigation"
    mainstem = get_json(f"{NLDI}/USGS-{anchor}", f="json")["features"][0]["properties"].get("mainstem")
    lines, sites, seen = [], {}, set()
    for mode in ("UM", "DM"):
        found = {}
        for f in get_json(f"{base}/{mode}/nwissite", distance=distance_km, f="json")["features"]:
            props = f["properties"]
            site = props["identifier"].removeprefix("USGS-")
            if len(site) != 8:  # skip 15-digit miscellaneous/project sites
                continue
            if mainstem and props.get("mainstem") not in (None, mainstem):
                continue
            lon, lat = f["geometry"]["coordinates"]
            found[site] = (props["name"], lat, lon)
        if mode == "DM" and not found:
            continue  # nothing downstream on this river: don't draw the other river's line
        sites.update(found)
        for f in get_json(f"{base}/{mode}/flowlines", distance=distance_km, f="json")["features"]:
            comid = f["properties"].get("nhdplus_comid")
            if comid not in seen:  # the anchor's flowline comes back from both walks
                seen.add(comid)
                lines.append(f)
    return lines, sites


def usable(value):
    """Parse an NWIS value; None for blanks and the -999999 style no-data sentinels."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v <= NO_DATA or not math.isfinite(v) else v


def fetch_current(site_ids):
    """Latest gage height per site; only sites actively reporting recent stage are returned."""
    out, cutoff = {}, datetime.now(timezone.utc) - STALE_AFTER
    for i in range(0, len(site_ids), MAX_SITES_PER_REQUEST):
        chunk = site_ids[i:i + MAX_SITES_PER_REQUEST]
        data = get_json(f"{NWIS}/iv/", format="json", sites=",".join(chunk),
                        parameterCd=GAGE_HEIGHT, siteStatus="active")
        for ts in data["value"]["timeSeries"]:
            vals = ts["values"][0]["value"]
            if not vals:
                continue
            v = vals[-1]
            stage = usable(v["value"])
            if stage is None or datetime.fromisoformat(v["dateTime"]) < cutoff:
                continue
            out[ts["sourceInfo"]["siteCode"][0]["value"]] = (stage, v["dateTime"])
    return out


def get_text(url, **params):
    r = session.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.text


def parse_rdb(text):
    """Rows of a USGS RDB (tab-separated) response as dicts. Comment lines and the column-type line
    that follows the header are skipped."""
    rows = [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]
    if len(rows) < 2:
        return []
    return [dict(zip(rows[0], row)) for row in rows[2:]]


def fetch_catalog(site_ids):
    """What each site has ever measured: {site: [data series rows]} from the USGS site catalog."""
    catalog = {}
    for i in range(0, len(site_ids), MAX_SITES_PER_REQUEST):
        text = get_text(SITE_SERVICE, format="rdb", sites=",".join(site_ids[i:i + MAX_SITES_PER_REQUEST]),
                        seriesCatalogOutput="true", siteStatus="all")
        for row in parse_rdb(text):
            catalog.setdefault(row["site_no"], []).append(row)
    return catalog


def classify_site(rows, now=None):
    """Say what a site that is not reporting stage is, from its series catalog rows.

    Returns (category, details). Categories: "discharge_only" (reporting flow but no stage),
    "offline" and "retired" (a stage or flow record that has ended, within or over a year ago),
    "field_visits", "water_quality", "other", and "no_data". Details (last and first data dates and
    what was measured) are given for offline and retired gages only.
    """
    now = now or datetime.now(timezone.utc)
    series = [r for r in rows if r.get("data_type_cd") in CONTINUOUS and r.get("end_date")]
    gage = [r for r in series if r.get("parm_cd") in STAGE_OR_FLOW]
    if gage:
        last = max(r["end_date"] for r in gage)
        first = min((r["begin_date"] for r in gage if r.get("begin_date")), default=last)
        if any(r["parm_cd"] == "00060" and r["end_date"] >= (now - STALE_AFTER).date().isoformat()
               for r in gage):
            return "discharge_only", {}
        status = "retired" if last < (now - RETIRED_AFTER).date().isoformat() else "offline"
        measured = sorted({PARAM_NAMES[r["parm_cd"]] for r in series if r.get("parm_cd") in PARAM_NAMES})
        return status, {"last": last, "first": first, "params": measured}
    kinds = {r.get("data_type_cd") for r in rows}
    if "sv" in kinds:
        return "field_visits", {}
    if "qw" in kinds:
        return "water_quality", {}
    return ("other" if rows else "no_data"), {}


def describe_sites(n_sites, n_reporting, counts, retired_count, draw_retired):
    """The one-line summary printed to stderr: where every site on the river went."""
    msg = f"{n_sites} sites on river: {n_reporting} reporting stage"
    if n_sites > n_reporting:
        msg += f"; {n_sites - n_reporting} not"
        if counts is None:
            msg += " (site details unavailable)"
        else:
            parts = [f"{counts[key]} {label}" for key, label in CATEGORY_LABELS if counts.get(key)]
            msg += f" ({', '.join(parts)})" if parts else ""
            if retired_count and draw_retired:
                msg += f"; drawing the {retired_count} that no longer report"
            elif retired_count:
                msg += f"; --retired draws the {retired_count} that no longer report"
    return msg


def fetch_history(site):
    """Past year of daily gage heights for a site (empty if unavailable).

    Uses the daily mean. Some sites publish only a daily max and min for stage (the response then
    lists max first, so taking the first series would silently compare against daily maxima); for
    those the midpoint of each day's max and min stands in for the mean.
    """
    try:
        data = get_json(f"{NWIS}/dv/", format="json", sites=site,
                        parameterCd=GAGE_HEIGHT, period="P365D")
        by_stat = {ts["name"].rsplit(":", 1)[-1]:
                   {v["dateTime"]: usable(v["value"]) for v in ts["values"][0]["value"]}
                   for ts in data["value"]["timeSeries"]}
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return []  # one gage's odd response must not stop the whole run
    mean = [v for v in by_stat.get(DAILY_MEAN, {}).values() if v is not None]
    if len(mean) >= MIN_HISTORY_DAYS:
        return mean
    hi, lo = by_stat.get(DAILY_MAX, {}), by_stat.get(DAILY_MIN, {})
    return [(hi[d] + lo[d]) / 2 for d in hi
            if d in lo and hi[d] is not None and lo[d] is not None]


def percentile(history, value):
    if len(history) < MIN_HISTORY_DAYS:
        return None
    return 100 * sum(h <= value for h in history) / len(history)


def fetch_recent(site):
    """Last RECENT_DAYS of instantaneous gage heights as [(epoch seconds, ft)] (empty if unavailable)."""
    try:
        data = get_json(f"{NWIS}/iv/", format="json", sites=site,
                        parameterCd=GAGE_HEIGHT, period=f"P{RECENT_DAYS}D")
        series = data["value"]["timeSeries"]
        if not series:
            return []
        pts = []
        for v in series[0]["values"][0]["value"]:
            val = usable(v["value"])
            if val is not None:
                pts.append((datetime.fromisoformat(v["dateTime"]).timestamp(), val))
        return sorted(pts)
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return []  # one gage's odd response must not stop the whole run


def slope_per_hour(pts):
    """Least-squares slope (ft/hr) of [(epoch seconds, ft)]; a fit resists single-reading sensor noise."""
    n = len(pts)
    if n < 2:
        return 0.0
    mt, mv = sum(t for t, _ in pts) / n, sum(v for _, v in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return 3600 * sum((t - mt) * (v - mv) for t, v in pts) / den if den else 0.0


def recent_stats(pts, now=None):
    """Summarize the recent record: range position, trend rate, 24 h change, peak, hourly series.

    Returns None when there are too few readings or they cover too short a span. The trend and
    24 h change are None when the last reading is older than TREND_MAX_AGE_HOURS, so a stale
    gage is never described as currently rising or falling. Other fields that can't be computed
    are None too.
    """
    if len(pts) < MIN_RECENT_POINTS or pts[-1][0] - pts[0][0] < MIN_RECENT_SPAN_HOURS * 3600:
        return None
    now = time.time() if now is None else now
    t_now, stage = pts[-1]
    fresh = now - t_now <= TREND_MAX_AGE_HOURS * 3600
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    peak_t, peak = max(pts, key=lambda p: (p[1], p[0]))  # latest reading at the maximum

    window = [p for p in pts if p[0] >= t_now - TREND_HOURS * 3600]
    rate = (slope_per_hour(window)
            if fresh and len(window) >= 4 and window[-1][0] - window[0][0] >= TREND_HOURS * 1800
            else None)

    day_ago = min(pts, key=lambda p: abs(p[0] - (t_now - 86400)))
    delta24 = (stage - day_ago[1]
               if fresh and abs(day_ago[0] - (t_now - 86400)) <= 3600 else None)

    hourly = {}  # last reading in each clock hour keeps the series small enough to embed
    for t, v in pts:
        hourly[int(t // 3600)] = [round(t * 1000), round(v, 2)]
    return {"low": lo, "high": hi, "peak": peak, "peak_time": round(peak_t * 1000),
            "range_pos": 100 * (stage - lo) / (hi - lo) if hi - lo >= MIN_RANGE_FT else None,
            "rate": rate, "delta24": delta24, "series": list(hourly.values())}


def fetch_nws(site, lat, lon, nws_gauges):
    """Find the NWS gauge co-located with this USGS site and return its flood stages."""
    best = min(nws_gauges, key=lambda g: haversine_km(lat, lon, g["latitude"], g["longitude"]),
               default=None)
    if best is None or haversine_km(lat, lon, best["latitude"], best["longitude"]) > MATCH_RADIUS_KM:
        return None
    try:
        detail = get_json(f"{NWPS}/{best['lid']}")
    except requests.RequestException:
        return None
    if detail.get("usgsId") != site:
        return None
    cats = (detail.get("flood") or {}).get("categories") or {}
    stages = {}
    for cat in FLOOD_CATEGORIES:
        stage = (cats.get(cat) or {}).get("stage")
        if isinstance(stage, (int, float)) and stage > NO_DATA:
            stages[cat] = stage
    if not stages:
        return None
    return {"lid": best["lid"], "stages": stages}


def fetch_forecast(lid, stages):
    """NWS stage forecast for a gauge: series plus the forecast crest. None if no usable forecast."""
    try:
        data = get_json(f"{NWPS}/{lid}/stageflow/forecast")
    except requests.RequestException:
        return None
    if data.get("primaryUnits") != "ft":  # thresholds are in feet; don't compare flow to them
        return None
    now = datetime.now(timezone.utc)
    pts = [(p["validTime"], p["primary"]) for p in data.get("data", [])
           if isinstance(p.get("primary"), (int, float)) and p["primary"] > NO_DATA
           and datetime.fromisoformat(p["validTime"].replace("Z", "+00:00")) >= now - timedelta(hours=6)]
    if not pts:
        return None
    crest_time, crest = max(pts, key=lambda p: p[1])  # crest of the remaining forecast only
    return {"issued": data.get("issuedTime"), "series": pts, "crest": crest,
            "crest_time": crest_time, "crest_category": flood_category(crest, stages)}


def flood_category(stage, stages):
    for cat in ("major", "moderate", "minor", "action"):
        if cat in stages and stage >= stages[cat]:
            return cat
    return "normal"


KEEP_UPPER = {"SR", "US", "SH", "FL", "I75", "WWTP", "NR", "NW", "NE", "SW", "SE"}


def pretty_name(name):
    """Title-case a USGS site name but keep road and state abbreviations uppercase."""
    words = [w if w.strip(",") in KEEP_UPPER else w.title() for w in name.split()]
    return " ".join(words)


def build_gages(anchor, distance_km, draw_retired=False):
    """Return (flowlines, gages, retired). `retired` lists the sites that no longer report stage but
    once had a stage or flow record; it is only filled in when draw_retired is set."""
    lines, sites = fetch_river(anchor, distance_km)
    current = fetch_current(list(sites))
    not_reporting = [s for s in sites if s not in current]
    counts, retired = None, []
    if not_reporting:
        try:
            catalog = fetch_catalog(not_reporting)
        except (requests.RequestException, KeyError, ValueError):
            catalog = None  # the summary line is nicer with it, but nothing depends on it
        if catalog is not None:
            counts = {}
            for site in not_reporting:
                category, info = classify_site(catalog.get(site, []))
                counts[category] = counts.get(category, 0) + 1
                if category in ("retired", "offline"):
                    name, lat, lon = sites[site]
                    retired.append({"site": site, "name": pretty_name(name), "lat": lat, "lon": lon,
                                    "status": category, **info})
    print(describe_sites(len(sites), len(current), counts, len(retired), draw_retired), file=sys.stderr)
    if not current:
        sys.exit("No gages on this river are reporting recent gage height (parameter 00065).")

    lats = [sites[s][1] for s in current]
    lons = [sites[s][2] for s in current]
    nws = get_json(NWPS, **{"bbox.xmin": min(lons) - 0.02, "bbox.ymin": min(lats) - 0.02,
                            "bbox.xmax": max(lons) + 0.02, "bbox.ymax": max(lats) + 0.02,
                            "srid": "EPSG_4326"})["gauges"]

    def build(site):
        name, lat, lon = sites[site]
        stage, when = current[site]
        gage = {"site": site, "name": pretty_name(name), "lat": lat,
                "lon": lon, "stage": stage, "time": when,
                "recent": recent_stats(fetch_recent(site))}
        flood = fetch_nws(site, lat, lon, nws)
        if flood:
            gage.update(method="nws", lid=flood["lid"], stages=flood["stages"],
                        category=flood_category(stage, flood["stages"]),
                        forecast=fetch_forecast(flood["lid"], flood["stages"]))
        else:
            pct = percentile(fetch_history(site), stage)
            gage.update(method="percentile", pct=pct)
        return gage

    with ThreadPoolExecutor(max_workers=8) as pool:
        gages = list(pool.map(build, current))
    retired = sorted(retired, key=lambda g: -g["lat"]) if draw_retired else []
    return lines, sorted(gages, key=lambda g: -g["lat"]), retired


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ stream gages</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
  html, body, #map { height: 100%; margin: 0; font-family: system-ui, sans-serif; }
  .panel { background: rgba(255,255,255,.95); padding: 8px 12px; border-radius: 6px;
           box-shadow: 0 1px 5px rgba(0,0,0,.4); font-size: 12px; line-height: 1.5; }
  .legend { max-height: 45vh; overflow-y: auto; }
  .legend h4 { white-space: nowrap; }
  .panel h4 { margin: 0 0 4px; font-size: 13px; }
  .sw { display: inline-block; width: 12px; height: 12px; margin-right: 6px;
        vertical-align: middle; border: 1px solid rgba(0,0,0,.5); }
  .sw.dia { transform: rotate(45deg) scale(.8); }
  .sw.round { border-radius: 50%; }
  .mkw { width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; }
  .mk { width: 18px; height: 18px; border: 2px solid #fff; box-shadow: 0 0 3px #000;
        display: flex; align-items: center; justify-content: center; }
  .mk.fc { box-shadow: 0 0 0 3px var(--fc), 0 0 3px 3px #000; }
  .mk.round { border-radius: 50%; }
  .mk.dia { transform: rotate(45deg); width: 16px; height: 16px; }
  .gl { color: #fff; font-size: 11px; line-height: 1; font-weight: bold; white-space: nowrap;
        text-shadow: 0 0 2px #000, 0 0 2px #000, 0 0 2px #000, 0 0 3px #000; }
  .mk.dia .gl { transform: rotate(-45deg); }  /* keep the glyph upright inside the rotated square */
  .gl.fast { font-size: 8px; line-height: 6px; }
  .gl.fast i { display: block; font-style: normal; }
  .rt { width: 10px; height: 10px; border: 2px solid #6b6b6b; border-radius: 50%;
        background: rgba(255,255,255,.7); }
  .sw.ret { width: 8px; height: 8px; border: 2px solid #6b6b6b; border-radius: 50%; background: #fff; }
  .modes label { display: block; cursor: pointer; }
  .cap { font-size: 10px; color: #666; }
  .pop table { border-collapse: collapse; }
  .pop td { padding: 1px 8px 1px 0; }
</style>
</head>
<body>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const RIVER = __RIVER__;
const GAGES = __GAGES__;
const RETIRED = __RETIRED__;
const GENERATED = "__GENERATED__";
const TITLE = __TITLE_JSON__;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

const NWS = {normal: ["#2e9e4f", "Normal"], action: ["#f2d424", "Action"],
             minor: ["#f28c1c", "Minor flood"], moderate: ["#d62828", "Moderate flood"],
             major: ["#8e2fb8", "Major flood"]};
const PCT = [[10, "#c9b79c", "Below 10th pct"], [25, "#9ecae1", "10th-25th"],
             [75, "#4a90c8", "25th-75th"], [90, "#1f5fa8", "75th-90th"],
             [101, "#0b2f6b", "Above 90th pct"]];
// position within the last 7 days' min..max: 100 = at the peak right now
const RANGE = [[10, "#c9b79c", "Near 7-day low (<10%)"], [30, "#9ecae1", "10-30%"],
               [70, "#4a90c8", "30-70%"], [90, "#1f5fa8", "70-90%"],
               [101, "#0b2f6b", "Near 7-day peak (90%+)"]];
// rate of change over the last 6 h in ft/hr; 0.02 ft/hr is about half a foot a day
const TREND = [[-0.10, "#2166ac", "Falling fast (< -0.10 ft/h)", "▼▼"],
               [-0.02, "#92c5de", "Falling", "▼"],
               [0.02, "#bdbdbd", "Steady (within 0.02 ft/h)", "▬"],
               [0.10, "#f4a582", "Rising", "▲"],
               [Infinity, "#b2182b", "Rising fast (> 0.10 ft/h)", "▲▲"]];
const MODES = {
  range: {label: "Position in 7-day range", legend: "No NWS stages: position in 7-day range", bins: RANGE},
  trend: {label: "Trend (last 6 h)", legend: "No NWS stages: trend over last 6 h", bins: TREND},
  pct:   {label: "Percentile of past year", legend: "No NWS stages: percentile of past year", bins: PCT},
};
const NODATA = "#999999";
let mode = "range";

const bin = (bins, v) => v == null ? null : bins.find(b => v < b[0]);
// inclusive on the "steady" side, so the boundaries match the legend labels
const trendBin = g => {
  const r = g.recent && g.recent.rate;
  if (r == null) return null;
  return TREND[r < -0.10 ? 0 : r < -0.02 ? 1 : r <= 0.02 ? 2 : r <= 0.10 ? 3 : 4];
};
// round once, so the number shown in a popup and the color bin it falls in always agree
const rounded = v => v == null ? null : Math.round(v);
function binFor(g) {
  return mode === "trend" ? trendBin(g)
       : bin(MODES[mode].bins, rounded(mode === "range" ? g.recent && g.recent.range_pos : g.pct));
}
function color(g) {
  if (g.method === "nws") return NWS[g.category][0];
  const b = binFor(g);
  return b ? b[1] : NODATA;
}

const map = L.map("map");
const base = {
  "Street (Esri)": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    {maxZoom: 18, attribution: "Esri, HERE, Garmin, OpenStreetMap contributors"}),
  "USGS Topo": L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
    {maxZoom: 16, attribution: "USGS The National Map"}),
  "Satellite (Esri)": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {maxZoom: 18, attribution: "Esri, Maxar, Earthstar Geographics"}),
};
base["Street (Esri)"].addTo(map);
const layersCtl = L.control.layers(base, null, {position: "topleft"}).addTo(map);

const river = L.geoJSON(RIVER, {style: {color: "#5b8db8", weight: 3, opacity: .8}}).addTo(map);
const bounds = river.getBounds().isValid() ? river.getBounds()
  : L.latLngBounds(GAGES.map(g => [g.lat, g.lon]));
bounds.isValid() ? map.fitBounds(bounds.pad(0.05)) : map.setView([39, -98], 4);

function spark(g) {
  if (!g.forecast) return "";
  const pts = g.forecast.series.map(([t, v]) => [new Date(t).getTime(), v]);
  const now = Date.now(), W = 260, H = 90, m = 4;
  const xs = pts.map(p => p[0]).concat(now), t0 = Math.min(...xs), t1 = Math.max(...xs);
  const thr = Object.entries(g.stages).filter(([, v]) => v < Math.max(...pts.map(p => p[1])) + 2);
  const ys = pts.map(p => p[1]).concat(g.stage, thr.map(([, v]) => v));
  const y0 = Math.min(...ys) - .5, y1 = Math.max(...ys) + .5;
  const X = t => m + (t - t0) / (t1 - t0 || 1) * (W - 2 * m), Y = v => H - m - (v - y0) / (y1 - y0) * (H - 2 * m);
  const line = [[now, g.stage]].concat(pts.filter(p => p[0] > now)).map(p => `${X(p[0])},${Y(p[1])}`).join(" ");
  const lines = thr.map(([c, v]) => `<line x1="0" x2="${W}" y1="${Y(v)}" y2="${Y(v)}" stroke="${NWS[c][0]}" stroke-dasharray="3 3"/>`).join("");
  return `<svg width="${W}" height="${H}" style="margin-top:4px;background:#fafafa;border:1px solid #ddd">${lines}
    <polyline points="${line}" fill="none" stroke="#222" stroke-width="2"/>
    <circle cx="${X(now)}" cy="${Y(g.stage)}" r="3" fill="#222"/></svg>
    <div style="font-size:10px;color:#666">NWS forecast (issued ${new Date(g.forecast.issued).toLocaleString()}); dashed = flood stages</div>`;
}

function recentSpark(g) {
  const r = g.recent;
  if (!r || r.series.length < 2) return "";
  const W = 260, H = 70, m = 4, pts = r.series;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0], pad = (r.high - r.low) * .1 || .5;
  const y0 = r.low - pad, y1 = r.high + pad;
  const X = t => m + (t - t0) / (t1 - t0 || 1) * (W - 2 * m), Y = v => H - m - (v - y0) / (y1 - y0) * (H - 2 * m);
  const line = pts.map(p => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
  return `<svg width="${W}" height="${H}" style="margin-top:4px;background:#fafafa;border:1px solid #ddd">
    <line x1="0" x2="${W}" y1="${Y(r.high)}" y2="${Y(r.high)}" stroke="#bbb" stroke-dasharray="3 3"/>
    <line x1="0" x2="${W}" y1="${Y(r.low)}" y2="${Y(r.low)}" stroke="#bbb" stroke-dasharray="3 3"/>
    <polyline points="${line}" fill="none" stroke="#222" stroke-width="1.5"/>
    <circle cx="${X(t1)}" cy="${Y(pts[pts.length - 1][1])}" r="3" fill="#222"/></svg>
    <div class="cap">Last 7 days, hourly; dashed = 7-day low and high</div>`;
}

const signed = (v, d = 2) => { const r = Number(v.toFixed(d)); return (r >= 0 ? "+" : "-") + Math.abs(r).toFixed(d); };
const ordinal = n => { const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
function since(dateStr, refMs = Date.now()) {
  const days = Math.floor((refMs - new Date(dateStr).getTime()) / 864e5);
  return days < 1 ? "today" : days < 60 ? `${days} day${days === 1 ? "" : "s"} ago`
       : days < 731 ? `${Math.round(days / 30.44)} months ago` : `${Math.floor(days / 365.25)} years ago`;
}
function ago(ms, refMs) {
  const h = (refMs - ms) / 36e5;
  return h < 1 / 60 ? "at latest reading" : h < 1 ? `${Math.round(h * 60)} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${(h / 24).toFixed(1)} days ago`;
}

function recentRows(g) {
  const r = g.recent;
  if (!r) return `<tr><td>Recent trend</td><td>n/a (too few readings)</td></tr>`;
  const ref = new Date(g.time).getTime(), tb = trendBin(g);
  let rows = "";
  if (tb) rows += `<tr><td>Trend (6 h)</td><td><b>${tb[3] ? tb[3] + " " : ""}${tb[2].split(" (")[0]}</b> ${signed(r.rate, 3)} ft/h</td></tr>`;
  if (r.delta24 != null) rows += `<tr><td>Change (24 h)</td><td>${signed(r.delta24)} ft</td></tr>`;
  rows += `<tr><td>7-day range</td><td>${r.low.toFixed(2)} to ${r.high.toFixed(2)} ft` +
          (r.range_pos == null ? " (flat)" : ` (now at ${Math.round(r.range_pos)}%)`) + `</td></tr>`;
  rows += `<tr><td>7-day peak</td><td>${r.peak.toFixed(2)} ft, ${ago(r.peak_time, ref)}</td></tr>`;
  return rows;
}

function popup(g) {
  const t = new Date(g.time).toLocaleString();
  let rows = `<tr><td>Stage</td><td><b>${g.stage.toFixed(2)} ft</b></td></tr>
              <tr><td>Reported</td><td>${t}</td></tr>` + recentRows(g);
  if (g.method === "nws") {
    rows += `<tr><td>NWS category</td><td><b>${NWS[g.category][1]}</b></td></tr>`;
    for (const c of ["action", "minor", "moderate", "major"])
      if (g.stages[c] != null) rows += `<tr><td>${NWS[c][1]} stage</td><td>${g.stages[c]} ft</td></tr>`;
    if (g.forecast) {
      const f = g.forecast, ct = new Date(f.crest_time).toLocaleString([], {weekday: "short", hour: "numeric"});
      rows += `<tr><td>Forecast crest</td><td><b>${f.crest.toFixed(1)} ft</b> (${NWS[f.crest_category][1]}) ${ct}</td></tr>`;
    } else rows += `<tr><td>Forecast</td><td>none issued</td></tr>`;
    rows += `<tr><td>NWS gauge</td><td><a href="https://water.noaa.gov/gauges/${esc(g.lid)}" target="_blank">${esc(g.lid)}</a></td></tr>`;
  } else {
    rows += `<tr><td>Past year</td><td>${g.pct == null ? "n/a (short record)" : ordinal(Math.round(g.pct)) + " percentile"}</td></tr>`;
  }
  return `<div class="pop"><b>${esc(g.name)}</b><br>USGS ${esc(g.site)}<table>${rows}</table>${recentSpark(g)}${spark(g)}
    <a href="https://waterdata.usgs.gov/monitoring-location/USGS-${esc(g.site)}/" target="_blank">USGS page</a></div>`;
}

function icon(g) {
  const shape = g.method === "nws" ? "round" : "dia", tb = trendBin(g);
  const chars = tb ? [...tb[3]] : [];  // doubled glyphs (fast) are stacked vertically
  const glyph = !chars.length ? "" : chars.length > 1
    ? `<span class="gl fast">${chars.map(c => `<i>${c}</i>`).join("")}</span>`
    : `<span class="gl">${chars[0]}</span>`;
  return L.divIcon({className: "", iconSize: [24, 24],
    html: `<div class="mkw"><div class="mk ${shape}${g.forecast ? " fc" : ""}" style="background:${color(g)};` +
          `--fc:${g.forecast ? NWS[g.forecast.crest_category][0] : "#fff"}">${glyph}</div></div>`});
}

function retiredPopup(g) {
  const status = g.status === "retired" ? "Retired" : "Offline (not reporting stage now)";
  let rows = `<tr><td>Status</td><td><b>${status}</b></td></tr>
              <tr><td>Last data</td><td>${esc(g.last)} (${since(g.last)})</td></tr>
              <tr><td>Record</td><td>${esc(g.first.slice(0, 4))} to ${esc(g.last.slice(0, 4))}</td></tr>`;
  if (g.params.length) rows += `<tr><td>Measured</td><td>${esc(g.params.join(", "))}</td></tr>`;
  return `<div class="pop"><b>${esc(g.name)}</b><br>USGS ${esc(g.site)}<table>${rows}</table>
    <a href="https://waterdata.usgs.gov/monitoring-location/USGS-${esc(g.site)}/" target="_blank">USGS page</a></div>`;
}

if (RETIRED.length) {
  const retiredLayer = L.layerGroup(RETIRED.map(g =>
    L.marker([g.lat, g.lon], {icon: L.divIcon({className: "", iconSize: [14, 14], html: '<div class="rt"></div>'}),
                              title: g.name, zIndexOffset: -1000}).bindPopup(retiredPopup(g)))).addTo(map);
  layersCtl.addOverlay(retiredLayer, `Retired or offline (${RETIRED.length})`);
}

const markers = GAGES.map(g =>
  L.marker([g.lat, g.lon], {icon: icon(g), title: g.name}).bindPopup(popup(g)).addTo(map));

const legend = L.control({position: "bottomright"});
function drawLegend() {
  let h = "<h4>NWS flood category</h4>";
  for (const [c, n] of Object.values(NWS)) h += `<div><span class="sw round" style="background:${c}"></span>${n}</div>`;
  h += "<div style='margin-top:4px'>Ring = NWS forecast crest category</div>";
  h += `<h4 style='margin-top:6px'>${MODES[mode].legend}</h4>`;
  for (const [, c, n] of MODES[mode].bins) h += `<div><span class="sw dia" style="background:${c}"></span>${n}</div>`;
  h += `<div><span class="sw dia" style="background:${NODATA}"></span>No data</div>`;
  h += "<div style='margin-top:4px'>▲/▼ inside a gage = rising/falling over the last 6 h (double = fast)<br>▬ = steady, none = no trend data</div>";
  if (RETIRED.length) h += '<div style="margin-top:4px"><span class="sw ret"></span>No longer reporting (retired or offline)</div>';
  legend.getContainer().innerHTML = h;
}
legend.onAdd = () => L.DomUtil.create("div", "panel legend");
legend.addTo(map);
drawLegend();

const modeCtl = L.control({position: "topleft"});
modeCtl.onAdd = () => {
  const d = L.DomUtil.create("div", "panel modes");
  d.innerHTML = "<h4>Color diamonds by</h4>" + Object.entries(MODES).map(([k, m]) =>
    `<label><input type="radio" name="mode" value="${k}"${k === mode ? " checked" : ""}> ${m.label}</label>`).join("");
  L.DomEvent.disableClickPropagation(d);
  d.addEventListener("change", e => {
    mode = e.target.value;
    GAGES.forEach((g, i) => { if (g.method !== "nws") markers[i].setIcon(icon(g)); });
    drawLegend();
  });
  return d;
};
modeCtl.addTo(map);

const title = L.control({position: "topright"});
title.onAdd = () => {
  const d = L.DomUtil.create("div", "panel");
  d.innerHTML = `<h4>${esc(TITLE)} stream gages</h4>${GAGES.length} gage${GAGES.length === 1 ? "" : "s"}${RETIRED.length ? ` + ${RETIRED.length} no longer reporting` : ""} &middot; generated ${GENERATED}`;
  return d;
};
title.addTo(map);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("river", nargs="?", help="river name as USGS spells it, e.g. 'Myakka River'")
    ap.add_argument("--state", help="state abbreviation or name, to disambiguate shared river names")
    ap.add_argument("--anchor", help="USGS site number on the river (skips the name lookup)")
    ap.add_argument("--list", action="store_true", help="list candidate gages for the name and exit")
    ap.add_argument("--distance", type=float, default=200,
                    help="miles to navigate each direction (default: %(default)g)")
    ap.add_argument("--retired", action="store_true",
                    help="also draw gages that no longer report stage (retired or offline) as faint markers")
    ap.add_argument("--out", default=None, help="output HTML path")
    args = ap.parse_args()
    if not args.river and not args.anchor:
        ap.error("give a river name or --anchor")

    if args.list and not args.river:
        ap.error("--list needs a river name")
    # --list always shows candidates (and exits), even if --anchor was also given
    anchor = args.anchor if args.anchor and not args.list else resolve_anchor(
        args.river, args.state, args.list)
    title = args.river or f"USGS {anchor}"
    lines, gages, retired = build_gages(anchor, round(args.distance * KM_PER_MILE), args.retired)
    out = args.out or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") + "_gages.html"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def safe_json(obj):  # keep "</script>" in a site name from ending the script block
        return json.dumps(obj).replace("<", "\\u003c")

    values = {"TITLE": html.escape(title), "TITLE_JSON": safe_json(title),
              "RIVER": safe_json({"type": "FeatureCollection", "features": lines}),
              "GAGES": safe_json(gages), "RETIRED": safe_json(retired), "GENERATED": generated}
    # one pass, so text inside a substituted value can never be mistaken for a placeholder
    page = re.sub(r"__(TITLE_JSON|TITLE|RIVER|GAGES|RETIRED|GENERATED)__", lambda m: values[m.group(1)], HTML)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out}", file=sys.stderr)
    for g in gages:
        fc = g.get("forecast")
        info = ((f"{g['category']:8s}" + (f" -> crest {fc['crest']} ft {fc['crest_category']} {fc['crest_time']}" if fc else ""))
                if g["method"] == "nws"
                else f"p{g['pct']:.0f}" if g["pct"] is not None else "n/a")
        r = g["recent"]
        trend = f"{r['rate']:+.3f} ft/h" if r and r["rate"] is not None else "n/a"
        print(f"{g['site']}  {g['stage']:7.2f} ft  {info:9s} {trend:>11s}  {g['name']}")
    if retired:
        print(f"\nNo longer reporting stage ({len(retired)}):")
        for r in retired:
            print(f"{r['site']}  last data {r['last']}  {r['status']:<8s} {r['name']}")


if __name__ == "__main__":
    main()
