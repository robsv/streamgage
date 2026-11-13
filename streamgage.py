#!/usr/bin/env python3
"""Draw an interactive map of a river with USGS gages colored by current stage.

Data sources (all public; no API key needed, though one raises a USGS rate limit):
  * USGS NLDI           - river flowlines and gages along the main stem
  * USGS Water Data API - the last 7 days of gage height (00065) readings, 365 days of daily values,
                          gage time zones, and what each site that is not reporting has measured
  * USGS Water Services - daily statistics
  * NOAA NWPS           - NWS flood categories (action/minor/moderate/major)

Gages with NWS flood stages are colored by flood category (circles). Gages
without them (diamonds) can be recolored in the page by position within the
last 7 days' range (default), by 6-hour trend, or by the percentile of the
current stage within the past year of daily values. Every gage gets a
rising/falling arrow and a 7-day sparkline.

Examples:
    python streamgage.py "Peace River"
    python streamgage.py "Peace River" --as-of 2026-11-03T12:00-05:00   # the river at a past moment
    python streamgage.py "Myakka River" --state FL
    python streamgage.py "Peace River" --list         # show candidate gages and exit
    python streamgage.py "Peace River" --anchor 02296750   # skip the name lookup
    python streamgage.py "Shenango River" "Mahoning River" "Beaver River" --state PA   # several rivers
                                                      # at once, with a marker where they meet
"""

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import combinations

import requests

NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite"
NWIS = "https://waterservices.usgs.gov/nwis"
STAT_SERVICE = f"{NWIS}/stat/"  # daily statistics over every year of record
NWPS = "https://api.water.noaa.gov/nwps/v1/gauges"
GAGE_HEIGHT = "00065"
DAILY_MAX, DAILY_MIN, DAILY_MEAN = "00001", "00002", "00003"  # NWIS statistic codes
MIN_HISTORY_DAYS = 30  # fewer daily values than this and there is no yearly percentile
MAX_STAT_SITES = 10  # the statistics service rejects requests for more sites than this
MIN_STAT_YEARS = 10  # fewer years of record than this and there is no long-term percentile
# the statistics columns that give the distribution of one calendar day across all years of
# record, and the percentile each one is (the minimum is the 0th and the maximum the 100th)
STAT_COLUMNS = (("min_va", 0), ("p05_va", 5), ("p10_va", 10), ("p20_va", 20), ("p25_va", 25),
                ("p50_va", 50), ("p75_va", 75), ("p80_va", 80), ("p90_va", 90), ("p95_va", 95),
                ("max_va", 100))
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
WATERDATA = "https://api.waterdata.usgs.gov/"  # the USGS Water Data API, the only service a key is sent to
API_KEY_VARIABLE = "WATERDATA_API_KEY"  # an optional free key for it, which raises its hourly limit
OGCAPI = f"{WATERDATA}ogcapi/v1/collections"
MONITORING = f"{OGCAPI}/monitoring-locations/items"
CONTINUOUS_ITEMS = f"{OGCAPI}/continuous/items"  # readings from the gages' sensors, every 15 minutes at most gages
TIME_SERIES = f"{OGCAPI}/time-series-metadata/items"  # what each series is, such as a gage's primary or backup sensor
DAILY_ITEMS = f"{OGCAPI}/daily/items"  # one value per day: the mean, maximum and minimum
FIELD_MEASUREMENTS = f"{OGCAPI}/field-measurements/items"  # what staff measured on visits to a site
SAMPLE_SITES = f"{WATERDATA}samples-data/monitoring-locations"  # the sites with water-quality samples
READINGS_SITES_PER_REQUEST = 100  # most sites on a river have no readings; a batch that overflows a page is split
HISTORY_SITES_PER_REQUEST = 40  # a year of daily mean, maximum and minimum for this many sites fits in a page
PAGE_ROWS = 50000  # the most rows the API returns at once; a full page may have been cut short
# hours from UTC of the standard time in each time zone USGS names for a gage
ZONE_OFFSETS = {"AST": -4, "EST": -5, "CST": -6, "MST": -7, "PST": -8, "AKST": -9, "HST": -10}
NO_DATA = -999  # NWIS/NWPS report missing values as -999, -9999 or -999999
MAX_SITES_PER_REQUEST = 100  # NWIS limit
STALE_AFTER = timedelta(days=3)  # ignore gages whose latest stage reading is older than this
FLOOD_CATEGORIES = ("action", "minor", "moderate", "major")  # the ones the page knows how to color
MATCH_RADIUS_KM = 0.5  # max distance between a USGS site and its NWS gauge
CONFLUENCE_MERGE_KM = 0.5  # confluence points closer than this are one meeting of rivers
KM_PER_MILE = 1.609344  # distances are shown in miles; --distance is in miles and the NLDI API takes whole kilometers
MAX_NAMES_IN_FILE = 3  # more rivers than this and the file name gives the first few, "plus N" and a hash
RECENT_DAYS = 7  # window for range position, peak and the recent sparkline
TREND_HOURS = 6  # window for the rate-of-change fit
MIN_RECENT_POINTS = 12  # fewer instantaneous readings than this and the recent stats are skipped
MIN_RANGE_FT = 0.1  # a 7-day range narrower than this is sensor noise, so no range position
MIN_RECENT_SPAN_HOURS = 24  # a record shorter than this (new or returning gage) has no meaningful range
TREND_MAX_AGE_HOURS = 3  # a last reading older than this is not "current", so no trend or 24 h change

session = requests.Session()
session.headers["User-Agent"] = "streamgage-map/1.0"


RETRY_STATUSES = (429, 502, 503, 504)  # rate limited, or briefly unavailable (a plain 500 is not retried)
RETRY_DELAYS = (2, 5, 10, 20, 30)  # seconds to wait before each retry; the last one is the final try
MAX_RETRY_AFTER = 60  # a server asking for longer than this is not waited for: the request fails at once


nws_state = {"down": False}  # set once the NWS has refused us after every retry, so later rivers skip it


def get(url, params):
    """GET with retries. A rate-limited (429) or briefly failing service gets a few more tries, waiting
    as long as it asks (Retry-After) or the next backoff step; any other error is raised at once.

    A service that asks for more than MAX_RETRY_AFTER seconds (the USGS does, once its hourly limit is
    used up: about 12 minutes) is not retried, since the retries would all fail. The error says how long
    to wait instead."""
    headers = api_headers(url)
    for delay in (*RETRY_DELAYS, None):
        r = session.get(url, params=params, timeout=60, headers=headers)
        if r.status_code == 403 and headers:
            raise requests.HTTPError(f"403 from {url.split('?')[0]}: the USGS refused the key in {API_KEY_VARIABLE}; "
                                     "check it, or unset the variable to run without one", response=r)
        if r.status_code not in RETRY_STATUSES or delay is None:
            r.raise_for_status()
            return r
        try:
            asked = float(r.headers.get("Retry-After", ""))
        except ValueError:
            asked = 0
        if asked > MAX_RETRY_AFTER:
            raise requests.HTTPError(
                f"{r.status_code} from {url.split('?')[0]}: the service asks to wait {asked:g} s "
                f"(about {asked / 60:.0f} minutes) before trying again", response=r)
        wait = max(delay, asked)
        print(f"  {r.status_code} from {url.split('?')[0]}; retrying in {wait:g} s", file=sys.stderr)
        time.sleep(wait)


def api_headers(url):
    """The X-Api-Key header for a request to the USGS Water Data API when WATERDATA_API_KEY is set, and
    None otherwise. The key is sent to no other service, and it travels in a header rather than the URL
    so that error messages, which quote the URL, never show it."""
    key = os.environ.get(API_KEY_VARIABLE, "").strip()
    return {"X-Api-Key": key} if key and url.startswith(WATERDATA) else None


def is_refusal(err):
    """True for an HTTP error that means the service is too busy or briefly down (the kind get() retries),
    as opposed to a bad request, a missing page, or a connection that never got an answer."""
    response = getattr(err, "response", None)
    return isinstance(err, requests.HTTPError) and response is not None and response.status_code in RETRY_STATUSES


def get_json(url, **params):
    return get(url, params).json()


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
    """Stream gages whose USGS name starts with the river name, as dicts, largest basin first.

    The name filter is case-sensitive and USGS is inconsistent (Florida names are upper case,
    Pennsylvania's are mixed), so the one query asks for the name as typed, in title case and in
    upper case, joined with OR. (Three queries, or two, used up the API's hourly limit quickly.)
    """
    river = river.strip()
    likes = [f"monitoring_location_name LIKE '{name.replace(chr(39), chr(39) * 2)} %'"
             for name in dict.fromkeys([river, river.title(), river.upper()])]
    params = {"f": "json", "limit": 1000, "site_type_code": "ST",
              "filter": " OR ".join(likes), "filter-lang": "cql-text"}
    if state:
        params["state_name"] = STATES.get(state.upper(), state.title())
    out = []
    for f in get_json(MONITORING, **params)["features"]:
        p = f["properties"]
        site = p["monitoring_location_number"]
        if len(site) != 8:  # skip miscellaneous/project sites
            continue
        out.append({"site": site, "name": p["monitoring_location_name"],
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


def fetch_river(anchor, distance_km, paths=None, comids=None):
    """Return (flowline features, {site_id: (name, lat, lon)}) up and downstream of anchor.

    Only gages on the anchor's own mainstem are kept, so gages on a river that the
    downstream walk runs into (past the mouth/confluence) are not shown as part of this one.

    If `paths` is a dict, every flowline the walks return is also recorded in it, by comid, including
    the stretch past the mouth that is not drawn. That is what find_confluences needs, and it comes for
    free here, so it never has to be downloaded a second time. If `comids` is a dict, the flowline each
    gage sits on is recorded in it ({site: comid}), to tell the gages above a confluence from those below.
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
            if comids is not None:
                comids[site] = props.get("comid")
        draw = not (mode == "DM" and not found)  # nothing downstream on this river: don't draw the other river's line
        if not draw and paths is None:
            continue
        sites.update(found)
        for f in get_json(f"{base}/{mode}/flowlines", distance=distance_km, f="json")["features"]:
            comid = f["properties"].get("nhdplus_comid")
            if paths is not None:
                paths.setdefault(comid, f)
            if draw and comid not in seen:  # the anchor's flowline comes back from both walks
                seen.add(comid)
                lines.append(f)
    return lines, sites


def _ends(feature):
    """The (first, last) vertex of a flowline, rounded so that segments sharing a node compare equal.
    NLDI draws each flowline in the direction the water flows."""
    geom = feature["geometry"]
    coords = geom["coordinates"]
    if geom["type"] == "MultiLineString":
        coords = [pt for part in coords for pt in part]
    return tuple(tuple(round(v, 6) for v in pt[:2]) for pt in (coords[0], coords[-1]))


def downstream_of(features, node):
    """The comids of the flowlines in `features` ({comid: flowline}) that run on down from `node`."""
    starts = {}
    for comid, f in features.items():
        starts.setdefault(_ends(f)[0], []).append(comid)
    below, todo = set(), [node]
    while todo:
        for comid in starts.get(todo.pop(), ()):
            if comid not in below:
                below.add(comid)
                todo.append(_ends(features[comid])[1])
    return below


def fetch_drainage_areas(sites):
    """{site: drainage area in square miles} for the sites that have one, in a single request."""
    quoted = ",".join(f"'{site}'" for site in sites)
    feats = get_json(MONITORING, f="json", limit=len(sites) + 1, filter=f"monitoring_location_number IN ({quoted})",
                     **{"filter-lang": "cql-text"})["features"]
    return {f["properties"]["monitoring_location_number"]: f["properties"]["drainage_area"]
            for f in feats if f["properties"].get("drainage_area")}


def find_confluences(paths, areas=None, detail=False):
    """Where the rivers in `paths` ({name: {comid: flowline}}) meet, as [{"lat", "lon", "rivers"}].

    Two rivers meet where their channels start to coincide: the upstream end of the stretch they
    share, if each river arrives there on a segment of its own. Requiring both arrivals rules out the
    far end of a walk, where one river's path merely stops while the other's carries on.
    Meetings within CONFLUENCE_MERGE_KM of each other are one confluence, so three rivers that
    join at one place give a single marker naming all three.

    A river's path can run through a meeting it is no part of: Joshua Creek's walk goes down the Peace
    past where Horse Creek joins. Such a river arrives only on segments another river also arrives on,
    and is the smaller of the two, and it has already met that river at a confluence of its own, so it
    is dropped from this one. "Smaller" is by `areas` ({name: drainage area at the anchor}) when both
    rivers have one, since a river's path is as long as its walk happened to be (the Susquehanna
    anchored far downstream walks less far up than a tributary anchored nearer its mouth), and by the
    number of flowlines walked otherwise. (Rivers NLDI files as one channel, like the Mahoning and the
    Beaver, never meet each other, so they are all kept.)

    With `detail`, each confluence also has "below": the comids of the stretch that runs on down from
    it, which is how a gage below a meeting is told from one on a river's own side of it.
    """
    found = []  # [lat, lon, [river names], {node keys}, {comids below}]
    for a, b in combinations(paths, 2):
        pa, pb = paths[a], paths[b]
        shared = pa.keys() & pb.keys()
        if not shared:
            continue
        ends = [_ends(pa[c]) for c in shared]
        shared_ends = {last for _, last in ends}
        arrivals_a = {_ends(f)[1] for c, f in pa.items() if c not in shared}
        arrivals_b = {_ends(f)[1] for c, f in pb.items() if c not in shared}
        for first, _ in ends:
            if first in shared_ends or first not in arrivals_a or first not in arrivals_b:
                continue
            lon, lat = first
            below = downstream_of({c: pa[c] for c in shared}, first)
            for hit in found:
                if haversine_km(lat, lon, hit[0], hit[1]) <= CONFLUENCE_MERGE_KM:
                    hit[2].extend(n for n in (a, b) if n not in hit[2])
                    hit[3].add(first)
                    hit[4] |= below
                    break
            else:
                found.append([lat, lon, [a, b], {first}, set(below)])
    areas = areas or {}

    def smaller(n, o):
        if areas.get(n) and areas.get(o):
            return areas[n] < areas[o]
        return len(paths[n]) < len(paths[o])

    out = []
    for i, (lat, lon, names, nodes, below) in sorted(enumerate(found), key=lambda e: -e[1][0]):
        arrives = {n: {c for c, f in paths[n].items() if _ends(f)[1] in nodes} for n in names}
        # the pairs that also meet at some other confluence: those rivers have already joined
        elsewhere = {frozenset(pair) for j, other in enumerate(found) if j != i
                     for pair in combinations(other[2], 2)}
        keep = [n for n in names
                if not any(arrives[n] <= arrives[o] and smaller(n, o)
                           and frozenset((n, o)) in elsewhere for o in names if o != n)]
        if len(keep) > 1:  # defensive: pruning should never leave a lone river, but that is no meeting
            out.append({"lat": lat, "lon": lon, "rivers": sorted(keep, key=list(paths).index)})
            if detail:
                out[-1]["below"] = sorted(below)
    return out


def usable(value):
    """Parse an NWIS value; None for blanks and the -999999 style no-data sentinels."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if v <= NO_DATA or not math.isfinite(v) else v


def fetch_csv(url, site_ids, per_request, properties, **params):
    """The rows of a CSV query to the USGS Water Data API about many sites, as dicts.

    Sites go `per_request` to a request, and the requests run in parallel. The CSV response gives no
    sign that rows were cut off, so a batch that fills a whole page (PAGE_ROWS) is split in two and
    asked again. A failed request is raised.
    """
    def fetch(batch):
        text = get_text(url, f="csv", skipGeometry="true", limit=PAGE_ROWS, properties=properties,
                        monitoring_location_id=",".join(f"USGS-{site}" for site in batch), **params)
        rows = list(csv.DictReader(io.StringIO(text)))
        if len(rows) >= PAGE_ROWS and len(batch) > 1:
            half = len(batch) // 2
            return fetch(batch[:half]) + fetch(batch[half:])
        return rows

    batches = [site_ids[i:i + per_request] for i in range(0, len(site_ids), per_request)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [row for rows in pool.map(fetch, batches) for row in rows]


def site_of(row):
    """The site number of a row from the USGS Water Data API, whose ids look like USGS-02296750."""
    return row["monitoring_location_id"].removeprefix("USGS-")


def fetch_readings(site_ids, as_of=None):
    """The last RECENT_DAYS of gage-height readings per site, as {site: [(epoch seconds, ft)]}, oldest
    first. The days end at `as_of` (an aware datetime) when given, and now otherwise. Sites with no
    usable reading in those days are left out.

    Sites go READINGS_SITES_PER_REQUEST to a request (see fetch_csv). A site with more than one
    gage-height series (a backup sensor, say) keeps the one series_rank puts first. A failed request
    is raised: without readings there is no map to draw.
    """
    end = as_of or datetime.now(timezone.utc)
    window = (f"{(end - timedelta(days=RECENT_DAYS)).isoformat(timespec='minutes')}/"
              f"{end.isoformat(timespec='minutes')}")
    rows = fetch_csv(CONTINUOUS_ITEMS, site_ids, READINGS_SITES_PER_REQUEST,
                     "monitoring_location_id,time_series_id,time,value", parameter_code=GAGE_HEIGHT, datetime=window)
    series = {}  # (site, series id) -> [(epoch seconds, ft)]
    for row in rows:
        stage = usable(row.get("value"))
        try:
            when = datetime.fromisoformat(row["time"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if stage is not None:
            series.setdefault((site_of(row), row.get("time_series_id")), []).append((when, stage))
    for pts in series.values():
        pts.sort()
    by_site = {}
    for site, ts in series:
        by_site.setdefault(site, []).append(ts)
    shared = sorted(site for site, ids in by_site.items() if len(ids) > 1)
    kinds = fetch_series_kinds(shared) if shared else {}
    out = {}
    for site, ids in by_site.items():
        best = max(ids, key=lambda ts, site=site: series_rank(series[site, ts], kinds.get(ts)))
        out[site] = series[site, best]
    return out


def fetch_series_kinds(site_ids):
    """{series id: (primary, description)} for the gage-height series at these sites, from the USGS
    series catalog. A failed request gives {}, and series_rank then goes by the readings alone."""
    kinds = {}
    try:
        for i in range(0, len(site_ids), MAX_SITES_PER_REQUEST):
            batch = site_ids[i:i + MAX_SITES_PER_REQUEST]
            for f in get_json(TIME_SERIES, f="json", skipGeometry="true", limit=1000, parameter_code=GAGE_HEIGHT,
                              monitoring_location_id=",".join(f"USGS-{site}" for site in batch),
                              properties="id,primary,web_description")["features"]:
                p = f["properties"]
                kinds[p["id"]] = (p.get("primary") == "Primary", p.get("web_description"))
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {}
    return kinds


def series_rank(pts, kind=None):
    """How to choose among one site's gage-height series: the primary one first, then one with no
    description (a backup or auxiliary sensor is labeled as one), then the newest and longest record."""
    primary, description = kind or (False, "unknown")
    return primary, not description, pts[-1][0], len(pts)


def latest_readings(readings, as_of=None):
    """{site: (stage, epoch seconds)} of the newest reading at each site whose newest reading is no
    older than STALE_AFTER, judged against `as_of` when given and now otherwise."""
    cutoff = ((as_of or datetime.now(timezone.utc)) - STALE_AFTER).timestamp()
    return {site: (pts[-1][1], pts[-1][0]) for site, pts in readings.items() if pts and pts[-1][0] >= cutoff}


def fetch_time_zones(site_ids):
    """{site: (zone abbreviation, uses daylight saving time)} from the USGS location records, such as
    ("EST", True). A failed request gives {}, and those gages' times are then given in UTC."""
    zones = {}
    try:
        for i in range(0, len(site_ids), MAX_SITES_PER_REQUEST):
            batch = site_ids[i:i + MAX_SITES_PER_REQUEST]
            quoted = ",".join(f"'{site}'" for site in batch)
            for f in get_json(MONITORING, f="json", skipGeometry="true", limit=len(batch) + 1,
                              filter=f"monitoring_location_number IN ({quoted})", **{"filter-lang": "cql-text"},
                              properties="monitoring_location_number,time_zone_abbreviation,uses_daylight_savings",
                              )["features"]:
                p = f["properties"]
                zones[p["monitoring_location_number"]] = (p.get("time_zone_abbreviation"),
                                                          p.get("uses_daylight_savings") == "Y")
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {}  # the times are still right, only shown in UTC
    return zones


def nth_sunday(year, month, n, tz):
    """Midnight at the start of the nth Sunday of a month, in time zone `tz`."""
    first = datetime(year, month, 1, tzinfo=tz)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


def local_time(epoch, zone=None):
    """An instant (epoch seconds) as an ISO 8601 time on the gage's own clock, such as
    2026-11-05T12:30:00-05:00, from its time zone (see fetch_time_zones). The calendar day of a reading
    is read from this. A zone this tool doesn't know, or none, gives the time in UTC.

    Daylight saving time follows the US rules in force since 2007: from 2 a.m. on the second Sunday in
    March to 2 a.m. on the first Sunday in November. The API's readings start in October 2007.
    """
    utc = datetime.fromtimestamp(epoch, timezone.utc)
    abbreviation, daylight = zone or (None, False)
    if abbreviation not in ZONE_OFFSETS:
        return utc.isoformat()
    offset = timedelta(hours=ZONE_OFFSETS[abbreviation])
    standard = utc.astimezone(timezone(offset))
    if daylight:
        # both changes happen at 2 a.m. on the clock: 2 a.m. standard time in March, and 2 a.m.
        # daylight time, which is 1 a.m. standard time, in November
        starts = nth_sunday(standard.year, 3, 2, standard.tzinfo) + timedelta(hours=2)
        ends = nth_sunday(standard.year, 11, 1, standard.tzinfo) + timedelta(hours=1)
        if starts <= standard < ends:
            return utc.astimezone(timezone(offset + timedelta(hours=1))).isoformat()
    return standard.isoformat()


def parse_as_of(text, now=None):
    """Read --as-of: an ISO 8601 time that carries its UTC offset, such as 2026-11-03T12:00-05:00 or
    2026-11-03T17:00Z. The offset is required because the tool has no timezone database and a bare
    clock time would be a guess. Raises ValueError, with a message fit to show, for anything else,
    including a time that has not happened yet."""
    raw = text.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"cannot read {text!r} as a time; use something like 2026-11-03T12:00-05:00 "
                         "or 2026-11-03T17:00Z") from None
    if when.tzinfo is None:
        raise ValueError(f"{text!r} has no UTC offset; add one, for example 2026-11-03T12:00-05:00 "
                         "(Eastern Standard Time) or end it with Z for UTC")
    if when > (now or datetime.now(timezone.utc)):
        raise ValueError(f"{text!r} is in the future")
    return when


def describe_moment(when):
    """A past moment as shown on the page: 2026-11-03 12:00 UTC-05:00."""
    offset = when.strftime("%z")
    return f"{when:%Y-%m-%d %H:%M} UTC{offset[:3]}:{offset[3:]}"


def get_text(url, **params):
    return get(url, params).text


def parse_rdb(text):
    """Rows of a USGS RDB (tab-separated) response as dicts. Comment lines and the column-type line
    that follows the header are skipped."""
    rows = [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")]
    if len(rows) < 2:
        return []
    return [dict(zip(rows[0], row)) for row in rows[2:]]


def fetch_catalog(site_ids):
    """What each site has measured: {site: [rows]}, each row shaped like the legacy USGS site catalog's
    ({"parm_cd", "data_type_cd", "begin_date", "end_date"}), for classify_site.

    The data series come from the USGS series catalog, with "uv" for a continuous record, "dv" for a
    daily one and "other" for the rest (annual peaks, say). A series with no dates at all holds no data
    (the catalog lists some such, even for gages whose old record was not carried over), so it is left
    out. Field visits ("sv") and water-quality samples ("qw") are not in that catalog, so they are
    looked up only for the sites with no continuous record of anything (a reservoir with a lake-level
    gage is "other", not "field visits"), and samples only for those of them with no field visits,
    since those come first. A failed request is raised.
    """
    kinds = {"Points": "uv", "Daily": "dv"}
    catalog = {}
    for row in fetch_csv(TIME_SERIES, site_ids, MAX_SITES_PER_REQUEST,
                         "monitoring_location_id,parameter_code,computation_period_identifier,begin,end"):
        if not (row.get("begin") or row.get("end")):
            continue
        catalog.setdefault(site_of(row), []).append({
            "parm_cd": row["parameter_code"], "data_type_cd": kinds.get(row["computation_period_identifier"], "other"),
            "begin_date": (row.get("begin") or "")[:10], "end_date": (row.get("end") or "")[:10]})
    bare = [site for site in site_ids
            if not any(r["data_type_cd"] in CONTINUOUS and r["end_date"] for r in catalog.get(site, []))]
    visited = {site_of(row) for row in fetch_csv(FIELD_MEASUREMENTS, bare, MAX_SITES_PER_REQUEST,
                                                 "monitoring_location_id")} if bare else set()
    unvisited = [site for site in bare if site not in visited]
    sampled = fetch_sample_sites(unvisited) if unvisited else set()
    for site in visited:
        catalog.setdefault(site, []).append({"parm_cd": "", "data_type_cd": "sv", "begin_date": "", "end_date": ""})
    for site in sampled:
        catalog.setdefault(site, []).append({"parm_cd": "", "data_type_cd": "qw", "begin_date": "", "end_date": ""})
    return catalog


def fetch_sample_sites(site_ids):
    """The sites among these with water-quality samples, from the USGS samples API, which lists only
    sites with at least one sample (its "locations" list does not: it has sites with none). A failed
    request is raised."""
    found = set()
    for i in range(0, len(site_ids), MAX_SITES_PER_REQUEST):
        batch = [f"USGS-{site}" for site in site_ids[i:i + MAX_SITES_PER_REQUEST]]
        for f in get_json(SAMPLE_SITES, monitoringLocationIdentifier=batch, pageSize=1000)["features"]:
            found.add(f["properties"]["monitoringLocationIdentifier"].removeprefix("USGS-"))
    return found


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


def percentile_label(g):
    """The percentile column for a gage with no NWS stages: pNN for the same calendar day over all
    years of record, REC above every past value for the day and LOW below every one. A gage with no
    long-term statistics shows its past-year percentile marked with a *, or n/a."""
    lt = g.get("longterm")
    if lt:
        return {"above": "REC", "below": "LOW"}.get(lt["status"]) or f"p{lt['pct']:.0f}"
    return f"p{g['pct']:.0f}*" if g.get("pct") is not None else "n/a"


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


def fetch_histories(site_ids, as_of=None):
    """The past year of daily gage heights per site, as {site: [ft]}, for the year ending on the date
    of `as_of` when given and today otherwise. A site with none, or too few, is left out or empty.

    Uses the daily mean. Some sites publish only a daily maximum and minimum for stage; for those the
    midpoint of each day's maximum and minimum stands in for the mean. Sites go
    HISTORY_SITES_PER_REQUEST to a request (see fetch_csv). A failed request costs those sites their
    past-year percentile, not the run.
    """
    end = (as_of or datetime.now(timezone.utc)).date()
    try:
        rows = fetch_csv(DAILY_ITEMS, site_ids, HISTORY_SITES_PER_REQUEST, "monitoring_location_id,statistic_id,time,value",
                         parameter_code=GAGE_HEIGHT, statistic_id=f"{DAILY_MAX},{DAILY_MIN},{DAILY_MEAN}",
                         datetime=f"{(end - timedelta(days=365)).isoformat()}/{end.isoformat()}")
    except (requests.RequestException, KeyError, ValueError):
        return {}  # the past-year percentile is a second opinion; the map does without it
    by_site = {}  # site -> statistic -> {date: value}
    for row in rows:
        value = usable(row.get("value"))
        if value is not None and row.get("time"):
            by_site.setdefault(site_of(row), {}).setdefault(row.get("statistic_id"), {})[row["time"]] = value
    return {site: daily_history(by_stat) for site, by_stat in by_site.items()}


def daily_history(by_stat):
    """A year of daily stage from {statistic: {date: value}}: the means, or with fewer than
    MIN_HISTORY_DAYS of them, the midpoint of each day's maximum and minimum."""
    mean = list(by_stat.get(DAILY_MEAN, {}).values())
    if len(mean) >= MIN_HISTORY_DAYS:
        return mean
    hi, lo = by_stat.get(DAILY_MAX, {}), by_stat.get(DAILY_MIN, {})
    return [(hi[d] + lo[d]) / 2 for d in hi if d in lo]


def percentile(history, value):
    if len(history) < MIN_HISTORY_DAYS:
        return None
    return 100 * sum(h <= value for h in history) / len(history)


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


def reading_day(when):
    """The (month, day) of a reading on the gage's own clock; its timestamp carries the UTC offset."""
    d = datetime.fromisoformat(when)
    return d.month, d.day


def fetch_daily_stats(dates):
    """The statistics row for each site's own calendar day: {site: row}.

    `dates` maps site -> (month, day). The service takes at most MAX_STAT_SITES sites per request, so
    sites go in batches that run in parallel. A batch that fails is skipped, so its sites simply have
    no long-term statistics; sites the service has no statistics for are absent from the result.
    """
    sites = sorted(dates)
    batches = [sites[i:i + MAX_STAT_SITES] for i in range(0, len(sites), MAX_STAT_SITES)]

    def fetch(batch):
        try:
            text = get_text(STAT_SERVICE, format="rdb", sites=",".join(batch), statReportType="daily",
                            statTypeCd="all", parameterCd=GAGE_HEIGHT)
            rows = parse_rdb(text)
            return {r["site_no"]: r for r in rows
                    if r["site_no"] in dates and (int(r["month_nu"]), int(r["day_nu"])) == dates[r["site_no"]]}
        except (requests.RequestException, KeyError, ValueError):
            return {}

    found = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for part in pool.map(fetch, batches):
            found.update(part)
    return found


def long_term(stage, row):
    """Where a stage falls among the same calendar day's values in every year of record.

    `row` is a statistics row (see fetch_daily_stats). The service gives only a few percentile points
    (5th, 10th, 20th, 25th, 50th, 75th, 80th, 90th and 95th, plus the minimum and maximum), so the
    percentile is interpolated between the points that are present. Returns None when the record is
    shorter than MIN_STAT_YEARS or the minimum or maximum is missing. Otherwise a dict whose "status"
    is "within", "above" (higher than every past value, percentile 100) or "below" (lower than every
    one, percentile 0), with the span of the record and its median and extremes for display.
    """
    try:
        years = int(row["count_nu"])
    except (KeyError, TypeError, ValueError):
        return None
    if years < MIN_STAT_YEARS:
        return None
    anchors = sorted((v, pct) for col, pct in STAT_COLUMNS if (v := usable(row.get(col))) is not None)
    if len(anchors) < 2 or anchors[0][1] != 0 or anchors[-1][1] != 100:
        return None  # without the minimum and the maximum there is nothing to bound the range
    low, high = anchors[0][0], anchors[-1][0]
    if stage > high:
        status, pct = "above", 100.0
    elif stage < low:
        status, pct = "below", 0.0
    else:
        status = "within"
        same = [p for v, p in anchors if v == stage]
        if same:  # on a flat stretch (repeated values) take the middle of its percentile span
            pct = (min(same) + max(same)) / 2
        else:
            v0, p0 = [a for a in anchors if a[0] < stage][-1]
            v1, p1 = next(a for a in anchors if a[0] > stage)
            pct = p0 + (p1 - p0) * (stage - v0) / (v1 - v0)

    def year(col):
        text = row.get(col, "").strip()
        return int(text) if text.isdigit() else None

    return {"pct": round(pct, 1), "status": status, "years": years,
            "first": year("begin_yr"), "last": year("end_yr"),
            "month": int(row["month_nu"]), "day": int(row["day_nu"]),
            "median": usable(row.get("p50_va")),
            "high": high, "high_year": year("max_va_yr"),
            "low": low, "low_year": year("min_va_yr")}


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


def build_gages(anchor, distance_km, draw_retired=False, as_of=None, paths=None, comids=None):
    """Return (flowlines, gages, retired). `retired` lists the sites that no longer report stage but
    once had a stage or flow record; it is only filled in when draw_retired is set.

    With `as_of` (an aware datetime) the gages are as they were at that moment. NWS forecasts are not
    kept for past times, so there are none; flood stages and the site catalog are today's.

    If `paths` and `comids` are dicts they are filled with the river's flowlines and its gages' flowlines,
    for find_confluences and nearest_gages (see fetch_river).
    """
    extra = {k: v for k, v in (("paths", paths), ("comids", comids)) if v is not None}
    lines, sites = fetch_river(anchor, distance_km, **extra)
    readings = fetch_readings(list(sites), as_of)
    latest = latest_readings(readings, as_of)
    zones = fetch_time_zones(list(latest)) if latest else {}
    current = {site: (stage, local_time(when, zones.get(site))) for site, (stage, when) in latest.items()}
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
                category, info = classify_site(catalog.get(site, []), as_of)
                counts[category] = counts.get(category, 0) + 1
                if category in ("retired", "offline"):
                    name, lat, lon = sites[site]
                    retired.append({"site": site, "name": pretty_name(name), "lat": lat, "lon": lon,
                                    "status": category, **info})
    if as_of:
        print(f"Showing the river as of {describe_moment(as_of)}. The NWS keeps no past forecasts, so there are "
              "no forecast rings or charts.", file=sys.stderr)
    print(describe_sites(len(sites), len(current), counts, len(retired), draw_retired), file=sys.stderr)
    if not current:
        sys.exit("No gages on this river are reporting recent gage height (parameter 00065).")

    lats = [sites[s][1] for s in current]
    lons = [sites[s][2] for s in current]
    nws = []
    if not nws_state["down"]:
        try:
            nws = get_json(NWPS, **{"bbox.xmin": min(lons) - 0.02, "bbox.ymin": min(lats) - 0.02,
                                    "bbox.xmax": max(lons) + 0.02, "bbox.ymax": max(lats) + 0.02,
                                    "srid": "EPSG_4326"})["gauges"]
        except requests.RequestException as err:
            if not is_refusal(err):  # a bad request or a dropped connection is not "too busy"
                raise
            # flood stages and forecasts are extras: without them every gage is colored by percentile
            print(f"NWS flood stages unavailable ({err}); drawing the gages without flood categories "
                  "or forecasts. The rest of this run will skip the NWS; run it again later for those.",
                  file=sys.stderr)
            nws_state["down"] = True

    stats = fetch_daily_stats({site: reading_day(when) for site, (_, when) in current.items()})

    def build(site):
        name, lat, lon = sites[site]
        stage, when = current[site]
        gage = {"site": site, "name": pretty_name(name), "lat": lat,
                "lon": lon, "stage": stage, "time": when,
                "recent": recent_stats(readings[site], now=as_of.timestamp() if as_of else None),
                "longterm": long_term(stage, stats[site]) if site in stats else None}
        flood = fetch_nws(site, lat, lon, nws)
        if flood:
            gage.update(method="nws", lid=flood["lid"], stages=flood["stages"],
                        category=flood_category(stage, flood["stages"]),
                        forecast=None if as_of else fetch_forecast(flood["lid"], flood["stages"]))
        else:
            gage.update(method="percentile", pct=None)  # filled in below, once every gage's method is known
        return gage

    with ThreadPoolExecutor(max_workers=8) as pool:
        gages = list(pool.map(build, current))
    diamonds = [g for g in gages if g["method"] == "percentile"]
    histories = fetch_histories([g["site"] for g in diamonds], as_of) if diamonds else {}
    for g in diamonds:
        g["pct"] = percentile(histories.get(g["site"], []), g["stage"])
    retired = sorted(retired, key=lambda g: -g["lat"]) if draw_retired else []
    return lines, sorted(gages, key=lambda g: -g["lat"]), retired


def nearest_gages(confluence, gages_by_river, comids=None):
    """For each river at a confluence, its closest reporting gage as {"river", "site", "name", "miles"}.

    A river's walk carries on below the meeting, so its gages include ones on the river it joined or
    formed (the Allegheny's include the Ohio's). "Nearest on each river" means on its own side, above
    the meeting: gages on the stretch listed in the confluence's "below" (by `comids`, {river: {site:
    flowline}}) are only used when a river has none above, and are then marked "below": True.
    """
    below = set(confluence.get("below", ()))
    out = []
    for river in confluence["rivers"]:
        gages = gages_by_river.get(river, [])
        flowline = (comids or {}).get(river, {})
        above = [g for g in gages if flowline.get(g["site"]) not in below]
        pool = above or gages
        if pool:
            km, g = min(((haversine_km(confluence["lat"], confluence["lon"], g["lat"], g["lon"]), g)
                         for g in pool), key=lambda t: t[0])
            entry = {"river": river, "site": g["site"], "name": g["name"], "miles": round(km / KM_PER_MILE, 1)}
            if not above:
                entry["below"] = True
            out.append(entry)
    return out


def build_rivers(anchors, labels, distance_km, draw_retired=False, as_of=None):
    """Return (flowlines, gages, retired, confluences) for one or more rivers.

    `anchors` and `labels` are parallel lists. With a single river this is build_gages and there are
    no confluences. With several, each river is built on its own and the results merged (a gage or
    flowline shared by two rivers appears once); a river with nothing to show is skipped with a
    warning. Confluences are where the rivers meet, each with the nearest gage on every river.
    """
    if len(anchors) == 1:
        return (*build_gages(anchors[0], distance_km, draw_retired, as_of), [])
    lines, gages, retired, by_river, paths, flowlines, seen = [], {}, {}, {}, {}, {}, set()
    for anchor, label in zip(anchors, labels):
        print(f"\n{label}:", file=sys.stderr)
        river_paths, river_comids = {}, {}
        try:
            river_lines, river_gages, river_retired = build_gages(
                anchor, distance_km, draw_retired, as_of, paths=river_paths, comids=river_comids)
        except SystemExit as err:
            if not isinstance(err.code, str):
                raise
            print(f"  skipping {label}: {err.code}", file=sys.stderr)
            continue
        except requests.RequestException as err:  # one river's outage should not lose the others
            print(f"  skipping {label}: {err}", file=sys.stderr)
            continue
        for f in river_lines:
            comid = f.get("properties", {}).get("nhdplus_comid")
            if comid is None or comid not in seen:
                seen.add(comid)
                lines.append(f)
        for g in river_gages:
            gages.setdefault(g["site"], g)
        for g in river_retired:
            retired.setdefault(g["site"], g)
        by_river[label] = river_gages
        paths[label] = river_paths
        flowlines[label] = river_comids
    if not by_river:
        sys.exit("None of these rivers has a gage reporting recent gage height (parameter 00065).")
    retired = [g for site, g in retired.items() if site not in gages]
    confluences = []
    if len(by_river) > 1:
        try:
            site_of = {label: anchor for anchor, label in zip(anchors, labels)}
            try:
                by_site = fetch_drainage_areas([site_of[label] for label in by_river])
            except (requests.RequestException, KeyError, ValueError):
                by_site = {}  # the rule then falls back to how far each walk went
            confluences = find_confluences(
                paths, {label: by_site.get(site_of[label]) for label in by_river}, detail=True)
        except (KeyError, ValueError, IndexError, TypeError) as err:  # a flowline in an unexpected shape
            print(f"Could not look for confluences: {err}", file=sys.stderr)
        for c in confluences:
            c["nearest"] = nearest_gages(c, by_river, flowlines)
            c.pop("below", None)  # only needed to choose the gages; not for the page
    return (lines, sorted(gages.values(), key=lambda g: -g["lat"]),
            sorted(retired, key=lambda g: -g["lat"]), confluences)


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
  .gl svg { display: block; }
  .pop b svg { vertical-align: middle; }
  .crest-sw { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
              border-radius: 50%; background: #6b6b6b; vertical-align: middle; margin-right: 4px; }
  .rt { width: 10px; height: 10px; border: 2px solid #6b6b6b; border-radius: 50%;
        background: rgba(255,255,255,.7); }
  .cf { width: 14px; height: 14px; border: 3px solid #6a1b9a; border-radius: 50%; background: #fff;
        box-shadow: 0 0 3px #000; box-sizing: border-box; }
  .sw.cf { width: 12px; height: 12px; box-shadow: none; border-width: 3px; vertical-align: middle; margin-right: 6px; }
  .sw.ret { width: 8px; height: 8px; border: 2px solid #6b6b6b; border-radius: 50%; background: #fff; }
  .modes label { display: block; cursor: pointer; }
  .cap { font-size: 10px; color: #666; }
  .sub { color: #666; }
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
const AS_OF = __AS_OF__;
const TITLE = __TITLE_JSON__;
const CONFLUENCES = __CONFLUENCES__;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

const NWS = {normal: ["#2e9e4f", "Normal"], action: ["#f2d424", "Action"],
             minor: ["#f28c1c", "Minor flood"], moderate: ["#d62828", "Moderate flood"],
             major: ["#8e2fb8", "Major flood"]};
// percentile of the same calendar day over every year of record; the last bin is for a stage higher
// than any past value on that day (binned as 101 so it never lands in the one before)
const DATE_BINS = [[5, "#c9b79c", "Below 5th"], [25, "#9ecae1", "5th-25th"], [75, "#4a90c8", "25th-75th"],
                   [95, "#1f5fa8", "75th-95th"], [101, "#0b2f6b", "95th, up to the record"],
                   [Infinity, "#b0006f", "Above the record for this date"]];
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
  date:  {label: "Percentile for this date (all years)", legend: "No NWS stages: percentile for this date",
          bins: DATE_BINS},
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
function dateValue(g) {
  const lt = g.longterm;
  return !lt ? null : lt.status === "above" ? 101 : Math.round(lt.pct);
}
function binFor(g) {
  return mode === "trend" ? trendBin(g)
       : bin(MODES[mode].bins, mode === "range" ? rounded(g.recent && g.recent.range_pos) : dateValue(g));
}
function color(g) {
  if (g.method === "nws") return NWS[g.category][0];
  const b = binFor(g);
  return b ? b[1] : NODATA;
}

// "Cresting": the stage has leveled off just past its 7-day high. Strict on purpose: the peak must
// already be behind us, so a gage still setting new highs is not called cresting. It is only a glyph;
// the trend color stays "steady".
const STEADY_BAND = 0.02;        // ft/h: the edge of the "steady" trend bin
const CREST_MIN_AGE_H = 0.25;    // the 7-day peak was at least this long ago (15 minutes)...
const CREST_MAX_AGE_H = 6;       // ...and no longer ago than this
const CREST_MIN_RANGE_POS = 95;  // and the stage is still in the top 5% of its 7-day range
const peakAgeHours = g => (Date.parse(g.time) - g.recent.peak_time) / 36e5;
function isCresting(g) {
  const r = g.recent;
  if (!r || r.rate == null || r.range_pos == null) return false;
  const age = peakAgeHours(g);
  return Math.abs(r.rate) <= STEADY_BAND && r.range_pos >= CREST_MIN_RANGE_POS &&
         age >= CREST_MIN_AGE_H && age <= CREST_MAX_AGE_H;
}
// at the top of the week's range with the peak not yet behind it (the latest reading is the high)
const atWeeklyHigh = g => !!g.recent && g.recent.range_pos != null &&
                          g.recent.range_pos >= CREST_MIN_RANGE_POS && peakAgeHours(g) < CREST_MIN_AGE_H;
// a crest then a trough: a wave. White with a dark halo for use on a colored marker; pass a dark ink
// and no halo to draw it on a light background.
function crestSvg(w = 16, h = 10, ink = "#fff", halo = "#000") {
  const d = "M2 6 A3 3 0 0 1 8 6 A3 3 0 0 0 14 6";
  return `<svg width="${w}" height="${h}" viewBox="0 0 16 10" fill="none" stroke-linecap="round" role="img" aria-label="cresting">` +
         (halo ? `<path d="${d}" stroke="${halo}" stroke-width="3.6" opacity=".9"/>` : "") +
         `<path d="${d}" stroke="${ink}" stroke-width="${halo ? 1.9 : 1.8}"/></svg>`;
}
function glyphFor(g) {
  if (isCresting(g)) return `<span class="gl">${crestSvg()}</span>`;
  const tb = trendBin(g), chars = tb ? [...tb[3]] : [];  // doubled glyphs (fast) are stacked vertically
  return !chars.length ? "" : chars.length > 1
    ? `<span class="gl fast">${chars.map(c => `<i>${c}</i>`).join("")}</span>`
    : `<span class="gl">${chars[0]}</span>`;
}
const crestLegendLine = () => `<div style="margin-top:2px"><span class="crest-sw">${crestSvg(14, 9)}</span>` +
                              `= at its crest: just past its 7-day high, leveling off</div>`;

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
  if (tb) {
    const cresting = isCresting(g);
    let label = tb[2].split(" (")[0];
    if (cresting) label = "Cresting";
    else if (tb[2].startsWith("Steady") && atWeeklyHigh(g)) label = "Steady, at its 7-day high";
    const mark = cresting ? crestSvg(14, 9, "#222", null) + " " : tb[3] ? tb[3] + " " : "";
    rows += `<tr><td>Trend (6 h)</td><td><b>${mark}${label}</b> ${signed(r.rate, 3)} ft/h</td></tr>`;
  }
  if (r.delta24 != null) rows += `<tr><td>Change (24 h)</td><td>${signed(r.delta24)} ft</td></tr>`;
  rows += `<tr><td>7-day range</td><td>${r.low.toFixed(2)} to ${r.high.toFixed(2)} ft` +
          (r.range_pos == null ? " (flat)" : ` (now at ${Math.round(r.range_pos)}%)`) + `</td></tr>`;
  rows += `<tr><td>7-day peak</td><td>${r.peak.toFixed(2)} ft, ${ago(r.peak_time, ref)}</td></tr>`;
  return rows;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function longTermRow(g) {
  const lt = g.longterm;
  if (!lt) return `<tr><td>This date</td><td>no long-term statistics</td></tr>`;
  const inYear = y => y ? ` in ${y}` : "";
  const sub = [`${lt.years} years` + (lt.first && lt.last ? `, ${lt.first}-${lt.last}` : "")];
  if (lt.median != null) sub.push(`median ${lt.median.toFixed(2)} ft`);
  let head;
  if (lt.status === "above") head = `<b>Above the record</b> (previous high ${lt.high.toFixed(2)} ft${inYear(lt.high_year)})`;
  else if (lt.status === "below") head = `<b>Below the record low</b> (previous low ${lt.low.toFixed(2)} ft${inYear(lt.low_year)})`;
  else {
    head = `<b>${ordinal(Math.round(lt.pct))} percentile</b>`;
    sub.push(`record ${lt.high.toFixed(2)} ft${lt.high_year ? ` (${lt.high_year})` : ""}`);
  }
  return `<tr><td>For ${MONTHS[lt.month - 1]} ${lt.day}</td><td>${head}<br><span class="sub">${sub.join("; ")}</span></td></tr>`;
}

function popup(g) {
  const t = new Date(g.time).toLocaleString();
  let rows = `<tr><td>Stage</td><td><b>${g.stage.toFixed(2)} ft</b></td></tr>
              <tr><td>Reported</td><td>${t}</td></tr>` + recentRows(g) + longTermRow(g);
  if (g.method === "nws") {
    rows += `<tr><td>NWS category</td><td><b>${NWS[g.category][1]}</b></td></tr>`;
    for (const c of ["action", "minor", "moderate", "major"])
      if (g.stages[c] != null) rows += `<tr><td>${NWS[c][1]} stage</td><td>${g.stages[c]} ft</td></tr>`;
    if (g.forecast) {
      const f = g.forecast, ct = new Date(f.crest_time).toLocaleString([], {weekday: "short", hour: "numeric"});
      rows += `<tr><td>Forecast crest</td><td><b>${f.crest.toFixed(1)} ft</b> (${NWS[f.crest_category][1]}) ${ct}</td></tr>`;
    } else rows += `<tr><td>Forecast</td><td>${AS_OF ? "not kept for past times" : "none issued"}</td></tr>`;
    rows += `<tr><td>NWS gauge</td><td><a href="https://water.noaa.gov/gauges/${esc(g.lid)}" target="_blank">${esc(g.lid)}</a></td></tr>`;
  } else {
    rows += `<tr><td>Past year</td><td>${g.pct == null ? "n/a (short record)" : ordinal(Math.round(g.pct)) + " percentile"}</td></tr>`;
  }
  return `<div class="pop"><b>${esc(g.name)}</b><br>USGS ${esc(g.site)}<table>${rows}</table>${recentSpark(g)}${spark(g)}
    <a href="https://waterdata.usgs.gov/monitoring-location/USGS-${esc(g.site)}/" target="_blank">USGS page</a></div>`;
}

function icon(g) {
  const shape = g.method === "nws" ? "round" : "dia", glyph = glyphFor(g);
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

function confluencePopup(c) {
  const names = c.rivers.map(esc), last = names.pop();
  const meet = names.length ? `${names.join(", ")} and ${last}` : last;
  const rows = (c.nearest || []).map(n =>
    `<tr><td>${esc(n.river)}</td><td>${esc(n.name)}<br><span class="sub">USGS ${esc(n.site)}, ${n.miles} mi away${n.below ? ", below the confluence" : ""}</span></td></tr>`).join("");
  return `<div class="pop"><b>River confluence</b><br>${meet} meet here.` +
         (rows ? `<br><span class="cap">Nearest gage on each:</span><table>${rows}</table>` : "") + "</div>";
}

if (CONFLUENCES.length) {
  const confluenceLayer = L.layerGroup(CONFLUENCES.map(c =>
    L.marker([c.lat, c.lon], {icon: L.divIcon({className: "", iconSize: [14, 14], html: '<div class="cf"></div>'}),
                              title: "Confluence: " + c.rivers.join(", "), zIndexOffset: 500})
      .bindPopup(confluencePopup(c)))).addTo(map);
  layersCtl.addOverlay(confluenceLayer, `River confluences (${CONFLUENCES.length})`);
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
  if (GAGES.some(isCresting)) h += crestLegendLine();
  if (RETIRED.length) h += '<div style="margin-top:4px"><span class="sw ret"></span>No longer reporting (retired or offline)</div>';
  if (CONFLUENCES.length) h += '<div style="margin-top:4px"><span class="sw cf"></span>River confluence</div>';
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
  d.innerHTML = `<h4>${esc(TITLE)} stream gages</h4>${GAGES.length} gage${GAGES.length === 1 ? "" : "s"}${RETIRED.length ? ` + ${RETIRED.length} no longer reporting` : ""}${CONFLUENCES.length ? `, ${CONFLUENCES.length} confluence${CONFLUENCES.length === 1 ? "" : "s"}` : ""} &middot; ` +
    (AS_OF ? `as of ${esc(AS_OF)}<br><span class="cap">generated ${GENERATED}</span>` : `generated ${GENERATED}`);
  return d;
};
title.addTo(map);
</script>
</body>
</html>
"""


def slug(text):
    """Lower case, with every run of other characters as one underscore: a safe piece of a file name."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def file_stem(labels, anchors):
    """The default file name, without the "_gages.html", for a map of these rivers.

    One river: its name. A few: the names, each without a trailing "River" (it says nothing the rest
    of the name doesn't). More than MAX_NAMES_IN_FILE: the first few, then "plus N" and six hex digits
    from the sites, so a different set of rivers never shares a file with this one and the same set
    always does (a rerun replaces its own map).
    """
    if len(labels) == 1:
        return slug(labels[0])
    short = [slug(re.sub(r"\s+river$", "", label, flags=re.IGNORECASE)) or slug(label) for label in labels]
    if len(short) <= MAX_NAMES_IN_FILE:
        return "_".join(short)
    digest = hashlib.sha1("|".join(anchors).encode()).hexdigest()[:6]
    return "_".join(short[:MAX_NAMES_IN_FILE]) + f"_plus_{len(short) - MAX_NAMES_IN_FILE}_{digest}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("river", nargs="*", help="river name as USGS spells it, e.g. 'Myakka River'; "
                    "give several to map them together and mark where they meet")
    ap.add_argument("--state", help="state abbreviation or name, to disambiguate shared river names")
    ap.add_argument("--anchor", action="append",
                    help="USGS site number on the river (skips the name lookup); repeat it for several rivers")
    ap.add_argument("--list", action="store_true", help="list candidate gages for the name and exit")
    ap.add_argument("--distance", type=float, default=200,
                    help="miles to navigate each direction (default: %(default)g)")
    ap.add_argument("--retired", action="store_true",
                    help="also draw gages that no longer report stage (retired or offline) as faint markers")
    ap.add_argument("--as-of", metavar="TIME", dest="as_of",
                    help="show the river as it was at a past moment, an ISO 8601 time with its UTC offset, "
                         "e.g. 2026-11-03T12:00-05:00 (no NWS forecasts for past times)")
    ap.add_argument("--name", help="a name for the map, such as 'Pittsburgh rivers': its title and the start of "
                    "the file name (several rivers make a long default name)")
    ap.add_argument("--out", default=None, help="output HTML path")
    args = ap.parse_args()
    name = args.name.strip() if args.name else None
    if args.name is not None and not slug(args.name):
        ap.error("--name needs at least one letter or digit")
    if not args.river and not args.anchor:
        ap.error("give a river name or --anchor")

    if args.list and not args.river:
        ap.error("--list needs a river name")
    if args.anchor and args.river and (len(args.river) > 1 or len(args.anchor) > 1) and not args.list:
        ap.error("--anchor goes with one river name and replaces its lookup; to map several rivers by "
                 "site, repeat --anchor and leave the names off")
    try:
        as_of = parse_as_of(args.as_of) if args.as_of else None
    except ValueError as err:
        ap.error(f"--as-of: {err}")
    # --list always shows candidates (and exits), even if --anchor was also given
    if args.list:
        for name in args.river:
            if len(args.river) > 1:
                print(f"\n{name}:")
            try:
                resolve_anchor(name, args.state, True)  # prints the candidates, then exits
            except SystemExit as err:
                if err.code not in (0, None):  # "no gages found" and the like still stop the run
                    raise
        sys.exit(0)
    if args.anchor:
        anchors = args.anchor
        labels = [args.river[0]] if len(anchors) == 1 and args.river else [f"USGS {a}" for a in anchors]
    else:
        names = list(dict.fromkeys(n.strip() for n in args.river))  # a name given twice is one river
        anchors = [resolve_anchor(name, args.state) for name in names]
        labels = names
    seen = {}  # the same site given twice, or by two names, is one river
    for anchor, label in zip(anchors, labels):
        seen.setdefault(anchor, label)
    anchors, labels = list(seen), list(seen.values())
    title = name or " + ".join(labels)
    lines, gages, retired, confluences = build_rivers(
        anchors, labels, round(args.distance * KM_PER_MILE), args.retired, as_of)
    stem = (slug(name) if name else file_stem(labels, anchors)) + (f"_{as_of:%Y-%m-%d_%H%M}" if as_of else "")
    out = args.out or f"{stem}_gages.html"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def safe_json(obj):  # keep "</script>" in a site name from ending the script block
        return json.dumps(obj).replace("<", "\\u003c")

    values = {"TITLE": html.escape(title), "TITLE_JSON": safe_json(title),
              "RIVER": safe_json({"type": "FeatureCollection", "features": lines}),
              "GAGES": safe_json(gages), "RETIRED": safe_json(retired),
              "CONFLUENCES": safe_json(confluences), "GENERATED": generated,
              "AS_OF": safe_json(describe_moment(as_of) if as_of else None)}
    # one pass, so text inside a substituted value can never be mistaken for a placeholder
    page = re.sub(r"__(TITLE_JSON|TITLE|RIVER|GAGES|RETIRED|CONFLUENCES|AS_OF|GENERATED)__", lambda m: values[m.group(1)], HTML)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {out}", file=sys.stderr)
    for g in gages:
        fc = g.get("forecast")
        info = ((f"{g['category']:8s}" + (f" -> crest {fc['crest']} ft {fc['crest_category']} {fc['crest_time']}" if fc else ""))
                if g["method"] == "nws"
                else percentile_label(g))
        r = g["recent"]
        trend = f"{r['rate']:+.3f} ft/h" if r and r["rate"] is not None else "n/a"
        print(f"{g['site']}  {g['stage']:7.2f} ft  {info:9s} {trend:>11s}  {g['name']}")
    if confluences:
        print(f"\nConfluences ({len(confluences)}):")
        for c in confluences:
            near = "; ".join(f"{n['river']}: {n['site']} {n['miles']} mi" for n in c["nearest"])
            print(f"{c['lat']:.4f}, {c['lon']:.4f}  {' + '.join(c['rivers'])}" + (f"  (nearest gages {near})" if near else ""))
    if retired:
        print(f"\nNo longer reporting stage ({len(retired)}):")
        for r in retired:
            print(f"{r['site']}  last data {r['last']}  {r['status']:<8s} {r['name']}")


if __name__ == "__main__":
    main()
