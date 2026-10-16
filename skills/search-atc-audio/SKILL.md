---
name: search-atc-audio
description: >-
  Find, download and listen to archived air traffic control radio (LiveATC) for a
  specific aircraft, airport and time, and work out what was said. Use when the user
  wants to know what an aircraft said to ATC, to identify an unknown or radar-only
  (TIS-B "~" hex) aircraft's callsign or type from the radio, to check a takeoff,
  landing, go-around, emergency or diversion against ATC audio, to "listen to
  LiveATC", or to correlate ADS-B tracks with tower/approach/ground transmissions.
  Covers picking feeds and 30-minute archive slots from ADS-B times, downloading
  LiveATC archives through the user's browser when the user asks for them,
  transcribing them locally for free with UTC timestamps, identifying callsigns without biasing the model, and
  cutting clips for the user to listen to.
compatibility: >-
  Needs the transcribe-audio skill, ffmpeg, uv, and access to the user's browser for
  LiveATC downloads (or user-provided archive files). Free local transcription needs
  macOS on Apple Silicon; elsewhere use the OpenAI backend (paid, OPENAI_API_KEY).
---

# Search ATC audio

Answer "what did this aircraft say / what is it called" from archived ATC radio.
Transcription itself uses the **`transcribe-audio`** skill's script; this skill covers
everything around it.

Before transcribing, load the `transcribe-audio` skill and resolve its
`scripts/transcribe.py` to an absolute path using that skill's base directory.
Use the host's skill discovery instead of assuming an installation path or that the
two skills are adjacent. If it is missing, ask the user to install both skills from
this repository. Run the script with `uv run`, keeping the user's project as the
working directory. Resolve this skill's `references/` relative to this `SKILL.md`.

## Workflow

1. **Get the times and place from ADS-B first.** Pin down takeoff, landing, handoffs
   and other key moments to the minute (from whatever source of ADS-B data you have,
   or the user's own data).
   For a radar-only/TIS-B target, the first and last fixes near the airport bracket
   the departure and arrival.

   **Start with takeoff and landing.** They are usually the best times to find an
   aircraft's comms. Most LiveATC feeds are tower, ground and approach receivers near
   airports. Around takeoff and landing the aircraft talks a lot (taxi, takeoff
   clearance, departure check-in, approach check-in, landing clearance), says its
   callsign each time, and is low and close to the receiver. En route it is often out
   of range, or on a center frequency that nobody archives.
2. **Pick feeds and slots.**
   - Tower is the obvious feed. Busy airports split tower by runway, so pull multiple
     tower frequencies if you're unsure.
   - Add ground (taxi) and the approach/departure sector.
   - The check-in with approach is often the clearest statement of callsign and
     intentions ("1,000 climbing, request 8,500, work east of LAX in the Bravo").
   - Archives are 30-min files named `...-HHMMZ.mp3`, and each slot is published
     only after it ends.
3. **Download only at the user's direction.** Downloads go through the user's own
   browser (Claude in Chrome or another supported browser integration). If no such
   integration is available, help the user select the archives and ask them to
   download the approved files and provide their local paths. Do not substitute a
   server-side HTTP client to work around browser access restrictions.
   Before downloading anything, list the filenames, the count and the size (about 2–3 MB per file), and wait for
   the user's go-ahead. Never download in bulk or on speculation. Read
   `references/liveatc-download.md` for the exact steps. Save into the user's project
   directory (e.g. `<project>/audio/`), not the session scratchpad.

4. **Transcribe with UTC times.** If you're transcribing with a local
   model on Apple Silicon macOS it's free, so re-run windows as often as you need.
   On other hosts, use `--backend openai` after following the transcription skill's
   API setup and upload-consent guidance; do not blindly run the local-only example.
   LiveATC files are squelch-gated and mostly silence, so use
   `--remove-silence` for whole files. It runs many times faster,
   gives fewer hallucinations and more accurate start times, and the
   timestamps still point into the original file (see the
   `transcribe-audio` skill):
   ```bash
   T="/absolute/path/to/transcribe-audio/scripts/transcribe.py"
   uv run "$T" audio/KLAX-Twr-South-Oct-08-2026-1730Z.mp3 --backend local --timestamps --clock auto --remove-silence -o audio/twr_south_1730Z.txt
   # focused 2-minute window, still in true UTC
   uv run "$T" audio/KLAX-Twr-South-Oct-08-2026-1730Z.mp3 --backend local --timestamps --clock auto --start 00:11:30 --duration 00:02:00
   ```
   A 30-minute file usually takes several minutes with `large-v3-turbo`, or can take seconds with
   `--remove-silence` on a quiet frequency.
5. **Match transmissions to the track.** Line up the transmissions with the ADS-B
   positions minute by minute:
   - Clearances against the takeoff and landing.
   - Turn requests and approvals against heading changes.
   - Reported altitudes ("1,000 climbing") against track altitude.
   - Traffic calls ("12 o'clock, 1 mile, 1,400") against the other aircraft's
     ADS-B position and altitude.

   Then rule out everything else: list the other aircraft in the area and their
   callsigns. If the only aircraft on frequency without an ADS-B target lines up with
   the only target without ADS-B, the match is solid.
6. **Report a best reading with the evidence**, quoting the timestamped lines. Cut
   listening clips for anything uncertain.

## Identifying an unknown callsign without biasing the model

If you use Whisper or a whisper variant, `--prompt` biases it toward
the words you give it. That helps with spelling known terms and hurts
when the question is what was said. For example if you are trying to
determine a callsign and you use the prompt to see with candidate
callsigns ("Ranger 52, Hawk 52…") the model will likely output those
callsigns, maybe even erroneously for unrelated calls.

So:

1. Use **no prompt**, or one with information that may already be
   known and isn't in question, e.g. facility names if you aren't
   trying to figure out a facility name, or callsigns if you're not
   trying to figure out callsigns.
2. Fragments that **recur across files** can be trusted slightly more
   (but can also be a common error).
3. **Context helps**. Run entire files at once (with silences
   removed)--most transcription models can use the context around
   utterances to improve accuracy.
4. **Departure and arrival both help.** The callsign gets said several
   times at each end, and one may be easier to hear than another.
5. **Edit together clips** so the user can listen. Always re-encode,
   because stream-copied cuts of LiveATC mp3s can come out silent:
   ```bash
   ffmpeg -loglevel error -y -ss 718 -t 22 -i audio/<file>.mp3 -af loudnorm audio/clip_landing_clearance.mp3
   ```
   `-ss` is in seconds from the start of the file (slot start + offset = UTC).

## Aircraft type from the radio

Pilots and controllers rarely state a type outright. It shows up in **traffic calls
to other aircraft**: "extend downwind, traffic a Stationair on a 4-mile final".
Search all transcripts for type words, and remember Whisper mishears them:

| Heard as | Meaning |
|---|---|
| "stationary" | Stationair (C206) |
| "Sessler" / "system" | Cessna |
| "Skyhawk" | C172 |
| "Cherokee" | PA-28 |

Then confirm which aircraft the call was about by checking ADS-B: who was on a
4-mile final for that runway at that second.

## Whisper artifacts in ATC audio

- "Thank you." every ~30 s over silence. `--remove-silence` removes most of these.
- Foreign-language garbage on static.
- One phrase repeated dozens of times in a couple of seconds. The script collapses
  these and tags them `[repeated xN; likely hallucination]`.
- Runway and frequency digits garbled ("265" for 26L).

Treat any single line as a hint, not a fact.

## References

- `references/liveatc-download.md` — finding feeds and facility names, the archive
  form and signed links, saving mp3s via Chrome, and the gotchas.
- The `transcribe-audio` skill — script options, the OpenAI backend, output formats.
