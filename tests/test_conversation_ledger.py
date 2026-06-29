from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.domain.models import NormalizedMessage, ReplyCandidate


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

    def test_records_self_message_without_losing_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("self1", "sent by me", is_self=True, sender_name="Me"))

            self.assertTrue(entry.is_self)
            self.assertEqual(entry.role, "self")

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
            self.assertIn("file parsed content", markdown)
            self.assertIn("manifest=workspace/file123/manifest.json", markdown)

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

    def test_file_section_includes_content_artifact_path(self) -> None:
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
                            "parse": {"status": "parsed", "summary": "ok", "text": "body"},
                        }
                    ]
                },
            )
            store.append_message(message)

            snapshot = LedgerContextAssembler(store, max_recent_entries=5, token_budget=300).build_snapshot(message)
            rendered = snapshot.render_for_prompt()

            self.assertIn("Available file refs", rendered)
            self.assertIn("content=derived", rendered)
            self.assertIn("content.md", rendered)


def _message(
    message_id: str,
    text: str,
    *,
    metadata: dict | None = None,
    is_self: bool = False,
    sender_name: str = "PAGE",
) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id="conv1",
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
