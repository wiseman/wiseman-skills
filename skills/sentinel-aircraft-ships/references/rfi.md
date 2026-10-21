# Radio interference (RFI) in radar imagery: detecting and locating emitters

Read this for questions about jammers, radars or other transmitters seen in Sentinel-1 or NISAR
images, "glows", "blobs" or "streaks" in SAR, or the NISAR RFI filter.

## Which satellite can see what

- NISAR L-band main band (frequency A) records 1219-1239 MHz (20 MHz mode), 1219-1259 MHz
  (40 MHz) or 1219-1296 MHz (77 MHz), plus usually a side band (frequency B) at 1291-1296 MHz.
  The rarer 5+5 mode records 1219-1224 and 1234-1239 MHz, and 20+20 adds 1276-1296 MHz (table in
  `nisar.md`). GPS L2 (1227.6 MHz) is inside the main band in the 20, 40 and 77 MHz modes, not in
  5+5. L-band air-surveillance radars (1215-1400 MHz, FAA/USAF ARSR-4 and others) appear when their
  channel falls inside the recorded band.
- Sentinel-1 C-band records about 5.355-5.455 GHz. It cannot see GNSS jammers (no GNSS frequency is
  in its band). It sees C-band radars such as Patriot (AN/MPQ-53/65) and some naval and air-defence
  radars, and other C-band SARs. Claims that Sentinel-1 shows GPS jamming are wrong unless the
  jammer also transmits near 5.4 GHz.

## What interference looks like

- Sentinel-1: a bright stripe right across a subswath (or all three), narrow along track. Its
  along-track position says the emitter was in the antenna beam at that moment, so the emitter lies
  near a line across the swath at that azimuth time; range tells you almost nothing. A fix needs
  crossing lines from other passes (ascending x descending, or adjacent tracks).
- NISAR: usually a roughly round blob tens of km across (sometimes elongated, or with sharp bands
  along the range direction), clearest in HV, because interference shows best against weak
  cross-pol returns (ASF). NISAR's receive beam steps across the swath as each echo arrives, so it
  hears an emitter only while pointing near it; the blob sits around the emitter. One pass gives a
  2-D position. Interferograms show it as decorrelation.
- Not every NISAR streak is interference: BETA products have calibration banding and parallel
  streaks over very dark areas (lakes, sand), especially in HV, and PROVISIONAL SLCs can show faint
  ripples over dark backgrounds (`nisar.md`, known issues).
- No-login first look: the Worldview/GIBS NISAR mosaic (URL in `nisar.md`) puts HV in green, so
  a strong blob shows as a saturated green patch (Kaliningrad, 4 Sep 2026). It has PROVISIONAL
  (filtered) data only, so filtered radars won't show.
- Hills inside a NISAR blob are probably a processing effect: GCOV's terrain correction applied to
  an even glow stamps the elevation model's slope pattern onto it (r = -0.86 between blob detail and
  the product's `rtcGammaToSigmaFactor` at Vladivostok). Don't read them as reflected RF.

## NISAR's RFI filter (tone-rank) and what it means for detection

- BETA products (a subset of passes, Oct 2025-Jan 2026): detection only, nothing removed. Interference stays in
  the image, but whether an emitter shows still depends on its frequency, whether it was transmitting
  toward the satellite, its strength, the geometry and the background: only 11 of 40 ARSR-4 sites
  showed a clear blob. No blob does not mean no transmitter.
- PROVISIONAL (every pass from 17 June 2026, plus selected frames back to Oct 2025): the filter
  was turned on with CRID P05023. The RSLC names the algorithm
  (`processingInformation/algorithms/rfiMitigation`, printed by `rfimeta`). It was `tone-rank` in
  every product we checked, dithered and fixed-PRF; the CRID notes also mention an ST-EVD method
  for dithered-PRF mode, so check the name before applying the description below. ASF: the filter
  "underperforms in some places by leaving residual RFI, or overperforms by removing features in
  high contrast areas". Tone-rank works on raw echoes before imaging. In blocks of 512 pulses
  it splits each 1,024-sample segment into frequency slices, estimates the typical level from the
  median, flags single pulse x slice cells about 9 dB above the estimated mean (12 dB above the
  median), and fills them from the neighbouring pulses.
- It suppresses tones, pulsed radars and frequency hoppers. In same-pass comparisons, radar blobs at
  Ajo, San Clemente Island, Melbourne, Key West and Riverhead were no longer visible. So: to find
  radars in PROVISIONAL products, use the filter's record (`rfimeta`), not the image.
- It struggles with interference spread across the band and present in every pulse (always-on
  broadband noise): that raises the median, so little is flagged. Blobs over Kaliningrad (during
  GPS jamming) and Vladivostok survive in PROVISIONAL images. A surviving blob is consistent with
  such a source but does not prove a jammer: corroborate with ADS-B GPS-quality reports (Hexbird
  NACp queries, or the gps-jammer-geolocation skill if available).
- The record is in the RSLC product under `metadata/RFI/frequency{A,B}/{pol}/raw*/`: `hitCount`
  (share of pulses flagged per time x range x 1,024 frequency bins, tens of MB), `signalMean` (the
  detector's typical level per time x range block, tens of kB), `interferenceSignalRatio`. A pass
  has several `raw*` chunks; read them all. Inside persistent broadband interference `signalMean`
  rises (+25-30 dB at Kaliningrad) while flags drop. GCOV `rfiLikelihood` is unreliable (values far
  outside its documented 0-1 range) and GCOV `rfiMitigationApplied=False` refers only to the GCOV step.
- Tones at 1233.1, 1239.0, 1293.5 and 1296.0 MHz are flagged on many passes everywhere we looked;
  `rfimeta` excludes bins whose pass-average flagged share exceeds `--tone-frac` (default 0.05),
  plus four neighbouring bins on each side. This is a background heuristic: exclusion alone
  does not prove a tone was present throughout the pass.

## NISAR commands

Synopsis in SKILL.md; `--help` lists every option.
- `blobs` searches GCOV cross-pol (HV, or VH in VV/VH modes) in a window around a point for up to
  `--max` blobs more than `--min-db` above background (2 km smoothing), and reports each blob's
  brightest point and brightness-weighted centre. `--size-km` is the side length of a square in
  local east/north coordinates: 120 means 60 km either side of the search point. Pixels outside
  it are excluded from detection and background estimation, including on rotated polar grids.
  The PNG retains the product's map orientation, so the masked search boundary may look rotated.
  Use the brightest point when the output says the
  blob runs off the data or window (the centre is then unreliable); use the centre when the whole
  blob fits. Writes PNG + JSON. Tested accuracy: the brightest point was within 1.4-8.2 km of 13 of
  14 known radars (one pass each; the 14th was 20 km off), so quote "within about 8 km in most
  tests", not a guaranteed accuracy.
- `rfimeta` maps the filter's flags and typical level over time x slant range, lists hotspots with
  their main frequencies, and converts them to ground positions allowing for the beam's forward squint
  (pass `--height` for an emitter on high ground). Writes PNG + JSON. The positions are rough: the
  strongest hotspot was 11-24 km from 3 of 4 known radars and 97 km from the fourth, and weaker
  hotspots are often spurious. Treat them as "an emitter was active in this region at this frequency".
  Check each frequency's JSON `flag_status` before interpreting `flag_hotspots`: `ok` means the
  analysis ran; `no_frequency_bins` means tone exclusion left no bins to analyze. The latter
  produces an explicit notice in the PNG and terminal, while the full spectrum and any available
  `signalMean` analysis remain usable. `frequency_bins_used`, `frequency_bins_total`, and
  `tone_frac` record the exclusion settings. An unavailable analysis is not a negative detection.
  Raising `--tone-frac` retains more bins, but changes the background assumption; disclose the
  setting when comparing runs rather than adjusting it solely to obtain a hotspot.
- Find candidate passes with `scenes`; check public browse images for blobs before streaming data.

## Workflows

**Is there a transmitter near X, and where exactly?**
1. `nisartool.py scenes` over X. Prefer BETA passes for radars (unfiltered); any release for
   always-on broadband sources. BETA covers only some passes before 20 Jan 2026. Unfiltered data
   for later passes exists only as raw L0B (RRSD, public since July 2026), which the tool doesn't
   read.
2. `blobs` around X on one or more passes. Report the brightest point (and centre if the blob fits),
   the peak in dB above background, the pass time and the tested accuracy (above). Agreement
   between passes has not been tested: two estimates can each be within 8 km yet 16 km apart, so
   don't reject a single fixed emitter because passes disagree by that much.
3. If several blobs: they may be several emitters, or one emitter heard through sidelobes. Check
   whether they repeat across passes.
4. In PROVISIONAL products with no blob, run `rfimeta` to see whether the filter removed something
   there: hotspots give time, range and main frequency, and only a rough ground position (see
   above). To pin it down, look for the same source in a BETA pass with `blobs`.

**What is this glow / streak in a Sentinel-1 image?**
1. Find the pass (`s1tool.py scenes`), look with `s1tool.py crop ... --pol vh` at a size that shows
   the whole stripe (wide crops are downsampled; up to tens of km).
2. The emitter is near the stripe's along-track position: draw a line across the swath through the
   stripe's centre, perpendicular to the ground track (`s1tool.py when` gives the track direction).
3. Repeat on a pass from another direction; the lines cross near the emitter. Same-direction passes
   give nearly parallel lines. Accuracy is a few km at best.
4. Sentinel-1 also writes `annotation/rfi/rfi-iw-<pol>.xml` (rfiMitigationApplied, per-burst and
   noise reports). Its burst metrics were high even over quiet Danish waters in our one check, so
   don't treat them as a detector without validation.

**Is this jamming?** Radar sees a transmitter, not its purpose. Combine a blob with independent
evidence: ADS-B NACp/NIC degradation at the same time and place, NOTAMs for GPS tests, known sites.
Earlier attempts to locate jammers from SAR streak geometry were off by 10-178 km (Kaliningrad,
Shiraz); the NISAR blob centre is the better method.

## Reporting

- Say which satellite, pass time (Z), direction, release (BETA unfiltered / PROVISIONAL filtered).
- Give positions with the method and its tested accuracy ("brightest point of the blob; within
  ~8 km of known radars in 13 of 14 tests"), not more precision than that.
- Say what the radar can't tell you (emitter type, purpose) and what corroborates it.
- Credit: "Contains NASA-ISRO NISAR data, NASA/JPL-Caltech/ISRO, distributed by ASF DAAC" (cite
  the collection DOI in a report, see `nisar.md`) and/or "Contains modified Copernicus Sentinel
  data [year]".

## Sources

NISAR: ASF NISAR Data User Guide, read in full Oct 2026:
[About NISAR](https://nisar-docs.asf.alaska.edu/nisar-intro/) (frequencies by mode),
[PROVISIONAL known issues](https://nisar-docs.asf.alaska.edu/provisional-known-issues/) (filter
residuals and over-removal), [BETA known issues](https://nisar-docs.asf.alaska.edu/product-known-issues/)
(what RFI looks like, non-RFI streaks), [Glossary](https://nisar-docs.asf.alaska.edu/glossary/)
(RFI detection on L0B before focusing), and the
[CRID release notes](https://www.earthdata.nasa.gov/data/platforms/space-based-platforms/nisar/nisar-composite-release-id-crid)
(P05023 turned on tone-rank; ST-EVD for dithered PRF). The tone-rank internals (512-pulse blocks,
thresholds) come from the open-source isce3 implementation
(https://github.com/isce-framework/isce3), not ASF, and every tested accuracy figure comes from our
own tests (`validation.md`).
