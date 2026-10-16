# wiseman-skills

Agent skills for [Claude Code](https://claude.com/claude-code) and other tools that
support [Agent Skills](https://agentskills.io).

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
- **LiveATC downloads** go through your own browser, so you need a browser tool such
  as the [Claude in Chrome](https://claude.com/chrome) extension.
- **ADS-B data** makes `search-atc-audio` much more useful: any source of aircraft
  tracks for the time and place you're looking at.

### Install

In Claude Code:

```
/plugin marketplace add wiseman/wiseman-skills
/plugin install atc-audio@wiseman-skills
```

For claude.ai, zip each folder under `skills/` and upload the zips under
Settings → Capabilities → Skills.

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
