---
name: sentinel-aircraft-ships
description: >-
  Find aircraft and ships in Sentinel-2 (optical) and Sentinel-1 (radar) satellite
  imagery, anywhere in the world. Use when someone asks whether an aircraft (by ADS-B
  hex, registration or callsign) shows up in a Sentinel-2 pass, which aircraft made a
  colour-fringed (red/green/blue) streak in a Sentinel-2 image, when a satellite pass
  imaged a place, whether a satellite image matches an ADS-B track, to look for a ship
  (for example one a helicopter was operating from), or about a Sentinel-1 / SAR / radar
  image (VV, VH), including whether something in one is real or an artefact (ghosts /
  azimuth ambiguities, multipath, sidelobes, "is that another ship?"). Use it even for
  a Copernicus Browser screenshot or a "satellite image" that doesn't name Sentinel. Covers scene lookup, the per-pixel imaging time, the band-by-band colour
  fringe of moving aircraft, parallax, false matches, radar ship detection by day or
  night and through cloud, fixed objects, ship sightings and speed, and how to report
  the result.
compatibility: >-
  Needs uv and network access. Imagery comes from public, no-account sources (Earth
  Search, Microsoft Planetary Computer, Copernicus Data Space catalogue, Copernicus DEM
  on AWS). ADS-B lookups by hex and `identify`, and OSM structure flags in radar ship
  detection, use the Hexbird HTTP API ($HEXBIRD_TOKEN); `find --track CSV` works with
  ADS-B from any source.
---

# Aircraft and ships in Sentinel-2 and Sentinel-1 imagery

Sentinel-2 (optical, most of this guide) is the one for aircraft, and for ships in clear daylight.
Sentinel-1 (radar, "Sentinel-1" near the end) sees ships at dawn and dusk, at night and through
cloud, but not aircraft.

Sentinel-2 (S2A, S2B, S2C) images the Earth at 10 m per pixel in a 290 km swath, on daytime
descending passes around 10:00-11:30 local solar time, revisiting a given place every 2-5 days.
Moving aircraft show up as a short streak of separate blue, green and red copies (the bands are
imaged fractions of a second apart). Ships show up as bright hulls with wakes. With an ADS-B track
you can predict exactly where an aircraft should appear and check it to within a pixel.

## Tools and data sources

Two scripts in `scripts/`, relative to this skill's base directory. Run them with `uv run`, which
installs their dependencies (numpy, scipy, pyproj, rasterio, requests, matplotlib) from the inline
script header:

```
S2=<skill base directory>/scripts/s2tool.py
S1=<skill base directory>/scripts/s1tool.py
uv run $S2 scenes 50.719 -0.209 2026-10-13 2026-10-16
```

Where the data comes from:
- **Imagery** is all public and needs no account. Sentinel-2 L2A from the Earth Search STAC
  (Element84, collection `sentinel-2-c1-l2a`) and AWS open data; Sentinel-1 from Microsoft
  Planetary Computer (RTC, or GRD that the tool geocodes and calibrates itself); passes not yet
  processed are listed from the Copernicus Data Space catalogue; terrain and the land mask from
  the Copernicus GLO-30 DEM on AWS; the geoid from PROJ.
- **ADS-B** for `s2tool.py find` can come from any source as a CSV: `--track track.csv` with
  columns `time,lat,lon,alt_geom_ft,alt_baro_ft` (time as ISO 8601 or epoch seconds; either
  altitude may be blank). This needs no account. `find --hex HEX` and `identify` fetch ADS-B from
  the Hexbird HTTP API (`$HEXBIRD_API_BASE`, default `https://hexbird.io`, and `$HEXBIRD_TOKEN`).
  `identify` searches every aircraft near a point, so it needs Hexbird; without a token, get the
  candidate tracks from your own source and run `find --track` on each.
- **Mapped structures** (bridges, turbines, platforms, buoys) for `s1tool.py ships` come from
  OpenStreetMap through the Hexbird HTTP API. Without `$HEXBIRD_TOKEN`, `ships` says "OSM
  structures unavailable" and relies on the other-pass check for fixed objects. That check
  catches wind farms and platforms but not bridges, which move between orbits (see Sentinel-1
  below), so look harder at targets near bridges.

`s1tool.py` caches downloads in `$S1TOOL_CACHE`, or `~/.cache/s1tool` by default.

## Sentinel-2 commands

```
s2tool.py scenes LAT LON START END [--max-cloud 60]   # passes over a point, with the time the point was imaged
s2tool.py when ITEM LAT LON                           # imaging time, viewing angle and detector at a point
s2tool.py find ITEM --hex HEX [--out DIR]             # is this aircraft in the image? fit + overlay PNG
s2tool.py find ITEM --track track.csv                 # same with any ADS-B source (time,lat,lon,alt_geom_ft,alt_baro_ft)
s2tool.py identify ITEM LAT LON [--radius-km 15]      # which ADS-B aircraft made the streak near this point?
s2tool.py crop LAT LON SIZE_M ITEM [ITEM ...] [--grid N] [--nir] [--out PNG]   # images to look at
s2tool.py ships ITEM LAT LON SIZE_M [--compare ITEM2] [--out PNG]   # ship candidates as a contact sheet
s2tool.py ship-motion ITEM1 LAT1 LON1 ITEM2 LAT2 LON2 # speed and course from two sightings
```
ITEM is an Earth Search item id such as `S2B_T30UXA_20261014T110010_L2A`; get it from `scenes`.
`scenes` also lists passes already on Copernicus Data Space but not yet on Earth Search (the
first hour or two after a pass): the pass exists, but no tool can read it yet; try again later.
`find`/`identify` write a JSON result and an overlay PNG (to `s2out/` by default) that is good to
show people: the image, the ADS-B path where the satellite sees it, and the predicted band
positions. `crop` and `ships` write `crop.png` and `ships.png` in the working directory unless you
pass `--out`.

Typical times: `scenes` 5-15 s, `when` 3 s, `find` 10-30 s, `identify` 5-15 s per nearby aircraft
(keep the radius small in busy airspace), `crop` 5-15 s per scene at any size (wide crops are read
downsampled), `ships` 10-25 s for a 30 km box. Use these tools rather than writing your own
scanners: reading full-resolution bands over tens of km in your own script takes many minutes.

## Timing: the trap everyone falls into

- The time of a scene is the tile's `SENSING_TIME` (granule metadata; Earth Search's `datetime`).
  Never use the time in the datatake id (`GS2B_20261014T110009_...`) or the product name: that is
  when the satellite started imaging the whole strip, often thousands of km and many minutes away.
- A tile is 110 km tall and takes ~15 s to image; points further north are imaged earlier. The
  tool's model: `t = SENSING_TIME + 0.3 s - 0.126 s per km north of the tile centre`, good to
  ~2.4 s rms (worst 5.6 s). `scenes` and `when` print it. Use the fitted time from `find` when you
  have an aircraft: it is good to ~0.1 s.
- Compare against ADS-B at that moment, not at closest approach. An orbiting aircraft passes the
  same spot many times; only the pass at the imaging time counts, and its direction of travel must
  match the fringe.
- Two satellites can image the same place minutes apart (S2A/S2B/S2C overlap, adjacent orbits).
  Check which scene the user is looking at; an aircraft is usually in only one of them.
- Copernicus Browser shows the date and, with its time filter, roughly the pass time; find the
  matching item with `scenes`.

## How an aircraft appears (why the fringe looks the way it does)

- Band timing after the first band: B08 (NIR) +0.264 s, B03 (green) +0.527 s, B04 (red) +1.005 s.
  On odd-numbered detectors B02 (blue) comes first; on even-numbered detectors the order is
  reversed (B04 first). The 12 detectors are strips across the swath, so the colour order of a
  streak flips depending on where in the swath it is. `when` and `find` report the detector.
- Each band shows the aircraft where it was at that band's time, displaced away from the
  satellite by `height x tan(view zenith)` (view zenith 0-12 deg: ~0.7-1.3 km of displacement at
  FL350). The orthorectified image puts the ground right; aircraft are not on the ground.
- Height is above the terrain the image was orthorectified to: ADS-B geometric altitude (WGS84
  ellipsoid) minus geoid minus terrain. The tool does this. A fixed sea-level assumption is fine
  at sea and badly wrong over high ground (Denver is 1,600 m up).
- Fringe = motion during ~1 s plus inter-band parallax. The parallax runs roughly along the
  satellite's ground track (about north-south) with size ~height x 0.0095 (about 100 m at FL350),
  so a high aircraft flying east-west shows a fringe tilted 20-25 deg off its track, and a slow
  high one more. This is expected; `find` predicts the tilt and the measured fringe should match it.
- Typical streaks: airliners at cruise 200-350 m long (20-35 px); a light aircraft at 1,000 ft
  ~40 m (4 px). An aircraft on the ground has no fringe at all.
- High aircraft often trail a contrail. It shows as three colour-offset parallel stripes; the
  aircraft is the dot at the head. A match on the stripes means the aircraft is there but the fit
  locked onto its contrail.

## Workflows

**Did the satellite catch aircraft X?** `scenes` for the place and date (pick low-cloud scenes),
then `find ITEM --hex HEX` (or `--track` with your own ADS-B). Read the verdict and flags, then
look at the overlay PNG yourself.

**What made this streak?** (someone shares a Sentinel-2 crop with a colour fringe) Get the scene
(`scenes` at their coordinates and date; ask which pass if there are two), then `identify ITEM LAT
LON --radius-km 5-15`. A real match has its image position within ~100 m of the streak, time
within the model's +-7 s, four bands seen, a matching fringe direction, and few flags. If nothing
matches: aircraft without ADS-B (many military, some GA), a balloon, a bird flock, or a different
scene. Say what you checked.

**Find a ship** (for example the ship a helicopter was working with):
1. Use ADS-B for hints. Helicopters that descend to deck height offshore and stay there, tracks
   that end offshore, and low slow orbits mark ships. Read frozen positions correctly: when ADS-B
   goes altitude-only, the reported lat/lon stops updating (`seen_pos` grows). The last fix is a real
   position at the time it was received (if that is offshore, the aircraft was offshore). After it,
   the aircraft is somewhere it could reach from that fix in the time since; if altitude reports keep
   coming and stay low (a few hundred ft, or deck height), it is still close by and low, on or near a
   ship, and still in range of the same receivers. Start the ship search around the last fix, out to
   the distance it could fly before it went low, and say what the frozen data does and doesn't show.
2. Search the clue's own area, not some other aircraft's patch: run `scenes` at the clue's
   coordinates to get the tile(s) covering it. A box can run off a tile: the part outside shows as
   no data (black or blank) in crops and yields nothing in `ships`. If any of your area is missing,
   run `scenes` at a point in the missing part and search that neighbouring tile too (tiles overlap
   by ~10 km; the same pass images neighbouring tiles seconds apart).
3. Pick the search box from those clues (where a helicopter could have got to from its last fix,
   a track's end, a loiter), at most a few tens of km. Run `ships ITEM LAT LON SIZE_M --compare
   ITEM2` (ITEM2 = another scene of the same tile, to drop turbines and platforms) and look at the
   contact sheet it writes: it shows up to 24 one-km chips of bright compact objects on water away
   from cloud fields. Judge each one yourself; most are small clouds. Then `crop ... 1500` around a
   good candidate at full resolution to see the hull and wake. For context or big wakes, `crop` at
   6-40 km (downsampled: ships vanish at that scale, wakes don't always).
4. Ships are bright compact hulls with a wake trailing behind. In the case we checked, the
   brighter end of the streak was the bow; confirm the heading with a second sighting. Clouds are
   fluffy with dark shadows offset towards the sun's opposite side; wind turbines, platforms and
   buoys are fixed points (often with a long thin shadow) that appear in every scene.
5. With two scenes minutes apart (S2A/S2B/S2C overlaps), the ship has moved: at 10-20 kt about
   3-6 km in 10 min. Search the whole reachable disc, starting along the wake direction. Then
   `ship-motion` gives speed and course; it should agree with the wake heading. Run it before
   judging a candidate's speed or type: don't rule a ship in or out (as "a fast ferry" or "too
   fast/slow to be the one") from its wake or looks. A long, bright wake can come from a ship
   making 10 kt.
6. Size: hull length is hard to read at 10 m; the measured extent includes wake. Treat length as
   +-50 m at best and never name a ship from imagery alone. AIS or an official source names ships.

**Look at anything else** (an airfield, a carrier, a runway): `crop` with `--grid` and look. Use
`--nir` (NIR false colour) to separate vegetation and water.

## Judging a match

`find` sets `detected` only with 3+ bands, significance >= 6, a plausible time, not on cloud, and
a fringe in the predicted direction. Read the flags:
- `imaging time ... from the timing model`: almost always a false match (field edges, roads, cloud).
- `match sits on cloud`: false, even if the time looks fine.
- `low aircraft over land`: ground texture can mimic a fringe. Check the overlay by eye; real ones
  have three clean, equally spaced colour blobs on the predicted crosses.
- `ADS-B track does not cover`: positions are too sparse near the imaging time; the fit is weaker.
- `fit_rms_m` under ~15 m and measured fringe within ~10-15% and ~5 deg of the prediction are strong.

Always look at the overlay before saying an aircraft is or isn't there. An aircraft that isn't
found may be under cloud, over bright or busy ground, too low and slow, or outside the scene.

## Reporting

- State the scene and the moment: "Sentinel-2B, 14 Oct 11:07:15Z", and that this is the per-pixel
  time, not the strip time.
- Say what matched: position, direction and speed from the fringe vs ADS-B ("170 kt toward 035
  deg vs ADS-B 173 kt on 036"). If it fits to ~10 m with all bands, say it's a confident match;
  don't call it "plausible, not proof" when it isn't.
- If you only had the image and no ADS-B match, say it's an unidentified moving object.
- If someone else found the image, credit them.
- Include the overlay PNG (`find` / `identify` write it): it shows the match better than words do.
- Use the fitted time from `find` (e.g. 11:07:15Z) as the moment the aircraft was imaged, not the
  model estimate.
- Credit line for any image you share: "Contains modified Copernicus Sentinel data [year]".

## Sentinel-1 (radar): ships by day or night, through cloud

```
s1tool.py scenes LAT LON START END                      # radar passes over a point, imaging time there
s1tool.py when ITEM LAT LON [--sog KT --cog DEG [--at TIME]]   # geometry; where a moving ship appears
s1tool.py ships ITEM LAT LON SIZE_M [--min-db -6] [--out DIR]  # ship candidates, contact sheet + JSON
s1tool.py crop LAT LON SIZE_M ITEM [ITEM ...] [--pol vv|vh] [--out PNG]
```
ITEM is a GRD id such as `S1D_IW_GRDH_1SDV_20261012T170109_20261012T170134_004985_009693`, from
`scenes`. `ships` writes `s1_ships/s1_ships.png` and `s1_ships.json` unless you pass `--out`.
Typical times: `scenes` 10-30 s, `when` 2 s, `ships` 1.5-3 min for a 40 km box (it also reads
three earlier passes; `--compare 0` skips them), `crop` 20-60 s per scene.

When it helps: Sentinel-1C and 1D pass near 06:00 (descending) and 18:00 (ascending) local solar
time, never at Sentinel-2's ~10:30, and see a place every 1-3 days. Use it when Sentinel-2 is
cloudy or dark, or for a ship's position at dawn or dusk (with Sentinel-2, a track across the
day). It can't confirm a Sentinel-2 sighting at the same moment. Passes appear ~4 h after
imaging; `scenes` also lists ones so far only on Copernicus Data Space.

Reading it:
- VV (sent and received vertical) is the main channel: ships, platforms and turbines are bright, and
  a rough sea is brighter than a calm one. VH (cross-polarised) is darker and less affected by
  waves; ship superstructure shows well in it. A bright cross around a target is the radar's
  sidelobes, not its shape. Pixels are 10 m, resolution ~20 m: no colour, type or name.
- `ships` flags targets also seen in earlier passes (fixed, or a ship anchored there for days) and
  targets at mapped structures (bridges, turbines, platforms, buoys; needs Hexbird, see above).
  Look at the contact sheet: a ship is a compact blob or a streak; a moving one is often smeared
  along the satellite track. Report "fixed" as a flag rather than silently dropping the target.
- Bridges sit tens of metres up, so each orbit displaces them differently towards the satellite and
  their returns vary pass to pass; the other-pass check misses them and only OSM catches them.
- Ghosts: a very bright target (a big ship, a platform, a bridge, port cranes) makes faint copies
  ~4.6-5.8 km away along the satellite track, about 30 dB fainter and often smeared, so one can
  read as a long ship. A ghost is not an object. Over open water it looks exactly like a ship with
  no AIS. `ships` flags a candidate as a possible ghost when a target 25 dB or more brighter sits
  at that distance, and names it. Never report a flagged target as a ship, least of all a "dark"
  ship. In the benchmark 2 of 23 unexplained detections were ghosts of 300-400 m ships.
- Benchmark (99 AIS ships in Danish waters, Oct 2026): every ship >=100 m found, 14/16 at 50-100 m,
  19/26 at 15-50 m, 3/9 under 15 m; ~1.5 unexplained detections per 1000 km2. Small slow boats,
  ships alongside others and ships in harbour are often missed. `--min-db -12` finds more small
  boats at ~4x the false alarms, and more ghosts. A target with no AIS match may be a ship without
  AIS, or something the flags missed: say it is unexplained, not that it is a ship hiding.
- Moving targets are displaced along the satellite track by ~120 m per 1 m/s of speed along the
  radar's line of sight (up to ~500 m for a ship at 20 kt). To check whether a known ship (from
  AIS, or from Sentinel-2 and `ship-motion`) is a given detection, run `when ITEM LAT LON --sog KT
  --cog DEG --at TIME`: it dead-reckons to the radar's imaging time and applies the shift. Within
  ~100 m is a good match. Imaging times from `when` and `ships` are good to ~0.01 s.
- Aircraft in flight are displaced by kilometres and smeared: don't look for them in radar images,
  and don't read "not there" as anything. Parked aircraft show as bright targets.
- If someone shares a radar image (grey or false-colour, VV/VH labels, crosses around ships), the
  Sentinel-2 methods (bands, fringe, `s2tool`) don't apply to it.

**Checking a radar image**, for example "are these ghosts or other ships?". Run the tools before
answering; offsets and brightness read off a screenshot are unreliable:
1. Find the pass with `scenes`, from the place and the time on the image.
2. Run `ships` on a box covering the targets. It returns calibrated positions, dB and flags; the
   screenshot's stretch and scale are unknown.
3. For each pair (a bright target and a fainter one), compare their offset with the ghost
   geometry: `when` gives the track direction, and the ghost distance D is in the `ships`
   flags or ~4.6-5.8 km. A ghost is at D along track, within ~50 m across, and ~25-30 dB
   fainter, and all ghosts in one scene share the offset. Otherwise it's a real target.
4. Check other passes with `scenes`. Note which satellite and orbit each one is: S1C and S1D
   a day apart do not see from the same geometry, so an artefact won't repeat in the same
   place but ships travelling together will.

Multipath and sidelobe artefacts sit within ~100 m of the ship, never km away.
`references/validation.md` has a worked example of this recipe (two "offset dots" that turned out
to be real vessels) and the benchmark details.

Reporting radar results: "Sentinel-1D radar, 12 Oct 17:01:28Z", the target's position, and that
radar can't name a ship. Readers can't see the tool's numbering: give positions, or include
`s1_ships.png` if you refer to targets by number. Credit: "Contains modified Copernicus Sentinel
data [year]".

## Limits

Sentinel-2: 10 m pixels; nothing at night; cloud blocks it; tiles appear hours after the pass (Earth
Search L2A usually within ~2-6 h). Over land, bright or textured ground hides small aircraft. ADS-B
coverage and update rate limit the fit (17-20 s gaps are fine for straight flight, worse in turns).
Validated on 13 tiles over the English Channel, northern France and Belgium (S2A/B/C, Sep-Oct
2026; 22 aircraft from FL410 down to 950 ft, 7 false matches correctly flagged) and over Denver
(1,600 m terrain, land background, two low aircraft confirmed).

Sentinel-1: validated on one evening ascending pass over Danish waters (calm to moderate sea) plus
spot checks; small boats are under-sampled. See `references/validation.md`.
