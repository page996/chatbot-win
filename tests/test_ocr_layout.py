from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.vision.ocr import (
    OcrItem,
    OcrResult,
    _parse_health_payload,
    assemble_layout_text,
    ocr_rows_payload,
)
import scripts.rapidocr_worker as rapidocr_worker
from scripts.rapidocr_worker import _sticker_false_positive_reason


def _box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


class OcrLayoutTest(unittest.TestCase):
    def test_table_rows_and_columns_are_reconstructed(self) -> None:
        # Two rows, three columns. Feed items out of order to prove sorting.
        items = [
            OcrItem("London", 0.98, _box(200, 50, 260, 70)),
            OcrItem("Name", 0.99, _box(10, 10, 60, 30)),
            OcrItem("City", 0.99, _box(200, 11, 250, 30)),
            OcrItem("29", 0.98, _box(100, 51, 140, 70)),
            OcrItem("Age", 0.99, _box(100, 12, 140, 30)),
            OcrItem("Ava", 0.98, _box(10, 50, 60, 70)),
        ]

        text = assemble_layout_text(items)

        self.assertEqual(text, "Name\tAge\tCity\nAva\t29\tLondon")

    def test_single_column_stays_one_per_line(self) -> None:
        items = [
            OcrItem("49868", 0.9, _box(10, 10, 60, 30)),
            OcrItem("671", 0.9, _box(10, 50, 60, 70)),
            OcrItem("41", 0.9, _box(10, 90, 60, 110)),
        ]

        text = assemble_layout_text(items)

        self.assertEqual(text, "49868\n671\n41")

    def test_low_confidence_items_can_be_filtered(self) -> None:
        items = [
            OcrItem("keep", 0.95, _box(10, 10, 60, 30)),
            OcrItem("watermark", 0.20, _box(10, 50, 60, 70)),
        ]

        text = assemble_layout_text(items, min_score=0.5)

        self.assertEqual(text, "keep")

    def test_empty_items_return_empty_string(self) -> None:
        self.assertEqual(assemble_layout_text([]), "")

    def test_ocr_result_item_count(self) -> None:
        result = OcrResult(text="a", items=[OcrItem("a", 0.9, _box(0, 0, 1, 1))])
        self.assertEqual(result.item_count, 1)

    def test_rows_payload_preserves_cells(self) -> None:
        rows = ocr_rows_payload(
            [
                OcrItem("Name", 0.99, _box(10, 10, 60, 30), backend="gpu"),
                OcrItem("Age", 0.99, _box(100, 10, 140, 30), backend="gpu"),
            ]
        )

        self.assertEqual(rows[0]["text"], "Name\tAge")
        self.assertEqual(rows[0]["cells"][0]["backend"], "gpu")

    def test_sticker_single_j_false_positive_is_suppressed(self) -> None:
        reason = _sticker_false_positive_reason(
            [{"text": "J", "score": 0.55}],
            {"sticker_candidate": True, "width": 128, "height": 128, "alpha_ratio": 0.2},
        )

        self.assertEqual(reason, "likely_sticker_single_char_false_positive")
        self.assertEqual(_sticker_false_positive_reason([{"text": "J", "score": 0.55}], {"sticker_candidate": False}), "")

    def test_auto_ocr_health_keeps_gpu_disabled_when_cuda_is_ready(self) -> None:
        stdout = (
            "OCR_HEALTH_JSON:"
            '{"rapidocr": true, "onnxruntime": true, "paddleocr": true, "paddle": true, '
            '"paddle_cuda": true, "cuda_device_count": 1, "gpu_available": true, "detail": ""}'
        )

        health = _parse_health_payload(stdout, mode="auto")

        self.assertIsNotNone(health)
        self.assertTrue(health.available)
        self.assertTrue(health.gpu_available)
        self.assertFalse(health.gpu_used)
        self.assertEqual(health.backend, "rapidocr_cpu_subprocess")

    def test_gpu_ocr_health_requires_gpu_mode(self) -> None:
        stdout = (
            "OCR_HEALTH_JSON:"
            '{"rapidocr": true, "onnxruntime": true, "paddleocr": true, "paddle": true, '
            '"paddle_cuda": true, "cuda_device_count": 1, "gpu_available": true, "detail": ""}'
        )

        health = _parse_health_payload(stdout, mode="gpu")

        self.assertIsNotNone(health)
        self.assertTrue(health.available)
        self.assertTrue(health.gpu_used)
        self.assertEqual(health.backend, "paddleocr_gpu_subprocess")

    def test_rapidocr_worker_auto_does_not_try_paddle_gpu(self) -> None:
        variant = {"path": Path("image.png"), "scale": 1.0, "offset_x": 0.0, "offset_y": 0.0, "variant": "original"}
        with (
            mock.patch.object(rapidocr_worker, "_image_variants", return_value=([variant], [])),
            mock.patch.object(rapidocr_worker, "_image_profile", return_value={"sticker_candidate": False}),
            mock.patch.object(
                rapidocr_worker,
                "_run_rapidocr",
                return_value=[{"text": "hello", "score": 0.9, "box": [], "backend": "rapidocr"}],
            ),
            mock.patch.object(rapidocr_worker, "_run_paddleocr") as paddle,
            mock.patch("sys.argv", ["rapidocr_worker.py", "image.png"]),
            mock.patch("builtins.print"),
        ):
            status = rapidocr_worker.main()

        self.assertEqual(status, 0)
        paddle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
