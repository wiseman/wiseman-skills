"""Offline regression tests: no API requests, credentials, or model downloads.

Run from the repository root with: python -m unittest discover -s tests -v
"""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills/transcribe-audio/scripts/transcribe.py"
SPEC = importlib.util.spec_from_file_location("transcribe", SCRIPT)
transcribe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transcribe)


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = Path(self.tmp.name) / "recording with spaces.wav"
        self.audio.write_bytes(b"offline test audio")
        self.client = mock.Mock()
        self.openai = SimpleNamespace(OpenAI=mock.Mock(return_value=self.client))
        self.stderr = io.StringIO()

    def run_main(self, args, *, has_key=True, stdout=None):
        stdout = stdout if stdout is not None else io.StringIO()
        # This placeholder is never sent anywhere: OpenAI is replaced in sys.modules.
        env = {"OPENAI_API_KEY": "unused-test-placeholder"} if has_key else {}
        with mock.patch.object(sys, "argv", [str(SCRIPT), *args]), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(sys.modules, {"openai": self.openai}), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", self.stderr):
            transcribe.main()
        return stdout

    def test_upload_is_closed_after_success(self):
        captured = []

        def create(**kwargs):
            captured.append(kwargs["file"])
            self.assertFalse(kwargs["file"].closed)
            self.assertEqual(kwargs["file"].read(), b"offline test audio")
            self.assertEqual(kwargs["language"], "en")
            self.assertEqual(kwargs["prompt"], "Names")
            self.assertEqual(kwargs["timestamp_granularities"], ["segment"])
            return SimpleNamespace(text="hello", segments=[
                SimpleNamespace(start=1.0, end=2.0, text=" hello ")])

        self.client.audio.transcriptions.create.side_effect = create
        result = transcribe.transcribe_one(
            self.client, self.audio, model="whisper-1", response_format="verbose_json",
            language="en", prompt="Names", want_segments=True)
        self.assertEqual(result, ("hello", [{"start": 1.0, "end": 2.0, "text": "hello"}]))
        self.assertTrue(captured[0].closed)

    def test_upload_is_closed_after_error(self):
        captured = []

        def create(**kwargs):
            captured.append(kwargs["file"])
            raise RuntimeError("mock API error")

        self.client.audio.transcriptions.create.side_effect = create
        with self.assertRaisesRegex(RuntimeError, "mock API error"):
            transcribe.transcribe_one(
                self.client, self.audio, model="gpt-4o-transcribe", response_format="text",
                language=None, prompt=None, want_segments=False)
        self.assertTrue(captured[0].closed)

    def test_output_file_is_explicitly_utf8(self):
        output = Path(self.tmp.name) / "transcript.txt"
        text = "Café 東京 ✈"
        self.client.audio.transcriptions.create.return_value = text
        with mock.patch.object(Path, "write_text", autospec=True,
                               side_effect=Path.write_text) as write_text:
            self.run_main([str(self.audio), "--output", str(output)])
        write_text.assert_called_once_with(output, text + "\n", encoding="utf-8")
        self.assertEqual(output.read_bytes(), (text + os.linesep).encode("utf-8"))

    def test_redirected_stdout_is_utf8_even_with_legacy_encoding(self):
        text = "Café 東京 ✈"
        self.client.audio.transcriptions.create.return_value = text
        raw = io.BytesIO()
        with io.TextIOWrapper(raw, encoding="cp1252", newline="\n") as stdout:
            self.run_main([str(self.audio)], stdout=stdout)
            stdout.flush()
            self.assertEqual(raw.getvalue(), (text + "\n").encode("utf-8"))

    def test_unsupported_local_platforms_fail_before_processing(self):
        for system, machine in [("linux", "x86_64"), ("linux", "aarch64"),
                                ("win32", "AMD64"), ("darwin", "x86_64")]:
            for backend in ["auto", "local"]:
                with self.subTest(system=system, machine=machine, backend=backend), \
                        mock.patch.object(sys, "platform", system), \
                        mock.patch.object(transcribe.platform, "machine", return_value=machine), \
                        mock.patch.object(transcribe, "ffmpeg_extract") as extract, \
                        mock.patch.object(transcribe, "transcribe_local") as local:
                    self.stderr.seek(0)
                    self.stderr.truncate()
                    with self.assertRaises(SystemExit) as error:
                        self.run_main([str(self.audio), "--backend", backend, "--normalize"],
                                      has_key=False)
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn("requires macOS with Apple Silicon", self.stderr.getvalue())
                    self.assertIn("--backend openai", self.stderr.getvalue())
                    extract.assert_not_called()
                    local.assert_not_called()
                    self.openai.OpenAI.assert_not_called()

    def test_apple_silicon_auto_backend_still_uses_local(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(transcribe.platform, "machine", return_value="arm64"), \
                mock.patch.object(transcribe, "transcribe_local",
                                  return_value=("local text", [])) as local:
            stdout = self.run_main([str(self.audio)], has_key=False)
        self.assertEqual(stdout.getvalue(), "local text\n")
        local.assert_called_once()
        self.openai.OpenAI.assert_not_called()

    def test_openai_backend_remains_available_on_each_platform(self):
        for system, machine in [("linux", "x86_64"), ("win32", "AMD64"),
                                ("darwin", "x86_64"), ("darwin", "arm64")]:
            with self.subTest(system=system, machine=machine), \
                    mock.patch.object(sys, "platform", system), \
                    mock.patch.object(transcribe.platform, "machine", return_value=machine):
                self.client.audio.transcriptions.create.return_value = "API text"
                stdout = self.run_main([str(self.audio), "--backend", "openai"])
                self.assertEqual(stdout.getvalue(), "API text\n")

    def test_openai_backend_requires_key(self):
        with self.assertRaises(SystemExit) as error:
            self.run_main([str(self.audio), "--backend", "openai"], has_key=False)
        self.assertEqual(error.exception.code, 2)
        self.assertIn("OPENAI_API_KEY", self.stderr.getvalue())
        self.openai.OpenAI.assert_not_called()

    def test_subprocess_output_is_decoded_without_locale_dependency(self):
        # Real Python subprocess, with neither ffmpeg nor network access needed.
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xc3\\xa9\\xff')"]
        self.assertEqual(transcribe.run(command), "café\ufffd")

    def test_subprocess_failure_preserves_diagnostic_for_non_utf8_output(self):
        command = [sys.executable, "-c",
                   "import sys; sys.stderr.buffer.write(b'bad \\xff'); sys.exit(1)"]
        with self.assertRaisesRegex(RuntimeError, "bad \ufffd"):
            transcribe.run(command)

    def test_silence_detection_failure_handles_non_utf8_diagnostic(self):
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"bad \xff")
        with mock.patch.object(transcribe.subprocess, "run", return_value=result), \
                self.assertRaisesRegex(RuntimeError, "bad \ufffd"):
            transcribe.condense_silence(self.audio, Path(self.tmp.name) / "out.wav",
                                      noise_db=-40, min_silence=1)


if __name__ == "__main__":
    unittest.main()
