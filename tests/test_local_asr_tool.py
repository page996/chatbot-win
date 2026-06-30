from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.voice.asr_tool import LocalAsrTool
from app.personal_wechat_bot.voice.asr import AsrHealth, AsrTranscript


class LocalAsrToolTest(unittest.TestCase):
    def test_local_asr_tool_writes_transcript_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "inbox" / "voice.m4a"
            audio.parent.mkdir()
            audio.write_bytes(b"fake audio")
            tool = LocalAsrTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                allowed_input_roots=[audio.parent],
                max_input_bytes=1024,
                asr_engine=_FakeAsr("hello transcript"),
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="voice.local_asr",
                    call_id="call1",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"input_path": str(audio)},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertIn("hello transcript", result.summary)
            self.assertTrue(Path(result.output_refs[0]).exists())

    def test_local_asr_tool_reports_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "inbox" / "voice.m4a"
            audio.parent.mkdir()
            audio.write_bytes(b"fake audio")
            tool = LocalAsrTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                allowed_input_roots=[audio.parent],
                max_input_bytes=1024,
                asr_engine=_FakeAsr("", status="blocked", error="local_asr_not_configured"),
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="voice.local_asr",
                    call_id="call1",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"input_path": str(audio)},
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.error, "local_asr_not_configured")


class _FakeAsr:
    def __init__(self, text: str, *, status: str = "transcribed", error: str = ""):
        self.text = text
        self.status = status
        self.error = error

    def health(self) -> AsrHealth:
        return AsrHealth("fake_asr", True)

    def transcribe(self, audio_path: str | Path) -> AsrTranscript:
        return AsrTranscript(self.status, self.text, backend="fake_asr", model="fake", source_path=str(audio_path), error=self.error)


if __name__ == "__main__":
    unittest.main()
