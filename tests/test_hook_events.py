from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.backend_events import BackendEventJsonlDriver
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter, hook_event_from_payload


class HookEventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)
        self.config = load_config(self.data_dir)
        self.hook_file = self.data_dir / "hook_events.jsonl"
        self.backend_file = self.data_dir / "backend_events.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hook_payload_normalizes_common_message_fields(self) -> None:
        event = hook_event_from_payload(
            {
                "talker_id": "wxid_page",
                "talker_name": "PAGE",
                "sender_id": "wxid_page",
                "sender_name": "PAGE",
                "msgid": "1001",
                "sort_seq": 123456789,
                "content": "hello",
            }
        )

        self.assertEqual(event.conversation_key, "wxid_page")
        self.assertEqual(event.chat_title, "PAGE")
        self.assertEqual(event.raw_id, "hook:message:wxid_page:1001:123456789")
        self.assertEqual(event.sort_key, "123456789")

    def test_importer_appends_only_new_hook_jsonl_lines(self) -> None:
        self._append_hook({"talker": "wxid_page", "talker_name": "PAGE", "sender_name": "PAGE", "msgid": "1", "text": "one"})
        importer = HookEventJsonlImporter(self.hook_file, self.backend_file)

        first = importer.import_new()
        second = importer.import_new()
        backend_lines = self.backend_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first.appended_count, 1)
        self.assertEqual(second.appended_count, 0)
        self.assertEqual(len(backend_lines), 1)
        self.assertEqual(json.loads(backend_lines[0])["source_payload"]["conversation_key"], "wxid_page")

    def test_imported_hook_message_enters_ledger_with_attachment(self) -> None:
        note = self.data_dir / "inbox" / "note.txt"
        note.write_text("hook file body", encoding="utf-8")
        self._append_hook(
            {
                "talker": "wxid_page",
                "talker_name": "PAGE",
                "sender_name": "PAGE",
                "msgid": "2",
                "text": "read file",
                "attachments": [{"path": "note.txt", "name": "note.txt", "kind": "file"}],
            }
        )
        HookEventJsonlImporter(self.hook_file, self.backend_file).import_new()
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.backend_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
            attachment_parser=BackendAttachmentParser(ocr_engine=_FakeOcr("")),
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        message = result["processed"][0]["message"]
        entries = runtime.ledger_store.read_entries(message["conversation_id"])

        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(message["metadata"]["backend_event_source"], "wechat_hook_jsonl")
        self.assertEqual(message["metadata"]["conversation_key"], "wxid_page")
        self.assertEqual(entries[0].text_blocks[1]["text"], "hook file body")

    def test_recall_event_marks_target_message_recalled(self) -> None:
        self._append_hook(
            {
                "talker": "wxid_page",
                "talker_name": "PAGE",
                "sender_name": "PAGE",
                "msgid": "3",
                "text": "will be recalled",
            }
        )
        target_raw_id = hook_event_from_payload(
            {
                "talker": "wxid_page",
                "talker_name": "PAGE",
                "sender_name": "PAGE",
                "msgid": "3",
                "text": "will be recalled",
            }
        ).raw_id
        self._append_hook(
            {
                "event_type": "recall",
                "talker": "wxid_page",
                "talker_name": "PAGE",
                "target_raw_id": target_raw_id,
                "msgid": "recall-3",
            }
        )
        HookEventJsonlImporter(self.hook_file, self.backend_file).import_new()
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.backend_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        conversation_id = result["processed"][0]["message"]["conversation_id"]
        active = runtime.ledger_store.read_entries(conversation_id)
        all_entries = runtime.ledger_store.read_entries(conversation_id, include_removed=True)

        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(result["processed"][1]["recall"]["status"], "marked")
        self.assertEqual(active, [])
        self.assertEqual(all_entries[0].status, "recalled")

    def _append_hook(self, payload: dict) -> None:
        self.hook_file.parent.mkdir(parents=True, exist_ok=True)
        with self.hook_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class _FakeOcr:
    def __init__(self, text: str):
        self.text = text

    def health(self):
        return None

    def read_text(self, image_path: str | Path) -> str:
        return self.text


if __name__ == "__main__":
    unittest.main()
