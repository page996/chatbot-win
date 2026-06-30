from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.personal_wechat_bot.voice.asr import AsrHealth, AsrTranscript
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser


class BackendAttachmentParserTest(unittest.TestCase):
    def test_text_attachment_preview_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.txt"
            path.write_text(" first line \n\n second line ", encoding="utf-8")

            result = BackendAttachmentParser(asr_engine=_FakeAsr("", status="blocked", error="local_asr_not_configured"), max_preview_chars=100).parse(path)

            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.kind, "text")
            self.assertEqual(result.summary, "已读取文本附件预览")
            self.assertEqual(result.text, "first line\nsecond line")

    def test_text_attachment_preview_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.txt"
            path.write_text("abcdef", encoding="utf-8")

            result = BackendAttachmentParser(max_preview_chars=4).parse(path)

            self.assertEqual(result.text, "abc…")

    def test_text_attachment_preview_strips_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.txt"
            path.write_text("\ufeff带 BOM 的文本", encoding="utf-8")

            result = BackendAttachmentParser(max_preview_chars=100).parse(path)

            self.assertEqual(result.text, "带 BOM 的文本")

    def test_docx_attachment_extracts_document_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.docx"
            _write_minimal_docx(path, ["第一段", "第二段"])

            result = BackendAttachmentParser(max_preview_chars=100).parse(path)

            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.kind, "docx")
            self.assertEqual(result.summary, "已提取 DOCX 文本预览")
            self.assertEqual(result.text, "第一段\n第二段")

    def test_image_attachment_uses_injected_ocr_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"
            path.write_bytes(b"not a real image for this unit test")

            result = BackendAttachmentParser(ocr_engine=_FakeOcr("图片里的任务信息"), max_preview_chars=100).parse(path)

            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.kind, "image")
            self.assertEqual(result.summary, "已完成图片 OCR 预览")
            self.assertEqual(result.text, "图片里的任务信息")

    def test_pdf_is_registered_when_worker_python_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            path.write_bytes(b"%PDF-placeholder")

            result = BackendAttachmentParser(worker_python=Path("missing-python.exe")).parse(path)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.kind, "pdf")
            self.assertIn("worker Python 不可用", result.summary)

    def test_audio_attachment_is_preserved_with_explicit_asr_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.m4a"
            path.write_bytes(b"fake audio")

            result = BackendAttachmentParser(
                asr_engine=_FakeAsr("", status="blocked", error="local_asr_not_configured"),
                max_preview_chars=100,
            ).parse(path)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.kind, "audio")
            self.assertIn("本地 ASR 暂不可用", result.summary)
            self.assertEqual(result.error, "local_asr_not_configured")

    def test_audio_attachment_reports_asr_decode_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.m4a"
            path.write_bytes(b"fake audio")

            result = BackendAttachmentParser(
                asr_engine=_FakeAsr("", status="failed", error="invalid_audio_format"),
                max_preview_chars=100,
            ).parse(path)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.kind, "audio")
            self.assertIn("本地 ASR 转写失败", result.summary)
            self.assertEqual(result.error, "invalid_audio_format")

    def test_audio_attachment_uses_injected_asr_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.m4a"
            path.write_bytes(b"fake audio")

            result = BackendAttachmentParser(asr_engine=_FakeAsr("转写文本"), max_preview_chars=100).parse(path)

            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.kind, "audio")
            self.assertIn("本地 ASR", result.summary)
            self.assertEqual(result.text, "转写文本")

    def test_csv_attachment_extracts_rows_with_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            path.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")

            result = BackendAttachmentParser(max_preview_chars=100).parse(path)

            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.kind, "spreadsheet")
            self.assertEqual(result.summary, "已提取表格文本预览")
            self.assertIn("name\tvalue", result.text)
            self.assertIn("alpha\t1", result.text)

    def test_default_worker_python_prefers_project_vendor_runtime(self) -> None:
        parser = BackendAttachmentParser()

        self.assertIsNotNone(parser.worker_python)
        self.assertIn("vendor", str(parser.worker_python))
        self.assertTrue(str(parser.worker_python).endswith("python.exe"))


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


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr("word/document.xml", document)


if __name__ == "__main__":
    unittest.main()
