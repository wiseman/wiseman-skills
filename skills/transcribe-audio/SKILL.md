---
name: transcribe-audio
description: >-
  Transcribe audio (or the audio track of a video) to text using OpenAI's
  speech-to-text API or a free local Whisper (mlx-whisper on Apple Silicon). Use this whenever the user wants speech turned into text:
  transcribing a recording, voice memo, interview, podcast, meeting, lecture,
  phone call, YouTube/video audio, or ATC/LiveATC radio; getting subtitles
  (SRT, or VTT converted from it) or timestamps for a clip; pulling quotes out of an audio file;
  finding when something was said; or asking "what's in this mp3/m4a/wav",
  "transcribe this", "what did they say", "make captions for this". Reach for it
  even when the user names a file (.mp3 .m4a .wav .mp4 .webm .flac .ogg) and just
  asks what's in it, or hands you a LiveATC/ATC recording to read (for finding,
  downloading or searching ATC audio, also use search-atc-audio). Handles long
  files (auto-splits past the 25 MB limit), timestamps, vocabulary biasing for
  jargon/names/callsigns, and clipping to a time window.
compatibility: >-
  Needs ffmpeg/ffprobe and uv. The local backend (mlx-whisper) runs only on macOS on
  Apple Silicon; on other systems use the OpenAI backend, which needs OPENAI_API_KEY
  and network access. A shell and a readable local input file are required.
---

# Transcribe audio

Turn speech in an audio (or video) file into text, either with OpenAI's transcription
API or locally with mlx-whisper. The bundled script `scripts/transcribe.py` handles the
parts that are easy to get wrong — the upload size limit, model/format compatibility,
vocabulary biasing, and timestamp handling — so prefer it over hand-rolling `curl` or
ad-hoc Whisper scripts each time.

## Locate the bundled script

Resolve `scripts/transcribe.py` relative to the directory containing this loaded
`SKILL.md`, not the shell's current working directory. Use the skill path supplied
by the host (Claude Code or Codex); do not assume a `~/.claude` or `~/.codex` path.
Keep the shell in the user's project so relative input/output paths still work.

The shell examples below use `T` for the resolved **absolute** script path. Set it
once in the shell where you will run the command, replacing the example path:

```bash
T="/absolute/path/to/transcribe-audio/scripts/transcribe.py"
```

In PowerShell, use `$T = "C:\path\to\transcribe-audio\scripts\transcribe.py"`;
`uv run "$T" ...` works there too. If shell calls do not preserve variables, pass
the quoted absolute script path directly on each call.

## Backends: local (free) or OpenAI (paid)

`--backend auto` (the default) uses OpenAI when `OPENAI_API_KEY` is set, otherwise local.

- **`--backend local`** — mlx-whisper on macOS with Apple Silicon. Free, no upload
  limit, always returns segment timestamps (so `--timestamps`, `--srt`, `--clock` all
  work). Models: `--model large-v3-turbo` (default; about a minute or a few per 30-min
  LiveATC file on an M-series Mac, depending on the chip) or `--model large-v3` (slower,
  a useful second opinion). The first run of each model downloads it from Hugging Face
  (about 1.5 GB for large-v3-turbo, about 3 GB for large-v3). Repeated-line hallucination loops on static (e.g. the same
  phrase 30× in two seconds) are collapsed and tagged `[repeated xN; likely hallucination]`.
- **`--backend openai`** — the hosted API (`gpt-4o-transcribe` etc., see below). Costs
  money (roughly $0.006/min); reach for it when local output is too garbled to use.

The local backend is a perfectly good default for ATC work: in practice it handled
LiveATC tower/approach audio well, and it costs nothing to re-run windows many times.

## Setup (check once)

- **API key** (OpenAI backend only): the script reads `OPENAI_API_KEY` from the
  environment and nothing else. If it isn't set and you need the OpenAI backend, ask the
  user to configure it in their environment through a secrets manager or secure
  credential flow, then restart the agent if needed so its shell inherits it. Do not
  ask them to paste it in chat, echo it, or put it in a command or project file.
  If a key is exposed in chat or logs, suggest rotating it. On Apple Silicon macOS,
  `--backend local` avoids the API key and audio upload.
- **ffmpeg / ffprobe** must be on PATH (used for sizing, clipping, normalizing). Install
  with `brew install ffmpeg`, apt, winget, etc.
- **uv** must be on PATH (see https://docs.astral.sh/uv/getting-started/installation/).
  The script declares its own deps inline, so always run it with **`uv run`** — no
  other install step.

## Quick start

```bash
uv run "$T" recording.mp3
```

That prints plain text. With `OPENAI_API_KEY` set it uploads audio to OpenAI's
paid API using `gpt-4o-transcribe`; otherwise it runs locally with
`large-v3-turbo` on Apple Silicon macOS. On other machines, configure the OpenAI
backend first. Confirm the user's choice of hosted transcription before sending
private recordings; installing or invoking the skill is not blanket upload consent.

## Choosing model and output format (OpenAI backend)

Two facts drive every choice here, both from OpenAI's current API (the local backend
has none of these restrictions):

1. **Timestamps and subtitles only exist on `whisper-1`.** The newer `gpt-4o-transcribe`
   / `gpt-4o-mini-transcribe` models return higher-accuracy text but **cannot** produce
   `verbose_json`, segment timestamps, or SRT/VTT. So the moment the user wants "when was
   this said", subtitles, or a timeline, you're on `whisper-1`. The script enforces this
   automatically — `--timestamps` and `--srt` switch the model for you.
2. **`gpt-4o-transcribe` is the accuracy default; `gpt-4o-mini-transcribe` is the cheap
   option.** Use mini for long, low-stakes bulk audio where cost matters.

| The user wants... | Use |
|---|---|
| Just the words, best quality | default (`gpt-4o-transcribe`, `--format text`) |
| Cheap bulk transcription | `--model gpt-4o-mini-transcribe` |
| Timestamps / "when did they say X" | `--timestamps` (→ whisper-1, `[+HH:MM:SS] line` output) |
| Subtitles for a video | `--srt -o subs.srt` (→ whisper-1) |
| Raw segment data for further processing | `--format verbose_json` |

## The single biggest quality lever: `--prompt`

Seed the `--prompt` with the proper nouns and jargon you expect — names, places, product
names, acronyms, callsigns. The model uses it as a vocabulary/spelling hint, and it
sharply reduces garbled domain terms. This mattered more than model choice in practice.

```bash
# A technical interview
uv run "$T" interview.m4a \
  --prompt "Guests: Dr. Anya Sharma, Marco. Topics: Kubernetes, etcd, gRPC, Anthropic, RAG."

# ATC radio — give it callsigns, runways, and the phraseology you expect
uv run "$T" tower.mp3 --timestamps \
  --prompt "Long Beach Tower ATC. N249CP, Skyhawk, runway 30, runway 26, cleared for takeoff, cleared to land, traffic, go around."
```

### When the prompt is a trap

`--prompt` biases the model toward the words you give it. That helps with spelling
terms you already know. It hurts when the unknown word is the thing you're trying to
find out: candidate names in the prompt get "heard" even where they weren't said.
For that case:

- Use no prompt, or a generic one with no candidates.
- Compare multiple models: local vs an API or different local models.
- Use windows with a few minutes of context at least. Very short cuts
  tend to get worse.
- Cut clips the user can listen to.

For ATC callsigns, the **`search-atc-audio`** skill has the full method.

## Common tasks

**Clip to a window first** (faster, cheaper, and focuses the model) — times are
`HH:MM:SS` or seconds:

```bash
uv run "$T" long_meeting.mp3 --start 01:12:00 --duration 00:05:00 --timestamps
```

**Faint or noisy audio** — loudness-normalize and downsample before sending:

```bash
uv run "$T" noisy.wav --normalize --timestamps
```

**A video file** — the API accepts mp4/webm directly; just pass it. (If it's large,
the script downsamples the audio automatically; see below.)

```bash
uv run "$T" talk.mp4 --srt -o talk.srt
```

The script only writes SRT. For WebVTT, convert it: `ffmpeg -i talk.srt talk.vtt`.

**Several files at once** — each gets a `# filename` header in the output:

```bash
uv run "$T" freq1.mp3 freq2.mp3 freq3.mp3 --timestamps --prompt "..."
```

**Local, free, with UTC clock times** (the usual LiveATC case):

```bash
uv run "$T" KLGB1-ES-Twr1-119.400-Oct-16-2026-0230Z.mp3 \
  --backend local --timestamps --clock auto --start 00:11:30 --duration 00:02:00
```

`--clock auto` accounts for `--start`, so lines read in true UTC (`[02:42:00Z] ...`).

## Remove silence first (radio, long recordings with dead air)

If a recording is mostly silence, pass `--remove-silence`. LiveATC archives are the
typical case: they are squelch-gated, so a quiet tower file can be over 90% silence.
On a 32-minute KCNO tower file (96% silent) it:

- **Ran in 7 s instead of 57 s.** Only 43 s of audio went to the model.
- **Removed hallucinations.** The full run had "Thank you." ×51 and others over
  silence. The condensed run had almost none.
- **Gave more accurate timestamps.** Over long silences Whisper drifts a segment's
  start time backwards. The full run put a transmission at 05:00 that really began at
  05:08. The condensed run put it at 05:09.
- **Costs less on the OpenAI backend**, which bills per minute of audio.

```bash
uv run "$T" KCNO-Twr-Oct-13-2026-0000Z.mp3 \
  --backend local --timestamps --clock auto --remove-silence -o cno_0000Z.txt
```

Timestamps still refer to the **original** file, so `--clock`, `--start`, `--srt` and
later `ffmpeg -ss` clip cuts all line up. It works like this:

1. One ffmpeg pass decodes to 16 kHz mono PCM and runs
   `silencedetect=noise=-40dB:d=1` on the same stream.
2. The speech spans between silences get 0.3 s of padding on each side, and
   overlapping spans are merged.
3. Those spans are copied back to back into **one** WAV, sample-exactly in Python,
   with a map of `(condensed_start, original_start, length)` for each chunk.
4. Whisper gets that one file, not one file per chunk. One call keeps the model warm
   and avoids the junk output that very short clips produce.
5. Each segment's start and end are mapped back through the chunk map before output.

Do it this way, not with ffmpeg's `aselect`/`concat` filters. Those cut on frame
boundaries, so the error grows with each chunk and gets large across hundreds of
transmissions.

Tuning: the stderr line `removed N s of silence; transcribing M s ... in K chunks`
shows how much was kept. Squelched radio is near digital silence, so the default
`-40 dB` threshold is safe. On hissy, unsquelched audio, raise it (`--silence-db -30`).
If a transmission you know about is missing, lower it, or re-run that window without
the flag. `--min-silence` (default 1 s) sets the shortest gap that gets cut.

## Long files and the 25 MB limit (OpenAI backend)

OpenAI rejects uploads over 25 MB. The script handles this for you: if a file is too big
(or `--normalize` is set) it re-encodes to 16 kHz mono — which is lossless for speech
timing and shrinks an hour of audio to a few MB — and only if it's *still* too big does it
split into time-based chunks, transcribe each, and stitch the results with timestamps
offset correctly. You don't normally need to think about it; just pass the file.

## Wall-clock timestamps (recordings with a known start time)

For a recording that started at a known UTC time — LiveATC archive files are the classic
case — map segment offsets to actual clock time with `--clock`:

```bash
# Parse the start time from a LiveATC filename like KLGB1-...-Jun-19-2026-0000Z.mp3
uv run "$T" KLGB1-ES-Twr1-119.400-Jun-19-2026-0000Z.mp3 --timestamps --clock auto

# Or give the base explicitly
uv run "$T" session.mp3 --timestamps --clock 09:00:00
```

Output lines then read `[16:07:13Z] ...` instead of `[+00:07:13]`, which makes it easy to
line up against ADS-B tracks, logs, or other timestamped sources.

## After transcribing

Don't just dump the raw transcript and stop — finish the user's actual task. If they asked
what happened, summarize and quote the relevant lines (with timestamps if available). If
they're hunting for a specific moment, point them to it. ATC and noisy audio in particular
produce imperfect lines; flag anything that looks garbled rather than presenting it as
certain. To clean up a critical passage, re-run a `--start/--duration` window of a minute
or two: add a richer `--prompt` only for known vocabulary, and use no prompt when the
unknown is the thing you're trying to find (see "When the prompt is a trap" above).

## ATC / LiveATC

For ATC radio work, use the **`search-atc-audio`** skill alongside this one. It covers:

- Downloading LiveATC archives (through the user's browser, only at the user's
  direction).
- Picking feeds and 30-minute slots from ADS-B times.
- Matching transmissions to tracks.
- Identifying callsigns and aircraft types.
- Whisper's typical mistakes on ATC audio.

## More detail

Check the current OpenAI API reference to find the current models,
exact `response_format` support, all parameters, pricing, the raw
`curl` form, streaming, and diarization
(`gpt-4o-transcribe-diarize`). Read it when you need an option the
script doesn't expose or want to confirm a model capability.
