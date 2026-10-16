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
- **LiveATC downloads** need access to your own browser through a browser integration
  supported by your agent (for example, [Claude in Chrome](https://claude.com/chrome)).
  Installing these skills does not install a browser integration. Without one,
  download the approved archive files yourself and give the agent their local paths;
  feed selection and transcription still work.
- **ADS-B data** makes `search-atc-audio` much more useful: any source of aircraft
  tracks for the time and place you're looking at.

### Install

#### Claude Code

In Claude Code:

```
/plugin marketplace add wiseman/wiseman-skills
/plugin install atc-audio@wiseman-skills
```

#### Codex CLI

Ask the built-in installer to install **both** skills:

```text
$skill-installer Install search-atc-audio and transcribe-audio from https://github.com/wiseman/wiseman-skills, using skills/search-atc-audio and skills/transcribe-audio.
```

For a shared, editable checkout on macOS or Linux, clone this repository (or use an
existing clone), then run the following from its root. This links both skill folders
into Codex's user-level discovery directory without copying their contents:

```sh
mkdir -p "$HOME/.agents/skills"
for skill in search-atc-audio transcribe-audio; do
  target="$HOME/.agents/skills/$skill"
  if [ -e "$target" ] || [ -L "$target" ]; then
    printf 'Already exists, leaving unchanged: %s\n' "$target" >&2
  else
    ln -s "$PWD/skills/$skill" "$target"
  fi
done
```

For project-only use, put the two complete skill folders in
`<project>/.agents/skills/` instead. On Windows, use the installer or copy the folders;
symlinks can require additional OS permissions. Keep `scripts/` and `references/`
with their respective skills. Do not install a second copy if the installer already
made the skills available.

Run `/skills` in Codex to check discovery, then invoke `$search-atc-audio` or
`$transcribe-audio`. Restart Codex if a newly installed skill is missing. The Claude
marketplace commands above are for Claude Code; Codex can load the same `SKILL.md`
files directly.

#### claude.ai

Zip each folder under `skills/` and upload both zips under
Settings → Capabilities → Skills. Transcription also needs an execution environment
with the prerequisites above; uploading a skill does not supply those tools.

### Compatibility notes

- The same skill sources serve both CLIs: their frontmatter uses the shared `name`,
  `description`, and `compatibility` fields. No Claude-only hooks, context forks,
  tool-name permissions, or inline shell substitutions are required.
- Scripts and references are resolved from the loaded skill's location, not the
  project working directory or a hard-coded Claude/Codex installation path.
- Browser capabilities and transcription backends depend on the execution machine,
  not which agent reads the skill. `mlx-whisper` needs Apple Silicon macOS; the
  OpenAI backend works on other platforms with an API key and network access.
- `--backend auto` uses the paid OpenAI API whenever `OPENAI_API_KEY` is set. Use
  `--backend local` explicitly on a supported Mac to keep transcription local.

See the official [Claude Code skills documentation](https://code.claude.com/docs/en/skills)
and [Codex skills documentation](https://learn.chatgpt.com/docs/build-skills) for
current discovery and invocation behavior.

### Development checks

Run the offline regression tests with Python 3.11+ from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

These checks do not call the transcription API or download Whisper models. Live
browser downloads and model transcription still need testing in a configured host.

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
