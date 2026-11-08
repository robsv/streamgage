# Where Pittsburgh's rivers meet

[Back to the README](../README.md)

Several rivers on one map, with a ring where they meet. This page shows the
[multi-river option](../README.md#several-rivers-at-once) on the rivers around Pittsburgh,
Pennsylvania: the Allegheny, the Monongahela, the Youghiogheny, the Kiskiminetas and the Beaver. It
is one real map, made on the evening of Saturday, November 7, 2026 (generated 01:47 UTC on the 8th).
All 45 gages with NWS flood stages were in the Normal category, so this is a page about the
confluences, not about a flood.

> **Not for safety decisions.** These are USGS provisional readings, drawn by a personal
> data-visualization tool. For flood warnings use the official NWS pages at
> [water.noaa.gov](https://water.noaa.gov).

The map was made with:

```
python3 streamgage.py "Allegheny River" "Monongahela River" "Youghiogheny River" \
    "Kiskiminetas River" "Beaver River" --state PA
```

## The map

79 gages along the five rivers, with the same colors, diamonds and glyphs as a single-river map
(see the README's [legend sections](../README.md#diamond-colors-three-modes)), and four purple rings
where the rivers meet. The rings are a layer you can switch off from the layers control at the
top left, and the title panel counts them.

![Map of the rivers around Pittsburgh, Pennsylvania, with 79 USGS gages as colored markers along five rivers and four purple rings where the rivers meet: at Pittsburgh, at McKeesport, near Freeport and where the Beaver River joins the Ohio; the legend at the lower right ends with a purple ring labeled River confluence](pittsburgh-confluences-map.png)

The four rings, north to south, with the place each one is on the ground:

| Marker says | Where it is | Known place |
|---|---|---|
| Allegheny River + Beaver River | 40.695, -80.289 | The Beaver joins the Ohio at Rochester, about 1 km away |
| Allegheny River + Kiskiminetas River | 40.679, -79.670 | The Kiskiminetas joins the Allegheny near Freeport, about 2 km away |
| Allegheny River + Monongahela River | 40.443, -80.018 | The Point in Pittsburgh, where the two make the Ohio, about 1 km away |
| Monongahela River + Youghiogheny River | 40.355, -79.871 | The Youghiogheny joins the Monongahela at McKeesport, about 2 km away |

The "known place" distances are against rough coordinates for each town, so they say the rings are
in the right place, not how precisely.

## A confluence popup

Clicking a ring names the rivers that meet there and the nearest gage on each, with its distance.
This is the Point, where the Allegheny and the Monongahela meet to form the Ohio:

![Popup for the confluence of the Allegheny and Monongahela rivers in Pittsburgh: "Allegheny River and Monongahela River meet here", nearest gages Allegheny R at L&D 2 (Lower Pool) at Aspinwall, 9.9 km away, and Monongahela R at Point State Park at Pittsburgh, 0.7 km away](pittsburgh-confluence-popup.png)

The nearest gage is the nearest reporting gage on that river's own side of the meeting, above it. A
river's walk carries on below the meeting, so without that rule the Allegheny's "nearest gage" here
would have been an Ohio River gage at Emsworth Dam, which is not on the Allegheny at all. If a river
has no reporting gage above the meeting, the nearest one below is shown and the popup says "below the
confluence".

## Names at a meeting

The Beaver's ring says "Allegheny River + Beaver River", though the Beaver joins the **Ohio**. The
Ohio is not on the list, so the marker borrows the name of the bigger of the two rivers that make
it (the Allegheny, by drainage area at its gage). The place is right; only the name is borrowed.

Adding `"Ohio River"` to the command changes the two northern rings: the Beaver's now reads "Ohio
River + Beaver River", which is right, but the one near Freeport reads "Kiskiminetas River + Ohio
River", though the Ohio only begins at the Point, about 40 km away in a straight line. NLDI files the Ohio's upstream
channel under the Allegheny, the way it files the Beaver's under the Mahoning, so where one channel
has two names either can come up. The rings stay in the same places in both versions.

## How the rings were checked

The confluence rules were run on four river systems, comparing the rings with known places:

| System | Rings | Result |
|---|---|---|
| Shenango, Mahoning, Beaver (PA) | 1 | One ring at the start of the Beaver, naming all three |
| Peace and its creeks (FL) | 3 | One for each of Joshua, Horse and Shell creeks; the Myakka correctly has none |
| Allegheny, Monongahela, Youghiogheny, Kiskiminetas, Ohio, Beaver (PA) | 4 | The four on this page, within about 2 km of the towns |
| Susquehanna, West Branch, Juniata, Swatara (PA) | 3 | Northumberland, Duncannon and Middletown, within about 2.5 km |

The Susquehanna turned up a mistake in an earlier version, which named the West Branch at the Juniata
and the Swatara. The Susquehanna gage is far downstream, so its walk is shorter than the West
Branch's, and the earlier rule took the longer walk for the bigger river. It now compares drainage
areas. The flowlines for all four systems are recorded in `tests/fixtures/nldi_paths.json`, so the
tests check them without the network.

## Making your own

The same command works for any rivers, and `--state` applies to every name:

```
python3 streamgage.py "Shenango River" "Mahoning River" "Beaver River" --state PA
python3 streamgage.py --anchor 03104515 --anchor 03099695 --anchor 03107500
```

The screenshots on this page are headless-Chrome screenshots of the generated page, as in the
[README's development notes](../README.md#code-layout-and-development), zoomed to the area; the
second one has the Point's popup opened.

## Things to know

- **The map is a snapshot.** Gage readings and the generated time are from the evening of
  November 7, 2026. A map made today looks different.
- **Two rivers, one channel.** Where NLDI files two named rivers as one channel (the Mahoning and the
  Beaver, the Allegheny and the Ohio), gages below the meeting count for both, and the name in a
  marker can be either. See [Several rivers at once](../README.md#several-rivers-at-once).
- **Only your rivers.** A ring marks where two of the rivers you listed meet. A tributary you did
  not name is not drawn and gets no ring.

Data: USGS National Water Information System (provisional), the USGS National Hydrography Dataset
through NLDI, and NOAA National Weather Service flood stages. Basemaps: Esri and the USGS National Map.
Map library: Leaflet.
