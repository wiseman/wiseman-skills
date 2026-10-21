# Radio interference (RFI) in radar imagery: detecting and locating emitters

Read this for questions about jammers, radars or other transmitters seen in Sentinel-1 or NISAR
images, "glows", "blobs" or "streaks" in SAR, or the NISAR RFI filter.

## Which satellite can see what

- NISAR L-band main band (frequency A) records 1219-1239 MHz (20 MHz mode), 1219-1259 MHz
  (40 MHz) or 1219-1296 MHz (77 MHz), plus usually a side band (frequency B) at 1291-1296 MHz.
  The rarer 5+5 mode records 1219-1224 and 1234-1239 MHz, and 20+20 adds 1276-1296 MHz (table in
  `nisar.md`). GPS L2 (1227.6 MHz) is inside the main band in the 20, 40 and 77 MHz modes, not in
  5+5. L-band air-surveillance radars (1215-1400 MHz: FAA/USAF ARSR-4, FAA CARSR and others) appear
  when their channel falls inside the recorded band.
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
- An always-on radar shows in the record on every pass: at Boron (an FAA radar) a hotspot appeared on
  all 48 PROVISIONAL passes in a year, 0.6-22 km from the radar (median 9.5 km), in both directions.
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
  hotspots are often spurious. Treat them as "an emitter was active in this region at this frequency",
  and use the hotspot as the starting point for `rawfix`. The hotspot's top frequency can be a
  background tone: at Boron it was the 1239.0 MHz tone on 8 of 48 passes, with the radar's frequency
  second or third, so read all three `main_freqs_MHz`.
  Check each frequency's JSON `flag_status` before interpreting `flag_hotspots`: `ok` means the
  analysis ran; `no_frequency_bins` means tone exclusion left no bins to analyze. The latter
  produces an explicit notice in the PNG and terminal, while the full spectrum and any available
  `signalMean` analysis remain usable. `frequency_bins_used`, `frequency_bins_total`, and
  `tone_frac` record the exclusion settings. An unavailable analysis is not a negative detection.
  Raising `--tone-frac` retains more bins, but changes the background assumption; disclose the
  setting when comparing runs rather than adjusting it solely to obtain a hotspot.
- `rawfix` locates an emitter from the raw echoes; see the next section.
- Find candidate passes with `scenes`; check public browse images for blobs before streaming data.

## Locating an emitter from the raw echoes (`rawfix`)

How it works: NISAR's receive beam is steered across the swath as each echo arrives, so inside any
received pulse the emitter's power peaks at the sample whose slant range points the beam at the
emitter. A fit of that hump over many pulses (common beam shape, free gain per pulse) gives the
slant range. The emitter's power over time (the azimuth envelope) gives when the beam crossed it,
and with the Doppler centroid from the pass's RSLC, the along-track position. The height comes
from the Copernicus DEM. The RFI filter acts only on processed products, so this works on every
PROVISIONAL pass that has raw data (RRSD, public 2-10 h after the pass).

- `rawfix LAT LON GRANULE [GRANULE ...]`: LAT LON only chooses what to read. It should be within
  about 10 km of slant range and a few seconds of beam time: an `rfimeta` hotspot or a blob centre is
  enough. Each GRANULE is any product name of a pass from `scenes`; the tool finds the raw granule
  (if the frame straddles two, the one with most data either side of the beam crossing). Granules
  that come from the same raw acquisition (two products or adjacent frames of one pass) are used once.
- It reads about 15 s of raw echoes over ~38 km of slant range in the 40 MHz mode (1.4 GB, cached;
  2-5 min), finds the
  strongest narrowband lines, detects complete pulses and takes the commonest long pulse. With
  linear chirps it keeps pulses that fit a chirp; with non-linear sweeps (CARSR) it uses all
  complete pulses. `--freq-mhz` picks another line.
- If it finds no pulses, it treats the source as continuous interference (`--kind noise`, for
  jammers): it streams the whole swath for ~50 s (tens of GB, tens of minutes, not kept) and fits the
  same cues to the power profile, over the whole recorded band unless `--freq-mhz` (with
  `--bw-mhz`) picks a band. The range correction below is not applied: it was measured on pulsed
  radars. Not tested against a known transmitter. At Kaliningrad two such
  fixes on different dates were 16 km apart; we could not tell two emitters from a bias for a very
  strong, 45 km-wide source.
- Accuracy: single-pass fixes were 0.1-2.7 km (median 2.0) from 8 known ARSR-4 radars with the range
  correction, which is leave-one-out (`validation.md`). The range cue reads short by 0.91 km on
  average (sd 0.70), and the tool adds that back to pulse fixes (`--no-bias-correction` to turn it
  off). The
  beam-crossing time is unbiased (sd 0.21 s, about 1.5 km along track). The printed "95% within"
  radius is a Monte Carlo over those spreads (about 3 km for one pass), not a guarantee.
- Several passes give a joint fix. Use both directions: the along-track errors of ascending and
  descending passes run in different directions and partly cancel. At Boron 4 passes gave a fix 1.3 km
  from the radar, against 1.5-5.3 km for single passes. Each pass is weighted by its spread (the
  validated one, or the cue's own if larger), so a shaky pass pulls less. If a pass disagrees by more
  than 3 times its spread the tool warns: check each pass, since there may be two emitters.
- Read the warnings. A hump or envelope cut by the edge of the data (swath edge, granule start)
  biases the cue; split halves of the pulses that disagree by over 3 km mean the range fit is
  unstable. The tested accuracy may not carry over to other emitters: on the strong, non-linear
  CARSR at Boron the cues were about twice as noisy as in validation and one pass was 5.3 km off.
  Clipped pulses (counted in the output) were common at good and bad sites alike.
- The output also characterises the signal (pulse length, linear-fit sweep rate, how well a linear
  chirp fits, other narrowband lines), which helps identify the emitter (below).

## Workflows

**Is there a transmitter near X, and where exactly?**
1. `nisartool.py scenes` over X. Recent passes are PROVISIONAL (filtered); BETA (unfiltered images)
   covers only some passes before 20 Jan 2026.
2. Is something there? On PROVISIONAL passes run `rfimeta`: hotspots give time, range, main
   frequency and a rough ground position. On BETA passes run `blobs`. Always-on broadband sources
   also survive as blobs in PROVISIONAL images.
3. Where is it? `rawfix` with the hotspot or blob as LAT LON, on two or more passes from both
   directions if you can. Report the fix, the tested accuracy (above) and any warnings. Without raw
   data (some early passes), fall back to the blob's brightest point (and centre if the blob fits)
   with its tested accuracy. Blob agreement between passes has not been tested: two estimates can
   each be within 8 km yet 16 km apart, so don't reject a single fixed emitter for that.
4. If several blobs or hotspots: they may be several emitters, or one emitter heard through
   sidelobes. Check whether they repeat across passes, and compare their frequencies.
5. What is it? See below.

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

**What is the emitter?** A position alone rarely identifies a transmitter; the signal often does.
- ARSR-4 (FAA/USAF long-range radar): single 88 us linear up-chirps at about 12.2 kHz/us (chirp-fit
  residual ~0.5 rad), carriers 1241.5-1257 MHz; a site can show two lines (two channels). Same at all
  8 sites tested.
- CARSR (FAA long-range radar, Boron): a ~117 us non-linear sweep (`rawfix` measures ~111 us at half
  power, a linear-fit 8.2 kHz/us and a poor chirp fit, ~1.6 rad), plus a ~20 us pulse 5.2 MHz lower
  that `rawfix` lists as a second line. Separate analysis also found it ~260 us after the long
  pulse, and three staggered pulse intervals (2744.5, 3157.0, 3472.7 us), as published for a CARSR
  in Puerto Rico.
- The waveform gives the type, the position the site: check site lists (Wikipedia's Joint
  Surveillance System list, FortWiki's CARSR list, OpenStreetMap towers) near the fix.
- A source on every pass for months is an always-on surveillance radar, not scheduled range activity:
  NOTAMs won't explain it.
- A radar near the fix in another band can't be the source: the KEYX weather radar (S-band) was 1.6
  km from the Boron fix.
- A second signal nearby is not necessarily the same transmitter: at Boron a CARSR-like signal at
  1294.5 MHz (frequency B) was not time-locked to the 1255.6 MHz radar. `rawfix` reads frequency A only.

**Is this jamming?** Radar sees a transmitter, not its purpose. Combine a blob with independent
evidence: ADS-B NACp/NIC degradation at the same time and place, NOTAMs for GPS tests, known sites.
Earlier attempts to locate jammers from SAR streak geometry were off by 10-178 km (Kaliningrad,
Shiraz); the NISAR blob centre is the better method.

## Reporting

- Say which satellite, pass time (Z), direction, release (BETA unfiltered / PROVISIONAL filtered).
- Give positions with the method and its tested accuracy ("raw-data fix; single-pass fixes were
  0.1-2.7 km from 8 known radars" or "brightest point of the blob; within ~8 km of known radars in 13
  of 14 tests"), not more precision than that.
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
