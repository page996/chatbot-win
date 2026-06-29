from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AttachmentParseResult
from app.personal_wechat_bot.workspace.attachment_pipeline import AttachmentPipeline, IncomingAttachment
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


class AttachmentPipelineTest(unittest.TestCase):
    def test_process_stages_parses_and_indexes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            source = inbox / "note.txt"
            source.write_text("hello", encoding="utf-8")
            pipeline = AttachmentPipeline(
                file_index=FileIndex(root / "file_index.sqlite"),
                file_workspace=FileWorkspace(root / "file_workspace"),
                attachment_parser=_Parser("parsed text"),
                allowed_input_roots=[inbox],
                allowed_extensions=[".txt"],
                max_input_bytes=1024,
            )

            result = pipeline.process(
                IncomingAttachment(path="note.txt", original_name="note.txt", kind="file"),
                conversation_id="conv1",
                session_id="session1",
            )

            self.assertEqual(result["status"], "indexed")
            self.assertEqual(result["parse"]["text"], "parsed text")
            self.assertEqual(result["artifacts"]["chunk_count"], 1)
            self.assertTrue(Path(result["workspace"]["staged_path"]).exists())
            self.assertTrue((Path(result["workspace"]["derived_dir"]) / "content.md").exists())

    def test_process_returns_chunk_artifacts_for_long_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            source = inbox / "long.txt"
            source.write_text("hello", encoding="utf-8")
            long_text = "\n\n".join(f"row {index} " + ("x" * 500) for index in range(60))
            pipeline = AttachmentPipeline(
                file_index=FileIndex(root / "file_index.sqlite"),
                file_workspace=FileWorkspace(root / "file_workspace"),
                attachment_parser=_Parser(long_text),
                allowed_input_roots=[inbox],
                allowed_extensions=[".txt"],
                max_input_bytes=1024,
            )

            result = pipeline.process(
                IncomingAttachment(path="long.txt", original_name="long.txt", kind="file"),
                conversation_id="conv1",
                session_id="session1",
            )

            self.assertEqual(result["status"], "indexed")
            self.assertGreater(result["artifacts"]["chunk_count"], 1)
            self.assertTrue(Path(result["artifacts"]["chunks"][0]["path"]).exists())

    def test_process_blocks_disallowed_file_without_copying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            workspace = FileWorkspace(root / "file_workspace")
            pipeline = AttachmentPipeline(
                file_index=FileIndex(root / "file_index.sqlite"),
                file_workspace=workspace,
                attachment_parser=_Parser("unused"),
                allowed_input_roots=[root / "inbox"],
                allowed_extensions=[".txt"],
                max_input_bytes=1024,
            )

            result = pipeline.process(
                IncomingAttachment(path=str(outside), original_name="outside.txt", kind="file"),
                conversation_id="conv1",
                session_id="session1",
            )

            self.assertEqual(result["status"], "blocked")
            self.assertFalse(any(workspace.root.rglob("outside.txt")))


class _Parser:
    def __init__(self, text: str):
        self.text = text

    def parse(self, path: str | Path) -> AttachmentParseResult:
        return AttachmentParseResult("parsed", "text", "summary", self.text)


if __name__ == "__main__":
    unittest.main()
