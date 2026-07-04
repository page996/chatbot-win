from __future__ import annotations

import unittest

from app.personal_wechat_bot.vision.ocr import OcrItem, OcrResult, assemble_layout_text


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


if __name__ == "__main__":
    unittest.main()
