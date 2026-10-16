# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "openai>=1.60",
#     "mlx-whisper; sys_platform == 'darwin' and platform_machine == 'arm64'",
# ]
# ///
"""Transcribe audio files with OpenAI's speech-to-text API or locally with mlx-whisper.

Handles the things that bite you when transcribing real recordings:
  - the 25 MB upload limit (downsamples to 16 kHz mono, then splits if still too big)
  - model/format compatibility (gpt-4o-* can't do verbose_json/srt; whisper-1 can)
  - vocabulary biasing via --prompt (huge quality win for jargon: callsigns, names)
  - segment timestamps, optionally mapped to a wall-clock base (e.g. LiveATC 0000Z files)

Backends (--backend): `openai` (hosted API, costs money), `local` (mlx-whisper on Apple
Silicon, free, models cached under ~/.cache/huggingface), or `auto` (default: openai if
OPENAI_API_KEY is set, else local). The local backend has no size limit and always
returns segment timestamps.

The API key is read from the OPENAI_API_KEY environment variable only. Never paste a
key on the command line or into a file — it ends up in shell history and transcripts.

Run with `uv run transcribe.py ...` so dependencies are fetched automatically.
Requires ffmpeg/ffprobe on PATH for size handling, windowing, and normalization.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_UPLOAD_BYTES = 24 * 1024 * 1024  # API hard limit is 25 MB; stay under it.

# response_format support is model-dependent. Only whisper-1 returns timestamps/SRT.
TIMESTAMP_MODEL = "whisper-1"
GPT4O_MODELS = {"gpt-4o-transcribe", "gpt-4o-mini-transcribe"}

# Local (mlx-whisper) model aliases -> Hugging Face repos.
LOCAL_MODELS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",  # default: fast, good
    "large-v3": "mlx-community/whisper-large-v3-mlx",          # slower; a second opinion
}
LOCAL_DEFAULT = "large-v3-turbo"


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{res.stderr.strip()}")
    return res.stdout.strip()


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nk=1:nw=1", str(path)])
    try:
        return float(out)
    except ValueError:
        return 0.0


def parse_clock(s: str) -> float:
    """Accept HH:MM:SS, HH:MM, MM:SS, or seconds -> seconds."""
    s = s.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + sec


def base_from_liveatc(name: str) -> float | None:
    """LiveATC archive files end in e.g. ...-Jun-19-2026-0000Z.mp3 -> base 00:00:00 UTC."""
    m = re.search(r"-(\d{2})(\d{2})Z", name)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    return None


def fmt_offset(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def fmt_clock(t: float, base: float) -> str:
    tot = int(round(base + t))
    return f"{(tot // 3600) % 24:02d}:{(tot % 3600) // 60:02d}:{tot % 60:02d}Z"


def fmt_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ffmpeg_extract(src: Path, dst: Path, *, start: float | None, duration: float | None,
                   downsample: bool, normalize: bool):
    """Cut and/or re-encode a working copy. Downsampling to 16 kHz mono mp3 shrinks
    speech dramatically (an hour fits well under the limit) and is lossless for timing."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if duration:
        cmd += ["-t", str(duration)]
    if downsample or normalize:
        cmd += ["-ar", "16000", "-ac", "1", "-b:a", "48k"]
        if normalize:
            cmd += ["-af", "loudnorm"]
        cmd += [str(dst)]
    else:
        cmd += ["-c", "copy", str(dst)]
    run(cmd)


def transcribe_one(client, path: Path, *, model: str, response_format: str,
                   language: str | None, prompt: str | None, want_segments: bool):
    """Single API call. Returns (text, segments) where segments is a list of
    dicts {start,end,text} (empty unless want_segments and model supports it)."""
    kwargs = dict(model=model, response_format=response_format)
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt
    if want_segments and model == TIMESTAMP_MODEL:
        kwargs["timestamp_granularities"] = ["segment"]
    # Close uploads before temporary files are removed, including on API errors.
    # Windows does not allow deleting a file while this handle is still open.
    with path.open("rb") as audio:
        resp = client.audio.transcriptions.create(file=audio, **kwargs)

    if response_format == "verbose_json":
        segs = [{"start": s.start, "end": s.end, "text": s.text.strip()}
                for s in getattr(resp, "segments", []) or []]
        return resp.text, segs
    if response_format in ("json",):
        return resp.text, []
    # text / srt / vtt come back as a plain string
    return (resp if isinstance(resp, str) else getattr(resp, "text", str(resp))), []


def transcribe_local(path: Path, *, model: str, language: str | None, prompt: str | None):
    """mlx-whisper on Apple Silicon. Returns (text, segments). No size limit; always
    yields segments. condition_on_previous_text=False keeps one garbled stretch of
    radio from poisoning everything after it."""
    try:
        import mlx_whisper
    except ImportError:
        eprint("ERROR: the local backend needs mlx-whisper on Apple Silicon (macOS arm64).")
        sys.exit(2)
    repo = LOCAL_MODELS.get(model, model)
    r = mlx_whisper.transcribe(str(path), path_or_hf_repo=repo, language=language,
                               initial_prompt=prompt or None,
                               condition_on_previous_text=False, verbose=None)
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()}
            for s in r.get("segments", [])]
    return r.get("text", ""), collapse_repeats(segs)


def collapse_repeats(segs: list[dict], min_run: int = 3) -> list[dict]:
    """Whisper sometimes loops on noise/static, emitting the same line dozens of times
    in a second or two. Collapse runs of >= min_run identical consecutive segments."""
    out: list[dict] = []
    i = 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and segs[j + 1]["text"] == segs[i]["text"]:
            j += 1
        n = j - i + 1
        if n >= min_run:
            out.append({"start": segs[i]["start"], "end": segs[j]["end"],
                        "text": f"{segs[i]['text']} [repeated x{n}; likely hallucination]"})
        else:
            out.extend(segs[i:j + 1])
        i = j + 1
    return out


SR = 16000  # condensed audio is 16 kHz mono s16


def condense_silence(src: Path, dst: Path, *, noise_db: float, min_silence: float,
                     pad: float = 0.3) -> tuple[list[tuple[float, float, float]], float]:
    """Cut the silences out of src into one WAV, dst. Returns (chunks, total) where chunks
    lists (condensed_start, original_start, length) in seconds. The chunks sit back to back
    in dst, so any time in dst maps back with remap_time().

    ffmpeg decodes once: silencedetect logs to stderr and raw PCM goes to stdout. The PCM is
    sliced here sample-exactly. Using ffmpeg's aselect/concat instead drifts by up to a
    frame per chunk, and that drift adds up over hundreds of transmissions."""
    import wave
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
         "-af", f"aformat=sample_rates={SR}:channel_layouts=mono,"
                f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
        capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg silencedetect failed on {src}:\n"
                           f"{res.stderr.decode(errors='replace')[-2000:]}")
    pcm, log = res.stdout, res.stderr.decode(errors="replace")
    total = len(pcm) / 2 / SR

    # silencedetect prints silence_start/silence_end pairs; a trailing start has no end.
    silences, cur = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", log):
        if m.group(1) == "start":
            cur = max(0.0, float(m.group(2)))
        elif cur is not None:
            silences.append((cur, float(m.group(2))))
            cur = None
    if cur is not None:
        silences.append((cur, total))

    # Speech = the gaps between silences, padded so word edges survive, then merged.
    speech, t = [], 0.0
    for s0, s1 in silences:
        if s0 > t:
            speech.append((t, s0))
        t = max(t, s1)
    if t < total:
        speech.append((t, total))
    merged: list[list[float]] = []
    for a, b in speech:
        a, b = max(0.0, a - pad), min(total, b + pad)
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    chunks, pos = [], 0
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        for a, b in merged:
            i0, i1 = int(round(a * SR)), int(round(b * SR))
            w.writeframes(pcm[2 * i0:2 * i1])
            chunks.append((pos / SR, i0 / SR, (i1 - i0) / SR))
            pos += i1 - i0
    return chunks, total


def remap_time(t: float, chunks: list[tuple[float, float, float]]) -> float:
    """Map a time in the condensed audio back to the original file's timeline."""
    import bisect
    i = max(0, bisect.bisect_right([c[0] for c in chunks], t) - 1)
    c0, o0, length = chunks[i]
    return o0 + min(max(t - c0, 0.0), length)


def split_points(total: float, n: int) -> list[tuple[float, float]]:
    step = total / n
    return [(i * step, step) for i in range(n)]


def main():
    # Redirected stdout can otherwise use a legacy Windows code page, which
    # cannot represent every language returned by either transcription backend.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(
        description="Transcribe audio with OpenAI (OPENAI_API_KEY) or locally with mlx-whisper.")
    ap.add_argument("files", nargs="+", help="audio file(s): mp3 m4a wav mp4 webm flac ogg ...")
    ap.add_argument("--backend", default="auto", choices=["auto", "openai", "local"],
                    help="openai = hosted API (paid); local = mlx-whisper on Apple Silicon (free); "
                         "auto (default) = openai if OPENAI_API_KEY is set, else local.")
    ap.add_argument("--model", default=None,
                    help="openai: gpt-4o-transcribe (default, best), gpt-4o-mini-transcribe (cheapest), "
                         "whisper-1 (needed for timestamps/SRT; auto-selected from --timestamps/--srt). "
                         "local: large-v3-turbo (default) or large-v3, or any mlx-community HF repo.")
    ap.add_argument("--format", dest="fmt", default=None,
                    choices=["text", "segments", "srt", "json", "verbose_json"],
                    help="Output format. Default: 'segments' if --timestamps, else 'text'.")
    ap.add_argument("--timestamps", action="store_true",
                    help="Produce per-segment timestamps (forces whisper-1).")
    ap.add_argument("--srt", action="store_true", help="Output SRT subtitles (forces whisper-1).")
    ap.add_argument("--prompt", default=None,
                    help="Vocabulary/style hint. Seed it with the proper nouns and jargon you "
                         "expect (names, callsigns, runways, product names) — markedly improves accuracy.")
    ap.add_argument("--language", default=None, help="ISO-639-1 code, e.g. en. Optional but speeds things up.")
    ap.add_argument("--start", default=None, help="Cut from this point first (HH:MM:SS or seconds).")
    ap.add_argument("--duration", default=None, help="Length of the cut window (HH:MM:SS or seconds).")
    ap.add_argument("--normalize", action="store_true",
                    help="Loudness-normalize + 16 kHz mono before sending (helps faint/noisy audio).")
    ap.add_argument("--remove-silence", action="store_true",
                    help="Cut out silence and transcribe one condensed file. Timestamps are mapped "
                         "back to the original timeline, so --timestamps/--clock/--srt stay correct.")
    ap.add_argument("--silence-db", type=float, default=-40.0,
                    help="With --remove-silence: audio below this level counts as silence (default -40).")
    ap.add_argument("--min-silence", type=float, default=1.0,
                    help="With --remove-silence: only cut silences at least this many seconds long "
                         "(default 1.0).")
    ap.add_argument("--clock", default=None,
                    help="Map segment times to wall clock. Pass a base time (HH:MM:SS), or 'auto' "
                         "to parse a LiveATC-style ...-0000Z.mp3 filename.")
    ap.add_argument("-o", "--output", default=None, help="Write to this file instead of stdout.")
    args = ap.parse_args()

    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    backend = args.backend if args.backend != "auto" else ("openai" if has_key else "local")
    if backend == "openai" and not has_key:
        eprint("ERROR: set OPENAI_API_KEY in your environment first (do not pass keys on the CLI), "
               "or use --backend local on macOS with Apple Silicon.")
        sys.exit(2)
    local = backend == "local"
    if local and (sys.platform != "darwin" or platform.machine().lower() != "arm64"):
        eprint("ERROR: the local backend requires macOS with Apple Silicon (arm64). "
               "On this system, set OPENAI_API_KEY and use --backend openai.")
        sys.exit(2)

    want_ts = args.timestamps or args.srt or args.fmt in ("segments", "srt", "verbose_json")
    fmt = args.fmt or ("srt" if args.srt else "segments" if want_ts else "text")

    if local:
        model = args.model or LOCAL_DEFAULT
        api_fmt = None
        client = None
    else:
        model = args.model or (TIMESTAMP_MODEL if want_ts else "gpt-4o-transcribe")

        # Guardrail: timestamped/SRT output only exists on whisper-1.
        if want_ts and model in GPT4O_MODELS:
            eprint(f"NOTE: {model} can't return timestamps/SRT; switching to {TIMESTAMP_MODEL}.")
            model = TIMESTAMP_MODEL

        # Pick the wire format the API call should request.
        api_fmt = "verbose_json" if fmt in ("segments", "verbose_json") else \
                  "srt" if fmt == "srt" else "json" if fmt == "json" else "text"
        if api_fmt in ("verbose_json", "srt") and model != TIMESTAMP_MODEL:
            api_fmt = "json" if api_fmt == "verbose_json" else "text"

        from openai import OpenAI
        client = OpenAI()
    eprint(f"backend: {backend}, model: {model}")

    out_chunks: list[str] = []
    for fpath in args.files:
        src = Path(fpath).expanduser()
        if not src.exists():
            eprint(f"ERROR: not found: {src}")
            sys.exit(1)

        base = None
        if args.clock:
            base = base_from_liveatc(src.name) if args.clock == "auto" else parse_clock(args.clock)
            if base is None:
                eprint(f"NOTE: couldn't parse a clock base from {src.name}; using offsets.")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            start = parse_clock(args.start) if args.start else None
            duration = parse_clock(args.duration) if args.duration else None
            need_edit = bool(start or duration or args.normalize)
            too_big = not local and src.stat().st_size > MAX_UPLOAD_BYTES
            if base is not None and start:
                base += start  # segment times are relative to the cut, not the file

            work = src
            if need_edit or too_big:
                work = tmp / "work.mp3"
                # Stream-copying a cut of a VBR/damaged mp3 (LiveATC files) can yield
                # near-silent audio, so always re-encode cuts for the local backend.
                ffmpeg_extract(src, work, start=start, duration=duration,
                               downsample=too_big or args.normalize or local,
                               normalize=args.normalize)

            chunks = None
            if args.remove_silence:
                condensed = tmp / "condensed.wav"
                chunks, total = condense_silence(work, condensed, noise_db=args.silence_db,
                                                 min_silence=args.min_silence)
                kept = sum(c[2] for c in chunks)
                eprint(f"{src.name}: removed {total - kept:.0f} s of silence; transcribing "
                       f"{kept:.0f} s of {total:.0f} s in {len(chunks)} chunks.")
                if not chunks:
                    eprint(f"{src.name}: no audio above {args.silence_db} dB; skipping.")
                    continue
                work = condensed

            # Even after downsampling, multi-hour audio may exceed the cap -> split & merge.
            pieces = [(work, 0.0)]
            if not local and work.stat().st_size > MAX_UPLOAD_BYTES:
                total = probe_duration(work)
                n = (work.stat().st_size // MAX_UPLOAD_BYTES) + 1
                eprint(f"{src.name}: {work.stat().st_size/1e6:.1f} MB after re-encode; "
                       f"splitting into {n} chunks.")
                pieces = []
                for i, (off, dur) in enumerate(split_points(total, n)):
                    part = tmp / f"part{i}.mp3"
                    ffmpeg_extract(work, part, start=off, duration=dur,
                                   downsample=True, normalize=args.normalize)
                    pieces.append((part, off))

            all_segs: list[dict] = []
            text_parts: list[str] = []
            for part, off in pieces:
                eprint(f"transcribing {src.name} [{fmt_offset(off)}+] with {model} ...")
                if local:
                    text, segs = transcribe_local(part, model=model, language=args.language,
                                                  prompt=args.prompt)
                else:
                    text, segs = transcribe_one(
                        client, part, model=model, response_format=api_fmt,
                        language=args.language, prompt=args.prompt, want_segments=want_ts)
                text_parts.append(text.strip() if text else "")
                for s in segs:
                    all_segs.append({"start": s["start"] + off, "end": s["end"] + off,
                                     "text": s["text"]})
            if chunks:
                for s in all_segs:
                    s["start"] = remap_time(s["start"], chunks)
                    s["end"] = remap_time(s["end"], chunks)

        # ---- render output for this file ----
        header = f"# {src.name}" if len(args.files) > 1 else None
        if header:
            out_chunks.append(header)

        if fmt == "text":
            out_chunks.append("\n".join(p for p in text_parts if p))
        elif fmt == "json":
            import json
            out_chunks.append(json.dumps({"text": "\n".join(text_parts)}, ensure_ascii=False, indent=2))
        elif fmt == "verbose_json":
            import json
            out_chunks.append(json.dumps({"segments": all_segs}, ensure_ascii=False, indent=2))
        elif fmt == "srt":
            lines = []
            for i, s in enumerate(all_segs, 1):
                lines.append(f"{i}\n{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}\n{s['text']}\n")
            out_chunks.append("\n".join(lines) if lines else "\n".join(text_parts))
        else:  # segments
            lines = []
            for s in all_segs:
                stamp = fmt_clock(s["start"], base) if base is not None else f"+{fmt_offset(s['start'])}"
                txt = s["text"]
                if txt:
                    lines.append(f"[{stamp}] {txt}")
            out_chunks.append("\n".join(lines) if lines else "\n".join(text_parts))

    result = "\n\n".join(c for c in out_chunks if c) + "\n"
    if args.output:
        Path(args.output).expanduser().write_text(result, encoding="utf-8")
        eprint(f"wrote {args.output}")
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
