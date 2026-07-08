from __future__ import annotations

import unittest
import os
import tempfile
from pathlib import Path
from unittest import mock

import scripts.local_asr_worker as worker
from app.personal_wechat_bot.voice import asr as asr_module
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine


class LocalAsrWorkerTest(unittest.TestCase):
    def test_cpu_device_prefers_faster_whisper_before_funasr(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(
                worker,
                "_try_faster_whisper",
                return_value={
                    "ok": True,
                    "backend": "faster_whisper_cpu",
                    "model": "base",
                    "language": "zh",
                    "error": "",
                    "text_b64": "",
                },
            ) as whisper,
            mock.patch.object(worker, "_try_funasr") as funasr,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        whisper.assert_called_once()
        funasr.assert_not_called()

    def test_cpu_device_falls_back_to_funasr_when_whisper_model_unavailable(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(
                worker,
                "_try_faster_whisper",
                return_value={
                    "ok": False,
                    "backend": "faster_whisper_cpu",
                    "error": "LocalEntryNotFoundError: cannot find the appropriate snapshot",
                    "text_b64": "",
                },
            ),
            mock.patch.object(
                worker,
                "_try_funasr",
                return_value={
                    "ok": True,
                    "backend": "funasr_cpu",
                    "model": "base",
                    "language": "zh",
                    "error": "",
                    "text_b64": "",
                },
            ) as funasr,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        funasr.assert_called_once()

    def test_auto_device_mode_uses_cpu_even_when_cuda_is_available(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(worker, "_cuda_available", return_value=True) as cuda_available,
            mock.patch.object(
                worker,
                "_transcribe_with_backends",
                return_value={"ok": True, "backend": "faster_whisper_cpu", "text_b64": "", "error": ""},
            ) as transcribe,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        transcribe.assert_called_once()
        cuda_available.assert_not_called()
        self.assertEqual(transcribe.call_args.kwargs["device"], "cpu")
        self.assertFalse(transcribe.call_args.kwargs["strict_gpu"])

    def test_gpu_device_mode_uses_cuda_when_available(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(worker, "_cuda_available", return_value=True),
            mock.patch.object(
                worker,
                "_transcribe_with_backends",
                return_value={"ok": True, "backend": "faster_whisper_gpu", "text_b64": "", "error": ""},
            ) as transcribe,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav", "--device-mode", "gpu"])

        self.assertEqual(status, 0)
        transcribe.assert_called_once()
        self.assertEqual(transcribe.call_args.kwargs["device"], "cuda")
        self.assertTrue(transcribe.call_args.kwargs["strict_gpu"])

    def test_whisper_non_fallback_error_is_returned_without_funasr(self) -> None:
        with (
            mock.patch.object(worker, "_prepare_audio", return_value=(Path("voice.wav"), [])),
            mock.patch.object(
                worker,
                "_try_faster_whisper",
                return_value={
                    "ok": False,
                    "backend": "faster_whisper_cpu",
                    "error": "ValueError: unsupported audio format",
                    "text_b64": "",
                },
            ),
            mock.patch.object(worker, "_try_funasr") as funasr,
            mock.patch("builtins.print"),
        ):
            status = worker.main(["voice.wav"])

        self.assertEqual(status, 0)
        funasr.assert_not_called()

    def test_cuda_detection_uses_ctranslate2_when_torch_cuda_is_absent(self) -> None:
        fake_torch = mock.Mock()
        fake_torch.cuda.is_available.return_value = False
        fake_ctranslate2 = mock.Mock()
        fake_ctranslate2.get_cuda_device_count.return_value = 1

        real_import = __import__

        def import_side_effect(name, *args, **kwargs):
            if name == "torch":
                return fake_torch
            if name == "ctranslate2":
                return fake_ctranslate2
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=import_side_effect):
            self.assertTrue(worker._cuda_available())

    def test_whisper_model_candidates_use_configured_fallback_without_duplicates(self) -> None:
        with mock.patch.dict(os.environ, {"CHATBOT_WIN_ASR_FALLBACK_MODELS": "tiny,base,small"}, clear=False):
            self.assertEqual(worker._whisper_model_candidates("base"), ["base", "tiny", "small"])

    def test_local_whisper_model_ready_rejects_incomplete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "hub" / "models--Systran--faster-whisper-base"
            (cache / "snapshots" / "abc").mkdir(parents=True)
            (cache / "blobs").mkdir()
            (cache / "blobs" / "model.incomplete").write_bytes(b"")
            with mock.patch.dict(os.environ, {"HF_HOME": tmp}, clear=False):
                self.assertFalse(worker._local_whisper_model_ready("base"))

    def test_auto_asr_health_reports_cpu_path_when_cuda_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            python_path = Path(tmp) / "python.exe"
            python_path.write_text("", encoding="utf-8")
            completed = mock.Mock(
                returncode=0,
                stdout=(
                    "ASR_HEALTH_JSON:"
                    '{"funasr": false, "torch": true, "torchaudio": false, "modelscope": false, '
                    '"soundfile": true, "faster_whisper": true, "ctranslate2": true, '
                    '"pocketsphinx": false, "speech_recognition": false, '
                    '"cuda_available": true, "cuda_source": "nvidia-smi"}'
                ),
                stderr="",
            )
            with mock.patch.object(asr_module.subprocess, "run", return_value=completed):
                health = LocalAsrSubprocessEngine(python_path, mode="auto").health()

        self.assertTrue(health.available)
        self.assertTrue(health.gpu_available)
        self.assertFalse(health.gpu_used)
        self.assertEqual(health.backend, "local_asr_subprocess_cpu")

    def test_gpu_asr_health_reports_gpu_path_when_cuda_runtime_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            python_path = Path(tmp) / "python.exe"
            python_path.write_text("", encoding="utf-8")
            completed = mock.Mock(
                returncode=0,
                stdout=(
                    "ASR_HEALTH_JSON:"
                    '{"funasr": false, "torch": true, "torchaudio": false, "modelscope": false, '
                    '"soundfile": true, "faster_whisper": true, "ctranslate2": true, '
                    '"pocketsphinx": false, "speech_recognition": false, '
                    '"cuda_available": true, "cuda_source": "ctranslate2"}'
                ),
                stderr="",
            )
            with mock.patch.object(asr_module.subprocess, "run", return_value=completed):
                health = LocalAsrSubprocessEngine(python_path, mode="gpu").health()

        self.assertTrue(health.available)
        self.assertTrue(health.gpu_available)
        self.assertTrue(health.gpu_used)
        self.assertEqual(health.backend, "local_asr_subprocess_gpu")


if __name__ == "__main__":
    unittest.main()
