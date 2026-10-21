# NISAR (L-band radar): products, access, aircraft and other targets

Read this when a question involves NISAR imagery, L-band radar, HH/HV, or parked aircraft in radar.
For radio interference with NISAR or Sentinel-1, read `rfi.md` instead. Facts here come from ASF's
NISAR Data User Guide (sources at the end) unless marked as our own finding.

## What NISAR is

NASA-ISRO SAR, launched 30 July 2025. The L-band radar (24 cm wavelength) is the one with public
data in NASA's archive; S-band data (mostly over India) is on ISRO's Bhoonidhi portal. Left-looking
(full Antarctic coverage, a gap around the North Pole), 240 km swath, 12-day repeat, dawn-dusk
orbit: passes near 06:00 and 18:00 local solar time at low latitudes (`scenes` prints them).
Each observation area is planned about twice per 12 days (one ascending, one descending), more
where tracks overlap. The plan is reviewed every six months.

Each acquisition records a main band (frequency A) and usually a side band (frequency B, mainly
for ionospheric correction). Mode code = bandwidths of A then B in the granule name. Pixel spacing
(GSLC is always 5 m north; east spacing varies):

| Mode (name field) | Frequency A (MHz) | Frequency B (MHz) | GSLC posting (E x N) | GCOV posting |
|---|---|---|---|---|
| 20+5 (`2005`) | 1219-1239 | 1291-1296 | 10 x 5 m | 20 m |
| 40+5 (`4005`) | 1219-1259 | 1291-1296 | 5 x 5 m | 10 m |
| 77 (`7700`) | 1219-1296 | none | 2.5 x 5 m | 20 m |
| 20+20 | 1219-1239 | 1276-1296 | 10 x 5 m | 20 m |
| 5+5 (`0505`) | 1219-1224 | 1234-1239 | 40 x 5 m | 80 m |
| B only (`0005`) | none | 5 MHz | B: 40 x 5 m | 80 m |

Our CMR counts (3 days, Sep 2026): `2005`, `4005` and `0005` dominate; `0005` (oceans, sea ice,
mostly VV single-pol, pols `NASV`) is close to a third of granules; `7700` (mainly ice sheets),
`0505`, `2000` and `0500` also occur. Per the observation plan map, 40 MHz covers North America
and parts of Asia including India.
Frequency B datasets are posted at 80 m (GCOV) and have larger calibration residuals: too coarse
for targets. The tool's image commands read frequency A.

Polarisations (second name field after the mode, A then B; `NA` = band absent): `SH`/`SV` single,
`DH` (HH+HV) and `DV` (VV+VH) dual, `QP` quad, plus compact-pol codes the tool doesn't support. Most
land is `DHDH`. Quad-pol (`QP`) uses a fixed PRF, so **every QP image has bands of missing data
across the swath** (they move between cycles). A crop that lands in one shows "no valid data";
try the other pass direction or another cycle.

## Products and releases

- GCOV (L2): terrain-corrected backscatter as gamma-nought power (HHHH, HVHV), multilooked.
  Easiest to read; use it for interference blobs and wide views.
- GSLC (L2): geocoded single-look complex (HH, HV); |DN|^2 is beta-nought, not terrain-flattened.
  Finest detail; use it for aircraft, ships and sites. `crop` uses it by default and averages to
  about 10 x 10 m. Don't compare dB values between a GSLC and a GCOV crop.
- RSLC (L1): radar-geometry SLC. Its metadata holds the RFI filter's record (`rfimeta`).
- RRSD (L0B raw echoes, 10-75 GB per granule): public from the PROVISIONAL release, 2-10 h after
  the pass, also for some earlier passes. `rawfix` reads windows of it to locate emitters (`rfi.md`).
- BETA (pre-calibration, CRIDs X05007-X05010): a subset of passes from 12 Oct 2025 to 20 Jan 2026
  (up to 8 per track), not every pass. Uncalibrated (compare brightness only within one channel of
  one image) and no RFI removed.
- PROVISIONAL (CRID P05023 and higher, released 20 July 2026): calibrated, RFI filtered (tone-rank).
  Every pass from 17 June 2026, products 36-72 h after the pass. Also "supplemental" time series
  for selected frames back to late Oct 2025: in CMR about 5,400 GCOV acquisitions on ~370
  track-frames, ~940 of which also exist as BETA (our count, Oct 2026). So PROVISIONAL can exist
  for a 2025 pass; `scenes` lists both.
- The release comes from the CRID number, not its letter: some products in the PROVISIONAL
  collection carry `X05026` and are RFI-filtered (we checked one).
- **Permanent data gap: no data from 2026-07-27T22:03Z to 2026-08-10T00:55Z.**
- A reprocessed acquisition reappears with a higher product counter (`_002`); `scenes` keeps the
  latest.
- A validated reprocessing is expected to be complete by the end of 2026 and supersedes earlier
  versions. BETA may not stay online, so download what matters.
- Urgent Response (UR) products (`_UR_` instead of `_PR_` in the name) appear 2-6 h after the pass
  for disasters, with forecast orbits and no ionospheric or tropospheric geolocation correction.
  They are kept for 30 days. They are in separate collections that `scenes` does not search: use
  Vertex (Product Configuration: Urgent Response) or Earthdata Search (`nisar_ur`). `crop`, `blobs`
  and `rfimeta` should accept a UR name (untested: we have not seen a UR product).

Granule name fields: `NISAR_L2_PR_GCOV_<cycle>_<track>_<A|D>_<frame>_<mode>_<pols>_<A|M>_<start>_<end>_<CRID>_<orbit fidelity>_<F|P coverage>_J_<counter>`.
Every tool command accepts any product's name and switches to the product it needs.

## Known issues that matter here

PROVISIONAL (ASF, updated 30 July 2026):
- The RFI filter sometimes leaves residual RFI and sometimes removes real features in high-contrast
  areas, which may affect bright targets such as aircraft.
- At high latitudes ionospheric effects cause geolocation errors. Allow for them at Arctic sites
  (Rogachevo) and in blob positions there.
- Low-level ripples can appear in SLC data over dark backgrounds (calm water, tarmac).
- About 1-2% of products were made from incomplete raw data and can be shifted, misplaced or
  defocused. If a crop doesn't line up with an optical image, try another pass.
- Calibration-mode frames on track 161 (frames 174, 175) and track 169 (frames 090, 091) were
  unusable before August 2026.
- 40 MHz products taken jointly with S-band show visible radiometric bands.

BETA (ASF, 27 Feb 2026), besides being uncalibrated:
- Radiometric banding across the swath, and parallel streaks along or across track over very
  low-backscatter areas (lakes, sand), especially in HV. These are not interference.
- The valid-data mask is misaligned, so frame edges carry artefacts.
- The 77 MHz noise floor is higher than expected.
- Browse images can be mislocated by kilometres (the products themselves are accurate to about 5 m).

## Access

- Search needs no account (CMR). `nisartool.py scenes` lists passes, mode, pols, release.
- Reading data needs a free NASA Earthdata Login: `~/.netrc` with a `urs.earthdata.nasa.gov`
  entry, or `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`, or `EARTHDATA_TOKEN`. Without it,
  `crop`, `blobs` and `rfimeta` stop with instructions; say so and offer the public images below.
- Files are 1.5-26 GB but cloud-optimised: windows stream over HTTPS in seconds (a 700 m GSLC
  crop ~8 s, a 100 km GCOV blob search ~1.5 min). Never download whole files for a site question.
  Direct S3 access works only from AWS us-west-2.
- **No-login quick look at near-full resolution:** NASA Worldview/GIBS serves daily mosaics of
  PROVISIONAL GCOV (from 29 Oct 2025) at 15.625 m as false colour: HH (or VV) in red and blue, HV
  (or VH) in green. Rows of stored aircraft at Davis-Monthan and the broadband interference glow
  over Kaliningrad (saturated green) are both visible (our check, 4 Sep 2026). It shows the day,
  not the pass, so get the time from `scenes`. Fetch a PNG with:
  `https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=NISAR_L2_Geocoded_Polarimetric_Covariance&CRS=EPSG:4326&BBOX=<lat_min>,<lon_min>,<lat_max>,<lon_max>&WIDTH=1000&HEIGHT=1000&FORMAT=image/png&TIME=<YYYY-MM-DD>`
  (EPSG:4326 in WMS 1.3.0 takes the box as latitude first.)
- Public browse PNGs (no login) cover whole frames:
  `https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/<SHORT_NAME>/<GRANULE>/<GRANULE>_LATLON.png`
  for PROVISIONAL (also `_NATIVE.png` and single channels such as `_NATIVE_A_HVHV.png`, in the
  product's map grid). BETA uses `<GRANULE>.png`. SHORT_NAME is e.g. `NISAR_L2_GCOV_PROVISIONAL_V1`.
- Map grids: UTM between 60°S and 60°N; polar stereographic north of 60°N (EPSG:3413: Helsinki,
  Rogachevo) and south of 60°S (EPSG:3031), rotated up to ~100° from north. `crop` resamples to
  north-up; if you read products yourself, do the same. GDAL 3.13+ reads NISAR HDF5 directly.
- Citation: NASA asks for the dataset DOI, e.g. GCOV PROVISIONAL `10.5067/NIL2GCOV-P1`, GSLC
  PROVISIONAL `10.5067/NIL2GSLC-P1`, GCOV BETA `10.5067/NIL2GCOV-B1`. Short credit on images:
  "Contains NASA-ISRO NISAR data, NASA/JPL-Caltech/ISRO, distributed by ASF DAAC."

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
aircraft at gates or next to buildings, anything in hangars or shelters, and quad-pol passes
(data gaps).

Interference stands out most against weak cross-pol returns, so check the scene for interference
blobs or streaks (`rfi.md`) before trusting HV over a region, especially over Russia.

## Other uses

Change between dates: `crop` several granules of the same track (12 days apart) and compare. Don't
compare an ascending with a descending pass for change: they look from opposite sides, so buildings
change brightness whatever happened. Averaging a few passes before and after and subtracting showed
a demolished building at Disneyland as a clear dark patch. For a
short-lived object, check whether Sentinel-1 or the other NISAR direction passed within hours:
NISAR and Sentinel-1 both pass near 06:00 and 18:00.

Ships: same `crop`; L-band sees ships well at sea, but there is no NISAR ship detector here (use
Sentinel-1 `ships` for detection). Open ocean is mostly `0005` (80 m, VV only), so ships far
offshore may have no usable NISAR image. Sites and infrastructure: `crop` in GSLC; L-band
penetrates vegetation and dry sand somewhat, so HV is bright over forest and dark over smooth
pavement and water.

## Sources

ASF NISAR Data User Guide, https://nisar-docs.asf.alaska.edu/ (read in full Oct 2026, repo commit
785f734 of 9 Oct 2026): [About NISAR](https://nisar-docs.asf.alaska.edu/nisar-intro/) (bands,
frequencies, polarisations, quad-pol gaps, look direction),
[GCOV](https://nisar-docs.asf.alaska.edu/gcov/), [GSLC](https://nisar-docs.asf.alaska.edu/gslc/),
[Available data](https://nisar-docs.asf.alaska.edu/availability-overview/) (releases, dates, data
gap, latency), [PROVISIONAL known issues](https://nisar-docs.asf.alaska.edu/provisional-known-issues/),
[BETA known issues](https://nisar-docs.asf.alaska.edu/product-known-issues/),
[Projections](https://nisar-docs.asf.alaska.edu/projections/),
[Urgent Response](https://nisar-docs.asf.alaska.edu/urgent-response/),
[S3 access](https://nisar-docs.asf.alaska.edu/aws-s3-access/) (browse bucket),
[Worldview](https://nisar-docs.asf.alaska.edu/worldview/). Release notes per CRID:
https://www.earthdata.nasa.gov/data/platforms/space-based-platforms/nisar/nisar-composite-release-id-crid.
Mode and release counts, browse file names and DOIs come from our own CMR queries (Oct 2026).
