from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.document.translator import FakeDocumentTranslateTool
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots


class ToolPermissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)
        self.config = load_config(self.data_dir)
        self.inbox = self.data_dir / "inbox"
        self.tool = FakeDocumentTranslateTool(
            self.data_dir / "tool_outputs",
            FileIndex(self.data_dir / "file_index.sqlite"),
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self, input_path: str) -> ToolCallRequest:
        return ToolCallRequest(
            tool_name="document.translate",
            call_id="call_test",
            conversation_id="private:wxid_xiaoming",
            requested_by="chatbot",
            arguments={"input_path": input_path},
        )

    def test_relative_file_path_is_resolved_inside_inbox(self) -> None:
        source = self.inbox / "note.txt"
        source.write_text("hello from inbox", encoding="utf-8")

        result = self.tool.run(self.request("note.txt"))

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.output_refs[0].endswith("note翻译.docx"))

    def test_absolute_file_outside_allowed_root_is_blocked(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")

        result = self.tool.run(self.request(str(outside)))

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.summary, "文件读取被文件访问权限阻止")
        self.assertIn("outside allowed roots", result.error or "")

    def test_disallowed_extension_is_blocked(self) -> None:
        source = self.inbox / "library.dll"
        source.write_text("nope", encoding="utf-8")

        result = self.tool.run(self.request("library.dll"))

        self.assertEqual(result.status, "blocked")
        self.assertIn("extension not allowed", result.error or "")


if __name__ == "__main__":
    unittest.main()
