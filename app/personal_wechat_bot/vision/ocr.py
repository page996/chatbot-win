from __future__ import annotations

import importlib.util
import base64
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class OcrHealth:
    backend: str
    available: bool
    gpu_available: bool = False
    detail: str = ""


@dataclass(frozen=True)
class OcrItem:
    """A single OCR detection: text plus its quad box and confidence."""

    text: str
    score: float = 0.0
    box: list[list[float]] = field(default_factory=list)


@dataclass(frozen=True)
class OcrResult:
    """Structured OCR output: raw items plus a layout-aware text reconstruction."""

    text: str
    items: list[OcrItem] = field(default_factory=list)

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
    usable = [item for item in items if item.text.strip() and item.score >= min_score]
    if not usable:
        return ""
    measured = [(item, _box_bounds(item.box)) for item in usable]
    # Median box height sets the row-grouping tolerance.
    heights = sorted(bounds[3] - bounds[1] for _, bounds in measured if bounds[3] > bounds[1])
    median_height = heights[len(heights) // 2] if heights else 0.0
    tolerance = median_height * 0.6 if median_height else 0.0
    # Sort primarily by vertical position (top edge), then horizontal.
    measured.sort(key=lambda entry: (entry[1][1], entry[1][0]))
    rows: list[list[tuple[OcrItem, tuple[float, float, float, float]]]] = []
    for item, bounds in measured:
        top = bounds[1]
        placed = False
        if rows and tolerance:
            row_top = rows[-1][0][1][1]
            if abs(top - row_top) <= tolerance:
                rows[-1].append((item, bounds))
                placed = True
        if not placed:
            rows.append([(item, bounds)])
    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda entry: entry[1][0])
        cells = [entry[0].text.strip() for entry in row if entry[0].text.strip()]
        if cells:
            lines.append("\t".join(cells) if len(cells) > 1 else cells[0])
    return "\n".join(lines)


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

    def read_structured(self, image_path: str | Path) -> OcrResult:
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
        return self.read_structured(image_path).text

    def read_structured(self, image_path: str | Path) -> OcrResult:
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
        return OcrResult(text=text, items=items)


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
            box_points = [list(point) for point in box] if isinstance(box, (list, tuple)) else []
            items.append(OcrItem(text=text, score=score, box=box_points))
    return items


def _nvidia_smi_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        completed = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
