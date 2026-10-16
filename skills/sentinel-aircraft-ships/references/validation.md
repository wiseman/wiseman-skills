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
