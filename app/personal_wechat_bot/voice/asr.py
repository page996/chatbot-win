from __future__ import annotations

import base64
import os
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.personal_wechat_bot.runtime.resource_gate import ResourceGateTimeout, acquire_gpu


@dataclass(frozen=True)
class AsrHealth:
    backend: str
    available: bool
    detail: str = ""
    model: str = ""
    install: str = ""
    gpu_available: bool = False
    gpu_required: bool = False
    gpu_used: bool = False
    mode: str = "auto"


@dataclass(frozen=True)
class AsrTranscript:
    status: str
    text: str = ""
    backend: str = ""
    model: str = ""
    language: str = ""
    source_path: str = ""
    error: str = ""


class AsrEngine(Protocol):
    def health(self) -> AsrHealth: ...

    def transcribe(self, audio_path: str | Path) -> AsrTranscript: ...


class LocalAsrSubprocessEngine:
    """Project-local ASR wrapper.

    The implementation intentionally runs in a separate Python environment so
    OCR, LibreOffice, and ASR dependencies can evolve independently.
    """

    def __init__(
        self,
        python_executable: str | Path | None = None,
        *,
        model: str = "base",
        language: str = "auto",
        mode: str | None = None,
        timeout_seconds: int = 300,
    ):
        self.python_executable = Path(python_executable) if python_executable else _default_asr_python()
        self.model = model
        self.language = language
        self.mode = _normalize_asr_mode(mode or os.environ.get("CHATBOT_ASR_MODE") or "auto")
        self.timeout_seconds = timeout_seconds
        self._health_cache: AsrHealth | None = None
        self._health_cache_at = 0.0

    def health(self) -> AsrHealth:
        cached = self._cached_health()
        if cached is not None:
            return cached
        if self.python_executable is None or not self.python_executable.exists():
            return self._store_health(AsrHealth(
                backend="local_asr_subprocess",
                available=False,
                model=self.model,
                detail="asr python not found",
                install=(
                    "Create vendor/asr-python with Python 3.10-3.12, then install "
                    f"{_asr_requirements_file(self.mode)} there."
                ),
                gpu_required=self.mode == "gpu",
                mode=self.mode,
            ))
        command = [str(self.python_executable), "-c", _HEALTH_CHECK_CODE]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=_asr_subprocess_env(mode=self.mode),
            )
        except (OSError, subprocess.TimeoutExpired, ResourceGateTimeout) as exc:
            return self._store_health(AsrHealth(
                "local_asr_subprocess",
                False,
                detail=str(exc),
                model=self.model,
                gpu_required=self.mode == "gpu",
                mode=self.mode,
            ))
        payload = _parse_health_payload(completed.stdout)
        if payload:
            gpu_available = bool(payload.get("cuda_available", False))
            cuda_source = str(payload.get("cuda_source") or "").strip()
            funasr_ready = all(bool(payload.get(name, False)) for name in ("funasr", "torch", "torchaudio", "modelscope", "soundfile"))
            whisper_ready = all(bool(payload.get(name, False)) for name in ("faster_whisper",))
            backend_ready = funasr_ready or whisper_ready
            available = backend_ready and (self.mode != "gpu" or gpu_available)
            if self.mode == "gpu" and not gpu_available:
                detail = "GPU ASR required but CUDA is unavailable"
                backend = "local_asr_gpu_required_unavailable"
            elif self.mode == "gpu" and available:
                source_note = f" via {cuda_source}" if cuda_source else ""
                detail = f"GPU ASR backend available{source_note}"
                backend = "local_asr_subprocess_gpu"
            elif available:
                detail = "light CPU ASR available; GPU is reserved for explicit gpu mode" if self.mode == "auto" else "CPU ASR available"
                backend = "local_asr_subprocess_cpu"
            else:
                detail = _missing_asr_runtime_detail(payload, self.mode)
                backend = "local_asr_subprocess"
            return self._store_health(AsrHealth(
                backend,
                available,
                detail=detail,
                model=self.model,
                install=f"vendor/asr-python/Scripts/python.exe -m pip install -r {_asr_requirements_file(self.mode)}",
                gpu_available=gpu_available,
                gpu_required=self.mode == "gpu",
                gpu_used=gpu_available and self.mode == "gpu",
                mode=self.mode,
            ))
        detail = completed.stdout.strip() if completed.returncode == 0 else (completed.stderr or completed.stdout).strip()
        if completed.returncode != 0:
            detail = _missing_dependency_detail(detail)
        return self._store_health(AsrHealth(
            "local_asr_subprocess",
            completed.returncode == 0,
            detail=detail,
            model=self.model,
            install=f"vendor/asr-python/Scripts/python.exe -m pip install -r {_asr_requirements_file(self.mode)}",
            gpu_required=self.mode == "gpu",
            mode=self.mode,
        ))

    def _cached_health(self) -> AsrHealth | None:
        if self._health_cache is None:
            return None
        if time.monotonic() - self._health_cache_at > 60.0:
            return None
        return self._health_cache

    def _store_health(self, health: AsrHealth) -> AsrHealth:
        self._health_cache = health
        self._health_cache_at = time.monotonic()
        return health

    def transcribe(self, audio_path: str | Path) -> AsrTranscript:
        health = self.health()
        source = Path(audio_path)
        if not health.available:
            return AsrTranscript(
                status="blocked",
                backend=health.backend,
                model=self.model,
                source_path=str(source),
                error="local_asr_not_configured",
            )
        worker = Path(__file__).resolve().parents[3] / "scripts" / "local_asr_worker.py"
        command = [
            str(self.python_executable),
            str(worker),
            str(source),
            "--model",
            self.model,
            "--language",
            self.language,
            "--device-mode",
            self.mode,
        ]
        env = _asr_subprocess_env(mode=self.mode)
        try:
            if health.gpu_used:
                with acquire_gpu(reason=f"asr:{source.name}", timeout_seconds=max(30, self.timeout_seconds)):
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout_seconds,
                        check=False,
                        encoding="utf-8",
                        errors="replace",
                        env=env,
                    )
            else:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
        except (OSError, subprocess.TimeoutExpired, ResourceGateTimeout) as exc:
            return AsrTranscript(
                status="failed",
                backend=health.backend,
                model=self.model,
                source_path=str(source),
                error=f"{type(exc).__name__}: {exc}",
            )
        payload_line = ""
        for line in completed.stdout.splitlines():
            if line.startswith("LOCAL_ASR_JSON:"):
                payload_line = line.removeprefix("LOCAL_ASR_JSON:")
        if completed.returncode != 0 and not payload_line:
            return AsrTranscript(
                status="failed",
                backend=health.backend,
                model=self.model,
                source_path=str(source),
                error=(completed.stderr or completed.stdout).strip(),
            )
        if not payload_line:
            return AsrTranscript(
                status="failed",
                backend=health.backend,
                model=self.model,
                source_path=str(source),
                error="missing_asr_worker_payload",
            )
        payload = json.loads(payload_line)
        text_b64 = str(payload.get("text_b64", ""))
        text = base64.b64decode(text_b64.encode("ascii")).decode("utf-8") if text_b64 else ""
        worker_ok = bool(payload.get("ok"))
        worker_error = str(payload.get("error") or "")
        if text.strip():
            status = "transcribed"
        elif worker_ok and not worker_error:
            # The worker ran cleanly but produced no text: this is silence / no
            # detectable speech, not a failure. Mark it "empty" so the caller can
            # tell "recognized nothing" apart from "engine broke" (mirrors OCR).
            status = "empty"
        else:
            status = "failed"
        return AsrTranscript(
            status=status,
            text=text.strip(),
            backend=str(payload.get("backend") or health.backend),
            model=str(payload.get("model") or self.model),
            language=str(payload.get("language") or ""),
            source_path=str(source),
            error=worker_error,
        )


def _default_asr_python() -> Path | None:
    repo_root = Path(__file__).resolve().parents[3]
    candidates = [
        repo_root / "vendor" / "asr-python" / "Scripts" / "python.exe",
        repo_root / "vendor" / "ocr-python" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = shutil.which("python")
    return Path(found) if found else None


def _asr_subprocess_env(*, mode: str = "auto") -> dict[str, str]:
    env = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    env.setdefault("HF_HOME", str(repo_root / "vendor" / "asr-models" / "huggingface"))
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env["CHATBOT_ASR_HEALTH_MODE"] = _normalize_asr_mode(mode)
    return env


def _missing_dependency_detail(detail: str) -> str:
    missing: list[str] = []
    for package, module in (
        ("funasr", "funasr"),
        ("torch", "torch"),
        ("torchaudio", "torchaudio"),
        ("modelscope", "modelscope"),
        ("soundfile", "soundfile"),
        ("faster-whisper", "faster_whisper"),
        ("pocketsphinx", "pocketsphinx"),
        ("SpeechRecognition", "speech_recognition"),
    ):
        if module in detail or package in detail:
            missing.append(package)
    if missing:
        return "missing dependency: " + ", ".join(dict.fromkeys(missing))
    return detail


def _asr_requirements_file(mode: str) -> str:
    return "requirements-asr.txt" if _normalize_asr_mode(mode) == "gpu" else "requirements-asr-light.txt"


def _missing_asr_runtime_detail(payload: dict[str, object], mode: str) -> str:
    normalized_mode = _normalize_asr_mode(mode)
    if normalized_mode == "gpu":
        return "missing dependency: CUDA-capable faster-whisper or FunASR stack (install requirements-asr.txt)"
    missing = []
    if not bool(payload.get("faster_whisper")):
        missing.append("faster-whisper")
    if not bool(payload.get("soundfile")):
        missing.append("soundfile")
    return "missing light ASR dependency: " + ", ".join(missing or ["faster-whisper"])


def _normalize_asr_mode(mode: str) -> str:
    cleaned = str(mode or "auto").strip().lower()
    if cleaned in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if cleaned in {"cpu", "cpu-only", "cpu_only"}:
        return "cpu"
    return "auto"


def _parse_health_payload(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if not line.startswith("ASR_HEALTH_JSON:"):
            continue
        try:
            payload = json.loads(line.removeprefix("ASR_HEALTH_JSON:"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


_HEALTH_CHECK_CODE = r"""
import importlib.util, json, os, shutil, subprocess

def has(name):
    return importlib.util.find_spec(name) is not None

def physical_gpu_available():
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        return subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception:
        return False

health_mode = os.environ.get("CHATBOT_ASR_HEALTH_MODE", "auto").strip().lower()
cuda_available = physical_gpu_available()
cuda_source = ""
if cuda_available:
    cuda_source = "nvidia-smi"
if health_mode == "gpu":
    cuda_available = False
    cuda_source = ""
    try:
        import torch
        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            cuda_source = "torch"
    except Exception:
        cuda_available = False
    if not cuda_available:
        try:
            import ctranslate2
            cuda_available = int(getattr(ctranslate2, "get_cuda_device_count", lambda: 0)()) > 0
            if cuda_available:
                cuda_source = "ctranslate2"
        except Exception:
            cuda_available = False

payload = {
    "funasr": has("funasr"),
    "torch": has("torch"),
    "torchaudio": has("torchaudio"),
    "modelscope": has("modelscope"),
    "soundfile": has("soundfile"),
    "faster_whisper": has("faster_whisper"),
    "ctranslate2": has("ctranslate2"),
    "pocketsphinx": has("pocketsphinx"),
    "speech_recognition": has("speech_recognition"),
    "cuda_available": cuda_available,
    "cuda_source": cuda_source,
}
print("ASR_HEALTH_JSON:" + json.dumps(payload, ensure_ascii=True))
"""
