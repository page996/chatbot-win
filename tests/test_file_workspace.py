from __future__ import annotations

import tempfile
import threading
import unittest
import json
import os
import stat
import time
import zipfile
from pathlib import Path

from app.personal_wechat_bot.conversation.channel_registry_store import ChannelRegistryStore
from app.personal_wechat_bot.conversation.segment import conversation_segment
from app.personal_wechat_bot.tasks.manager import TaskStatusStore
from app.personal_wechat_bot.runtime.process_lock import scoped_process_lock_path, short_process_lock
from app.personal_wechat_bot.voice.asr import AsrHealth, AsrTranscript
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
            self.assertEqual(manifest["blob_path"], staged.blob_path)
            self.assertEqual(manifest["storage_mode"], staged.storage_mode)
            self.assertFalse(bool(staged_path.stat().st_mode & stat.S_IWUSR))
            # With human-readable naming and no chat_title, segment = hashPrefix only.
            # session_id segment is unchanged.
            self.assertIn("session-one", staged.workspace_dir)
            # conversation_id without chat_title → first 8 chars of sha256(conversation-a)
            self.assertIn("conversa", staged.workspace_dir)

            source.write_text("changed outside", encoding="utf-8")
            self.assertEqual(staged_path.read_text(encoding="utf-8"), "hello from wechat cache")

    def test_duplicate_content_reuses_one_content_addressed_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "wechat-a" / "note-a.txt"
            second_source = root / "wechat-b" / "note-b.txt"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_text("same attachment bytes", encoding="utf-8")
            second_source.write_text("same attachment bytes", encoding="utf-8")
            workspace = FileWorkspace(root / "data" / "file_workspace")

            first = workspace.stage_file(first_source, conversation_id="conversation-a", session_id="session-one")
            second = workspace.stage_file(second_source, conversation_id="conversation-b", session_id="session-two")

            first_manifest = json.loads(Path(first.manifest_path).read_text(encoding="utf-8"))
            second_manifest = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
            blobs = list((workspace.root / "_blobs").glob("*/*"))
            content_blobs = [path for path in blobs if path.is_file() and path.name == first.sha256]
            guard_files = [path for path in blobs if path.is_file() and path.name.endswith(".lock.guard")]
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first_manifest["blob_path"], second_manifest["blob_path"])
            self.assertEqual(len(content_blobs), 1)
            self.assertEqual(len(guard_files), 1)
            self.assertIn(first.storage_mode, {"hardlink", "copy"})
            self.assertIn(second.storage_mode, {"hardlink", "copy"})
            if first.storage_mode == second.storage_mode == "hardlink":
                self.assertTrue(os.path.samefile(first.staged_path, second.staged_path))

    def test_hardlink_dedup_does_not_overstate_cleanup_size_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "large.bin"
            payload_size = 1024 * 1024
            source.write_bytes(b"x" * payload_size)
            workspace = FileWorkspace(root / "data" / "file_workspace")
            first = workspace.stage_file(source, conversation_id="conversation-a", session_id="session-one")
            second = workspace.stage_file(source, conversation_id="conversation-b", session_id="session-two")
            if first.storage_mode != "hardlink" or second.storage_mode != "hardlink":
                self.skipTest("filesystem does not support hardlinks")

            result = workspace.cleanup(max_total_bytes=payload_size + 256 * 1024, keep_min=0)

            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["size_basis"], "unique_file_identity_bytes")

    def test_workspace_cleanup_reclaims_unreferenced_content_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wechat" / "note.txt"
            source.parent.mkdir()
            source.write_text("cleanup blob", encoding="utf-8")
            workspace = FileWorkspace(root / "data" / "file_workspace")
            staged = workspace.stage_file(source, conversation_id="conversation-a", session_id="session-one")

            result = workspace.cleanup(max_age_seconds=-1, keep_min=0)

            self.assertEqual(result["removed"], 1)
            self.assertEqual(result["removed_blobs"], 1)
            self.assertFalse(Path(staged.blob_path).exists())
            self.assertTrue(Path(f"{staged.blob_path}.lock.guard").exists())

    def test_stage_file_uses_stable_channel_segment_when_chat_title_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            conversation_id = "conversation-title-change"
            old_segment = conversation_segment(conversation_id, "Alice")
            new_segment = conversation_segment(conversation_id, "Alice Renamed")
            ChannelRegistryStore(data_dir).upsert(
                {
                    "conversation_id": conversation_id,
                    "conversation_type": "private",
                    "chat_title": "Alice Renamed",
                    "segment": old_segment,
                }
            )
            source = root / "wechat" / "note.txt"
            source.parent.mkdir()
            source.write_text("hello", encoding="utf-8")
            workspace = FileWorkspace(data_dir / "file_workspace")

            staged = workspace.stage_file(
                source,
                conversation_id=conversation_id,
                session_id="session_default",
                original_name="note.txt",
                chat_title="Alice Renamed",
            )

            self.assertIn(old_segment, staged.workspace_dir)
            self.assertNotIn(new_segment, staged.workspace_dir)
            self.assertTrue((data_dir / "file_workspace" / old_segment / "session_default").exists())
            self.assertFalse((data_dir / "file_workspace" / new_segment).exists())

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
            self.assertEqual(analysis["external_links"], [])
            self.assertEqual(analysis["external_link_count"], 1)
            self.assertTrue(analysis["external_links_hidden"])
            self.assertNotIn("https://example.com", content)
            self.assertIn("[file-internal-url-redacted]", content)
            self.assertEqual(manifest["parse"]["content_path"], str(derived / "content.md"))
            self.assertEqual(manifest["parse"]["analysis_path"], str(derived / "analysis.json"))
            self.assertEqual(manifest["parse"]["chunk_count"], 1)

    def test_parse_and_async_ai_analysis_are_visible_as_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            source = Path(tmp) / "note.txt"
            source.write_text("hello task visibility", encoding="utf-8")
            workspace = FileWorkspace(data_dir / "file_workspace", analyzer=_CaptureAnalyzer(), analysis_async=True)
            staged = workspace.stage_file(source, conversation_id="conv-task", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "hello task visibility"),
            )

            state = _wait_for_task_status(data_dir, f"file-ai-{staged.file_id}", "completed")
            tasks = {item["task_id"]: item for item in state["tasks"]}

            self.assertEqual(tasks[f"file-parse-{staged.file_id}"]["status"], "completed")
            self.assertEqual(tasks[f"file-parse-{staged.file_id}"]["kind"], "file_parse")
            self.assertEqual(tasks[f"file-ai-{staged.file_id}"]["status"], "completed")
            self.assertEqual(tasks[f"file-ai-{staged.file_id}"]["kind"], "file_ai_analysis")
            lane = state["channels"][0]
            self.assertEqual(lane["conversation_id"], "conv-task")
            self.assertTrue(any(item["task_id"] == f"file-ai-{staged.file_id}" for item in lane["history"]))
            # Windows can hold SQLite/WAL handles for a brief moment after the
            # background analysis thread records completion.
            import gc

            gc.collect()
            time.sleep(0.2)

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

    def test_audio_file_is_marked_as_audio_with_asr_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("skipped", "audio", "音频已保存到文件中间层，当前未配置本地 ASR 转写", error="local_asr_not_configured"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            content = (Path(staged.derived_dir) / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["file_type"], "audio")
            self.assertTrue(analysis["has_audio"])
            self.assertEqual(analysis["media"]["audio_count"], 1)
            self.assertIn("local_asr_not_configured", content)

    def test_audio_file_writes_local_asr_artifact_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "audio", "summary", "转写正文"),
                embedded_media_asr=_FakeAsr("转写正文"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            asr_path = Path(analysis["media_audio"][0]["asr_path"])
            content = (Path(staged.derived_dir) / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["media_asr_status"], "completed")
            self.assertEqual(analysis["media_asr_count"], 1)
            self.assertEqual(manifest["parse"]["media_asr_count"], 1)
            self.assertTrue(asr_path.exists())
            self.assertIn("转写正文", asr_path.read_text(encoding="utf-8"))
            self.assertIn("first_audio_asr", content)

    def test_standalone_audio_reuses_primary_transcript_without_second_asr_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "audio", "summary", "primary transcript"),
                embedded_media_asr=_FailingAsr(),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(analysis["media_asr_count"], 1)
            self.assertEqual(analysis["media_audio"][0]["asr_backend"], "primary_audio_parse")
            self.assertEqual(analysis["media_audio"][0]["asr_text"], "primary transcript")

    def test_audio_analysis_input_deduplicates_primary_and_media_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            analyzer = _CaptureAnalyzer()
            workspace = FileWorkspace(Path(tmp) / "workspace", analyzer=analyzer)
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "audio", "summary", "same transcript"),
                embedded_media_asr=_FakeAsr("same transcript"),
            )

            self.assertEqual(analyzer.calls, 1)
            self.assertEqual(analyzer.last_text, "same transcript")

    def test_ai_analysis_cache_prevents_repeat_llm_call_for_same_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "note.txt"
            source.write_text("hello", encoding="utf-8")
            analyzer = _CaptureAnalyzer()
            workspace = FileWorkspace(Path(tmp) / "workspace", analyzer=analyzer)
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            result = AttachmentParseResult("parsed", "text", "summary", "hello")

            workspace.write_parse_result(staged, result)
            workspace.write_parse_result(staged, result)

            self.assertEqual(analyzer.calls, 1)

    def test_audio_file_empty_transcript_is_not_counted_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "voice.m4a"
            source.write_bytes(b"fake audio")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1", kind="audio")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("empty", "audio", "no speech", ""),
                embedded_media_asr=_FakeAsr("", status="empty"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            asr_path = Path(analysis["media_audio"][0]["asr_path"])

            self.assertEqual(analysis["media_audio"][0]["asr_status"], "empty")
            self.assertEqual(analysis["media_asr_error_count"], 0)
            self.assertEqual(analysis["media_asr_count"], 0)
            self.assertTrue(asr_path.exists())
            self.assertIn("未识别到语音内容", asr_path.read_text(encoding="utf-8"))

    def test_docx_embedded_audio_uses_local_asr_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "audio.docx"
            _write_docx_with_media(source, include_audio=True)
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "docx", "summary", "正文"),
                embedded_media_asr=_FakeAsr("嵌入音频文本"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))

            self.assertTrue(analysis["has_audio"])
            self.assertEqual(analysis["media_asr_count"], 1)
            self.assertIn("embedded_image_extraction_and_ocr", analysis["blocked_capabilities"])
            self.assertNotIn("embedded_audio_extraction_and_asr", analysis["blocked_capabilities"])

    def test_pdf_media_artifacts_record_extraction_and_render_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "paper.pdf"
            source.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "pdf", "summary", "PDF text"),
                embedded_media_ocr=_FakeOcr("PDF 图像文字"),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            media_index = json.loads(Path(analysis["media_index_path"]).read_text(encoding="utf-8"))

            self.assertIn(analysis["media_status"], {"completed", "failed"})
            self.assertIn("page_render_status", media_index)
            self.assertTrue(Path(analysis["media_index_path"]).exists())

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

    def test_medium_cjk_image_parse_result_is_chunked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            long_text = "\u4e2d" * 7000

            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "image", "summary", long_text))

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            self.assertGreater(analysis["chunk_count"], 1)

    def test_token_heavy_cjk_under_char_limit_is_chunked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "medium.txt"
            source.write_text("seed", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "text", "summary", "\u4e2d" * 2500),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            self.assertGreater(analysis["chunk_count"], 1)
            self.assertLessEqual(max(item["token_estimate"] for item in analysis["chunks"]), 1800)

    def test_standalone_image_ocr_layout_rows_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "table.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            metadata = {
                "ocr": {
                    "items": [
                        {"text": "Name", "score": 0.99, "box": _box(10, 10, 60, 30), "backend": "fake_gpu"},
                        {"text": "Age", "score": 0.98, "box": _box(100, 10, 140, 30), "backend": "fake_gpu"},
                        {"text": "Ava", "score": 0.97, "box": _box(10, 50, 60, 70), "backend": "fake_gpu"},
                        {"text": "29", "score": 0.97, "box": _box(100, 50, 140, 70), "backend": "fake_gpu"},
                    ]
                }
            }

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "image", "summary", "Name\tAge\nAva\t29", metadata=metadata),
            )

            derived = Path(staged.derived_dir)
            analysis = json.loads((derived / "analysis.json").read_text(encoding="utf-8"))
            manifest = json.loads(Path(staged.manifest_path).read_text(encoding="utf-8"))
            chunk_path = Path(analysis["ocr_table_chunks"][0]["path"])
            chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
            content = (derived / "content.md").read_text(encoding="utf-8")

            self.assertEqual(analysis["ocr_table_chunk_count"], 1)
            self.assertEqual(analysis["ocr_table_row_count"], 2)
            self.assertEqual(manifest["parse"]["ocr_table_chunk_count"], 1)
            self.assertEqual(chunk["rows"][0]["text"], "Name\tAge")
            self.assertEqual(chunk["rows"][0]["cells"][0]["backend"], "fake_gpu")
            self.assertIn("## OCR Layout Rows", content)

    def test_embedded_media_ocr_text_contributes_to_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "media.docx"
            _write_docx_with_media(source)
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            ocr_text = "\u4e2d" * 7000

            workspace.write_parse_result(
                staged,
                AttachmentParseResult("parsed", "docx", "summary", "body"),
                embedded_media_ocr=_FakeOcr(ocr_text),
            )

            analysis = json.loads((Path(staged.derived_dir) / "analysis.json").read_text(encoding="utf-8"))
            full_text = (Path(staged.derived_dir) / "full_text.md").read_text(encoding="utf-8")
            self.assertGreater(analysis["chunk_count"], 1)
            self.assertIn(ocr_text[:100], full_text)

    def test_cached_parse_result_refreshes_stale_chunk_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "long.txt"
            source.write_text("long", encoding="utf-8")
            workspace = FileWorkspace(Path(tmp) / "workspace")
            staged = workspace.stage_file(source, conversation_id="c1", session_id="s1")
            long_text = "\u4e2d" * 7000
            workspace.write_parse_result(staged, AttachmentParseResult("parsed", "text", "summary", long_text))
            analysis_path = Path(staged.derived_dir) / "analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            analysis["chunk_count"] = 1
            analysis["chunks"] = analysis["chunks"][:1]
            analysis_path.write_text(json.dumps(analysis), encoding="utf-8")

            workspace.parse_or_get_cached(staged, _CountingParser())

            refreshed = json.loads(analysis_path.read_text(encoding="utf-8"))
            self.assertGreater(refreshed["chunk_count"], 1)

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
            self.assertEqual(loaded.blob_path, staged.blob_path)
            self.assertEqual(loaded.storage_mode, staged.storage_mode)

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
                blob_path=staged.blob_path,
                storage_mode=staged.storage_mode,
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


class _FakeAsr:
    def __init__(self, text: str, status: str = "transcribed"):
        self.text = text
        self.status = status

    def health(self) -> AsrHealth:
        return AsrHealth("fake_asr", True)

    def transcribe(self, audio_path: str | Path) -> AsrTranscript:
        return AsrTranscript(self.status, self.text, backend="fake_asr", model="fake", source_path=str(audio_path))


class _FailingAsr:
    def health(self) -> AsrHealth:
        return AsrHealth("failing_asr", True)

    def transcribe(self, audio_path: str | Path) -> AsrTranscript:
        raise AssertionError("standalone parsed audio should not be transcribed twice")


class _CaptureAnalyzer:
    def __init__(self) -> None:
        self.calls = 0
        self.last_text = ""

    def analyze(self, *, name: str, kind: str, text: str, extra: dict | None = None) -> dict:
        self.calls += 1
        self.last_text = text
        return {
            "status": "analyzed",
            "summary": f"summary for {name}",
            "key_points": ["point"],
            "topics": [kind],
            "model": "capture",
        }


def _write_docx_with_media(path: Path, *, include_audio: bool = False) -> None:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr("word/document.xml", document)
        docx.writestr("word/media/image1.png", b"fake image bytes")
        if include_audio:
            docx.writestr("word/media/audio1.mp3", b"fake audio bytes")


def _box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _wait_for_task_status(data_dir: Path, task_id: str, status: str) -> dict:
    last_state: dict = {}
    for _ in range(100):
        last_state = TaskStatusStore(data_dir).state()
        task = next((item for item in last_state["tasks"] if item["task_id"] == task_id), None)
        if task is not None and task["status"] == status:
            return last_state
        time.sleep(0.05)
    return last_state


class FileWorkspaceCleanupTest(unittest.TestCase):
    def _stage(self, workspace: FileWorkspace, root: Path, name: str) -> str:
        src = root / name
        src.write_text(f"content of {name}", encoding="utf-8")
        staged = workspace.stage_file(src, conversation_id="c1", session_id="s1", original_name=name, kind="file")
        return staged.file_id

    def test_cleanup_prunes_old_dirs_but_keeps_min(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = FileWorkspace(root / "ws")
            ids = [self._stage(ws, root, f"f{i}.txt") for i in range(5)]
            # Age all dirs well past the TTL.
            old = time.time() - 10_000
            for fid in ids:
                d = ws.file_dir("c1", "s1", fid)
                os.utime(d, (old, old))

            result = ws.cleanup(max_age_seconds=1000, keep_min=2)

            # 5 dirs, keep newest 2 -> at most 3 removable, all old -> 3 removed.
            self.assertEqual(result["removed"], 3)
            remaining = [fid for fid in ids if ws.file_dir("c1", "s1", fid).exists()]
            self.assertEqual(len(remaining), 2)

    def test_cleanup_noop_when_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = FileWorkspace(root / "ws")
            for i in range(3):
                self._stage(ws, root, f"f{i}.txt")

            result = ws.cleanup(max_age_seconds=100_000, keep_min=0)

            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["scanned"], 3)

    def test_cleanup_updates_session_index(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = FileWorkspace(root / "ws")
            ids = [self._stage(ws, root, f"f{i}.txt") for i in range(4)]
            old = time.time() - 10_000
            for fid in ids:
                d = ws.file_dir("c1", "s1", fid)
                os.utime(d, (old, old))

            ws.cleanup(max_age_seconds=1000, keep_min=1)

            index_files = {item["file_id"] for item in ws.list_session_files("c1", "s1")}
            existing = {fid for fid in ids if ws.file_dir("c1", "s1", fid).exists()}
            # Index only lists dirs that still exist.
            self.assertEqual(index_files, existing)

    def test_cleanup_size_cap_removes_oldest(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = FileWorkspace(root / "ws")
            ids = []
            base = time.time() - 5000
            for i in range(4):
                fid = self._stage(ws, root, f"f{i}.txt")
                ids.append(fid)
                # Stagger mtimes so "oldest" is deterministic.
                stamp = base + i * 100
                os.utime(ws.file_dir("c1", "s1", fid), (stamp, stamp))

            # Tiny size cap forces pruning of the oldest, keep_min small.
            result = ws.cleanup(max_total_bytes=1, keep_min=1, max_age_seconds=None)

            self.assertGreaterEqual(result["removed"], 1)
            # The very newest must survive (keep_min + recency).
            self.assertTrue(ws.file_dir("c1", "s1", ids[-1]).exists())

    def test_cleanup_skips_external_conversation_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = FileWorkspace(root / "ws")
            file_id = self._stage(ws, root, "held.txt")
            file_dir = ws.file_dir("c1", "s1", file_id)
            old = time.time() - 10_000
            os.utime(file_dir, (old, old))
            lock_path = scoped_process_lock_path(
                ws.root.parent,
                "file-workspace-conversation",
                str(file_dir.parents[1]),
            )
            with short_process_lock(
                lock_path,
                timeout_seconds=1.0,
                stale_after_seconds=120.0,
            ):
                result = ws.cleanup(max_age_seconds=1, keep_min=0)
                self.assertEqual(result["removed"], 0)
                self.assertTrue(file_dir.exists())

            result = ws.cleanup(max_age_seconds=1, keep_min=0)
            self.assertEqual(result["removed"], 1)
            self.assertFalse(file_dir.exists())

    def test_stale_parse_handle_does_not_recreate_cleaned_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "stale.txt"
            source.write_text("stale", encoding="utf-8")
            ws = FileWorkspace(root / "ws")
            staged = ws.stage_file(source, conversation_id="c1", session_id="s1")
            file_dir = Path(staged.workspace_dir)
            old = time.time() - 10_000
            os.utime(file_dir, (old, old))
            cleanup = ws.cleanup(max_age_seconds=1, keep_min=0)

            self.assertEqual(cleanup["removed"], 1)
            with self.assertRaises(FileNotFoundError):
                ws.parse_or_get_cached(staged, _CountingParser())
            self.assertFalse(file_dir.exists())

    def test_stage_waits_for_external_root_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "held-stage.txt"
            source.write_text("held", encoding="utf-8")
            ws = FileWorkspace(root / "ws")
            lock_path = scoped_process_lock_path(
                ws.root.parent,
                "file-workspace-root",
                str(ws.root),
            )
            finished = threading.Event()
            staged_result: dict[str, object] = {}

            def stage() -> None:
                staged_result["value"] = ws.stage_file(
                    source,
                    conversation_id="c1",
                    session_id="s1",
                )
                finished.set()

            with short_process_lock(
                lock_path,
                timeout_seconds=1.0,
                stale_after_seconds=120.0,
            ):
                worker = threading.Thread(target=stage)
                worker.start()
                self.assertFalse(finished.wait(0.1))

            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())
            self.assertIn("value", staged_result)


if __name__ == "__main__":
    unittest.main()
