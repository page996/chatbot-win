from __future__ import annotations

import base64
import os
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AsrHealth:
    backend: str
    available: bool
    detail: str = ""
    model: str = ""
    install: str = ""


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
        timeout_seconds: int = 300,
    ):
        self.python_executable = Path(python_executable) if python_executable else _default_asr_python()
        self.model = model
        self.language = language
        self.timeout_seconds = timeout_seconds

    def health(self) -> AsrHealth:
        if self.python_executable is None or not self.python_executable.exists():
            return AsrHealth(
                backend="local_asr_subprocess",
                available=False,
                model=self.model,
                detail="asr python not found",
                install=(
                    "Create vendor/asr-python with Python 3.10-3.12, then install "
                    "requirements-asr.txt there."
                ),
            )
        command = [
            str(self.python_executable),
            "-c",
            "import faster_whisper; import pocketsphinx; print('ok')",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AsrHealth("local_asr_subprocess", False, detail=str(exc), model=self.model)
        detail = (completed.stderr or completed.stdout).strip()
        if completed.returncode != 0:
            detail = _missing_dependency_detail(detail)
        return AsrHealth(
            "local_asr_subprocess",
            completed.returncode == 0,
            detail=detail,
            model=self.model,
            install="vendor/asr-python/Scripts/python.exe -m pip install -r requirements-asr.txt",
        )

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
        command = [str(self.python_executable), str(worker), str(source), "--model", self.model, "--language", self.language]
        env = _asr_subprocess_env()
        try:
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
        except (OSError, subprocess.TimeoutExpired) as exc:
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


def _asr_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    env.setdefault("HF_HOME", str(repo_root / "vendor" / "asr-models" / "huggingface"))
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    return env


def _missing_dependency_detail(detail: str) -> str:
    missing: list[str] = []
    for package, module in (
        ("faster-whisper", "faster_whisper"),
        ("pocketsphinx", "pocketsphinx"),
        ("SpeechRecognition", "speech_recognition"),
    ):
        if module in detail or package in detail:
            missing.append(package)
    if missing:
        return "missing dependency: " + ", ".join(dict.fromkeys(missing))
    return detail
