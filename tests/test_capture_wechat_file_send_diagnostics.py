from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.sidebar_api import clear_sidebar_history_data
from app.personal_wechat_bot.runtime.history_fence import active_history_writer_leases, history_writer_fence


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture_wechat_file_send_diagnostics.py"


def _listed_history_writer_leases(data_dir: Path) -> list[dict]:
    with history_writer_fence(data_dir, label="diagnostics_test_lease_inspection"):
        return active_history_writer_leases(data_dir)


def _load_module():
    spec = importlib.util.spec_from_file_location("capture_wechat_file_send_diagnostics", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureWechatFileSendDiagnosticsTests(unittest.TestCase):
    def test_extract_hints_classifies_file_send_candidates(self) -> None:
        module = _load_module()
        payload = {
            "events": [
                {
                    "wrapper": "uploadappattach_init",
                    "return_weixin_offset": "0x000000000522b2d6",
                    "args": {
                        "arg2": {
                            "string_candidates": [
                                {
                                    "offset": "0x0000000000000010",
                                    "encoding": "bytes",
                                    "value": "wxid_abc123",
                                }
                            ]
                        },
                        "arg3": {
                            "raw_string_candidates": [
                                {
                                    "offset": "0x0000000000000020",
                                    "encoding": "raw-utf16le",
                                    "value": r"E:\tmp\probe.csv",
                                }
                            ]
                        },
                        "arg4": {
                            "pointer_fields": [
                                {
                                    "offset": "0x0000000000000038",
                                    "ptr": "0x0000012345678000",
                                    "string_candidates": [
                                        {
                                            "offset": "0x0000000000000000",
                                            "encoding": "bytes",
                                            "value": "uploadappattach",
                                        },
                                        {
                                            "offset": "0x0000000000000010",
                                            "encoding": "bytes",
                                            "value": "probe.csv",
                                        },
                                        {
                                            "offset": "0x0000000000000020",
                                            "encoding": "bytes",
                                            "value": "e76551e81f1e19757015797755e52047",
                                        },
                                        {
                                            "offset": "0x0000000000000030",
                                            "encoding": "bytes",
                                            "value": "<msg><appmsg><title>probe.csv</title></appmsg></msg>",
                                        },
                                    ],
                                    "raw_string_candidates": [
                                        {
                                            "offset": "0x0000000000000040",
                                            "encoding": "raw-utf16le",
                                            "value": r"E:\tmp\nested.docx",
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
            ]
        }

        hints = module.extract_hints(payload)
        kinds = {hint["kind"] for hint in hints}
        self.assertIn("receiver_wxid", kinds)
        self.assertIn("file_path", kinds)
        self.assertIn("file_name", kinds)
        self.assertIn("file_md5", kinds)
        self.assertIn("appmsg_xml", kinds)
        self.assertIn("upload_endpoint", kinds)
        self.assertIn(r"E:\tmp\nested.docx", {hint["value"] for hint in hints})
        self.assertGreaterEqual(hints[0]["score"], hints[-1]["score"])

    def test_extract_hints_classifies_winapi_path_events(self) -> None:
        module = _load_module()
        payload = {
            "events": [
                {
                    "wrapper": "winapi_create_file_w",
                    "return_weixin_offset": "0x0000000001234567",
                    "path": r"E:\tmp\probe.csv",
                    "result_value": "0x0000000000000124",
                    "last_error": 0,
                },
                {
                    "wrapper": "winapi_create_hard_link_w",
                    "return_weixin_offset": "0x0000000001234999",
                    "path": r"E:\WeChat-doc\xwechat_files\wxid_a\FileStorage\File\probe.csv",
                    "path2": r"E:\tmp\probe.csv",
                    "result_value": "0x0000000000000001",
                    "last_error": 0,
                },
            ]
        }

        hints = module.extract_hints(payload)

        values = {hint["value"] for hint in hints}
        self.assertIn(r"E:\tmp\probe.csv", values)
        self.assertIn(r"E:\WeChat-doc\xwechat_files\wxid_a\FileStorage\File\probe.csv", values)
        self.assertIn("file_path", {hint["kind"] for hint in hints})
        self.assertTrue(all(str(hint["wrapper"]).startswith("winapi_") for hint in hints))

    def test_load_input_payload_reads_jsonl_events(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"wrapper": "uploadappattach_init", "args": {}}),
                        "not-json",
                        json.dumps({"wrapper": "sendfileuploadmsg_short", "args": {}}),
                    ]
                ),
                encoding="utf-8",
            )

            payload = module.load_input_payload(path)

        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["events_source"], "input_jsonl")
        self.assertEqual(payload["events"][0]["wrapper"], "uploadappattach_init")

    def test_load_input_payload_accepts_bom_json(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.json"
            path.write_text(
                "\ufeff" + json.dumps({"events": [{"wrapper": "sendfile_task_entry", "args": {}}]}),
                encoding="utf-8",
            )

            payload = module.load_input_payload(path)

        self.assertEqual(payload["event_count"], 1)
        self.assertEqual(payload["events"][0]["wrapper"], "sendfile_task_entry")

    def test_default_analyzed_output_path_does_not_overwrite_input(self) -> None:
        module = _load_module()
        path = Path(r"E:\tmp\file-send-events-1.json")

        analyzed = module.default_analyzed_output_path(path)

        self.assertEqual(analyzed.name, "file-send-events-1.analyzed.json")

    def test_generate_markdown_report_includes_wrappers_and_hints(self) -> None:
        module = _load_module()
        payload = {
            "events_source": "input_json",
            "persistent_log_path": r"C:\Temp\wechat_native_file_send_diagnostics.jsonl",
            "events": [
                {
                    "wrapper": "uploadappattach_init",
                    "return_weixin_offset": "0x000000000522b2d6",
                    "args": {},
                }
            ],
            "hints": [
                {
                    "score": 108,
                    "kind": "file_path",
                    "value": r"E:\tmp\probe.csv",
                    "wrapper": "uploadappattach_init",
                    "return_weixin_offset": "0x000000000522b2d6",
                    "arg": "arg3",
                    "source": "raw_string_candidates",
                    "offset": "0x20",
                    "pointer_offset": "",
                }
            ],
        }

        report = module.generate_markdown_report(payload)

        self.assertIn("uploadappattach_init", report)
        self.assertIn("Expected Diagnostic Wrappers", report)
        self.assertIn("sendfile_submit_factory", report)
        self.assertIn("sendfile_request_builder", report)
        self.assertIn("sendfile_vector_processor", report)
        self.assertIn("sendfile_context_dispatch", report)
        self.assertIn("sendfile_request_derived_ctor", report)
        self.assertIn("fileitem_business_ctor_a", report)
        self.assertIn("winapi_create_file_w", report)
        self.assertIn("Key Hints", report)
        self.assertIn("file_path", report)
        self.assertIn("probe.csv", report)
        self.assertIn("Confirm the top receiver hint", report)

    def test_waiting_capture_holds_writer_lease_and_blocks_history_clear(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            create_default_config(data_dir)
            native_dir = data_dir / "native_diagnostics"
            native_dir.mkdir()
            sentinel = native_dir / "before-reset.json"
            sentinel.write_text("sentinel", encoding="utf-8")
            waiting = threading.Event()
            finish_capture = threading.Event()
            results: list[int] = []
            errors: list[BaseException] = []
            read_calls = 0

            def read_events(_base_url: str) -> dict:
                nonlocal read_calls
                read_calls += 1
                if read_calls == 1:
                    return {"event_count": 0, "events": []}
                waiting.set()
                if not finish_capture.wait(5.0):
                    raise TimeoutError("capture test was not released")
                return {"event_count": 1, "events": [{"wrapper": "sendfile_task_entry", "args": {}}]}

            def run_capture() -> None:
                try:
                    results.append(module.main())
                except BaseException as exc:
                    errors.append(exc)

            argv = [
                str(SCRIPT),
                "--data-dir",
                str(data_dir),
                "--wait",
                "10",
                "--min-events",
                "1",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                module,
                "read_events_with_fallback",
                side_effect=read_events,
            ), mock.patch.object(module.time, "sleep", return_value=None), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(io.StringIO()):
                thread = threading.Thread(target=run_capture, daemon=True)
                thread.start()
                try:
                    self.assertTrue(waiting.wait(2.0))
                    self.assertTrue(_listed_history_writer_leases(data_dir))

                    clear_result = clear_sidebar_history_data(data_dir)

                    self.assertEqual(clear_result["status"], "blocked")
                    self.assertTrue(sentinel.exists())
                finally:
                    finish_capture.set()
                    thread.join(timeout=5.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [0])
            self.assertEqual(_listed_history_writer_leases(data_dir), [])
            self.assertEqual(len(list(native_dir.glob("file-send-events-*.json"))), 1)
            self.assertEqual(len(list(native_dir.glob("file-send-events-*.report.md"))), 1)

    def test_external_analysis_output_does_not_bind_default_data_dir(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            create_default_config(data_dir)
            config_path = data_dir / "config.json"
            config_before = config_path.read_bytes()
            external_dir = root / "external"
            external_dir.mkdir()
            input_path = external_dir / "capture.json"
            input_path.write_text(
                json.dumps({"events": [{"wrapper": "sendfile_task_entry", "args": {}}]}),
                encoding="utf-8",
            )
            output_path = external_dir / "analyzed.json"
            observed_data_leases: list[bool] = []
            original_generate = module.generate_markdown_report

            def generate_report(payload: dict) -> str:
                observed_data_leases.append(bool(_listed_history_writer_leases(data_dir)))
                return original_generate(payload)

            argv = [
                str(SCRIPT),
                "--data-dir",
                str(data_dir),
                "--input",
                str(input_path),
                "--out",
                str(output_path),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                module,
                "generate_markdown_report",
                side_effect=generate_report,
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = module.main()

            self.assertEqual(result, 0)
            self.assertEqual(observed_data_leases, [False])
            self.assertEqual(_listed_history_writer_leases(data_dir), [])
            self.assertEqual(config_path.read_bytes(), config_before)
            self.assertFalse((data_dir / "native_diagnostics").exists())
            self.assertTrue(output_path.exists())
            self.assertTrue(module.report_path_for(output_path).exists())


if __name__ == "__main__":
    unittest.main()
