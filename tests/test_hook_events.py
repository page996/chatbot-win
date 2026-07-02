from __future__ import annotations

import json
import tempfile
import threading
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
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    normalize_wcf_callback,
    normalize_weflow_message,
    normalize_weflow_push_event,
)


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

    def test_hook_payload_accepts_wcf_style_group_message(self) -> None:
        event = hook_event_from_payload(
            {
                "id": "1002",
                "roomid": "12345@chatroom",
                "sender": "wxid_member",
                "type": 1,
                "ts": 1719900000,
                "content": {"text": "hello from hook"},
                "extra": {"senderName": "Member", "roomName": "Study Room"},
            }
        )

        self.assertEqual(event.conversation_key, "12345@chatroom")
        self.assertEqual(event.chat_title, "Study Room")
        self.assertEqual(event.sender_wechat_id, "wxid_member")
        self.assertEqual(event.sender_name, "Member")
        self.assertTrue(event.is_group)
        self.assertEqual(event.text, "hello from hook")
        self.assertTrue(event.raw_id.startswith("hook:message:12345@chatroom:1002"))
        self.assertTrue(event.observed_at.startswith("2024-07-02T"))

    def test_hook_payload_accepts_weflow_style_message_and_implicit_attachment(self) -> None:
        event = hook_event_from_payload(
            {
                "msgId": "1003",
                "talkerId": "wxid_page",
                "talkerName": "PAGE",
                "senderWxid": "wxid_page",
                "senderNickname": "PAGE",
                "msgType": "49",
                "sortSeq": "9001",
                "displayContent": "file received",
                "localPath": "report.pdf",
            }
        )

        self.assertEqual(event.conversation_key, "wxid_page")
        self.assertEqual(event.raw_id, "hook:message:wxid_page:1003:9001")
        self.assertEqual(event.text, "file received")
        self.assertEqual(len(event.attachments), 1)
        self.assertEqual(event.attachments[0].path, "report.pdf")
        self.assertEqual(event.attachments[0].kind, "file")

    def test_hook_source_bridge_normalizes_weflow_push_and_wcf_callback(self) -> None:
        weflow = normalize_weflow_push_event(
            {
                "event": "message.new",
                "sessionId": "12345@chatroom",
                "sessionType": "group",
                "rawid": "wf-1",
                "sourceName": "Member",
                "groupName": "Study Room",
                "content": "from weflow",
                "timestamp": 1719900000,
            }
        )
        wcf = normalize_wcf_callback(
            {
                "id": "wcf-1",
                "type": 1,
                "sender": "wxid_member",
                "roomid": "12345@chatroom",
                "content": "from wcf",
                "is_group": True,
            }
        )

        self.assertEqual(hook_event_from_payload(weflow).text, "from weflow")
        self.assertEqual(weflow["talker"], "12345@chatroom")
        self.assertEqual(weflow["sender_name"], "Member")
        self.assertEqual(wcf["talker"], "12345@chatroom")
        self.assertEqual(wcf["sender_id"], "wxid_member")
        self.assertEqual(hook_event_from_payload(wcf).text, "from wcf")

    def test_weflow_raw_event_preserves_ordering_voice_and_context_only(self) -> None:
        normalized = normalize_weflow_message(
            {
                "localId": 7,
                "serverId": "0",
                "messageKey": "db:Msg_0:7",
                "localType": 34,
                "createTime": 1719900000,
                "sortSeq": 1719900000007,
                "isSend": 0,
                "senderUsername": "wxid_page",
                "content": "[语音]",
                "mediaType": "voice",
                "mediaFileName": "voice_7.wav",
                "mediaLocalPath": str(self.data_dir / "inbox" / "voice_7.wav"),
            },
            session_id="wxid_page",
            session_meta={"name": "PAGE", "media": {"exportPath": str(self.data_dir / "inbox")}},
            context_only=True,
        )
        self._append_hook(normalized)

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
        raw = driver.read_new_messages()[0]

        self.assertTrue(raw.driver_meta["context_only"])
        self.assertEqual(raw.driver_meta["source_payload"]["message_key"], "db:Msg_0:7")
        self.assertEqual(raw.driver_meta["source_payload"]["local_type"], "34")
        self.assertEqual(raw.driver_meta["ordering"]["message_key"], "db:Msg_0:7")
        self.assertEqual(raw.driver_meta["voice"]["audio_name"], "voice_7.wav")

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

    def test_importer_concurrent_import_new_does_not_duplicate_backend_events(self) -> None:
        for index in range(20):
            self._append_hook(
                {
                    "talker": "wxid_page",
                    "talker_name": "PAGE",
                    "sender_name": "PAGE",
                    "msgid": f"concurrent-{index}",
                    "text": f"line {index}",
                }
            )
        errors: list[BaseException] = []
        results = []

        def import_once() -> None:
            try:
                results.append(HookEventJsonlImporter(self.hook_file, self.backend_file).import_new())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=import_once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        if errors:
            raise errors[0]
        backend_lines = self.backend_file.read_text(encoding="utf-8").splitlines()
        imported_sequences = [json.loads(line)["source_payload"]["import_sequence"] for line in backend_lines]

        self.assertEqual(len(backend_lines), 20)
        self.assertEqual(sum(result.appended_count for result in results), 20)
        self.assertEqual(imported_sequences, list(range(1, 21)))

    def test_importer_waits_for_incomplete_tail_line(self) -> None:
        self.hook_file.parent.mkdir(parents=True, exist_ok=True)
        self.hook_file.write_text('{"talker":"wxid_page","talker_name":"PAGE"', encoding="utf-8")
        importer = HookEventJsonlImporter(self.hook_file, self.backend_file)

        first = importer.import_new()
        self.assertEqual(first.scanned_count, 0)
        self.assertEqual(first.appended_count, 0)
        self.assertEqual(first.error_count, 0)
        self.assertEqual(self.backend_file.read_text(encoding="utf-8").splitlines() if self.backend_file.exists() else [], [])

        with self.hook_file.open("a", encoding="utf-8") as f:
            f.write(',"sender_name":"PAGE","msgid":"tail-1","text":"complete"}\n')
        second = importer.import_new()

        self.assertEqual(second.appended_count, 1)
        self.assertEqual(json.loads(self.backend_file.read_text(encoding="utf-8").splitlines()[0])["text"], "complete")

    def test_importer_expands_batched_hook_payloads(self) -> None:
        self._append_hook(
            {
                "source": "weflow",
                "messages": [
                    {"talker": "wxid_page", "talker_name": "PAGE", "sender_name": "PAGE", "msgid": "batch-1", "text": "one"},
                    {"talker": "wxid_page", "talker_name": "PAGE", "sender_name": "PAGE", "msgid": "batch-2", "text": "two"},
                ],
            }
        )

        result = HookEventJsonlImporter(self.hook_file, self.backend_file).import_new()
        lines = self.backend_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result.appended_count, 2)
        self.assertEqual(result.backend_event_count, 2)
        self.assertEqual([json.loads(line)["text"] for line in lines], ["one", "two"])

    def test_importer_preserves_source_location_and_ordering_metadata(self) -> None:
        self._append_hook(
            {
                "source": "weflow",
                "messages": [
                    {
                        "talker": "wxid_page",
                        "talker_name": "PAGE",
                        "sender_name": "PAGE",
                        "msgid": "batch-1",
                        "sort_seq": 10,
                        "text": "one",
                    },
                    {
                        "talker": "wxid_page",
                        "talker_name": "PAGE",
                        "sender_name": "PAGE",
                        "msgid": "batch-2",
                        "sort_seq": 11,
                        "text": "two",
                    },
                ],
            }
        )

        result = HookEventJsonlImporter(self.hook_file, self.backend_file).import_new()
        lines = [json.loads(line) for line in self.backend_file.read_text(encoding="utf-8").splitlines()]
        first_source = lines[0]["source_payload"]
        second_source = lines[1]["source_payload"]

        self.assertEqual(result.imported_sequence_start, 1)
        self.assertEqual(result.imported_sequence_end, 2)
        self.assertEqual(first_source["source_path"], str(self.hook_file))
        self.assertEqual(first_source["source_line_no"], 1)
        self.assertEqual(first_source["batch_index"], 0)
        self.assertEqual(second_source["batch_index"], 1)
        self.assertEqual(first_source["import_sequence"], 1)
        self.assertEqual(second_source["import_sequence"], 2)
        self.assertEqual(second_source["hook"]["ordering"]["sort_key"], "11")

    def test_backend_message_metadata_exposes_hook_ordering(self) -> None:
        self._append_hook(
            {
                "source": "weflow",
                "talker": "wxid_page",
                "talker_name": "PAGE",
                "sender_name": "PAGE",
                "msgid": "100",
                "sort_seq": 50,
                "text": "ordered",
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

        raw = driver.read_new_messages()[0]

        self.assertEqual(raw.driver_meta["source_line_no"], 1)
        self.assertEqual(raw.driver_meta["import_sequence"], 1)
        self.assertEqual(raw.driver_meta["ordering"]["sort_key"], "50")
        self.assertEqual(raw.driver_meta["ordering"]["source_line_no"], 1)

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
        self.assertTrue(entries[0].text_blocks[1]["text"].startswith("hook file body"))
        self.assertIn("[file_artifacts]", entries[0].text_blocks[1]["text"])

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
