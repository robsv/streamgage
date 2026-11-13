// Runs under node after the page's own helper code has been pasted in front of it (see
// test_page.py). It tests the logic that decides what the map shows, without a browser.
const failures = [];
const check = (condition, message) => { if (!condition) failures.push(message); };
const equal = (actual, expected, message) =>
  check(Object.is(actual, expected) || actual === expected,
        `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);

// ---- trend bins: the boundaries must agree with the legend's labels ------------------------
const withRate = r => ({method: "percentile", recent: {rate: r, range_pos: null}, pct: null});
const trendLabel = r => { const b = trendBin(withRate(r)); return b && b[2].split(" (")[0]; };

equal(trendLabel(0.1001), "Rising fast", "above +0.10 is fast");
equal(trendLabel(0.10), "Rising", "+0.10 exactly is still just rising");
equal(trendLabel(0.0201), "Rising", "just over +0.02 is rising");
equal(trendLabel(0.02), "Steady", "+0.02 exactly is steady (\"within 0.02\")");
equal(trendLabel(0), "Steady", "zero is steady");
equal(trendLabel(-0.02), "Steady", "-0.02 exactly is steady");
equal(trendLabel(-0.0201), "Falling", "just under -0.02 is falling");
equal(trendLabel(-0.10), "Falling", "-0.10 exactly is still just falling");
equal(trendLabel(-0.1001), "Falling fast", "below -0.10 is fast");
equal(trendBin(withRate(null)), null, "no rate means no trend bin");
equal(trendBin({method: "x", recent: null}), null, "no recent stats means no trend bin");

equal(trendBin(withRate(0.3))[3], "▲▲", "rising fast glyph");
equal(trendBin(withRate(0.05))[3], "▲", "rising glyph");
equal(trendBin(withRate(0))[3], "▬", "steady glyph");
equal(trendBin(withRate(-0.05))[3], "▼", "falling glyph");
equal(trendBin(withRate(-0.3))[3], "▼▼", "falling fast glyph");

// ---- diamond colors by mode ---------------------------------------------------------------
const range = v => ({method: "percentile", recent: {range_pos: v, rate: null}, pct: null});
mode = "range";
equal(color(range(0)), RANGE[0][1], "0% is the low bin");
equal(color(range(9.4)), RANGE[0][1], "9.4% rounds to 9, still the low bin");
equal(color(range(9.6)), RANGE[1][1], "9.6% rounds to 10 (what the popup shows), so the next bin");
equal(color(range(50)), RANGE[2][1], "50% is the middle bin");
equal(color(range(89.6)), RANGE[4][1], "89.6% rounds to 90, the peak bin");
equal(color(range(100)), RANGE[4][1], "100% is the peak bin");
equal(color(range(null)), NODATA, "no range position is no data");
equal(color({method: "percentile", recent: null, pct: null}), NODATA, "no recent stats is no data");

// percentile for this date over all years of record
const longterm = (status, pct) => ({method: "percentile", longterm: {status, pct}});
mode = "date";
equal(color(longterm("within", 4.4)), DATE_BINS[0][1], "4.4 rounds to 4: the bottom bin");
equal(color(longterm("within", 4.6)), DATE_BINS[1][1], "4.6 rounds to 5 (as the popup shows): next bin");
equal(color(longterm("within", 50)), DATE_BINS[2][1], "the median is the middle bin");
equal(color(longterm("within", 94.4)), DATE_BINS[3][1], "94.4 rounds to 94: 75th-95th");
equal(color(longterm("within", 94.6)), DATE_BINS[4][1], "94.6 rounds to 95: 95th up to the record");
equal(color(longterm("within", 100)), DATE_BINS[4][1], "the record value itself is still within the record");
equal(color(longterm("above", 100)), DATE_BINS[5][1], "above the record has its own color");
equal(DATE_BINS[5][1], "#b0006f", "above the record is magenta");
equal(color(longterm("below", 0)), DATE_BINS[0][1], "below the record low is the bottom bin");
equal(color({method: "percentile", longterm: null}), NODATA, "no long-term statistics is no data");
equal(color({method: "percentile"}), NODATA, "a gage without the field is no data");
equal(dateValue(longterm("above", 100)), 101, "above the record is binned past 100");
equal(dateValue(longterm("within", 100)), 100, "the record value is binned as 100");

// the flow percentile for this date: the same bins, from the flow's own statistics
const flowGage = (status, pct, stagePct = 50) =>
  ({method: "percentile", longterm: {status: "within", pct: stagePct}, flow: 1000, flow_longterm: {status, pct}});
mode = "flow";
equal(color(flowGage("within", 50, 2)), DATE_BINS[2][1], "flow mode colors by the flow percentile, not the stage's");
equal(color(flowGage("within", 96)), DATE_BINS[4][1], "96th flow percentile: 95th up to the record");
equal(color(flowGage("above", 100)), DATE_BINS[5][1], "a flow above the record is magenta");
equal(color({method: "percentile", longterm: {status: "within", pct: 50}, flow: 1000, flow_longterm: null}), NODATA,
      "a flow without statistics is no data, even with stage statistics");
equal(color({method: "percentile", longterm: {status: "within", pct: 50}}), NODATA, "no flow at all is no data");
mode = "date";
equal(color(flowGage("within", 2, 50)), DATE_BINS[2][1], "and the stage mode still colors by the stage");

mode = "trend";
equal(color(withRate(0.5)), TREND[4][1], "trend mode colors by rate");
equal(color(withRate(null)), NODATA, "no rate is no data in trend mode");

for (const m of ["range", "trend", "date", "flow"]) {
  mode = m;
  equal(color({method: "nws", category: "minor"}), NWS.minor[0], `NWS circles ignore ${m} mode`);
  equal(color({method: "nws", category: "normal"}), NWS.normal[0], `NWS normal ignores ${m} mode`);
}

// ---- the bin tables themselves ------------------------------------------------------------
for (const [name, bins] of [["RANGE", RANGE], ["DATE_BINS", DATE_BINS], ["TREND", TREND]]) {
  const edges = bins.map(b => b[0]);
  check(edges.every((e, i) => i === 0 || e > edges[i - 1]), `${name} edges must increase`);
  check(bins.every(b => /^#[0-9a-f]{6}$/i.test(b[1])), `${name} colors must be hex`);
}
equal(RANGE.length, 5, "five range bins");
equal(DATE_BINS.length, 6, "six percentile-for-this-date bins");
equal(Object.keys(MODES).join(","), "range,trend,date,flow", "the four color modes, in order");
equal(TREND.length, 5, "five trend bins");
for (const m of Object.values(MODES)) check(!m.legend.includes("<br>"), "legend heading is one line");

// ---- formatting helpers -------------------------------------------------------------------
equal(signed(-0.0004, 3), "+0.000", "tiny negative is not shown as -0.000");
equal(signed(-0.004), "+0.00", "tiny negative change is not -0.00");
equal(signed(-0.42), "-0.42", "negative");
equal(signed(1.5), "+1.50", "positive");
equal(signed(0, 3), "+0.000", "zero");

const ordinals = [1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 41, 100, 101, 111, 112].map(ordinal);
equal(ordinals.join(" "), "1st 2nd 3rd 4th 11th 12th 13th 21st 22nd 23rd 41st 100th 101st 111th 112th",
      "ordinal suffixes");

const ref = Date.now();
equal(ago(ref, ref), "at latest reading", "a peak at the reference time");
equal(ago(ref - 30 * 1000, ref), "at latest reading", "30 seconds earlier");
equal(ago(ref - 45 * 60000, ref), "45 min ago", "45 minutes");
equal(ago(ref - 5 * 36e5, ref), "5 h ago", "5 hours");
equal(ago(ref - 47 * 36e5, ref), "47 h ago", "47 hours");
equal(ago(ref - 72 * 36e5, ref), "3.0 days ago", "3 days");

// ---- "since": how long ago the last data was ----------------------------------------------
const noon = Date.parse("2026-11-05T12:00:00Z");
const back = n => new Date(noon - n * 864e5).toISOString().slice(0, 10);
equal(since(back(0), noon), "today", "since today");
equal(since(back(1), noon), "1 day ago", "since 1 day");
equal(since(back(10), noon), "10 days ago", "since 10 days");
equal(since(back(59), noon), "59 days ago", "since 59 days");
equal(since(back(60), noon), "2 months ago", "since 60 days");
equal(since(back(365), noon), "12 months ago", "since a year");
equal(since(back(730), noon), "24 months ago", "since 730 days is still months");
equal(since(back(731), noon), "2 years ago", "since 731 days is two years, never 1 years");
equal(since(back(1100), noon), "3 years ago", "since three years");
equal(since("2005-09-29", noon), "21 years ago", "since 2005");
for (let n = 0; n < 9000; n += 7) check(!/^1 years|^0 /.test(since(back(n), noon)), `bad wording at ${n} days`);

// ---- the gage popup: escaping, and no "undefined" or "NaN" for any combination of data -----
const stages = {action: 9, minor: 11, moderate: 14, major: 16};
const recent = (over = {}) => ({low: 1, high: 3, peak: 3, peak_time: noon - 2 * 36e5, range_pos: 80,
                                rate: 0.05, delta24: 0.4, series: [[noon - 36e5, 2.5], [noon, 3]], ...over});
const lt = (over = {}) => ({pct: 87.9, status: "within", years: 53, first: 1973, last: 2026, month: 11,
                            day: 5, median: 71.44, high: 77.88, high_year: 2025, low: 69.83,
                            low_year: 2001, ...over});
const base = {site: "02297345", name: "Peace River At Platt FL", stage: 2.89,
              time: "2026-11-05T12:00:00.000-05:00", method: "percentile", pct: 41, recent: recent(),
              longterm: lt()};
const forecastG = {...base, method: "nws", category: "minor", stages, lid: "ARCF1", pct: undefined,
                   forecast: {issued: "2026-11-05T12:00:00Z", crest: 16.5, crest_category: "major",
                              crest_time: "2026-11-06T00:00:00Z",
                              series: [["2026-11-05T18:00:00Z", 16], ["2026-11-06T00:00:00Z", 16.5]]}};
const variants = {
  "diamond": base,
  "nws with forecast": forecastG,
  "nws without forecast": {...forecastG, forecast: null},
  "no recent stats": {...base, recent: null},
  "no trend": {...base, recent: recent({rate: null})},
  "flat range": {...base, recent: recent({range_pos: null, low: 2, high: 2})},
  "no 24 h change": {...base, recent: recent({delta24: null})},
  "no percentile": {...base, pct: null},
  "above the record": {...base, longterm: lt({status: "above", pct: 100})},
  "below the record low": {...base, longterm: lt({status: "below", pct: 0})},
  "no long-term statistics": {...base, longterm: null},
  "long-term without a median": {...base, longterm: lt({median: null})},
  "long-term without years": {...base, longterm: lt({first: null, last: null, high_year: null, low_year: null})},
  "one-point series": {...base, recent: recent({series: [[noon, 3]]})},
};
for (const [name, g] of Object.entries(variants)) {
  const html = popup(g);
  check(!/undefined|NaN|Infinity|\[object|>null</.test(html), `popup (${name}) shows a bad value`);
  check(html.includes("USGS 02297345"), `popup (${name}) names the site`);
}
check(popup(base).includes("41st percentile"), "popup shows the past-year percentile");

// the row for this date over all years of record
const rowText = g => popup(g).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ");
check(rowText(base).includes("For Nov 5 88th percentile 53 years, 1973-2026; median 71.44 ft; record 77.88 ft (2025)"),
      "within the record: percentile, span, median and record");
check(rowText(variants["above the record"]).includes(
      "For Nov 5 Above the record (previous high 77.88 ft in 2025) 53 years, 1973-2026; median 71.44 ft"),
      "above the record: says so and gives the previous high");
check(rowText(variants["below the record low"]).includes("Below the record low (previous low 69.83 ft in 2001)"),
      "below the record low: says so and gives the previous low");
check(rowText(variants["no long-term statistics"]).includes("This date no long-term statistics"),
      "no statistics: says so");
check(!rowText(variants["long-term without a median"]).includes("median"), "no median, none shown");
check(rowText(variants["long-term without years"]).includes("53 years;") &&
      !rowText(variants["long-term without years"]).includes("(null)") &&
      !rowText(variants["long-term without years"]).includes(" in null"), "missing years are left out");
check(popup(base).indexOf("7-day peak") < popup(base).indexOf("For Nov 5"), "the row follows the 7-day peak");
check(popup(base).indexOf("For Nov 5") < popup(base).indexOf("Past year"), "and comes before the past-year row");
check(popup({...forecastG, longterm: lt()}).includes("For Nov 5"), "circles get the row too");
// the flow rows: only for a gage with a flow reading, in cubic feet per second
const flowLt = over => ({pct: 97.7, status: "within", years: 95, first: null, last: null, month: 11, day: 5,
                         median: 422, high: 2920, high_year: null, low: 36, low_year: null, ...over});
check(!rowText(base).includes("Flow"), "no flow reading: no flow rows at all");
check(rowText({...base, flow: 2400, flow_longterm: flowLt()}).includes(
      "Flow 2,400 ft³/s Flow for Nov 5 98th percentile 95 years; median 422 ft³/s; record 2,920 ft³/s"),
      "flow: the reading, its percentile, the median and the record");
check(rowText({...base, flow: 30000, flow_longterm: flowLt({status: "above", pct: 100})}).includes(
      "Flow 30,000 ft³/s Flow for Nov 5 Above the record (previous high 2,920 ft³/s)"), "a flood flow");
check(rowText({...base, flow: 4150, flow_longterm: null}).includes("Flow 4,150 ft³/s Flow, this date no flow statistics"),
      "a flow without statistics says so");
check(popup({...base, flow: 2400, flow_longterm: flowLt()}).indexOf("For Nov 5") <
      popup({...base, flow: 2400, flow_longterm: flowLt()}).indexOf("Flow for Nov 5"), "the flow rows follow the stage's");
equal(cfs(4150), "4,150 ft³/s", "large flows: whole numbers with separators");
equal(cfs(42.37), "42.4 ft³/s", "tens: one decimal");
equal(cfs(3.456), "3.46 ft³/s", "small flows: two decimals");
equal(cfs(-512.4), "-512 ft³/s", "a tide running upstream keeps its sign");
for (const g of [{...base, flow: 2400, flow_longterm: flowLt()}, {...base, flow: 0, flow_longterm: null}])
  check(!/undefined|NaN|Infinity|\[object|>null</.test(popup(g)), "a flow popup shows no bad value");
// the forecast row says why there is no forecast when the page is for a past moment
check(rowText({...forecastG, forecast: null}).includes("Forecast none issued"), "now: no forecast issued");
AS_OF = "2026-11-03 12:00 UTC-05:00";
check(rowText({...forecastG, forecast: null}).includes("Forecast not kept for past times"),
      "past moment: forecasts are not kept");
check(!rowText({...forecastG, forecast: null}).includes("none issued"), "past moment: not 'none issued'");
AS_OF = null;
check(rowText({...base, longterm: lt({month: 2, day: 29})}).includes("For Feb 29"), "leap day label");
check(popup(variants["no recent stats"]).includes("too few readings"), "popup explains missing stats");
check(popup(variants["no percentile"]).includes("n/a (short record)"), "popup explains a missing percentile");
check(popup(forecastG).includes("Forecast crest"), "popup shows the forecast crest");
check(popup(forecastG).includes("water.noaa.gov/gauges/ARCF1"), "popup links the NWS gauge");

const evilGage = {...forecastG, site: '1"><img src=x onerror=alert(1)>', name: "<script>alert(1)</script>",
                  lid: '"><svg onload=alert(1)>'};
const evilHtml = popup(evilGage);
// the sparkline is a real <svg> element, so look for the attacker's own markup, not any <svg
check(!evilHtml.includes("<img") && !evilHtml.includes("<script>") && !evilHtml.includes("<svg onload"),
      "popup escapes hostile site, name and lid");
// drop quoted attribute values (escaped text inside them is harmless), then look for a live handler
const withoutValues = evilHtml.replace(/"[^"]*"/g, '""');
check(!/<[a-z][^>]*\son[a-z]+\s*=/i.test(withoutValues), "no injected event handler is live");
check(/<[a-z][^>]*\son[a-z]+\s*=/i.test('<img src=x onerror=alert(1)>'), "the handler check itself works");
check(evilHtml.includes("&lt;script&gt;"), "hostile name is shown as text");

// ---- cresting: strict on purpose --------------------------------------------------------------
// a gage whose stage has leveled off just past its 7-day high. `age` is how many hours ago the peak was.
const crestG = (over = {}, age = 0.8) => ({
  site: "02296750", name: "Peace River At SR 70 At Arcadia, FL", stage: 16.2,
  method: "percentile", time: new Date(noon).toISOString(), longterm: null, pct: null,
  recent: {low: 2, high: 16.2, peak: 16.2, peak_time: noon - age * 36e5, range_pos: 100, rate: 0.018,
           delta24: 1.2, series: [[noon, 16.2]], ...over}});
const crests = (over, age) => isCresting(crestG(over, age));

equal(STEADY_BAND, 0.02, "the steady band is the one the trend bins use");
equal(CREST_MIN_AGE_H, 0.25, "the peak must be at least 15 minutes behind");
equal(CREST_MAX_AGE_H, 6, "and no more than 6 hours behind");
equal(CREST_MIN_RANGE_POS, 95, "and the stage in the top 5% of its range");

equal(crests({}, 0.8), true, "Arcadia's case: steady, at the top, peak 0.8 h ago");
// how long ago the peak was
equal(crests({}, 0), false, "the peak is the latest reading: still at the high, not past it");
equal(crests({}, 0.24), false, "peak 14 minutes ago is not yet past");
equal(crests({}, 0.25), true, "peak exactly 15 minutes ago counts");
equal(crests({}, 6), true, "peak exactly 6 hours ago still counts");
equal(crests({}, 6.01), false, "peak over 6 hours ago does not");
// how steady
equal(crests({rate: 0.02}), true, "exactly +0.02 ft/h is steady");
equal(crests({rate: 0.0201}), false, "just above +0.02 is rising");
equal(crests({rate: -0.02}), true, "exactly -0.02 ft/h is steady");
equal(crests({rate: -0.0201}), false, "just below -0.02 is falling");
equal(crests({rate: 0}), true, "level");
equal(crests({rate: -0.05}, 3), false, "falling after the peak is not cresting");
// how near the top
equal(crests({range_pos: 95}), true, "exactly 95% of the range counts");
equal(crests({range_pos: 94.9}), false, "below 95% does not");
// missing data
equal(crests({range_pos: null}), false, "no range position");
equal(crests({rate: null}), false, "no trend");
equal(isCresting({...crestG(), recent: null}), false, "no recent statistics");
// the rule and the trend bins agree about where "steady" ends
equal(trendBin(crestG({rate: 0.02}))[2].startsWith("Steady"), true, "bins: +0.02 is steady");
equal(trendBin(crestG({rate: 0.0201}))[2].startsWith("Rising"), true, "bins: +0.0201 is rising");

// it changes the glyph, never the color
for (const m of ["range", "trend", "date", "flow"]) {
  mode = m;
  equal(color(crestG({}, 0.8)), color(crestG({}, 0)), `a cresting gage keeps its ${m}-mode color`);
}
mode = "trend";
equal(color(crestG()), TREND[2][1], "in trend mode a cresting gage is still the steady gray");
mode = "range";

// the marker glyph
const glyph = g => glyphFor(g);
check(glyph(crestG()).includes("<svg") && !glyph(crestG()).includes("▬"), "cresting: the wave, not the bar");
check(glyph(crestG({}, 0)).includes("▬") && !glyph(crestG({}, 0)).includes("<svg"), "steady at the high: the bar");
check(glyph(crestG({rate: 0.05}, 0)).includes("▲") && !glyph(crestG({rate: 0.05}, 0)).includes("<svg"), "rising");
check(glyph(crestG({rate: -0.05}, 3)).includes("▼"), "falling");
equal(glyph(crestG({rate: 0.3}, 0)), '<span class="gl fast"><i>▲</i><i>▲</i></span>', "rising fast is still stacked");
equal(glyph({...crestG(), recent: null}), "", "no trend, no glyph");
equal(glyph(crestG({rate: null})), "", "no rate, no glyph");
const svgOnMarker = glyph(crestG());
check(svgOnMarker.includes('stroke="#fff"') && svgOnMarker.includes('stroke="#000"'), "white wave with a dark halo on a marker");
const svgOnLight = crestSvg(14, 9, "#222", null);
check(svgOnLight.includes('stroke="#222"') && !svgOnLight.includes("#000") && !svgOnLight.includes("#fff"), "dark wave, no halo, on a light background");
check(svgOnLight.includes('aria-label="cresting"'), "the wave is labeled for screen readers");
check(crestLegendLine().includes("<svg") && crestLegendLine().includes("at its crest"), "legend line shows the wave and says what it means");

// the popup's trend row
const trendRow = g => rowText(g).match(/Trend \(6 h\)[^]*?ft\/h/)[0];
equal(trendRow(crestG()), "Trend (6 h) Cresting +0.018 ft/h", "cresting popup row");
check(popup(crestG()).includes('stroke="#222"'), "the popup draws the dark wave");
equal(trendRow(crestG({rate: 0.002}, 0)), "Trend (6 h) ▬ Steady, at its 7-day high +0.002 ft/h", "steady at the high: says so");
equal(trendRow(crestG({rate: 0.002, range_pos: 50}, 40)), "Trend (6 h) ▬ Steady +0.002 ft/h", "steady elsewhere: just steady");
equal(trendRow(crestG({rate: 0.002, range_pos: 97}, 10)), "Trend (6 h) ▬ Steady +0.002 ft/h", "peak 10 h ago: neither cresting nor at the high");
equal(trendRow(crestG({rate: 0.05}, 0)), "Trend (6 h) ▲ Rising +0.050 ft/h", "rising at the high: no 'at its high' note");
equal(trendRow(crestG({rate: -0.05}, 3)), "Trend (6 h) ▼ Falling -0.050 ft/h", "falling is unchanged");
equal(atWeeklyHigh(crestG({}, 0)), true, "at the weekly high");
equal(atWeeklyHigh(crestG({}, 0.25)), false, "peak 15 minutes ago is past the high");
equal(atWeeklyHigh(crestG({range_pos: 94}, 0)), false, "not in the top 5%");
equal(atWeeklyHigh({...crestG(), recent: null}), false, "no recent statistics");

// ---- the retired-gage popup ---------------------------------------------------------------
const retiredG = {site: "02298300", name: "Peace River At Punta Gorda FL", status: "retired",
                  last: "2005-09-29", first: "2002-09-30", params: ["stage", "water temperature"]};
const popupHtml = retiredPopup(retiredG);
check(popupHtml.includes("<b>Retired</b>"), "retired status shown");
check(popupHtml.includes("2005-09-29") && popupHtml.includes("years ago"), "last data and how long ago");
check(popupHtml.includes("2002 to 2005"), "record span");
check(popupHtml.includes("stage, water temperature"), "what was measured");
check(popupHtml.includes("USGS-02298300"), "link to the USGS page");
check(retiredPopup({...retiredG, status: "offline"}).includes("Offline (not reporting stage now)"),
      "offline status shown");
check(!retiredPopup({...retiredG, params: []}).includes("Measured"), "no empty Measured row");
const hostile = retiredPopup({site: '1"><img src=x onerror=alert(1)>', name: "<script>alert(1)</script>",
                              status: "retired", last: "2005-09-29", first: "2002-09-30",
                              params: ["<b>x</b>"]});
check(!hostile.includes("<img") && !hostile.includes("<script>") && !hostile.includes("<b>x"),
      "retired popup escapes hostile fields");

// ---- confluence popup ---------------------------------------------------------------------
const cf = (rivers, nearest) => ({lat: 40.9, lon: -80, rivers, nearest});
const near = (river, site, name, miles) => ({river, site, name, miles});
check(confluencePopup(cf(["Shenango River", "Mahoning River", "Beaver River"], []))
        .includes("Shenango River, Mahoning River and Beaver River meet here."), "three rivers are listed with a final 'and'");
check(confluencePopup(cf(["Peace River", "Horse Creek"], [])).includes("Peace River and Horse Creek meet here."),
      "two rivers are joined with 'and'");
check(confluencePopup(cf(["Peace River"], [])).includes("Peace River meet here."), "a lone name is still shown");
check(confluencePopup(cf(["A", "B"], [])).includes("River confluence"), "the popup has a heading");
check(!confluencePopup(cf(["A", "B"], [])).includes("Nearest gage"), "no nearest-gage table when none were found");
check(!confluencePopup({lat: 1, lon: 2, rivers: ["A", "B"]}).includes("Nearest gage"),
      "a confluence without a nearest list does not break the popup");
const withGages = confluencePopup(cf(["A", "B"], [near("A", "02297345", "Peace River At Platt FL", 1.8),
                                                  near("B", "02297310", "Horse Creek Near Myakka FL", 11.3)]));
check(withGages.includes("Nearest gage on each") && withGages.includes("Peace River At Platt FL"), "nearest gages are listed");
check(withGages.includes("USGS 02297345, 1.8 mi away") && withGages.includes("USGS 02297310, 11.3 mi away"),
      "each nearest gage shows its site and distance");
const withBelow = confluencePopup(cf(["A", "B"], [near("A", "1", "A gage", 2.5), {...near("B", "2", "B gage", 8.8), below: true}]));
check(withBelow.includes("USGS 2, 8.8 mi away, below the confluence"), "a gage below the meeting says so");
check(!withBelow.includes("USGS 1, 2.5 mi away, below"), "a gage on the river's own side does not");
const hostileCf = confluencePopup(cf(['<img src=x onerror=alert(1)>', "<script>alert(1)</script>"],
                                     [near("<b>x</b>", '1"><i>', "<script>y</script>", 1)]));
check(!hostileCf.includes("<img") && !hostileCf.includes("<script>") && !hostileCf.includes("<b>x") && !hostileCf.includes("<i>"),
      "confluence popup escapes hostile river and gage names");

equal(esc("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;", "esc tags");
equal(esc(`"quote" & 'apos'`), "&quot;quote&quot; &amp; &#39;apos&#39;", "esc quotes and ampersand");
equal(esc(42), "42", "esc coerces numbers");

if (failures.length) {
  console.log(failures.join("\n"));
  process.exit(1);
}
console.log("page logic ok");
