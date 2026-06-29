from __future__ import annotations

import importlib.util
import base64
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class OcrHealth:
    backend: str
    available: bool
    gpu_available: bool = False
    detail: str = ""


class OcrEngine(Protocol):
    def health(self) -> OcrHealth: ...

    def read_text(self, image_path: str | Path) -> str: ...


class PlaceholderGpuOcrEngine:
    def health(self) -> OcrHealth:
        gpu = _nvidia_smi_available()
        missing = [
            name
            for name in ["paddleocr", "easyocr", "rapidocr_onnxruntime", "onnxruntime", "torch"]
            if importlib.util.find_spec(name) is None
        ]
        detail = "missing OCR python packages: " + ", ".join(missing)
        return OcrHealth(backend="gpu_ocr_placeholder", available=False, gpu_available=gpu, detail=detail)

    def read_text(self, image_path: str | Path) -> str:
        raise RuntimeError(self.health().detail)


class RapidOcrSubprocessEngine:
    def __init__(self, python_executable: str | Path = "vendor/ocr-python/Scripts/python.exe"):
        self.python_executable = Path(python_executable)

    def health(self) -> OcrHealth:
        if not self.python_executable.exists():
            return OcrHealth("rapidocr_subprocess", False, _nvidia_smi_available(), "ocr venv python not found")
        command = [
            str(self.python_executable),
            "-c",
            "import rapidocr_onnxruntime, onnxruntime; print('ok')",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return OcrHealth("rapidocr_subprocess", False, _nvidia_smi_available(), str(exc))
        return OcrHealth(
            "rapidocr_subprocess",
            completed.returncode == 0,
            _nvidia_smi_available(),
            (completed.stderr or completed.stdout).strip(),
        )

    def read_text(self, image_path: str | Path) -> str:
        health = self.health()
        if not health.available:
            raise RuntimeError(health.detail)
        worker = Path(__file__).resolve().parents[3] / "scripts" / "rapidocr_worker.py"
        completed = subprocess.run(
            [str(self.python_executable), str(worker), str(image_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        payload_line = ""
        for line in completed.stdout.splitlines():
            if line.startswith("RAPIDOCR_JSON:"):
                payload_line = line.removeprefix("RAPIDOCR_JSON:")
        if not payload_line:
            return ""
        payload = json.loads(payload_line)
        text_b64 = str(payload.get("text_b64", ""))
        if not text_b64:
            return ""
        return base64.b64decode(text_b64.encode("ascii")).decode("utf-8")


def _nvidia_smi_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        completed = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
