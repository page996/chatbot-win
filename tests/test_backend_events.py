from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.voice.asr import AsrHealth, AsrTranscript
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.backend_events import (
    BackendEventJsonlDriver,
    append_backend_event,
    append_backend_event_payload,
)
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver
from app.personal_wechat_bot.wechat_driver.voice_transcription import WeChatVoiceTranscriptionResult


class BackendEventJsonlDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)
        self.config = load_config(self.data_dir)
        self.inbox = self.data_dir / "inbox"
        self.event_file = self.data_dir / "backend_events.jsonl"
        self.driver = BackendEventJsonlDriver(
            self.event_file,
            FileIndex(self.data_dir / "file_index.sqlite"),
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            attachment_parser=BackendAttachmentParser(
                ocr_engine=_FakeOcr("图片 OCR 内容"),
                asr_engine=_FakeAsr("", status="blocked", error="local_asr_not_configured"),
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_backend_message_event_and_indexes_attachment(self) -> None:
        note = self.inbox / "note.txt"
        note.write_text("hello", encoding="utf-8")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="请看这个附件",
            attachments=["note.txt"],
        )

        messages = self.driver.read_new_messages()
        second = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(second, [])
        self.assertIn("请看这个附件", messages[0].text)
        self.assertIn("[后台附件待处理] note.txt", messages[0].text)
        self.assertEqual(messages[0].driver_meta["attachments"][0]["status"], "pending")

        enriched = self.driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertIn("[后台附件] note.txt file_id=", enriched.text)
        self.assertIn("[后台附件内容]\nhello", enriched.text)
        self.assertEqual(enriched.driver_meta["attachments"][0]["status"], "indexed")
        self.assertEqual(enriched.driver_meta["attachments"][0]["parse"]["status"], "parsed")

    def test_blocks_attachment_outside_allowed_roots_but_keeps_message(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="这个附件应该被挡住",
            attachments=[str(outside)],
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].driver_meta["attachments"][0]["status"], "pending")
        enriched = self.driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertIn("[后台附件已阻止] outside.txt", enriched.text)
        self.assertEqual(enriched.driver_meta["attachments"][0]["status"], "blocked")

    def test_image_extension_is_allowed_by_default_for_backend_ingest(self) -> None:
        image = self.inbox / "screen.png"
        image.write_bytes(b"fake-png")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            attachments=["screen.png"],
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        enriched = self.driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertIn("[后台附件] screen.png file_id=", enriched.text)
        self.assertIn("图片 OCR 内容", enriched.text)

    def test_audio_extensions_are_available_for_existing_configs(self) -> None:
        self.assertIn(".m4a", self.config.file_allowed_extensions)
        self.assertIn(".silk", self.config.file_allowed_extensions)

    def test_backend_event_preserves_quote_metadata(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="这条引用继续处理",
            quote={"message_id": "quoted-message-id", "text": "被引用内容", "sender_name": "PAGE"},
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].driver_meta["quote"]["message_id"], "quoted-message-id")
        self.assertEqual(messages[0].driver_meta["quote"]["text"], "被引用内容")

    def test_backend_event_voice_text_becomes_voice_metadata(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            voice={"text": "这是微信自带转文字", "duration": "6s"},
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "这是微信自带转文字")
        self.assertEqual(messages[0].driver_meta["voice"]["text"], "这是微信自带转文字")
        self.assertEqual(messages[0].driver_meta["voice"]["source"], "wechat_builtin_voice_to_text")

    def test_plain_backend_message_does_not_create_recall_metadata(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="plain",
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(messages[0].driver_meta["recall"], {})

    def test_backend_event_payload_accepts_pending_voice_without_text(self) -> None:
        append_backend_event_payload(
            self.event_file,
            {
                "chat_title": "PAGE",
                "sender_name": "PAGE",
                "voice_status": "pending",
                "voice_duration": "4s",
            },
        )

        messages = self.driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertIn("[微信语音待转文字]", messages[0].text)
        self.assertTrue(messages[0].driver_meta["backend_voice_pending"])

    def test_pending_voice_uses_wechat_builtin_bridge_before_ledger(self) -> None:
        bridge = _FakeVoiceBridge(WeChatVoiceTranscriptionResult(status="transcribed", text="微信主路径转写成功", method="fake"))
        driver = BackendEventJsonlDriver(
            self.event_file,
            FileIndex(self.data_dir / "file_index_voice.sqlite"),
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            attachment_parser=BackendAttachmentParser(
                ocr_engine=_FakeOcr(""),
                asr_engine=_FakeAsr("", status="blocked", error="local_asr_not_configured"),
            ),
            voice_transcription_bridge=bridge,
        )
        append_backend_event_payload(
            self.event_file,
            {
                "chat_title": "PAGE",
                "sender_name": "PAGE",
                "voice_status": "pending",
            },
        )

        messages = driver.read_new_messages()
        enriched = driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertEqual(enriched.text, "微信主路径转写成功")
        self.assertEqual(enriched.driver_meta["voice"]["source"], "wechat_builtin_voice_to_text")
        self.assertEqual(enriched.driver_meta["backend_voice_pending"], False)
        self.assertEqual(bridge.calls, 1)

    def test_pending_voice_falls_back_to_local_asr_when_audio_path_exists(self) -> None:
        audio = self.inbox / "voice.m4a"
        audio.write_bytes(b"fake audio")
        bridge = _FakeVoiceBridge(WeChatVoiceTranscriptionResult(status="blocked", error="wechat_builtin_transcript_not_observed"))
        driver = BackendEventJsonlDriver(
            self.event_file,
            FileIndex(self.data_dir / "file_index_voice_asr.sqlite"),
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            attachment_parser=BackendAttachmentParser(
                ocr_engine=_FakeOcr(""),
                asr_engine=_FakeAsr("本地 ASR fallback 成功"),
            ),
            voice_transcription_bridge=bridge,
        )
        append_backend_event_payload(
            self.event_file,
            {
                "chat_title": "PAGE",
                "sender_name": "PAGE",
                "voice_status": "pending",
                "voice_audio_path": "voice.m4a",
            },
        )

        messages = driver.read_new_messages()
        enriched = driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertEqual(enriched.text, "本地 ASR fallback 成功")
        self.assertEqual(enriched.driver_meta["voice"]["source"], "local_asr_fallback")
        self.assertEqual(enriched.driver_meta["attachments"][0]["parse"]["kind"], "audio")
        self.assertEqual(enriched.driver_meta["attachments"][0]["parse"]["raw_text"], "本地 ASR fallback 成功")

    def test_pending_voice_can_resolve_readable_wechat_voice_cache(self) -> None:
        voice_cache = self.data_dir / "wechat_voice_cache"
        voice_cache.mkdir()
        audio = voice_cache / "voice_cache_123.m4a"
        audio.write_bytes(b"fake audio")
        bridge = _FakeVoiceBridge(WeChatVoiceTranscriptionResult(status="blocked", error="wechat_builtin_transcript_not_observed"))
        driver = BackendEventJsonlDriver(
            self.event_file,
            FileIndex(self.data_dir / "file_index_voice_cache.sqlite"),
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots + ["wechat_voice_cache"]),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            attachment_parser=BackendAttachmentParser(
                ocr_engine=_FakeOcr(""),
                asr_engine=_FakeAsr("缓存语音 ASR 成功"),
            ),
            voice_transcription_bridge=bridge,
            voice_cache_resolver=WeChatVoiceCacheResolver(
                [voice_cache],
                allowed_extensions=self.config.file_allowed_extensions,
                max_bytes=self.config.file_max_bytes,
            ),
        )
        append_backend_event_payload(
            self.event_file,
            {
                "chat_title": "PAGE",
                "sender_name": "PAGE",
                "voice_status": "pending",
                "voice": {"audio_name": "voice_cache_123"},
            },
        )

        messages = driver.read_new_messages()
        enriched = driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        self.assertEqual(enriched.text, "缓存语音 ASR 成功")
        self.assertEqual(enriched.driver_meta["voice"]["source"], "local_asr_fallback")
        self.assertEqual(enriched.driver_meta["attachments"][0]["source"], "wechat_voice_cache_resolver")
        self.assertEqual(enriched.driver_meta["attachments"][0]["voice_cache"]["status"], "resolved")

    def test_polling_runner_writes_backend_voice_text_to_ledger(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            voice={"text": "语音里安排一个任务"},
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        entry = runtime.ledger_store.read_entries(result["processed"][0]["message"]["conversation_id"])[0]

        self.assertEqual(entry.text_blocks[0]["kind"], "voice:transcript")
        self.assertEqual(entry.text_blocks[0]["text"], "语音里安排一个任务")

    def test_polling_runner_enriches_pending_voice_before_writing_ledger(self) -> None:
        append_backend_event_payload(
            self.event_file,
            {
                "chat_title": "PAGE",
                "sender_name": "PAGE",
                "voice_status": "pending",
            },
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
            voice_transcription_bridge=_FakeVoiceBridge(
                WeChatVoiceTranscriptionResult(status="transcribed", text="主路径语音进入对话文件", method="fake")
            ),
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        entry = runtime.ledger_store.read_entries(result["processed"][0]["message"]["conversation_id"])[0]

        self.assertEqual(entry.text_blocks[0]["kind"], "voice:transcript")
        self.assertEqual(entry.text_blocks[0]["text"], "主路径语音进入对话文件")
        self.assertNotIn("待转文字", entry.text_blocks[0]["text"])

    def test_audio_attachment_is_staged_and_marked_asr_not_configured(self) -> None:
        audio = self.inbox / "voice.m4a"
        audio.write_bytes(b"fake audio")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="请听这段语音文件",
            attachments=[{"path": "voice.m4a", "kind": "audio"}],
        )

        messages = self.driver.read_new_messages()
        enriched = self.driver.enrich_message_attachments(
            messages[0],
            conversation_id=messages[0].driver_meta["conversation_id_hint"],
            session_id=messages[0].driver_meta["session_id"],
        )

        attachment = enriched.driver_meta["attachments"][0]
        self.assertEqual(attachment["status"], "indexed")
        self.assertEqual(attachment["parse"]["kind"], "audio")
        self.assertEqual(attachment["parse"]["error"], "local_asr_not_configured")
        self.assertIn("本地 ASR 暂不可用", attachment["parse"]["summary"])

    def test_backend_event_history_is_emitted_before_current_as_context_only(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="current task",
            history=[
                {"sender_name": "PAGE", "text": "earlier one", "observed_at": "2026-06-29T00:00:00+00:00"},
                {"sender_name": "Agent", "text": "earlier self", "observed_at": "2026-06-29T00:01:00+00:00"},
            ],
        )

        messages = self.driver.read_new_messages()
        second = self.driver.read_new_messages()

        self.assertEqual([item.text for item in messages], ["earlier one", "earlier self", "current task"])
        self.assertTrue(messages[0].driver_meta["context_only"])
        self.assertTrue(messages[1].driver_meta["context_only"])
        self.assertFalse(messages[2].driver_meta["context_only"])
        self.assertEqual(second, [])

    def test_polling_runner_backfills_history_into_ledger_without_replying_to_history(self) -> None:
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="current task",
            history=[
                {"sender_name": "PAGE", "text": "already happened"},
                {"sender_name": "PAGE", "text": "already happened too"},
            ],
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)

        self.assertEqual(result["processed_count"], 3)
        self.assertTrue(result["processed"][0]["context_only"])
        self.assertTrue(result["processed"][1]["context_only"])
        self.assertNotIn("reply", result["processed"][0])
        entries = runtime.ledger_store.read_entries(result["processed"][-1]["message"]["conversation_id"])
        self.assertEqual(
            [entry.text_blocks[0]["text"] for entry in entries[:3]],
            ["already happened", "already happened too", "current task"],
        )

    def test_driver_never_sends(self) -> None:
        result = self.driver.send_message("conversation", "hello")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "backend_event_driver_never_sends")

    def test_polling_runner_auto_registers_unknown_contact_and_stages_attachment(self) -> None:
        note = self.inbox / "note.txt"
        note.write_text("secret", encoding="utf-8")
        append_backend_event(
            self.event_file,
            chat_title="NOT_PAGE",
            sender_name="NOT_PAGE",
            text="ignored",
            attachments=["note.txt"],
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)

        self.assertEqual(result["processed"][0]["route"]["action"], "process")
        self.assertIn("channel auto registered", result["processed"][0]["route"]["reason"])
        staged_files = [item for item in (self.data_dir / "file_workspace").rglob("*") if item.is_file()]
        self.assertTrue(staged_files)
        self.assertTrue((self.data_dir / "conversation_channels" / "index.json").exists())

    def test_polling_runner_stages_attachment_after_channel_route(self) -> None:
        note = self.inbox / "note.txt"
        note.write_text("hello", encoding="utf-8")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="please read",
            attachments=["note.txt"],
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)

        self.assertEqual(result["processed"][0]["route"]["action"], "process")
        self.assertIn("[后台附件内容]\nhello", result["processed"][0]["message"]["text"])
        self.assertTrue((self.data_dir / "file_workspace").exists())

    def test_clear_context_with_attachment_stages_into_new_session(self) -> None:
        self.config.accepted_contacts.add("PAGE")
        note = self.inbox / "note.txt"
        note.write_text("hello", encoding="utf-8")
        append_backend_event(
            self.event_file,
            chat_title="PAGE",
            sender_name="PAGE",
            text="清空当前对话上下文",
            attachments=["note.txt"],
        )
        runtime = build_runtime(self.config)
        driver = BackendEventJsonlDriver(
            self.event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(self.data_dir, self.config.file_read_roots),
            allowed_extensions=self.config.file_allowed_extensions,
            max_input_bytes=self.config.file_max_bytes,
        )

        result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=1)
        item = result["processed"][0]
        session_id = item["context"]["session_id"]
        attachment_session = item["message"]["metadata"]["attachments"][0]["workspace"]["session_id"]

        self.assertTrue(item["context"]["reset"])
        self.assertEqual(attachment_session, session_id)
        self.assertNotEqual(session_id, "session_default")


class _FakeOcr:
    def __init__(self, text: str):
        self.text = text

    def health(self):
        return None

    def read_text(self, image_path: str | Path) -> str:
        return self.text


class _FakeAsr:
    def __init__(self, text: str, *, status: str = "transcribed", error: str = ""):
        self.text = text
        self.status = status
        self.error = error

    def health(self) -> AsrHealth:
        return AsrHealth("fake_asr", True)

    def transcribe(self, audio_path: str | Path) -> AsrTranscript:
        return AsrTranscript(self.status, self.text, backend="fake_asr", model="fake", source_path=str(audio_path), error=self.error)


class _FakeVoiceBridge:
    def __init__(self, result: WeChatVoiceTranscriptionResult):
        self.result = result
        self.calls = 0

    def transcribe_selected_voice(self, conversation_id: str) -> WeChatVoiceTranscriptionResult:
        self.calls += 1
        return self.result


if __name__ == "__main__":
    unittest.main()
