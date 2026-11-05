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

mode = "pct";
equal(color({method: "percentile", pct: 99.72}), PCT[4][1], "99.7th percentile is the top bin");
equal(color({method: "percentile", pct: 5}), PCT[0][1], "5th percentile is the bottom bin");
equal(color({method: "percentile", pct: null}), NODATA, "no percentile is no data");

mode = "trend";
equal(color(withRate(0.5)), TREND[4][1], "trend mode colors by rate");
equal(color(withRate(null)), NODATA, "no rate is no data in trend mode");

for (const m of ["range", "trend", "pct"]) {
  mode = m;
  equal(color({method: "nws", category: "minor"}), NWS.minor[0], `NWS circles ignore ${m} mode`);
  equal(color({method: "nws", category: "normal"}), NWS.normal[0], `NWS normal ignores ${m} mode`);
}

// ---- the bin tables themselves ------------------------------------------------------------
for (const [name, bins] of [["RANGE", RANGE], ["PCT", PCT], ["TREND", TREND]]) {
  const edges = bins.map(b => b[0]);
  check(edges.every((e, i) => i === 0 || e > edges[i - 1]), `${name} edges must increase`);
  check(bins.every(b => /^#[0-9a-f]{6}$/i.test(b[1])), `${name} colors must be hex`);
}
equal(RANGE.length, 5, "five range bins");
equal(PCT.length, 5, "five percentile bins");
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

equal(esc("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;", "esc tags");
equal(esc(`"quote" & 'apos'`), "&quot;quote&quot; &amp; &#39;apos&#39;", "esc quotes and ampersand");
equal(esc(42), "42", "esc coerces numbers");

if (failures.length) {
  console.log(failures.join("\n"));
  process.exit(1);
}
console.log("page logic ok");
