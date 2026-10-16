# wiseman-skills

Agent skills for [Claude Code](https://claude.com/claude-code),
[Codex CLI](https://developers.openai.com/codex/cli), and other tools that support
[Agent Skills](https://agentskills.io).

## atc-audio

Two skills for working with air traffic control radio:

- **`search-atc-audio`** — find the archived [LiveATC](https://www.liveatc.net) audio
  for an aircraft, airport and time, transcribe it, and work out what was said. It
  picks feeds and 30-minute archive slots from ADS-B times, matches transmissions to
  tracks, identifies unknown callsigns without biasing the model, and cuts clips for
  you to listen to.
- **`transcribe-audio`** — transcribe any audio or video file, either free and local
  (mlx-whisper) or through OpenAI's speech-to-text API. It can remove silence while
  keeping timestamps correct, map times to UTC from LiveATC filenames, write SRT
  subtitles, and handle files over the API's 25 MB limit.

`search-atc-audio` uses `transcribe-audio`'s script, so they ship together.

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) and
  [ffmpeg](https://ffmpeg.org/download.html) on your PATH. The transcription script
  declares its Python dependencies inline, and `uv run` installs them automatically.
- **Free local transcription** needs a Mac with Apple Silicon. The first run downloads
  the Whisper model from Hugging Face (about 1.5 GB for `large-v3-turbo`).
- **Anywhere else**, set `OPENAI_API_KEY` in your environment to use OpenAI's API
  (paid, roughly $0.006 per minute of audio).
- **If `OPENAI_API_KEY` is set**, transcription uses OpenAI's paid API by default, even
  on a Mac. Ask for local transcription if you'd rather keep audio on your machine.
- **LiveATC downloads** go through your own browser, using a browser tool your agent
  supports, such as [Claude in Chrome](https://claude.com/chrome). Without one, you can
  download the files yourself and give the agent their paths.
- **ADS-B data** makes `search-atc-audio` much more useful: any source of aircraft
  tracks for the time and place you're looking at.

### Install

#### Claude Code

In Claude Code:

```
/plugin marketplace add wiseman/wiseman-skills
/plugin install atc-audio@wiseman-skills
```

`atc-audio` is the plugin; it installs two skills, `search-atc-audio` and
`transcribe-audio`. Claude Code lists them with the plugin name in front, as
`atc-audio:search-atc-audio` and `atc-audio:transcribe-audio`.

#### Codex CLI

Ask Codex's built-in installer for both skills:

```text
$skill-installer install skills/search-atc-audio and skills/transcribe-audio from wiseman/wiseman-skills
```

Or, to use a clone of this repo so your edits take effect directly, link both skill
folders into Codex's user skills folder (use `<project>/.agents/skills` instead for a
single project):

```sh
mkdir -p ~/.agents/skills
ln -s "$PWD/skills/search-atc-audio" "$PWD/skills/transcribe-audio" ~/.agents/skills/
```

Run `/skills` in Codex to check they're there, then use them by asking, or with
`$search-atc-audio` and `$transcribe-audio`.

#### Where they run

These skills are built for an agent running on your own machine. They need local
ffmpeg, uv, a browser tool and (for free transcription) Apple Silicon, so they won't
work as uploaded skills in claude.ai's code-execution sandbox.

### Development

Run the offline tests (no API calls or model downloads) from the repo root with
Python 3.11 or later:

```sh
python3 -m unittest discover -s tests -v
```

### Example prompts

- "What did N12345 say to Long Beach tower when it landed around 16:05Z yesterday?"
- "This TIS-B target left Van Nuys at 02:31Z. Find its callsign on LiveATC."
- "Transcribe this LiveATC file with UTC timestamps and remove the silence."
- "Make SRT subtitles for talk.mp4."

### About LiveATC

The skill downloads archive audio only when you ask for it, and asks you to approve
the specific files (names, count and size) before fetching anything. It doesn't
download in bulk or on speculation. LiveATC is a volunteer-fed service, so please
use it in line with the terms of use on [liveatc.net](https://www.liveatc.net).

## satellite-imagery

One skill, **`sentinel-aircraft-ships`**, for finding aircraft and ships in free
Copernicus Sentinel satellite imagery anywhere in the world:

- **Sentinel-2 (optical)** — find the passes over a place and the exact moment each
  pixel was imaged (not the misleading strip time), check whether an ADS-B-tracked
  aircraft appears in a scene, or work out which aircraft made a colour-fringed streak.
  It models the band-by-band timing and parallax that produce the red/green/blue
  fringe, fits the ADS-B track to within a pixel, flags false matches (fields, roads,
  cloud, contrails) and writes an overlay image. It also finds ship candidates, renders
  crops, and measures a ship's speed and course from two sightings.
- **Sentinel-1 (radar)** — detect ships by day or night and through cloud, flag fixed
  objects (seen in earlier passes, or at mapped bridges, turbines and platforms) and
  azimuth-ambiguity "ghosts", and predict where a moving ship lands in the image. It
  includes a recipe for checking whether something in a radar image is a real target
  or an artefact. In an AIS benchmark it found every ship of 100 m or more, with about
  1.5 unexplained detections per 1000 km².

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) and network access. The
  two scripts declare their Python dependencies inline, and `uv run` installs them
  automatically.
- **Imagery** needs no account: it comes from the Earth Search STAC (Element84) and AWS
  open data, Microsoft Planetary Computer, the Copernicus Data Space catalogue and the
  Copernicus DEM on AWS.
- **ADS-B** for checking aircraft can come from any source: pass a CSV of
  `time,lat,lon,alt_geom_ft,alt_baro_ft` with `--track`. Looking aircraft up by ICAO hex,
  and `identify` (which aircraft made this streak?), use the
  [Hexbird](https://hexbird.io) HTTP API: set `HEXBIRD_TOKEN` (and optionally
  `HEXBIRD_API_BASE`).
- **Mapped structures** (OpenStreetMap bridges, turbines, platforms) in the radar ship
  detector also come from Hexbird. Without a token the detector says so and relies on
  comparing earlier passes to flag fixed objects.

### Install

In Claude Code:

```
/plugin marketplace add wiseman/wiseman-skills
/plugin install satellite-imagery@wiseman-skills
```

Claude Code lists the skill as `satellite-imagery:sentinel-aircraft-ships`.

### Example prompts

- "Did Sentinel-2 catch BAW123 over the Channel on 14 October? Here's its ADS-B track as
  a CSV."
- "This Copernicus Browser crop at 50.72, -0.21 shows a red-green-blue streak. Which
  aircraft was it?"
- "A helicopter's ADS-B went altitude-only offshore near Brighton around 10:40Z. Can you
  find the ship it landed on?"
- "Are these two faint dots behind the ships in this Sentinel-1 image ghosts or other
  vessels?"
- "When did Sentinel-1 last image 57.45, 11.45, and are there ships there?"

Images made from the data should carry the credit "Contains modified Copernicus
Sentinel data [year]".

## License

MIT. See [LICENSE](LICENSE).
