from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class BackendEventsCliTest(unittest.TestCase):
    def test_append_and_poll_backend_events_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            inbox = data_dir / "inbox"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "accept-contact", "PAGE")
            (inbox / "note.txt").write_text("hello backend", encoding="utf-8")

            append_output = self._run(
                "--data-dir",
                str(data_dir),
                "append-backend-event",
                "--event-file",
                str(event_file),
                "--chat-title",
                "PAGE",
                "--sender-name",
                "PAGE",
                "--text",
                "后台事件测试",
                "--attachment",
                "note.txt",
            )
            append_payload = json.loads(append_output)
            self.assertEqual(append_payload["status"], "ok")
            self.assertFalse(append_payload["send_enabled"])

            poll_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "poll-backend-events",
                    "--event-file",
                    str(event_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--verbose",
                )
            )

            self.assertEqual(poll_payload["status"], "stopped")
            self.assertEqual(poll_payload["processed_count"], 1)
            self.assertFalse(poll_payload["send_enabled"])
            self.assertIn("[后台附件] note.txt", poll_payload["processed"][0]["message"]["text"])

    def test_scan_backend_files_cli_then_poll_backend_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            inbox = data_dir / "inbox"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "accept-contact", "PAGE")
            (inbox / "scan-note.txt").write_text("scan backend", encoding="utf-8")

            scan_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "scan-backend-files",
                    "--event-file",
                    str(event_file),
                    "--chat-title",
                    "PAGE",
                    "--sender-name",
                    "PAGE",
                )
            )

            self.assertEqual(scan_payload["created_count"], 1)
            self.assertFalse(scan_payload["send_enabled"])

            poll_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "poll-backend-events",
                    "--event-file",
                    str(event_file),
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--verbose",
                )
            )

            self.assertEqual(poll_payload["processed_count"], 1)
            self.assertIn("收到后台文件: scan-note.txt", poll_payload["processed"][0]["message"]["text"])

    def test_append_backend_event_cli_accepts_quote_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = data_dir / "backend_events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            append_payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "append-backend-event",
                    "--event-file",
                    str(event_file),
                    "--chat-title",
                    "PAGE",
                    "--sender-name",
                    "PAGE",
                    "--text",
                    "引用继续处理",
                    "--quote-text",
                    "被引用的正文",
                    "--quote-message-id",
                    "quoted-id",
                    "--quote-sender-name",
                    "PAGE",
                )
            )
            raw_event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(append_payload["status"], "ok")
            self.assertEqual(raw_event["quote"]["text"], "被引用的正文")
            self.assertEqual(raw_event["quote"]["message_id"], "quoted-id")

    def test_run_agent_cli_starts_backend_event_loop_without_wechat_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            event_file = Path(tmp) / "events.jsonl"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "run-agent",
                    "--loops",
                    "1",
                    "--interval",
                    "0",
                    "--backend-event-file",
                    str(event_file),
                    "--no-wechat-ocr",
                )
            )

            self.assertEqual(payload["status"], "stopped")
            self.assertEqual(payload["loops"], 1)
            self.assertEqual(payload["runners"][0]["name"], "backend-events")

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", *args],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        return completed.stdout


if __name__ == "__main__":
    unittest.main()
