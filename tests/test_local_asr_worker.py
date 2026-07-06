from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import scripts.local_asr_worker as worker


class LocalAsrWorkerTest(unittest.TestCase):
    def test_funasr_model_failure_falls_back_to_faster_whisper(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(
                worker,
                "_try_funasr",
                return_value={
                    "ok": False,
                    "backend": "funasr",
                    "error": "LocalEntryNotFoundError: cannot find the appropriate snapshot",
                    "text_b64": "",
                },
            ),
            mock.patch.object(
                worker,
                "_try_faster_whisper",
                return_value={
                    "ok": True,
                    "backend": "faster_whisper",
                    "model": "base",
                    "language": "zh",
                    "error": "",
                    "text_b64": "",
                },
            ) as whisper,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        whisper.assert_called_once()

    def test_funasr_non_fallback_error_is_returned_without_whisper(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(
                worker,
                "_try_funasr",
                return_value={
                    "ok": False,
                    "backend": "funasr",
                    "error": "ValueError: unsupported audio format",
                    "text_b64": "",
                },
            ),
            mock.patch.object(worker, "_try_faster_whisper") as whisper,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        whisper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
