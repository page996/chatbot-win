from __future__ import annotations

import importlib.util
import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.personal_wechat_bot.runtime.resource_gate import acquire_gpu


@dataclass(frozen=True)
class OcrHealth:
    backend: str
    available: bool
    gpu_available: bool = False
    gpu_required: bool = False
    gpu_used: bool = False
    mode: str = "auto"
    detail: str = ""


@dataclass(frozen=True)
class OcrItem:
    """A single OCR detection: text plus its quad box and confidence."""

    text: str
    score: float = 0.0
    box: list[list[float]] = field(default_factory=list)
    backend: str = ""


@dataclass(frozen=True)
class OcrResult:
    """Structured OCR output: raw items plus a layout-aware text reconstruction."""

    text: str
    items: list[OcrItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return len(self.items)


class OcrEngine(Protocol):
    def health(self) -> OcrHealth: ...

    def read_text(self, image_path: str | Path) -> str: ...

    def read_structured(self, image_path: str | Path) -> OcrResult: ...


def assemble_layout_text(items: list[OcrItem], *, min_score: float = 0.0) -> str:
    """Reconstruct reading order from detection boxes, preserving row/column layout.

    RapidOCR returns one detection box per text fragment, ordered roughly
    top-to-bottom. Naively joining them with newlines destroys tabular layout —
    a table row of N cells becomes N separate lines. Here we group fragments
    into rows by vertical overlap, order each row left-to-right, and join cells
    in a row with a tab so columns stay aligned and the reader can parse the
    table. This is what makes a long numeric table survive OCR instead of
    collapsing into one-number-per-line.
    """
    rows = assemble_layout_rows(items, min_score=min_score)
    lines: list[str] = []
    for row in rows:
        cells = [item.text.strip() for item in row if item.text.strip()]
        if cells:
            lines.append("\t".join(cells) if len(cells) > 1 else cells[0])
    return "\n".join(lines)


def assemble_layout_rows(items: list[OcrItem], *, min_score: float = 0.0) -> list[list[OcrItem]]:
    """Return OCR items grouped into visual rows and sorted left-to-right."""

    usable = [item for item in items if item.text.strip() and item.score >= min_score]
    if not usable:
        return []
    measured = [(item, _box_bounds(item.box)) for item in usable]
    heights = sorted(bounds[3] - bounds[1] for _, bounds in measured if bounds[3] > bounds[1])
    median_height = heights[len(heights) // 2] if heights else 0.0
    tolerance = max(4.0, median_height * 0.65) if median_height else 4.0
    measured.sort(key=lambda entry: (_box_center_y(entry[1]), entry[1][0]))
    rows: list[list[tuple[OcrItem, tuple[float, float, float, float]]]] = []
    row_centers: list[float] = []
    for item, bounds in measured:
        center_y = _box_center_y(bounds)
        placed_at: int | None = None
        for row_index, row_center in enumerate(row_centers):
            if abs(center_y - row_center) <= tolerance:
                placed_at = row_index
                break
        if placed_at is None:
            rows.append([(item, bounds)])
            row_centers.append(center_y)
            continue
        rows[placed_at].append((item, bounds))
        row_centers[placed_at] = sum(_box_center_y(entry[1]) for entry in rows[placed_at]) / len(rows[placed_at])

    sorted_rows: list[list[OcrItem]] = []
    for row in rows:
        row.sort(key=lambda entry: entry[1][0])
        sorted_rows.append([entry[0] for entry in row])
    return sorted_rows


def ocr_rows_payload(items: list[OcrItem], *, min_score: float = 0.0) -> list[dict[str, Any]]:
    rows = assemble_layout_rows(items, min_score=min_score)
    payload: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[dict[str, Any]] = []
        for column_index, item in enumerate(row, start=1):
            text = item.text.strip()
            if not text:
                continue
            cells.append(
                {
                    "column_index": column_index,
                    "text": text,
                    "score": round(float(item.score), 4),
                    "box": item.box,
                    "backend": item.backend,
                    "bounds": list(_box_bounds(item.box)),
                }
            )
        if cells:
            payload.append(
                {
                    "row_index": row_index,
                    "cell_count": len(cells),
                    "text": "\t".join(str(cell["text"]) for cell in cells),
                    "cells": cells,
                }
            )
    return payload


def _box_bounds(box: Any) -> tuple[float, float, float, float]:
    """Return (left, top, right, bottom) from a quad box; (0,0,0,0) if unusable."""
    if not isinstance(box, (list, tuple)) or not box:
        return (0.0, 0.0, 0.0, 0.0)
    xs: list[float] = []
    ys: list[float] = []
    for point in box:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                xs.append(float(point[0]))
                ys.append(float(point[1]))
            except (TypeError, ValueError):
                continue
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _box_center_y(bounds: tuple[float, float, float, float]) -> float:
    return (bounds[1] + bounds[3]) / 2.0


class PlaceholderGpuOcrEngine:
    def health(self) -> OcrHealth:
        gpu = _nvidia_smi_available()
        missing = [
            name
            for name in ["paddleocr", "paddle", "rapidocr_onnxruntime", "onnxruntime"]
            if importlib.util.find_spec(name) is None
        ]
        detail = "missing OCR python packages: " + ", ".join(missing)
        return OcrHealth(backend="gpu_ocr_placeholder", available=False, gpu_available=gpu, detail=detail)

    def read_text(self, image_path: str | Path) -> str:
        raise RuntimeError(self.health().detail)

    def read_structured(self, image_path: str | Path) -> OcrResult:
        raise RuntimeError(self.health().detail)


class RapidOcrSubprocessEngine:
    def __init__(
        self,
        python_executable: str | Path = "vendor/ocr-python/Scripts/python.exe",
        *,
        mode: str = "auto",
    ):
        self.python_executable = Path(python_executable)
        self.mode = _normalize_ocr_mode(mode)
        self._health_cache: OcrHealth | None = None
        self._health_cache_at = 0.0

    def health(self) -> OcrHealth:
        cached = self._cached_health()
        if cached is not None:
            return cached
        if not self.python_executable.exists():
            return self._store_health(OcrHealth(
                "rapidocr_subprocess",
                False,
                _nvidia_smi_available(),
                self.mode == "gpu",
                False,
                self.mode,
                "ocr venv python not found",
            ))
        command = [str(self.python_executable), "-c", _HEALTH_CHECK_CODE]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_ocr_subprocess_env(mode=self.mode),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._store_health(OcrHealth("rapidocr_subprocess", False, _nvidia_smi_available(), self.mode == "gpu", False, self.mode, str(exc)))
        health = _parse_health_payload(completed.stdout, mode=self.mode)
        if health:
            return self._store_health(health)
        return self._store_health(OcrHealth(
            "rapidocr_subprocess",
            completed.returncode == 0,
            _nvidia_smi_available(),
            self.mode == "gpu",
            False,
            self.mode,
            (completed.stderr or completed.stdout).strip(),
        ))

    def _cached_health(self) -> OcrHealth | None:
        if self._health_cache is None:
            return None
        if time.monotonic() - self._health_cache_at > 60.0:
            return None
        return self._health_cache

    def _store_health(self, health: OcrHealth) -> OcrHealth:
        self._health_cache = health
        self._health_cache_at = time.monotonic()
        return health

    def read_text(self, image_path: str | Path) -> str:
        return self.read_structured(image_path).text

    def read_structured(self, image_path: str | Path) -> OcrResult:
        health = self.health()
        if not health.available:
            raise RuntimeError(health.detail)
        worker = Path(__file__).resolve().parents[3] / "scripts" / "rapidocr_worker.py"
        command = [
            str(self.python_executable),
            str(worker),
            str(image_path),
            *(["--prefer-gpu"] if self.mode == "gpu" else []),
            "--backend",
            self.mode,
        ]
        if health.gpu_used:
            with acquire_gpu(reason=f"ocr:{Path(image_path).name}", timeout_seconds=300):
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=240,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    env=_ocr_subprocess_env(mode=self.mode),
                )
        else:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
                encoding="utf-8",
                errors="replace",
                env=_ocr_subprocess_env(mode=self.mode),
            )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip())
        payload_line = ""
        for line in completed.stdout.splitlines():
            if line.startswith("RAPIDOCR_JSON:"):
                payload_line = line.removeprefix("RAPIDOCR_JSON:")
        if not payload_line:
            return OcrResult(text="", items=[])
        payload = json.loads(payload_line)
        items = _items_from_payload(payload)
        # Prefer the layout-aware reconstruction; fall back to the worker's
        # newline join only when no boxes were returned.
        text = assemble_layout_text(items)
        if not text:
            text_b64 = str(payload.get("text_b64", ""))
            if text_b64:
                text = base64.b64decode(text_b64.encode("ascii")).decode("utf-8")
        metadata = {
            "backends": payload.get("backends", []),
            "variant_count": payload.get("variant_count", 0),
            "gpu_requested": bool(payload.get("gpu_requested", False)),
            "gpu_attempted": bool(payload.get("gpu_attempted", False)),
            "gpu_required": self.mode == "gpu",
            "gpu_used": bool(payload.get("gpu_used", False)),
            "ocr_mode": self.mode,
            "image_profile": payload.get("image_profile", {}),
            "filter_reason": str(payload.get("filter_reason", "")),
        }
        return OcrResult(text=text, items=items, metadata=metadata)


class GpuOcrSubprocessEngine(RapidOcrSubprocessEngine):
    """Mode-aware OCR engine; GPU is used only by explicit gpu mode."""

    def __init__(self, python_executable: str | Path = "vendor/ocr-python/Scripts/python.exe", *, mode: str = "auto"):
        super().__init__(python_executable, mode=mode)


def build_default_ocr_engine(
    python_executable: str | Path = "vendor/ocr-python/Scripts/python.exe",
    *,
    mode: str | None = None,
) -> OcrEngine:
    return GpuOcrSubprocessEngine(python_executable, mode=mode or os.environ.get("CHATBOT_OCR_MODE") or "auto")


def _items_from_payload(payload: Any) -> list[OcrItem]:
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items: list[OcrItem] = []
    if isinstance(raw_items, list):
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text", ""))
            try:
                score = float(entry.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            box = entry.get("box", [])
            box_points: list[list[float]] = []
            if isinstance(box, (list, tuple)):
                for point in box:
                    # Skip malformed points (scalars, wrong length) defensively so
                    # one bad detection never turns the whole image into an error.
                    if isinstance(point, (list, tuple)):
                        box_points.append(list(point))
            items.append(OcrItem(text=text, score=score, box=box_points, backend=str(entry.get("backend", ""))))
    return items


def _parse_health_payload(stdout: str, *, mode: str = "auto") -> OcrHealth | None:
    for line in stdout.splitlines():
        if not line.startswith("OCR_HEALTH_JSON:"):
            continue
        try:
            payload = json.loads(line.removeprefix("OCR_HEALTH_JSON:"))
        except json.JSONDecodeError:
            return None
        gpu_available = bool(payload.get("gpu_available", False))
        gpu_device_count = _int_payload(payload.get("cuda_device_count"), 0)
        gpu_ready = bool(payload.get("paddle_cuda", False)) and gpu_available and gpu_device_count > 0
        rapid_ready = bool(payload.get("rapidocr", False)) and bool(payload.get("onnxruntime", False))
        paddle_ready = bool(payload.get("paddleocr", False)) and bool(payload.get("paddle", False))
        cpu_ready = rapid_ready or paddle_ready
        if mode == "gpu":
            available = gpu_ready
        elif mode == "cpu":
            available = cpu_ready
        else:
            available = rapid_ready
        if mode == "gpu" and not gpu_ready:
            backend = "paddleocr_gpu_required_unavailable"
            detail = "GPU OCR required but CUDA-enabled PaddleOCR is unavailable"
        elif mode == "gpu":
            backend = "paddleocr_gpu_subprocess"
            detail = "GPU PaddleOCR available"
        elif mode == "auto" and rapid_ready:
            backend = "rapidocr_cpu_subprocess"
            detail = "light CPU RapidOCR available; GPU is reserved for explicit gpu mode"
        elif available:
            backend = "ocr_subprocess_cpu_fallback"
            detail = "CPU OCR available"
        else:
            backend = "ocr_subprocess"
            detail = (
                "missing light OCR dependency: rapidocr-onnxruntime"
                if mode == "auto"
                else str(payload.get("detail", "missing OCR python packages"))
            )
        return OcrHealth(
            backend,
            available,
            gpu_available,
            mode == "gpu",
            gpu_ready and mode == "gpu",
            mode,
            detail,
        )
    return None


def _normalize_ocr_mode(mode: str) -> str:
    cleaned = str(mode or "auto").strip().lower()
    if cleaned in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if cleaned in {"cpu", "rapidocr", "cpu-only", "cpu_only"}:
        return "cpu"
    return "auto"


def _nvidia_smi_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        completed = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _ocr_subprocess_env(*, mode: str = "auto") -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("CHATBOT_WIN_REPO_ROOT", str(Path(__file__).resolve().parents[3]))
    env["CHATBOT_OCR_HEALTH_MODE"] = _normalize_ocr_mode(mode)
    return env


def _int_payload(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_HEALTH_CHECK_CODE = r"""
import importlib.util, json, os, shutil, subprocess, sysconfig
from pathlib import Path

def has(name):
    return importlib.util.find_spec(name) is not None

def ensure_nvidia_dll_paths():
    roots = []
    purelib = sysconfig.get_paths().get("purelib", "")
    if purelib:
        roots.append(Path(purelib) / "nvidia")
    repo_root = os.environ.get("CHATBOT_WIN_REPO_ROOT", "").strip()
    if repo_root:
        roots.append(Path(repo_root) / "vendor" / "ocr-python" / "Lib" / "site-packages" / "nvidia")
    rels = [
        Path("cu13") / "bin" / "x86_64",
        Path("cu13") / "bin",
        Path("cu13") / "lib",
        Path("cudnn") / "bin",
    ]
    paths = []
    for root in roots:
        for rel in rels:
            path = root / rel
            if path.is_dir() and path not in paths:
                paths.append(path)
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for path in paths:
            try:
                os.add_dll_directory(str(path))
            except OSError:
                pass
    if paths:
        current = os.environ.get("PATH", "")
        prefix = os.pathsep.join(str(path) for path in paths)
        os.environ["PATH"] = prefix + (os.pathsep + current if current else "")

health_mode = os.environ.get("CHATBOT_OCR_HEALTH_MODE", "auto").strip().lower()
gpu_available = shutil.which("nvidia-smi") is not None
if gpu_available:
    try:
        gpu_available = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5).returncode == 0
    except Exception:
        gpu_available = False
paddle_cuda = False
cuda_device_count = 0
detail = ""
if health_mode == "gpu":
    try:
        ensure_nvidia_dll_paths()
        import paddle
        paddle_cuda = bool(getattr(paddle.device, "is_compiled_with_cuda", lambda: False)())
        if paddle_cuda:
            try:
                cuda_device_count = int(getattr(paddle.device.cuda, "device_count", lambda: 0)())
            except Exception as exc:
                detail = f"paddle_cuda_device_count:{type(exc).__name__}: {exc}"
    except Exception as exc:
        detail = f"paddle_import:{type(exc).__name__}: {exc}"
payload = {
    "rapidocr": has("rapidocr_onnxruntime"),
    "onnxruntime": has("onnxruntime"),
    "paddleocr": has("paddleocr"),
    "paddle": has("paddle"),
    "paddle_cuda": paddle_cuda,
    "cuda_device_count": cuda_device_count,
    "gpu_available": gpu_available,
    "detail": detail,
}
print("OCR_HEALTH_JSON:" + json.dumps(payload, ensure_ascii=True))
"""
