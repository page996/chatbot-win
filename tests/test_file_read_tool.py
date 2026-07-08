from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.tools.file_read import FileReadTool
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AttachmentParseResult
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


class FileReadToolTest(unittest.TestCase):
    def test_reads_chunk_by_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "long.txt"
            source.write_text("seed", encoding="utf-8")
            workspace = FileWorkspace(root / "file_workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "\u4e2d" * 7000),
            )
            tool = FileReadTool(root)

            result = tool.run(
                ToolCallRequest(
                    tool_name="file.read",
                    call_id="read1",
                    conversation_id="c1",
                    requested_by="test",
                    arguments={"file_id": staged.file_id, "artifact": "chunk", "chunk_index": 2},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertIn("# Chunk 2/", result.payload["text"])
            self.assertTrue(Path(result.output_refs[0]).is_file())

    def test_original_binary_returns_multimodal_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "file.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF")
            workspace = FileWorkspace(root / "file_workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "pdf", "summary", "pdf text"))
            tool = FileReadTool(root)

            result = tool.run(
                ToolCallRequest(
                    tool_name="file.read",
                    call_id="read2",
                    conversation_id="c1",
                    requested_by="test",
                    arguments={"file_id": staged.file_id, "artifact": "original"},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertTrue(result.payload["binary"])
            self.assertTrue(result.payload["multimodal_ref_available"])
            self.assertEqual(result.output_refs, [str(Path(staged.staged_path))])

    def test_redacts_internal_urls_unless_explicitly_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "note.txt"
            source.write_text("seed", encoding="utf-8")
            workspace = FileWorkspace(root / "file_workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "link https://example.com/path"),
            )
            tool = FileReadTool(root)

            hidden = tool.run(
                ToolCallRequest(
                    tool_name="file.read",
                    call_id="read-hidden",
                    conversation_id="c1",
                    requested_by="test",
                    arguments={"file_id": staged.file_id, "artifact": "full_text"},
                )
            )
            pinned = tool.run(
                ToolCallRequest(
                    tool_name="file.read",
                    call_id="read-pinned",
                    conversation_id="c1",
                    requested_by="test",
                    arguments={"file_id": staged.file_id, "artifact": "full_text", "pin_internal_urls": True},
                )
            )

            self.assertEqual(hidden.status, "completed")
            self.assertTrue(hidden.payload["internal_urls_hidden"])
            self.assertNotIn("https://example.com/path", hidden.payload["text"])
            self.assertIn("[file-internal-url-redacted]", hidden.payload["text"])
            self.assertFalse(pinned.payload["internal_urls_hidden"])
            self.assertIn("https://example.com/path", pinned.payload["text"])

    def test_reads_ocr_layout_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "image.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")
            workspace = FileWorkspace(root / "file_workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "image", "summary", "A\tB\n1\t2"),
            )
            tool = FileReadTool(root)

            result = tool.run(
                ToolCallRequest(
                    tool_name="file.read",
                    call_id="read3",
                    conversation_id="c1",
                    requested_by="test",
                    arguments={"file_id": staged.file_id, "artifact": "ocr_table", "chunk_index": 1},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertIn('"artifact_type": "ocr_layout_rows"', result.payload["text"])
            self.assertTrue(Path(result.output_refs[0]).is_file())


if __name__ == "__main__":
    unittest.main()
