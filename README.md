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

## License

MIT. See [LICENSE](LICENSE).
