from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate, SendResult, ToolCallResult


class ConversationLedgerStoreTest(unittest.TestCase):
    def test_append_message_writes_jsonl_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("m1", "hello https://example.com/a"))

            entries = store.read_entries("conv1")
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertEqual(entry.sequence, 1)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].text_blocks[0]["text"], "hello https://example.com/a")
            self.assertEqual(entries[0].links[0]["url"], "https://example.com/a")
            self.assertIn("000001", markdown)
            self.assertIn("hello https://example.com/a", markdown)

    def test_append_message_persists_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("m1", "hello", metadata={"session_id": "session_new"}))

            entries = store.read_entries("conv1")
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertEqual(entry.session_id, "session_new")
            self.assertEqual(entries[0].session_id, "session_new")
            self.assertIn("[session:session_new]", markdown)

    def test_append_message_upserts_by_dedupe_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            first = store.append_message_result(_message("raw-path-a", "old text", metadata={"dedupe_key": "same-real-message"}))
            second = store.append_message_result(_message("raw-path-b", "new text", metadata={"dedupe_key": "same-real-message"}))

            entries = store.read_entries("conv1")
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertEqual(first.status, "created")
            self.assertEqual(second.status, "updated")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].sequence, 1)
            self.assertEqual(entries[0].message_id, "raw-path-a")
            self.assertEqual(entries[0].text_blocks[0]["text"], "new text")
            self.assertIn("new text", markdown)
            self.assertNotIn("old text", markdown)

    def test_append_message_duplicate_dedupe_key_does_not_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("raw-path-a", "same text", metadata={"dedupe_key": "same-real-message"}))
            before = store.conversation_markdown_path("conv1").stat().st_mtime_ns
            duplicate = store.append_message_result(_message("raw-path-b", "same text", metadata={"dedupe_key": "same-real-message"}))
            after = store.conversation_markdown_path("conv1").stat().st_mtime_ns

            self.assertEqual(duplicate.status, "duplicate")
            self.assertEqual(len(store.read_entries("conv1")), 1)
            self.assertEqual(after, before)

    def test_concurrent_appends_keep_per_conversation_sequence_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            errors: list[BaseException] = []

            def append_many(conversation_id: str, prefix: str) -> None:
                try:
                    for index in range(20):
                        store.append_message(_message(f"{prefix}-{index}", f"{prefix} text {index}", conversation_id=conversation_id))
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=append_many, args=("conv1", "a")),
                threading.Thread(target=append_many, args=("conv1", "b")),
                threading.Thread(target=append_many, args=("conv2", "c")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            if errors:
                raise errors[0]
            conv1 = store.read_entries("conv1")
            conv2 = store.read_entries("conv2")

            self.assertEqual(len(conv1), 40)
            self.assertEqual(len(conv2), 20)
            self.assertEqual([entry.sequence for entry in conv1], list(range(1, 41)))
            self.assertEqual([entry.sequence for entry in conv2], list(range(1, 21)))

    def test_records_self_message_without_losing_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("self1", "sent by me", is_self=True, sender_name="Me"))

            self.assertTrue(entry.is_self)
            self.assertEqual(entry.role, "self")

    def test_pulled_back_self_echo_dedups_against_assistant_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("user1", "在吗"))
            reply = ReplyCandidate(
                message_id="user1",
                conversation_id="conv1",
                text="你好，我在的",
                send_mode="auto",
                model="fake",
            )
            store.append_reply(reply)

            # WeChat echoes the agent's own reply back through the pull with a
            # fresh message_id and is_self=True.
            echo = store.append_message(
                _message("weflow-echo-xyz", "你好，我在的", is_self=True, sender_name="Me")
            )

            entries = store.read_entries("conv1")
            assistant_entries = [e for e in entries if e.role == "assistant"]
            self_entries = [e for e in entries if e.role == "self"]

            # Exactly one assistant entry, and NO duplicate self entry for the echo.
            self.assertEqual(len(assistant_entries), 1)
            self.assertEqual(len(self_entries), 0)
            # The echo confirmed delivery on the assistant entry.
            self.assertEqual(echo.role, "assistant")
            self.assertEqual(assistant_entries[0].send.get("status"), "sent")
            self.assertEqual(assistant_entries[0].send.get("echo_message_id"), "weflow-echo-xyz")

    def test_pulled_back_self_message_without_match_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            # A genuine self message the user typed on their own phone (no matching
            # assistant reply) must still be recorded as role=self.
            entry = store.append_message(
                _message("phone-self", "我自己手机上发的", is_self=True, sender_name="Me")
            )
            self.assertEqual(entry.role, "self")
            self.assertEqual(len(store.read_entries("conv1")), 1)

    def test_quote_lookup_by_message_id_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("before", "before text"))
            quoted = store.append_message(_message("quoted", "quoted body with task detail"))
            store.append_message(_message("after", "after text"))

            by_id = store.lookup_quote_context("conv1", {"message_id": quoted.message_id})
            by_text = store.lookup_quote_context("conv1", {"text": "task detail"})

            self.assertEqual(by_id["status"], "found")
            self.assertEqual(by_id["matched_entry_id"], quoted.entry_id)
            self.assertEqual(by_text["status"], "found")
            self.assertEqual(len(by_id["entries"]), 3)

    def test_attachment_parse_text_becomes_text_block_and_file_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(
                _message(
                    "m1",
                    "please read this file",
                    metadata={
                        "attachments": [
                            {
                                "status": "indexed",
                                "file_id": "file123",
                                "name": "report.pdf",
                                "kind": "file",
                                "workspace": {
                                    "manifest_path": "workspace/file123/manifest.json",
                                    "derived_dir": "workspace/file123/derived",
                                },
                                "parse": {
                                    "status": "parsed",
                                    "kind": "pdf",
                                    "summary": "parsed pdf",
                                    "text": "file parsed content",
                                },
                            }
                        ]
                    },
                )
            )

            block_kinds = [block["kind"] for block in entry.text_blocks]
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertIn("attachment:pdf", block_kinds)
            self.assertEqual(entry.attachments[0]["file_id"], "file123")
            self.assertEqual(
                Path(entry.attachments[0]["artifacts"]["content_path"]),
                Path("workspace/file123/derived/content.md"),
            )
            self.assertEqual(
                Path(entry.attachments[0]["artifacts"]["analysis_path"]),
                Path("workspace/file123/derived/analysis.json"),
            )
            attachment_block = next(block for block in entry.text_blocks if block["kind"] == "attachment:pdf")
            self.assertEqual(attachment_block["text"], "file parsed content")
            self.assertFalse(attachment_block["metadata"]["visible_in_context"])
            self.assertNotIn("file parsed content", markdown)
            self.assertIn("[block:attachment:pdf file_id=file123 name=report.pdf hidden=true read=file.read]", markdown)
            self.assertIn("[file:file123 name=report.pdf kind=file status=indexed read=file.read]", markdown)
            self.assertNotIn("manifest=workspace/file123/manifest.json", markdown)

    def test_attachment_ai_analysis_brief_is_visible_but_raw_file_body_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            message = _message(
                "m1",
                "please inspect",
                metadata={
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file123",
                            "name": "report.pdf",
                            "kind": "file",
                            "workspace": {"manifest_path": "workspace/file123/manifest.json"},
                            "parse": {
                                "status": "parsed",
                                "kind": "pdf",
                                "summary": "parser summary",
                                "text": "raw parsed file body should stay hidden",
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "AI file summary",
                                "ai_key_points": ["first point", "second point"],
                            },
                            "artifacts": {
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "AI file summary",
                                "ai_key_points": ["first point", "second point"],
                            },
                        }
                    ]
                },
            )

            store.append_message(message)
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")
            rendered = LedgerContextAssembler(store, max_recent_entries=5, token_budget=800).build_snapshot(message).render_for_prompt()

            self.assertIn("[file_analysis]", markdown)
            self.assertIn("AI Analysis:", markdown)
            self.assertIn("Key Points:", markdown)
            self.assertNotIn('"summary": "AI file summary"', markdown)
            self.assertIn("- first point", markdown)
            self.assertIn("AI Analysis: AI file summary", rendered)
            self.assertIn("Key Points:", rendered)
            self.assertIn("- first point", rendered)
            self.assertNotIn("raw parsed file body should stay hidden", markdown)
            self.assertNotIn("raw parsed file body should stay hidden", rendered)

    def test_duplicate_attachment_metadata_collapses_to_single_block(self) -> None:
        # Voice messages can arrive with the same media emitted twice (a voice
        # backend event plus the generic attachment record). Only one block/
        # attachment should survive.
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            attachment = {
                "status": "indexed",
                "file_id": "voice-abc",
                "name": "voice_10.wav",
                "kind": "audio",
                "parse": {"status": "parsed", "kind": "audio", "text": "转写文本"},
            }
            entry = store.append_message(
                _message(
                    "m1",
                    "语音消息",
                    metadata={"attachments": [dict(attachment), dict(attachment)]},
                )
            )

            audio_blocks = [b for b in entry.text_blocks if b["kind"] == "attachment:audio"]
            self.assertEqual(len(audio_blocks), 1)
            self.assertEqual(len(entry.attachments), 1)

    def test_backend_message_uses_original_text_as_primary_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(
                _message(
                    "m1",
                    "request\n[backend attachment content]\nparsed body",
                    metadata={
                        "original_text": "request",
                        "attachments": [
                            {
                                "status": "indexed",
                                "file_id": "file123",
                                "name": "note.txt",
                                "parse": {"kind": "text", "text": "parsed body"},
                            }
                        ],
                    },
                )
            )

            self.assertEqual(entry.text_blocks[0]["kind"], "text")
            self.assertEqual(entry.text_blocks[0]["text"], "request")
            self.assertEqual(entry.text_blocks[1]["text"], "parsed body")
            self.assertFalse(entry.text_blocks[1]["metadata"]["visible_in_context"])

    def test_links_are_extracted_only_from_original_message_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(
                _message(
                    "m1",
                    "request\n[file parsed] https://internal.example/from-file",
                    metadata={
                        "original_text": "request",
                        "attachments": [
                            {
                                "status": "indexed",
                                "file_id": "file123",
                                "name": "note.txt",
                                "parse": {"kind": "text", "text": "https://internal.example/from-file"},
                            }
                        ],
                    },
                )
            )

            self.assertEqual(entry.links, [])

    def test_voice_transcript_is_marked_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(
                _message(
                    "voice1",
                    "请根据这条语音继续处理",
                    metadata={
                        "voice": {
                            "status": "transcribed",
                            "source": "local_asr_fallback",
                            "text": "请根据这条语音继续处理",
                            "duration": "8\"",
                        }
                    },
                )
            )
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertEqual(entry.text_blocks[0]["kind"], "voice:transcript")
            self.assertEqual(entry.text_blocks[0]["metadata"]["source"], "local_asr_fallback")
            self.assertIn("[block:voice:transcript", markdown)
            self.assertIn("请根据这条语音继续处理", markdown)

    def test_mark_recalled_hides_body_from_active_reads_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("m1", "visible text"))

            changed = store.mark_recalled("conv1", "m1")
            active = store.read_entries("conv1")
            all_entries = store.read_entries("conv1", include_removed=True)
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertEqual(active, [])
            self.assertEqual(all_entries[0].status, "recalled")
            self.assertIn("[recalled]", markdown)
            self.assertNotIn("visible text", markdown)

    def test_annotate_link_updates_link_and_adds_annotation_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("m1", "read https://example.com/a"))
            url_id = entry.links[0]["url_id"]

            changed = store.annotate_link(
                "conv1",
                entry.entry_id,
                url_id,
                status="completed",
                summary="summary text",
                text="full fetched page text",
                source_path="tool_outputs/web_fetch/a.md",
            )
            updated = store.read_entries("conv1")[0]
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertEqual(updated.links[0]["status"], "completed")
            self.assertEqual(updated.links[0]["annotation_path"], "tool_outputs/web_fetch/a.md")
            self.assertEqual(updated.text_blocks[-1]["kind"], "annotation:web")
            self.assertIn("summary text", updated.text_blocks[-1]["text"])
            self.assertIn("[block:annotation:web", markdown)
            self.assertTrue((store.annotations_dir("conv1") / f"{entry.entry_id}_{url_id}.md").exists())

    def test_append_reply_uses_conversation_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            reply = ReplyCandidate(
                message_id="m1",
                conversation_id="conv1",
                text="reply",
                send_mode="dry_run",
                model="fake",
            )

            entry = store.append_reply(reply, chat_title="Group", conversation_type="group")

            self.assertEqual(entry.conversation_type, "group")
            self.assertEqual(entry.role, "assistant")

    def test_append_reply_persists_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            reply = ReplyCandidate(
                message_id="m1",
                conversation_id="conv1",
                text="reply",
                send_mode="dry_run",
                model="fake",
            )

            entry = store.append_reply(reply, session_id="session_new")

            self.assertEqual(entry.session_id, "session_new")

    def test_append_reply_records_outgoing_attachments_tool_outputs_and_send_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            reply = ReplyCandidate(
                message_id="m1",
                conversation_id="conv1",
                text="reply with file",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": "out/report.pdf", "name": "report.pdf", "kind": "document"}],
                tool_result=ToolCallResult(
                    call_id="call1",
                    tool_name="document.translate",
                    status="completed",
                    summary="done",
                    output_refs=["out/translated.docx"],
                ),
            )

            entry = store.append_reply(reply)
            changed = store.update_reply_send_result(
                "conv1",
                entry.entry_id,
                SendResult(message_id="m1", conversation_id="conv1", status="queued_for_confirm", reason="confirm_required:q1"),
            )
            updated = store.read_entries("conv1")[0]
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertEqual(len(updated.attachments), 2)
            self.assertEqual(updated.attachments[0]["source"], "reply_candidate")
            self.assertEqual(updated.attachments[1]["source"], "tool_result")
            self.assertEqual(updated.attachments[1]["tool_name"], "document.translate")
            self.assertEqual(updated.send["status"], "queued_for_confirm")
            self.assertIn("[send:status=queued_for_confirm", markdown)
            self.assertIn("[file:outgoing name=translated.docx", markdown)

    def test_append_reply_writes_parsed_outgoing_attachment_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            reply = ReplyCandidate(
                message_id="m1",
                conversation_id="conv1",
                text="reply with parsed file",
                send_mode="confirm",
                model="fake",
                attachments=[
                    {
                        "status": "indexed",
                        "source": "tool_result",
                        "path": "out/result.md",
                        "name": "result.md",
                        "kind": "tool_output",
                        "file_id": "file123",
                        "workspace": {"manifest_path": "workspace/file123/manifest.json"},
                        "parse": {"kind": "text", "text": "agent generated file body"},
                    }
                ],
            )

            store.append_reply(reply)
            entry = store.read_entries("conv1")[0]
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")

            self.assertEqual(entry.text_blocks[1]["kind"], "attachment:text")
            self.assertEqual(entry.text_blocks[1]["text"], "agent generated file body")
            self.assertFalse(entry.text_blocks[1]["metadata"]["visible_in_context"])
            self.assertNotIn("agent generated file body", markdown)
            self.assertIn("[block:attachment:text file_id=file123 name=result.md hidden=true read=file.read]", markdown)


class LedgerContextAssemblerTest(unittest.TestCase):
    def test_build_snapshot_uses_active_entries_quote_window_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("m1", "first"))
            store.append_message(
                _message(
                    "m2",
                    "file message",
                    metadata={"attachments": [{"file_id": "f1", "name": "a.txt", "parse": {"summary": "ok"}}]},
                )
            )
            current = _message("m3", "reply to quote", metadata={"quote": {"message_id": "m2"}})
            store.append_message(current)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5).build_snapshot(current)
            rendered = snapshot.render_for_prompt()

            self.assertEqual(snapshot.quote_context["status"], "found")
            self.assertEqual(snapshot.file_refs[0]["file_id"], "f1")
            self.assertIn("Quoted-message window", rendered)
            self.assertIn("file message", rendered)

    def test_budget_keeps_quote_window_and_trims_recent_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            quoted = _message("quoted", "important quoted content")
            store.append_message(quoted)
            for index in range(20):
                store.append_message(_message(f"m{index}", f"recent filler message {index} " + ("x" * 80)))
            current = _message("current", "continue quoted task", metadata={"quote": {"message_id": "quoted"}})
            store.append_message(current)

            snapshot = LedgerContextAssembler(store, max_recent_entries=25, token_budget=90).build_snapshot(current)
            rendered = snapshot.render_for_prompt()
            section_names = [section.name for section in snapshot.sections]

            self.assertIn("quote", section_names)
            self.assertIn("important quoted content", rendered)
            self.assertTrue(
                "earlier recent context omitted by token budget" in rendered
                or "recent context omitted because forced context used the token budget" in rendered
            )
            self.assertGreater(snapshot.estimated_tokens, snapshot.token_budget)
            self.assertLessEqual(snapshot.estimated_tokens, 180)

    def test_file_section_includes_compact_file_artifact_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            message = _message(
                "m1",
                "file message",
                metadata={
                    "attachments": [
                        {
                            "file_id": "f1",
                            "name": "a.txt",
                            "workspace": {"manifest_path": "manifest.json", "derived_dir": "derived"},
                            "artifacts": {"content_path": "derived/content.md", "chunk_count": 3, "chunks_dir": "derived/chunks"},
                            "parse": {"status": "parsed", "summary": "ok", "text": "body"},
                        }
                    ]
                },
            )
            store.append_message(message)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=300).build_snapshot(message)
            rendered = snapshot.render_for_prompt()

            self.assertIn("Available file refs", rendered)
            self.assertIn("chunks=3", rendered)
            self.assertIn("read_tool=file.read", rendered)
            self.assertIn("summary=ok", rendered)
            self.assertNotIn("content=derived", rendered)
            self.assertNotIn("content.md", rendered)
            self.assertNotIn("chunks_dir=derived/chunks", rendered)

    def test_file_section_includes_table_artifact_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            message = _message(
                "m1",
                "table message",
                metadata={
                    "attachments": [
                        {
                            "file_id": "f1",
                            "name": "table.csv",
                            "workspace": {"manifest_path": "manifest.json", "derived_dir": "derived"},
                            "artifacts": {
                                "content_path": "derived/content.md",
                                "table_index_path": "derived/tables/index.json",
                                "table_chunk_count": 2,
                            },
                            "parse": {"status": "parsed", "summary": "table ok", "text": "first rows"},
                        }
                    ]
                },
            )
            store.append_message(message)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=300).build_snapshot(message)
            rendered = snapshot.render_for_prompt()

            self.assertIn("table_chunks=2", rendered)
            self.assertNotIn("table_index=derived/tables/index.json", rendered)

    def test_file_section_refreshes_ai_summary_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConversationLedgerStore(root)
            derived = root / "file_workspace" / "conv" / "s1" / "file123" / "derived"
            derived.mkdir(parents=True)
            analysis_path = derived / "analysis.json"
            analysis_path.write_text(
                json.dumps(
                    {
                        "ai_analysis_status": "analyzed",
                        "ai_summary": "final file summary",
                        "char_count": 42,
                        "chunk_count": 2,
                        "chunks": [{"index": 1, "path": str(derived / "chunks" / "chunk_0001.md")}],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = derived.parent / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "parse": {
                            "status": "parsed",
                            "kind": "pdf",
                            "summary": "parser summary",
                            "analysis_path": str(analysis_path),
                            "content_path": str(derived / "content.md"),
                            "chunks_dir": str(derived / "chunks"),
                            "chunk_count": 1,
                            "ai_analysis_status": "pending",
                            "ai_summary": "文件总结正在后台生成",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            message = _message(
                "m1",
                "file message",
                metadata={
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file123",
                            "name": "report.pdf",
                            "workspace": {"manifest_path": str(manifest_path), "derived_dir": str(derived)},
                            "artifacts": {"ai_analysis_status": "pending", "ai_summary": "文件总结正在后台生成", "chunk_count": 1},
                            "parse": {"status": "parsed", "summary": "parser summary", "text": "body"},
                        }
                    ]
                },
            )
            store.append_message(message)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=500).build_snapshot(message)
            rendered = snapshot.render_for_prompt()
            refreshed = store.read_entries("conv1")[0]

            self.assertIn("AI Analysis: final file summary", rendered)
            self.assertNotIn("summary=final file summary", rendered)
            self.assertIn("chunks=2", rendered)
            self.assertEqual(refreshed.attachments[0]["artifacts"]["ai_summary"], "final file summary")

    def test_refresh_file_refs_removes_legacy_file_analysis_schema_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ConversationLedgerStore(root)
            derived = root / "file_workspace" / "conv" / "s1" / "file123" / "derived"
            derived.mkdir(parents=True)
            message = _message(
                "m1",
                "file message",
                metadata={
                    "attachments": [
                        {
                            "status": "indexed",
                            "file_id": "file123",
                            "name": "report.pdf",
                            "workspace": {"manifest_path": str(derived.parent / "manifest.json"), "derived_dir": str(derived)},
                            "parse": {
                                "status": "parsed",
                                "kind": "pdf",
                                "text": "body",
                                "ai_analysis_status": "analyzed",
                                "ai_summary": "clean summary",
                                "ai_key_points": ["point one"],
                            },
                        }
                    ]
                },
            )
            store.append_message(message)
            entries = store._read_entries("conv1")
            for block in entries[0]["text_blocks"]:
                if block.get("kind") == "file:analysis":
                    block["text"] = (
                        "[file_analysis]\n"
                        "schema=file_analysis_brief_v2 source=content.md::AI Analysis+Key Points raw_content_hidden=true\n"
                        '{"schema":"file_analysis_brief_v2","summary":"duplicate"}\n'
                        "AI Analysis:\nclean summary\nKey Points:\n- point one\n[/file_analysis]"
                    )
            store._rewrite_entries("conv1", entries)
            store._render_conversation("conv1")

            changed = store.refresh_file_refs("conv1")
            markdown = store.conversation_markdown_path("conv1").read_text(encoding="utf-8")
            refreshed = store._read_entries("conv1")[0]
            analysis_blocks = [block for block in refreshed["text_blocks"] if block.get("kind") == "file:analysis"]

            self.assertTrue(changed)
            self.assertEqual(len(analysis_blocks), 1)
            self.assertNotIn("schema=file_analysis_brief_v2", markdown)
            self.assertNotIn('"schema"', markdown)
            self.assertIn("AI Analysis:\nclean summary", markdown)

    def test_recent_context_is_scoped_to_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("old", "old session text", metadata={"session_id": "old_session"}))
            current = _message("current", "new session text", metadata={"session_id": "new_session"})
            store.append_message(current)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=300).build_snapshot(current)
            rendered = snapshot.render_for_prompt()

            self.assertEqual(snapshot.session_id, "new_session")
            self.assertEqual([item["message_id"] for item in snapshot.recent_entries], ["current"])
            self.assertIn("new session text", rendered)
            self.assertNotIn("old session text", rendered)

    def test_explicit_quote_can_restore_cross_session_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("old", "old quoted text", metadata={"session_id": "old_session"}))
            current = _message(
                "current",
                "continue quote",
                metadata={"session_id": "new_session", "quote": {"message_id": "old"}},
            )
            store.append_message(current)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=300).build_snapshot(current)
            rendered = snapshot.render_for_prompt()

            self.assertEqual([item["message_id"] for item in snapshot.recent_entries], ["current"])
            self.assertEqual(snapshot.quote_context["status"], "found")
            self.assertIn("Quoted-message window", rendered)
            self.assertIn("old quoted text", rendered)


def _message(
    message_id: str,
    text: str,
    *,
    metadata: dict | None = None,
    is_self: bool = False,
    sender_name: str = "PAGE",
    conversation_id: str = "conv1",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        conversation_type="private",
        chat_title="PAGE",
        sender_name=sender_name,
        sender_wechat_id="wxid_page",
        text=text,
        is_self=is_self,
        received_at="2026-06-29T00:00:00+08:00",
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()
