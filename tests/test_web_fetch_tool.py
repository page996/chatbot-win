from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.web.fetch import WebFetchTool
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


class WebFetchToolTest(unittest.TestCase):
    def test_fetches_html_text_to_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                "<html><body><h1>Title</h1><p>Hello page text.</p><script>ignored()</script></body></html>",
                encoding="utf-8",
            )
            server = _LocalServer(root)
            server.start()
            try:
                tool = WebFetchTool(root / "outputs", FileIndex(root / "files.sqlite"))
                result = tool.run(
                    ToolCallRequest(
                        tool_name="web.fetch",
                        call_id="call1",
                        conversation_id="conv1",
                        requested_by="test",
                        arguments={"url": server.url("/index.html")},
                    )
                )
            finally:
                server.stop()

            self.assertEqual(result.status, "completed")
            self.assertIn("Hello page text.", result.summary)
            content = Path(result.output_refs[0]).read_text(encoding="utf-8")
            self.assertIn("Title", content)
            self.assertIn("Hello page text.", content)
            self.assertNotIn("ignored()", content)

    def test_blocks_login_wall_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "login.html").write_text(
                "<html><body><p>请登录后继续查看完整内容。</p></body></html>",
                encoding="utf-8",
            )
            server = _LocalServer(root)
            server.start()
            try:
                tool = WebFetchTool(root / "outputs", FileIndex(root / "files.sqlite"))
                result = tool.run(
                    ToolCallRequest(
                        tool_name="web.fetch",
                        call_id="call-login",
                        conversation_id="conv1",
                        requested_by="test",
                        arguments={"url": server.url("/login.html")},
                    )
                )
            finally:
                server.stop()

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.error, "login_or_paywall_detected")
            self.assertEqual(result.payload["content_kind"], "text")

    def test_file_url_enters_local_file_workspace_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report.txt").write_text("file url body\nsecond line", encoding="utf-8")
            server = _LocalServer(root)
            server.start()
            try:
                workspace = FileWorkspace(root / "file_workspace")
                tool = WebFetchTool(
                    root / "outputs",
                    FileIndex(root / "files.sqlite"),
                    file_workspace=workspace,
                    attachment_parser=BackendAttachmentParser(),
                )
                result = tool.run(
                    ToolCallRequest(
                        tool_name="web.fetch",
                        call_id="call-file",
                        conversation_id="conv1",
                        requested_by="test",
                        arguments={"url": server.url("/report.txt"), "session_id": "s1", "chat_title": "PAGE"},
                    )
                )
            finally:
                server.stop()

            self.assertEqual(result.status, "completed")
            self.assertIn("local file workflow", result.summary)
            self.assertEqual(result.payload["parse"]["status"], "parsed")
            self.assertEqual(result.payload["parse"]["kind"], "text")
            self.assertTrue(Path(result.payload["artifacts"]["content_path"]).exists())
            self.assertTrue(Path(result.payload["artifacts"]["full_text_path"]).exists())
            self.assertEqual(Path(result.output_refs[0]).name, "content.md")
            self.assertIn("file url body", Path(result.output_refs[0]).read_text(encoding="utf-8"))

    def test_blocks_non_http_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = WebFetchTool(root / "outputs", FileIndex(root / "files.sqlite"))

            result = tool.run(
                ToolCallRequest(
                    tool_name="web.fetch",
                    call_id="call1",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"url": "file:///tmp/a.txt"},
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.error, "invalid_url")


class _LocalServer:
    def __init__(self, root: Path):
        self.root = root
        self.httpd: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        root = self.root

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, format, *args):
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
