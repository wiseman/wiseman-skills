# NISAR (L-band radar): products, access, aircraft and other targets

Read this when a question involves NISAR imagery, L-band radar, HH/HV, or parked aircraft in radar.
For radio interference with NISAR or Sentinel-1, read `rfi.md` instead.

## What NISAR is

NASA-ISRO SAR, launched July 2025. The L-band radar (about 1.25 GHz, 24 cm wavelength) is the one
with public data in NASA's archive; the S-band radar's data is only on ISRO's Bhoonidhi portal.
Left-looking, 12-day repeat, sun-synchronous dawn-dusk orbit: passes near 06:00 and 18:00 local solar
time at low latitudes (local times shift at high latitude; `scenes` prints them). Each observation
area is planned about twice per 12 days (one ascending, one descending), more where tracks overlap.

Over most land it records HH and HV (dual-pol, "DH"); some areas get quad-pol ("QP"), oceans and ice
often single-pol. Modes and pixel spacing of the main band (frequency A):

| Mode (name field) | Where | GSLC posting | GCOV posting |
|---|---|---|---|
| 20+5 MHz (`2005`) | most land | 10 m E x 5 m N | 20 m |
| 40+5 MHz (`4005`) | North America, parts of Asia incl. India | 5 x 5 m | 10 m |
| 77 MHz (`7700`) | mainly ice sheets | 2.5 x 5 m | 20 m |
| 5 MHz only (`0005`) | oceans, sea ice | 40 x 5 m | 80 m |

Frequency B is a 5 MHz side band near 1293.5 MHz (80 m GCOV): too coarse for targets.

## Products and releases

- GCOV (L2): geocoded, terrain-corrected backscatter (HHHH, HVHV as power). Easiest to read; use it
  for interference blobs and wide views.
- GSLC (L2): geocoded single-look complex (HH, HV). Finest detail; use it for aircraft, ships and
  sites. `crop` uses it by default and averages to about 10 x 10 m.
- RSLC (L1): radar-geometry SLC. Its metadata holds the RFI filter's record (`rfimeta`).
- BETA release (CRIDs X0500x): passes from Oct 2025 to Jan 2026, frozen. Uncalibrated (compare
  brightness only within one channel of one image) and no RFI removed. About 900 BETA passes were
  reprocessed into PROVISIONAL too, so both versions exist for them.
- PROVISIONAL release (CRID P05023, from July 2026): calibrated, RFI filtered (tone-rank), global
  and continuous since mid-June 2026, products about 2 days after the pass. ASF warns the filter
  sometimes removes real features in high-contrast areas, which may affect bright targets.
- A validated reprocessing is planned for late 2026; BETA may not stay online. Download what matters.

Granule name fields: `NISAR_L2_PR_GCOV_<cycle>_<track>_<A|D>_<frame>_<mode>_<pols>_<A|M>_<start>_<end>_<CRID>_...`.
Every tool command accepts any product's name and switches to the product it needs.

## Access

- Search needs no account (CMR). `nisartool.py scenes` lists passes, mode, pols, release.
- Reading data needs a free NASA Earthdata Login: `~/.netrc` with a `urs.earthdata.nasa.gov`
  entry, or `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`, or `EARTHDATA_TOKEN`. Without it,
  `crop`, `blobs` and `rfimeta` stop with instructions; say so and offer the public browse images.
- Files are 1.5-26 GB but cloud-optimised: windows stream in seconds (a 700 m GSLC crop ~8 s, a
  100 km GCOV blob search ~1.5 min). Never download whole files for a site question.
- Public browse PNGs (no login) give a quick look at whole frames:
  `https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/<SHORT_NAME>/<GRANULE>/<GRANULE>_LATLON.png`
  (PROVISIONAL; BETA uses `<GRANULE>.png`). SHORT_NAME is e.g. `NISAR_L2_GCOV_PROVISIONAL_V1`.
- Near the poles products use a polar stereographic grid (EPSG:3413/3031) rotated up to ~100 deg
  from north. `crop` resamples to north-up; if you read products yourself, do the same.
- Credit: "Contains NASA-ISRO NISAR data, NASA/JPL-Caltech/ISRO, distributed by ASF DAAC."

## Commands

Synopsis in SKILL.md; `nisartool.py <command> --help` lists every option. `crop` writes one row per
granule (HH, HV, HV-HH; VV, VH, VH-VV in VV/VH modes) north-up, with display ranges in the titles,
and caps crops at 20 km. `--markers` takes a CSV with `lat` and `lon` columns and an optional
`label` (an ADS-B track CSV works) and circles each point. Several granules of the same track 12 days
apart give a change comparison.

## Parked aircraft

What we found (Davis-Monthan storage yard, Helsinki-Vantaa, Rogachevo; details in `validation.md`):
- Rows of large aircraft parked in the open show as rows of distinct bright returns in HH and HV
  in the 40 MHz mode (5 m GSLC), from both pass directions. They match the rows in an optical image.
  Detection has not been measured aircraft by aircraft (no reliable per-aircraft truth).
- At airport gates (Helsinki, 20 MHz mode, 10 m) aircraft cannot be told from the piers, whose
  walls give a huge HH return.
- A single 737 on an open stand gave a bright return 15-19 dB above its surroundings, but several
  other objects nearby were as bright: the image alone did not identify it as an aircraft.
- HV showed fewer bright spots than HH (1,128 vs 1,703 in a 3 km box) and made long straight
  features (probably fences, roads) fade, so aircraft rows separated more cleanly. Whether HV
  improves detection is untested.
- Bright spots that stay put across same-track passes 12 days apart are fixed objects or aircraft
  that did not move; persistence alone can't tell which.

Suggested approach (untested as a whole): `crop` the apron in GSLC; find candidates in HH; use HV
and HV-HH to discount long walls and fences; compare same-track passes 12 days apart for what
changed; confirm with optical imagery (Sentinel-2 `crop`, commercial), ADS-B or another source
before calling anything an aircraft. Report what you saw ("bright compact returns in rows matching
stored aircraft"), not counts you didn't measure.

Expect results for large aircraft on open aprons, rows of aircraft, and the 40 MHz mode. Expect
trouble with fighter-sized aircraft in the 20 MHz mode (bright, maybe, but unrecognisable at 10 m),
aircraft at gates or next to buildings, and anything in hangars or shelters.

HV is the channel radio interference spoils most visibly; check the scene for interference blobs or
streaks (`rfi.md`) before trusting HV over a region, especially over Russia.

## Other uses

Ships: same `crop`; L-band sees ships well at sea, but there is no NISAR ship detector here (use
Sentinel-1 `ships` for detection). Sites and infrastructure: `crop` in GSLC; L-band penetrates
vegetation and dry sand somewhat, so HV is bright over forest and dark over smooth pavement and water.
