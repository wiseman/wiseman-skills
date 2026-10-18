# Validation notes

Background for the numbers in SKILL.md. Read this when you need to explain how far to trust a
result, or want a worked example of checking a radar image for ghosts.

## Sentinel-2 aircraft (13 tiles, 22 Sep - 14 Oct 2026, Channel / northern France / Belgium)

- Imaging-time model `SENSING_TIME + 0.3 s - 0.13 s/km north of tile centre`: rms 2.4 s, max
  5.6 s over 13 confirmed aircraft in 9 tiles (S2A/B/C). `find` searches +-7 s.
- Band spacing measured on one aircraft matched the band timing ratios (0 : 38 : 76 : 146 m).
- Band order reverses on even-numbered detectors (alternate detector modules are mounted
  reversed). Before this was modelled, every even-detector fringe pointed opposite to its ADS-B
  track; with it, all 22 confirmed detections agree in direction.
- 79 aircraft searched, 32 flagged, 25 confirmed by eye (22 aircraft + 3 contrail matches), 7 false.
  By height, flagged/searched: <1 km 5/13, 1-3 km 2/7, 3-6 km 0/7, 6-9 km 8/13, 9-14 km 17/39.
  Lowest: a Robin DR400 at 1,350 ft and a Tecnam twin at 950 ft over water.
- False matches were fields, roads, buildings and cumulus. Most had an imaging time more than 9 s
  from the model; the rest were low, slow aircraft over land or matches sitting on cloud with a
  plausible time. Hence the cloud and low-over-land flags.
- Many aircraft couldn't be tested: the ADS-B track didn't cover +-25 s of the imaging time
  (positions ~15-20 s apart) or the path left the tile.
- The same aircraft seen in two overlapping tiles comes out at the same absolute time (+-0.5 s).

### Ship found from a helicopter's ADS-B (14 Oct 2026)

A Wildcat helicopter's last position fix was 10:40Z at 50.736, -0.363; after that ADS-B was
altitude-only (100 ft at 10:44, 255-830 ft 11:08-11:28), consistent with a deck landing. The
frozen position was not its location. S2B at 11:07:04Z showed a ship with a wake at
50.7193, -0.2088, 11 km east of the last fix; S2A at 11:17:14Z showed it again at 50.6965, -0.2328:
3.05 km in 610 s, 9.7 kt on 214 deg, matching the wake axis and the bright (bow) end. Identity
unknown without AIS. Lessons: search the whole reachable disc between passes, use the wake axis and
bright end as a heading prior, and difference two scenes to reject turbines and platforms.

## Sentinel-1 ship detection (AIS benchmark, 15 Oct 2026)

Ground truth: Danish Maritime Authority AIS. Scene: S1D 12 Oct 2026 17:00-17:01Z ascending, five
60 km boxes (Læsø, Anholt, Storebælt, Samsø, Skagerrak), 15,200 km2 of open water, 99 AIS ships.

- Moving-target shift model (d = -R * v_range_rate / V_sat along track): detections sit a median
  26-48 m from the predicted position (90%: 57-114 m), against 86-103 m from the raw AIS
  position. Ships up to 470 m off their AIS position land in place.
- Detector progression (VV peak >= -6 dB, contrast >= 8 dB; false alarm = detection with no AIS
  ship within 300 m, an upper bound because some are ships without AIS):

  | change                                                       | ships found | FA per 1000 km2 |
  |--------------------------------------------------------------|-------------|-----------------|
  | block-median background, DEM land mask, merge blobs < 150 m  | 84/98       | 12.8            |
  | + fixed = seen in other passes                               | 83/98       | 3.4-3.8         |
  | + 3 same-orbit passes, 120 m radius (turbine smear)          | 83/98       | 3.2             |
  | + OSM structures within 150 m (bridges, turbines, seamarks)  | 83/98       | 2.7             |
  | + VH peak >= -20 dB                                          | 83/98       | 1.5             |

- By AIS length: <15 m 3/9, 15-50 m 19/26, 50-100 m 14/16, >=100 m 47/47. With `--min-db -12`:
  90/98 (7/9 small boats) at 5.6 FA per 1000 km2.
- The Anholt wind farm (111 turbines) was 109 of 143 bright false alarms before the fixed check.
- The Storebælt bridge (deck ~65 m up) is displaced towards the satellite by different amounts on
  different orbits, and its returns vary; only the OSM check removes it. Without OSM, the bridge
  area gave 46 unexplained targets.
- A ship at anchor in the same spot for days was flagged "fixed" (1 of 99).
- Near other countries' coasts, ships outside Danish AIS coverage show up as unexplained.
- End-to-end in `s1tool.py`: RTC and GRD inputs give the same scores; positions agree to 10-30 m.

## Azimuth-ambiguity ghosts (16 Oct 2026)

Ghost distance D = PRF * lambda * R / (2 * V_sat), per subswath: ~5.0 km (IW1), ~4.6 km (IW2),
~5.8 km (IW3).

- 2 of the 23 benchmark false alarms were ghosts. Each had a target ~30 dB brighter at D, within
  0-20 m along track of the prediction: one of a 398 m container ship at 12 kt (+31.5 dB, 4.74 km
  away), smeared along track to 410 m so that by its extent it read as a large ship with no AIS;
  one of a 299 m ship at anchor (+24.7 dB, 4.59 km away).
- Controls: at 0.6 D and 1.5 D, 0 of 23 false alarms had such a target. Among AIS-matched ships
  fainter than +5 dB, 1 of 28 had a brighter target at D, only 21 dB brighter. The ">= 25 dB
  brighter at D" rule flags both ghosts and no real ships.
- Ghosts sit ~30 dB below the parent, so only targets above about +24 dB make ghosts above the
  -6 dB threshold: big ships and big structures, not most turbines. At `--min-db -12` more show.
- Ghosts of land structures can land on open water near coasts.

### Worked example: "are these offset dots ghosts?" (S1C, 9 Jul 2026 18:30Z, 54.25 N, 11.5 W)

Two bright ships (+14.3 dB and +11.4 dB) each had a fainter dot ~6 km behind them in a shared
radar screenshot. Checked with the tools, they are not ghosts:
- Along track: 5.68 and 6.07 km, against D = 4.62 km here (IW2). Real ghosts matched D to ~10 m.
- Across track: 290 and 580 m, against 20-40 m for real ghosts.
- Brightness: only 9.6 and 10.8 dB fainter, against ~30 dB. A ghost of a +14 dB ship would be
  ~-16 dB, lost in the sea clutter.
- The two pairs had different offsets; ghosts in one scene share one offset.
- The previous day's pass was S1D on a different orbit (incidence 46 deg vs 38 deg). The same
  formation in a different geometry argues for real ships.

So they were probably smaller vessels keeping station behind the two ships. Answering from the
screenshot alone got the offsets wrong (2.8-4.5 km) and wrongly assumed the same orbit a day later;
running `scenes` and `ships` got it right in about two minutes.

## NISAR (18 Oct 2026)

The supporting research scripts and data for these checks are unpublished and are not included
with the skill. The findings and limitations are summarized below.

Offline code regressions live in `tests/test_nisartool.py` in the repository; run
`uv run tests/test_nisartool.py`. Synthetic grids check that blob searches exclude sources outside
the requested square in UTM and polar projections and flag blobs cut by its boundary. Synthetic
RFI records check unavailable analysis when all bins are excluded, preservation of level analysis
and other frequency bands, and ordinary flag detection. These verify software behavior, not
emitter-localization accuracy or detection rates on real passes.

### Interference blobs vs known emitters

- 42 FAA ARSR-4 sites (Wikipedia Joint Surveillance System list). In BETA (unfiltered) browse
  images, 11 of 40 covered sites showed a clear blob, 4 a faint one; none in recent PROVISIONAL.
- Search test at 14 sites, one BETA pass each (9-20 Jan 2026): window up to 120 km, offset at random
  up to 30 km east-west and north-south from the radar; brightest point of the 2 km-smoothed HV image
  found without the radar position. Brightest point within 1.4-8.2 km at 13 sites (median 6.1 km;
  6.2 km including the 14th). Key West: 20 km from the listed radar, 2.7 km from the Cudjoe Key
  tethered aerostat radar site (band unconfirmed). Brightness-weighted centre: 0.2-2.4 km at the 3
  sites where the bright area fitted inside the data; 1.2-19.4 km where it ran off or merged with
  bright coast. Small, selected sample; one pass per site.
- Pankovo test site (Novaya Zemlya), PROVISIONAL, 15 Oct 2026: blob centre 1-4 km from the village
  map point on ascending and descending passes (transmitter position unknown).
- `rfimeta` ground positions vs known emitters (PROVISIONAL), mapped with the Doppler-centroid squint
  (~960 Hz; it reproduces Ajo's ~2 s raw-time offset exactly): strongest hotspot 24 km (Ajo), 97 km
  (Melbourne), 17 km (Riverhead), 11 km (Cudjoe Key). Mapping the centroid of the strongest
  smoothed patch per chunk instead gave 18, 19, 1.7 and 0.9 km in the best chunk, but other chunks
  gave 100+ km false patches, and choosing the right chunk needs the answer. The flag record is a
  rough indicator, not a locator.

### RFI filter (tone-rank) impact

- Same acquisitions in BETA and PROVISIONAL: no visible radar blob after filtering at Ajo, San
  Clemente Island, Melbourne, Key West, Riverhead; Mount Laguna and Morales had no blob either way.
  Ajo's filter record still shows the radar as a patch about 7 s long near 1241.5 MHz.
- Kaliningrad (track 028 A, BETA 12 Nov 2025 vs PROVISIONAL 14 Oct 2026, during GPS degradation in
  ADS-B on both dates): a blob ~150 km across survives filtering; `signalMean` rises 25-30 dB over
  it while flags drop. All 6 recent PROVISIONAL Vladivostok passes checked show large blobs.

### Parked aircraft

- Davis-Monthan storage yard, 40 MHz GSLC (BETA), 3 and 7 Nov 2025: rows of large aircraft appear as
  rows of distinct returns in HH and HV on both pass directions, matching Sentinel-2 (4 Nov). No
  per-aircraft truth (Sentinel-2 at 10 m could not label grey aircraft on bright desert reliably).
  Bright spots >12 dB over a 200 m median, 3 km box: HH 1,703, HV 1,128 (ascending); 94% of HV spots
  are >=6 dB in HH.
- Helsinki-Vantaa, 20 MHz GSLC (BETA), 12 Jan 2026 02:35Z, ADS-B: 13 aircraft with before/after
  positions within 15 m, 10 at the piers, none separable from the pier return. A remote-stand 737:
  15-19 dB above its 300 m surroundings in HH depending on smoothing, but not unique nearby.
- Memphis FedEx hub: only 7 aircraft with ADS-B ground positions bracketing the pass: no test.
- Rogachevo (PROVISIONAL, Oct 2026): compact returns persist across two descending passes 12 days
  apart; identity unknown (paired optical image cloudy).
