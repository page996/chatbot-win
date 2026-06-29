from __future__ import annotations

import tempfile
import unittest
import json
import zipfile
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import AttachmentParseResult
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


class FileWorkspaceTest(unittest.TestCase):
    def test_stage_file_copies_original_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wechat" / "note.txt"
            source.parent.mkdir()
            source.write_text("hello from wechat cache", encoding="utf-8")
            workspace = FileWorkspace(root / "data" / "file_workspace")

            staged = workspace.stage_file(
                source,
                conversation_id="conversation-a",
                session_id="session-one",
                original_name="note.txt",
                kind="file",
            )

            staged_path = Path(staged.staged_path)
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            self.assertTrue(staged_path.exists())
            self.assertEqual(staged_path.read_text(encoding="utf-8"), "hello from wechat cache")
            self.assertEqual(manifest["original_path"], str(source.resolve()))
            self.assertIn("conversation-a", staged.workspace_dir)
            self.assertIn("session-one", staged.workspace_dir)

            source.write_text("changed outside", encoding="utf-8")
            self.assertEqual(staged_path.read_text(encoding="utf-8"), "hello from wechat cache")

    def test_parse_result_is_cached_per_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("hello", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            parser = _CountingParser()

            first = workspace.parse_or_get_cached(staged, parser)
            second = workspace.parse_or_get_cached(staged, parser)

            self.assertEqual(first.text, "parsed text")
            self.assertEqual(second.text, "parsed text")
            self.assertEqual(parser.calls, 1)
            self.assertTrue((Path(staged.derived_dir) / "parse_result.json").exists())
            self.assertEqual((Path(staged.derived_dir) / "preview.txt").read_text(encoding="utf-8"), "parsed text")
            self.assertTrue((Path(staged.derived_dir) / "content.md").exists())
            self.assertTrue((Path(staged.derived_dir) / "analysis.json").exists())
            self.assertTrue((Path(staged.derived_dir) / "chunks" / "chunk_0001.md").exists())

    def test_parse_result_writes_standard_artifacts_and_manifest_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("hello https://example.com", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "hello https://example.com"),
            )

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            content = (derived / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["file_type"], "text")
            self.assertEqual(analysis["external_links"], ["https://example.com"])
            self.assertIn("hello https://example.com", content)
            self.assertEqual(manifest["parse"]["content_path"], str(derived / "content.md"))
            self.assertEqual(manifest["parse"]["analysis_path"], str(derived / "analysis.json"))
            self.assertEqual(manifest["parse"]["chunk_count"], 1)

    def test_spreadsheet_parse_result_writes_structured_table_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "table.csv"
            rows = ["name,value"]
            rows.extend(f"item-{index},{index}" for index in range(105))
            source.write_text("\n".join(rows), encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "spreadsheet", "summary", "name\tvalue\nitem-0\t0"),
            )

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            first_chunk_path = Path(analysis["table_chunks"][0]["path"])
            second_chunk_path = Path(analysis["table_chunks"][1]["path"])
            first_chunk = json.loads(first_chunk_path.read_text(encoding="utf-8"))
            content = (derived / "content.md").read_text(encoding="utf-8")

            self.assertTrue(analysis["has_tables"])
            self.assertEqual(analysis["table_status"], "completed")
            self.assertEqual(analysis["table_row_count"], 105)
            self.assertEqual(analysis["table_chunk_count"], 2)
            self.assertTrue(first_chunk_path.exists())
            self.assertTrue(second_chunk_path.exists())
            self.assertEqual(first_chunk["rows"][0], {"name": "item-0", "value": "0"})
            self.assertEqual(first_chunk["row_count"], 100)
            self.assertEqual(manifest["parse"]["table_chunk_count"], 2)
            self.assertEqual(Path(manifest["parse"]["table_index_path"]), derived / "tables" / "index.json")
            self.assertIn("## Tables", content)
            self.assertIn("table_count: 1", content)

    def test_docx_parse_result_marks_embedded_media_as_blocked_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "media.docx"
            _write_docx_with_media(source)
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "docx", "summary", "正文"))

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            content = (derived / "content.md").read_text(encoding="utf-8")
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))

            self.assertTrue(analysis["has_images"])
            self.assertEqual(analysis["media"]["image_count"], 1)
            self.assertEqual(analysis["media_extract_count"], 1)
            self.assertTrue(Path(analysis["media_index_path"]).exists())
            self.assertTrue(Path(analysis["media_images"][0]["path"]).exists())
            self.assertEqual(manifest["parse"]["media_extract_count"], 1)
            self.assertEqual(Path(manifest["parse"]["media_index_path"]), derived / "media" / "index.json")
            self.assertIn("embedded_image_extraction_and_ocr", analysis["blocked_capabilities"])
            self.assertIn("## Embedded Media", content)
            self.assertIn("blocked_capabilities", content)

    def test_docx_embedded_image_ocr_writes_media_markdown_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "media.docx"
            _write_docx_with_media(source)
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "docx", "summary", "正文"),
                embedded_media_ocr=_FakeOcr("图片文字"),
            )

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            media_index = json.loads(Path(analysis["media_index_path"]).read_text(encoding="utf-8"))
            ocr_path = Path(analysis["media_images"][0]["ocr_path"])
            content = (derived / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["media_ocr_status"], "completed")
            self.assertEqual(analysis["media_ocr_count"], 1)
            self.assertEqual(media_index["images"][0]["ocr_text"], "图片文字")
            self.assertEqual(manifest["parse"]["media_ocr_count"], 1)
            self.assertTrue(ocr_path.exists())
            self.assertIn("图片文字", ocr_path.read_text(encoding="utf-8"))
            self.assertIn("first_image_ocr", content)

    def test_standalone_image_is_not_marked_as_embedded_media_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "image.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "image", "summary", "OCR text"))

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))

            self.assertTrue(analysis["has_images"])
            self.assertEqual(analysis["blocked_capabilities"], [])

    def test_long_parse_result_is_chunked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long.txt"
            source.write_text("long", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            long_text = "\n\n".join(f"paragraph {index} " + ("x" * 500) for index in range(60))

            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "text", "summary", long_text))

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            chunks = list((derived / "chunks").glob("chunk_*.md"))

            self.assertTrue(analysis["chunked"])
            self.assertGreater(analysis["chunk_count"], 1)
            self.assertEqual(analysis["chunk_count"], len(chunks))
            self.assertEqual(manifest["parse"]["chunk_count"], analysis["chunk_count"])
            self.assertTrue(all(path.read_text(encoding="utf-8").startswith("# Chunk") for path in chunks))

    def test_staged_file_can_be_reloaded_from_manifest_for_later_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("hello", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            loaded = workspace.staged_from_manifest(staged.manifest_path)

            self.assertEqual(loaded.file_id, staged.file_id)
            self.assertEqual(loaded.staged_path, staged.staged_path)
            self.assertEqual(loaded.outputs_dir, staged.outputs_dir)

    def test_manifest_tracks_multiple_sources_for_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "wechat-a" / "note.txt"
            second_source = root / "wechat-b" / "renamed.txt"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_text("same", encoding="utf-8")
            second_source.write_text("same", encoding="utf-8")
            workspace = FileWorkspace(root / "workspace")

            first = workspace.stage_file(first_source, conversation_id="c1", session_id="s1", original_name="note.txt")
            second = workspace.stage_file(second_source, conversation_id="c1", session_id="s1", original_name="renamed.txt")
            manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
            index = workspace.list_session_files("c1", "s1")

            self.assertEqual(first.file_id, second.file_id)
            self.assertEqual(len(manifest["sources"]), 2)
            self.assertEqual(index[0]["source_count"], 2)

    def test_staged_from_manifest_rejects_paths_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "note.txt"
            source.write_text("hello", encoding="utf-8")
            workspace = FileWorkspace(root / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            manifest_path = Path(staged.manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["staged_path"] = str(root / "outside.txt")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(PermissionError):
                workspace.staged_from_manifest(manifest_path)

    def test_parse_cache_is_not_reused_when_staged_suffix_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("hello", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            parser = _CountingParser()
            workspace.parse_or_get_cached(staged, parser)
            changed = type(staged)(
                file_id=staged.file_id,
                conversation_id=staged.conversation_id,
                session_id=staged.session_id,
                original_name=staged.original_name,
                kind=staged.kind,
                sha256=staged.sha256,
                workspace_dir=staged.workspace_dir,
                staged_path=str(Path(staged.staged_path).with_suffix(".pdf")),
                manifest_path=staged.manifest_path,
                derived_dir=staged.derived_dir,
                outputs_dir=staged.outputs_dir,
                source=staged.source,
            )

            workspace.parse_or_get_cached(changed, parser)

            self.assertEqual(parser.calls, 2)

    def test_libreoffice_outputs_are_constrained_to_file_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "doc.docx"
            source.write_text("fake docx", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            result = workspace.libreoffice_convert_to_pdf(staged, runtime=_FakeLibreOffice())

            self.assertEqual(result.status, "completed")
            self.assertIn(str(Path(staged.outputs_dir)), result.output_path)

    def test_conversation_and_session_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "shared.txt"
            source.write_text("same content", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")

            first = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            second = workspace.stage_file(source, conversation_id="c1", session_id="s2")
            third = workspace.stage_file(source, conversation_id="c2", session_id="s1")

            self.assertNotEqual(Path(first.workspace_dir), Path(second.workspace_dir))
            self.assertNotEqual(Path(first.workspace_dir), Path(third.workspace_dir))


class _CountingParser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, path: str | Path) -> AttachmentParseResult:
        self.calls += 1
        return AttachmentParseResult("parsed", "text", "summary", "parsed text")


class _FakeLibreOffice:
    def convert_to_pdf(self, input_path: str | Path, output_dir: str | Path) -> Path:
        output = Path(output_dir) / (Path(input_path).stem + ".pdf")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("pdf", encoding="utf-8")
        return output


class _FakeOcr:
    def __init__(self, text: str):
        self.text = text

    def health(self):
        return None

    def read_text(self, image_path: str | Path) -> str:
        return self.text


def _write_docx_with_media(path: Path) -> None:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr("word/document.xml", document)
        docx.writestr("word/media/image1.png", b"fake image bytes")


if __name__ == "__main__":
    unittest.main()
