from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.audit import (
    build_artifact_cleanup_report,
    build_plan_audit,
    build_storage_migration_status,
)
from app.personal_wechat_bot.control.send_commands import set_send_controls


class AuditCleanupTest(unittest.TestCase):
    def test_plan_audit_reports_stale_notes_and_current_safety_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            create_default_config(data_dir)
            plan = root / "plan.md"
            plan.write_text(
                "\n".join(
                    [
                        "2026-06-28 old note",
                        "当前设备未发现 `soffice` / `libreoffice` / `tesseract`。",
                        "`.pdf` 当前只登记并返回“待接入 PDF 文本抽取”，避免误读。",
                    ]
                ),
                encoding="utf-8",
            )

            report = build_plan_audit(data_dir, plan_path=plan)

            self.assertEqual(report["status"], "ok")
            self.assertFalse(report["current_truth"]["send_enabled"])
            self.assertTrue(report["current_truth"]["real_send_implemented"])
            self.assertTrue(report["current_truth"]["wechat_read_only"])
            self.assertIn("ocr", report["current_truth"])
            self.assertIn("libreoffice", report["current_truth"])
            self.assertEqual(report["plan_residual_count"], 2)
            self.assertEqual(report["cleanup_order"][0]["item"], "plan-current-state-audit")

    def test_cleanup_dry_run_does_not_delete_candidates_or_retained_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            disposable = data_dir / "wechat_window.bmp"
            retained = data_dir / "processed_messages.sqlite"
            disposable.write_bytes(b"image")
            retained.write_bytes(b"sqlite")

            report = build_artifact_cleanup_report(data_dir, apply=False)

            self.assertEqual(report["status"], "dry_run")
            self.assertEqual(report["candidate_count"], 1)
            self.assertTrue(disposable.exists())
            self.assertTrue(retained.exists())
            self.assertEqual(report["candidates"][0]["action"], "would_delete")
            self.assertTrue(any(item["relative_path"] == "processed_messages.sqlite" for item in report["retained"]))

    def test_cleanup_apply_deletes_only_known_disposable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            disposable = data_dir / "wechat_upload_smoke.jsonl"
            retained = data_dir / "logs.jsonl"
            unknown = data_dir / "keep_me.jsonl"
            disposable.write_text("smoke", encoding="utf-8")
            retained.write_text("log", encoding="utf-8")
            unknown.write_text("unknown", encoding="utf-8")

            report = build_artifact_cleanup_report(data_dir, apply=True)

            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["deleted_count"], 1)
            self.assertFalse(disposable.exists())
            self.assertTrue(retained.exists())
            self.assertTrue(unknown.exists())

    def test_cleanup_retains_send_audit_ledger_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            send_audit = data_dir / "send_audit.jsonl"
            send_audit.write_text("audit\n", encoding="utf-8")

            report = build_artifact_cleanup_report(data_dir, apply=False)

            self.assertTrue(send_audit.exists())
            self.assertTrue(any(item["relative_path"] == "send_audit.jsonl" for item in report["retained"]))

    def test_cleanup_prunes_browser_profile_and_native_diagnostics_but_keeps_latest_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            profile_cache = data_dir / "runtime" / "sidebar_browser_profile" / "Default" / "Cache" / "data_1"
            profile_cache.parent.mkdir(parents=True, exist_ok=True)
            profile_cache.write_bytes(b"cache")
            native_dir = data_dir / "native_diagnostics"
            native_dir.mkdir(parents=True, exist_ok=True)
            latest = native_dir / "native-migration-latest.json"
            old_snapshot = native_dir / "native-migration-20260709-120000.json"
            new_snapshot = native_dir / "native-migration-20260710-120000.json"
            old_disasm = native_dir / "sendfile-v1-old.disasm.txt"
            latest.write_text("latest", encoding="utf-8")
            old_snapshot.write_text("old", encoding="utf-8")
            new_snapshot.write_text("new", encoding="utf-8")
            old_disasm.write_text("diagnostic", encoding="utf-8")
            os.utime(old_snapshot, (1000, 1000))
            os.utime(new_snapshot, (2000, 2000))

            dry_run = build_artifact_cleanup_report(data_dir, apply=False)
            candidate_paths = {item["relative_path"] for item in dry_run["candidates"]}
            retained_paths = {item["relative_path"] for item in dry_run["retained"]}

            self.assertIn("runtime/sidebar_browser_profile", candidate_paths)
            self.assertIn("native_diagnostics/native-migration-20260709-120000.json", candidate_paths)
            self.assertIn("native_diagnostics/sendfile-v1-old.disasm.txt", candidate_paths)
            self.assertIn("native_diagnostics/native-migration-latest.json", retained_paths)
            self.assertIn("native_diagnostics/native-migration-20260710-120000.json", retained_paths)

            applied = build_artifact_cleanup_report(data_dir, apply=True)

            self.assertGreaterEqual(applied["deleted_count"], 3)
            self.assertFalse((data_dir / "runtime" / "sidebar_browser_profile").exists())
            self.assertFalse(old_snapshot.exists())
            self.assertFalse(old_disasm.exists())
            self.assertTrue(latest.exists())
            self.assertTrue(new_snapshot.exists())

    def test_plan_audit_reports_guarded_real_send_rollout_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            plan = root / "plan.md"
            plan.write_text("在发送模块实现前，需要人工复制候选回复到微信。", encoding="utf-8")

            report = build_plan_audit(data_dir, plan_path=plan)
            real_send_item = next(item for item in report["cleanup_order"] if item["item"] == "real-wechat-send")

            self.assertTrue(report["current_truth"]["send_enabled"])
            self.assertTrue(report["current_truth"]["real_send_implemented"])
            self.assertFalse(report["current_truth"]["wechat_read_only"])
            self.assertEqual(real_send_item["status"], "guarded_confirm_rollout_ready")
            self.assertIn("confirm-mode", real_send_item["action"])

    def test_storage_migration_status_marks_db_file_truth_and_preserved_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            (data_dir / "conversation_ledgers" / "Alice_abcd1234").mkdir(parents=True)
            (data_dir / "conversation_ledgers" / "Alice_abcd1234" / "messages.jsonl").write_text(
                "{}\n",
                encoding="utf-8",
            )
            (data_dir / "file_workspace" / "Alice_abcd1234" / "session_default" / "file1").mkdir(parents=True)
            (data_dir / "confirm_queue.sqlite").write_bytes(b"sqlite")
            (data_dir / "send_audit.sqlite").write_bytes(b"sqlite")
            bridge = data_dir / "send_bridge"
            bridge.mkdir(parents=True)
            (bridge / "outbox.jsonl").write_text("outbox\n", encoding="utf-8")
            (bridge / "acks.jsonl").write_text("acks\n", encoding="utf-8")

            report = build_storage_migration_status(data_dir, include_sizes=True)
            items = {item["component_id"]: item for item in report["items"]}

            self.assertEqual(report["schema"], "storage_migration_status_v1")
            self.assertEqual(items["confirm_queue"]["storage_state"], "database_backed")
            self.assertEqual(items["conversation_ledgers"]["storage_state"], "file_truth_not_migrated")
            self.assertEqual(items["conversation_ledgers"]["clear_history"], "reset")
            self.assertEqual(items["file_workspace"]["storage_state"], "file_truth_not_migrated")
            self.assertEqual(items["send_bridge"]["clear_history"], "preserve")
            self.assertTrue(items["send_bridge"]["exists"])
            self.assertIn("delivery_evidence_chain", {item["boundary"] for item in report["migration_boundaries"]})
            self.assertGreaterEqual(report["summary"]["database_backed_count"], 1)
            self.assertGreaterEqual(report["summary"]["file_truth_not_migrated_count"], 1)
            self.assertGreaterEqual(report["summary"]["preserved_component_count"], 1)


if __name__ == "__main__":
    unittest.main()
