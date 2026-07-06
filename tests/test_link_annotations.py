from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.link_annotations import LinkAnnotationService
from app.personal_wechat_bot.domain.models import NormalizedMessage, ToolCallResult


class LinkAnnotationServiceTest(unittest.TestCase):
    def test_annotates_entry_with_web_fetch_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("please read https://example.com/a"))
            service = LinkAnnotationService(store, _FakeTools())

            results = service.annotate_entry(entry)
            updated = store.read_entries("conv1")[0]

            self.assertEqual(len(results), 1)
            self.assertEqual(updated.links[0]["status"], "completed")
            self.assertEqual(updated.text_blocks[-1]["kind"], "annotation:web")
            self.assertIn("summary text", updated.text_blocks[-1]["text"])
            annotation_path = Path(updated.text_blocks[-1]["source_ref"])
            self.assertIn("fetched text", annotation_path.read_text(encoding="utf-8"))

    def test_failed_fetch_marks_link_without_annotation_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("please read https://example.com/a"))
            service = LinkAnnotationService(store, _FakeTools(status="failed", text=""))

            service.annotate_entry(entry)
            updated = store.read_entries("conv1")[0]

            self.assertEqual(updated.links[0]["status"], "failed")
            self.assertEqual(len(updated.text_blocks), 1)

    def test_plain_url_is_not_auto_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            entry = store.append_message(_message("see https://example.com/a"))
            tools = _FakeTools()
            service = LinkAnnotationService(store, tools)

            results = service.annotate_entry(entry)
            updated = store.read_entries("conv1")[0]

            self.assertEqual(results, [])
            self.assertEqual(tools.calls, 0)
            self.assertEqual(updated.links[0]["status"], "pending")

    def test_explicit_read_of_quoted_url_fetches_target_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationLedgerStore(Path(tmp))
            store.append_message(_message("source https://example.com/a", message_id="source"))
            entry = store.append_message(
                _message("please read this link", message_id="reader", metadata={"quote": {"message_id": "source"}})
            )
            tools = _FakeTools()
            service = LinkAnnotationService(store, tools)

            results = service.annotate_entry(entry)
            updated_source = store.read_entries("conv1")[0]

            self.assertEqual(len(results), 1)
            self.assertEqual(tools.calls, 1)
            self.assertEqual(updated_source.links[0]["status"], "completed")


class _FakeTools:
    def __init__(self, status: str = "completed", text: str = "fetched text"):
        self.status = status
        self.text = text
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            status=self.status,
            summary="summary text",
            output_refs=["web_fetch/result.md"] if self.status == "completed" else [],
            error="" if self.status == "completed" else "network",
            payload={"text": self.text},
        )


def _message(text: str, *, message_id: str = "m1", metadata: dict | None = None) -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id="conv1",
        conversation_type="private",
        chat_title="PAGE",
        sender_name="PAGE",
        sender_wechat_id="wxid_page",
        text=text,
        is_self=False,
        received_at="2026-06-29T00:00:00+08:00",
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()
