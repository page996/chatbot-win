from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AttachmentParseResult
from app.personal_wechat_bot.workspace.file_analysis import LLMFileAnalyzer
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


class _JsonLLM:
    """Fake chat LLM that returns a fixed JSON analysis and counts calls."""

    def __init__(self, payload: dict, *, model: str = "fake"):
        self.payload = payload
        self.model = model
        self.calls = 0

    def generate_reply(self, prompt: str) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return json.dumps(self.payload, ensure_ascii=False)


class _ProseLLM:
    def generate_reply(self, prompt: str) -> str:
        return "这是一段没有 JSON 包裹的分析散文。"


class _BoomLLM:
    def generate_reply(self, prompt: str) -> str:
        raise RuntimeError("llm exploded")


class _FakeChatLLM:
    def generate_reply(self, prompt: str) -> str:
        return "ok\nPLAN: reply\nMONITOR: fake_llm.completed\nSUMMARY: ok"


class FileAnalyzerTest(unittest.TestCase):
    def test_analyze_parses_structured_json(self) -> None:
        llm = _JsonLLM({"summary": "一个签证清单", "key_points": ["需要护照", "需要照片"], "topics": ["签证", "留学"]})
        analyzer = LLMFileAnalyzer(llm, model="m1")

        result = analyzer.analyze(name="checklist.pdf", kind="pdf", text="visa checklist body")

        self.assertEqual(result.status, "analyzed")
        self.assertEqual(result.summary, "一个签证清单")
        self.assertEqual(result.key_points, ["需要护照", "需要照片"])
        self.assertEqual(result.topics, ["签证", "留学"])
        self.assertEqual(result.model, "m1")

    def test_analyze_skips_empty_text(self) -> None:
        llm = _JsonLLM({"summary": "x"})
        analyzer = LLMFileAnalyzer(llm)

        result = analyzer.analyze(name="a.png", kind="image", text="   ")

        self.assertEqual(result.status, "skipped")
        self.assertEqual(llm.calls, 0)

    def test_analyze_keeps_prose_when_not_json(self) -> None:
        analyzer = LLMFileAnalyzer(_ProseLLM())

        result = analyzer.analyze(name="a.txt", kind="text", text="body")

        self.assertEqual(result.status, "analyzed")
        self.assertIn("散文", result.summary)

    def test_analyze_returns_error_on_llm_exception(self) -> None:
        analyzer = LLMFileAnalyzer(_BoomLLM())

        result = analyzer.analyze(name="a.txt", kind="text", text="body")

        self.assertEqual(result.status, "error")
        self.assertIn("llm exploded", result.error)

    def test_analyze_ignores_fake_chat_reply(self) -> None:
        analyzer = LLMFileAnalyzer(_FakeChatLLM())

        result = analyzer.analyze(name="a.txt", kind="text", text="body")

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.error, "fake_llm_output_ignored")


class FileWorkspaceAnalysisTest(unittest.TestCase):
    def test_analysis_json_and_content_carry_ai_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("some parsed content", encoding="utf-8")
            llm = _JsonLLM({"summary": "简短摘要", "key_points": ["要点A"], "topics": ["主题X"]})
            workspace = FileWorkspace(Path(tmp) / "ws", analyzer=LLMFileAnalyzer(llm, model="m9"))
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="file")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "some parsed content"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            content = (Path(staged.derived_dir) / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["ai_analysis_status"], "analyzed")
            self.assertEqual(analysis["ai_summary"], "简短摘要")
            self.assertEqual(analysis["ai_key_points"], ["要点A"])
            self.assertEqual(analysis["ai_topics"], ["主题X"])
            self.assertIn("## AI Analysis", content)
            self.assertIn("简短摘要", content)
            self.assertIn("要点A", content)

    def test_ai_analysis_is_cached_across_rerenders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("stable content", encoding="utf-8")
            llm = _JsonLLM({"summary": "缓存摘要"})
            workspace = FileWorkspace(Path(tmp) / "ws", analyzer=LLMFileAnalyzer(llm))
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="file")
            result = AttachmentParseResult("parsed", "text", "summary", "stable content")

            workspace.write_parse_result(staged, result)
            workspace.write_parse_result(staged, result)

            # Second render reuses the cached analysis rather than calling the LLM again.
            self.assertEqual(llm.calls, 1)

    def test_disabled_analyzer_marks_status_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("content", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "ws")  # no analyzer
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="file")

            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "text", "s", "content"))

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            content = (Path(staged.derived_dir) / "content.md").read_text(encoding="utf-8")
            self.assertEqual(analysis["ai_analysis_status"], "disabled")
            self.assertNotIn("## AI Analysis", content)

    def test_async_analysis_backfills_manifest_parse_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("async parsed content", encoding="utf-8")
            llm = _JsonLLM({"summary": "async summary", "key_points": ["point A"], "topics": ["topic A"]})
            workspace = FileWorkspace(Path(tmp) / "ws", analyzer=LLMFileAnalyzer(llm), analysis_async=True)
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="file")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "async parsed content"),
            )

            manifest = _wait_for_manifest_ai_summary(Path(staged.manifest_path), "async summary")
            content = (Path(staged.derived_dir) / "content.md").read_text(encoding="utf-8")

            self.assertEqual(manifest["parse"]["ai_analysis_status"], "analyzed")
            self.assertEqual(manifest["parse"]["ai_summary"], "async summary")
            self.assertIn("async summary", content)

    def test_async_analysis_refreshes_conversation_file_analysis_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            source = Path(tmp) / "note.txt"
            source.write_text("async parsed content", encoding="utf-8")
            llm = _JsonLLM({"summary": "async ledger summary", "key_points": ["ledger point"]})
            workspace = FileWorkspace(data_dir / "file_workspace", analyzer=LLMFileAnalyzer(llm), analysis_async=True)
            staged = workspace.stage_file(source, conversation_id="conv1", session_id="s1", kind="file")
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_message(
                NormalizedMessage(
                    message_id="m1",
                    conversation_id="conv1",
                    conversation_type="private",
                    chat_title="Alice",
                    sender_name="Alice",
                    text="file incoming",
                    is_self=False,
                    received_at="2026-07-07T00:00:00+00:00",
                    metadata={
                        "session_id": "s1",
                        "attachments": [
                            {
                                "status": "indexed",
                                "file_id": staged.file_id,
                                "name": "note.txt",
                                "workspace": {"manifest_path": staged.manifest_path, "derived_dir": staged.derived_dir},
                                "parse": {
                                    "status": "parsed",
                                    "kind": "text",
                                    "summary": "summary",
                                    "text": "async parsed content",
                                    "ai_analysis_status": "pending",
                                },
                                "artifacts": {"ai_analysis_status": "pending"},
                            }
                        ],
                    },
                )
            )

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "async parsed content"),
            )

            markdown = _wait_for_markdown_text(
                ledger.conversation_markdown_path("conv1"),
                "async ledger summary",
            )
            self.assertIn("AI Analysis:\nasync ledger summary", markdown)
            self.assertIn("- ledger point", markdown)

    def test_empty_ocr_placeholder_is_not_analyzed(self) -> None:
        # Regression: when OCR/ASR recognized nothing, the placeholder text must
        # NOT be fed to the analyzer as if it were real content. The analyzer
        # should receive empty input and return skipped, and the LLM must not be
        # called with placeholder boilerplate.
        from app.personal_wechat_bot.voice.asr import AsrHealth, AsrTranscript

        class _EmptyAsr:
            def health(self):
                return AsrHealth("fake_asr", True)

            def transcribe(self, audio_path):
                return AsrTranscript("empty", "", backend="fake_asr", model="fake", source_path=str(audio_path))

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            llm = _JsonLLM({"summary": "不应被调用"})
            workspace = FileWorkspace(Path(tmp) / "ws", analyzer=LLMFileAnalyzer(llm))
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            # An audio whose ASR is empty: parse result is a placeholder ("empty").
            workspace.write_parse_result(
                staged,
                AttachmentParseResult("empty", "audio", "no speech", ""),
                embedded_media_asr=_EmptyAsr(),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(analysis["ai_analysis_status"], "skipped")
            self.assertEqual(llm.calls, 0)


def _wait_for_manifest_ai_summary(path: Path, expected: str, *, timeout_seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while time.monotonic() < deadline:
        last = json.loads(path.read_text(encoding="utf-8"))
        parse = last.get("parse") if isinstance(last.get("parse"), dict) else {}
        if parse.get("ai_summary") == expected:
            return last
        time.sleep(0.05)
    return last


def _wait_for_markdown_text(path: Path, expected: str, *, timeout_seconds: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        if path.exists():
            last = path.read_text(encoding="utf-8")
            if expected in last:
                return last
        time.sleep(0.05)
    return last


if __name__ == "__main__":
    unittest.main()
