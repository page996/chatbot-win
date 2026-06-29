from __future__ import annotations

import tempfile
import unittest
import json
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


if __name__ == "__main__":
    unittest.main()
