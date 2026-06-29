from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.vision.ocr_tool import OcrImageTool
from app.personal_wechat_bot.vision.ocr import OcrHealth


class OcrImageToolTest(unittest.TestCase):
    def test_ocr_tool_reads_allowed_image_and_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.png"
            image.write_bytes(b"fake image")
            tool = OcrImageTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                allowed_input_roots=[root],
                max_input_bytes=1000,
                ocr_engine=_FakeOcr("image text"),
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="vision.ocr",
                    call_id="call1",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"input_path": str(image)},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertIn("image text", result.summary)
            self.assertTrue(Path(result.output_refs[0]).exists())
            self.assertEqual(result.payload["text"], "image text")

    def test_ocr_tool_blocks_when_dependency_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.png"
            image.write_bytes(b"fake image")
            tool = OcrImageTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                allowed_input_roots=[root],
                max_input_bytes=1000,
                ocr_engine=_MissingOcr(),
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="vision.ocr",
                    call_id="call1",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"input_path": str(image)},
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.error, "missing")


class _FakeOcr:
    def __init__(self, text: str):
        self.text = text

    def health(self) -> OcrHealth:
        return OcrHealth("fake", True, False, "")

    def read_text(self, image_path: str | Path) -> str:
        return self.text


class _MissingOcr:
    def health(self) -> OcrHealth:
        return OcrHealth("fake", False, False, "missing")

    def read_text(self, image_path: str | Path) -> str:
        raise AssertionError("should not read")


if __name__ == "__main__":
    unittest.main()
