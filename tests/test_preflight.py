from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.personal_wechat_bot.config.loader import (
    accept_contact,
    create_default_config,
    load_config,
    set_deepseek_provider,
    update_config,
)
from app.personal_wechat_bot.control.preflight import build_preflight_report


class PreflightTest(unittest.TestCase):
    def test_concurrent_config_updates_do_not_overwrite_unrelated_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            def set_mode(config) -> None:
                config.mode = "confirm"
                time.sleep(0.05)

            def set_model(config) -> None:
                config.providers["chat"].model = "concurrency-test-model"
                time.sleep(0.05)

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda updater: update_config(data_dir, updater), [set_mode, set_model]))

            config = load_config(data_dir)
            self.assertEqual(config.mode, "confirm")
            self.assertEqual(config.providers["chat"].model, "concurrency-test-model")

    def test_preflight_reports_safe_send_policy_and_channel_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)

            report = build_preflight_report(config)

            self.assertEqual(report["mode"], "dry_run")
            self.assertFalse(report["send_policy"]["send_enabled"])
            self.assertTrue(report["send_policy"]["real_send_implemented"])
            self.assertTrue(report["wechat_access"]["read_only"])
            self.assertEqual(report["accepted_conversations"]["mode"], "channel_admission_guarded")
            self.assertEqual(
                report["conversation_channels"]["policy"],
                "verified_or_identified_wechat_channels",
            )
            self.assertNotIn("poll-clipboard", report["wechat_access"]["fallback_inputs"])
            self.assertNotIn("poll-clipboard", report["wechat_access"]["available_inputs"])
            self.assertIn("poll-clipboard", report["wechat_access"]["removed_inputs"])
            self.assertIn("wechat-capture", report["wechat_access"]["debug_inputs"])
            self.assertEqual(report["wechat_access"]["primary_inputs"], ["poll-backend-events"])
            self.assertEqual(report["wechat_access"]["context_only_inputs"], ["poll-snapshot"])
            self.assertEqual(report["wechat_access"]["page_ocr_ingestion"], "disabled")
            self.assertEqual(report["tools"]["ocr"]["name"], "vision.ocr")
            self.assertEqual(report["tools"]["ocr"]["scope"], "tool_layer_file_workspace_only")
            self.assertEqual(report["tools"]["web_search"]["name"], "web.search")
            self.assertFalse(report["tools"]["web_search"]["uses_browser"])
            self.assertIn("deep", report["tools"]["web_search"]["levels"])
            self.assertEqual(report["conversation_channels"]["auto_register_private"], "identified_or_accepted_only")
            self.assertTrue(report["conversation_channels"]["auto_register_groups"])
            self.assertTrue(report["conversation_channels"]["blocks_unknown_private"])
            storage = report["runtime_guards"]["state_storage_policy"]
            self.assertEqual(storage["conversation_ledger"], "sqlite_authority_jsonl_markdown_projection")
            self.assertEqual(storage["send_audit"], "sqlite_authority_jsonl_forensic_projection")

    def test_preflight_warns_when_provider_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_deepseek_provider(data_dir, api_key_env="MISSING_DEEPSEEK_PREFLIGHT_TEST")
            config = load_config(data_dir)

            report = build_preflight_report(config)

            self.assertEqual(report["status"], "warn")
            self.assertIn("MISSING_DEEPSEEK_PREFLIGHT_TEST", " ".join(report["warnings"]))

    def test_preflight_can_show_accepted_channels_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_xiaoming")
            config = load_config(data_dir)

            hidden = build_preflight_report(config, show_accepted=False)
            shown = build_preflight_report(config, show_accepted=True)

            self.assertIsNone(hidden["accepted_conversations"]["contacts"])
            self.assertEqual(shown["accepted_conversations"]["contacts"], ["wxid_xiaoming"])
            self.assertIn("api_key_env", shown["model"])
            self.assertIn("api_key_present", shown["model"])

    def test_config_uses_only_accepted_channel_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_current")

            config = load_config(data_dir)
            raw = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))

            self.assertEqual(config.accepted_contacts, {"wxid_current"})
            self.assertFalse((data_dir / "contacts_whitelist.json").exists())
            self.assertFalse((data_dir / "groups_whitelist.json").exists())
            self.assertNotIn("contacts_whitelist", raw)
            self.assertNotIn("groups_whitelist", raw)
            self.assertNotIn("accepted_contacts", raw)
            self.assertNotIn("accepted_groups", raw)
            self.assertNotIn("llm", raw)
            self.assertIn("chat", raw["providers"])

    def test_preflight_detects_present_api_key_without_exposing_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_deepseek_provider(data_dir, api_key_env="DEEPSEEK_PREFLIGHT_TEST")
            config = load_config(data_dir)
            old_value = os.environ.get("DEEPSEEK_PREFLIGHT_TEST")
            os.environ["DEEPSEEK_PREFLIGHT_TEST"] = "secret-value"
            try:
                report = build_preflight_report(config)
            finally:
                if old_value is None:
                    os.environ.pop("DEEPSEEK_PREFLIGHT_TEST", None)
                else:
                    os.environ["DEEPSEEK_PREFLIGHT_TEST"] = old_value

            self.assertTrue(report["model"]["api_key_present"])
            self.assertNotIn("secret-value", str(report))

    def test_preflight_reports_key_pool_refs_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config_path = data_dir / "config.json"

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["providers"]["chat"]["api_key_env_pool"] = ["POOL_KEY_A", "POOL_KEY_B"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            old_value = os.environ.get("POOL_KEY_B")
            os.environ["POOL_KEY_B"] = "secret-pool-value"
            try:
                report = build_preflight_report(load_config(data_dir))
            finally:
                if old_value is None:
                    os.environ.pop("POOL_KEY_B", None)
                else:
                    os.environ["POOL_KEY_B"] = old_value

            self.assertEqual(report["model"]["api_key_env_pool_count"], 2)
            self.assertTrue(any(item["ref"] == "POOL_KEY_B" and item["available"] for item in report["model"]["key_pool_refs"]))
            self.assertNotIn("secret-pool-value", str(report))

    def test_preflight_does_not_treat_unknown_send_driver_as_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.send_enabled = True
            config.send_driver = "unknown-real-driver"

            report = build_preflight_report(config)

            self.assertFalse(report["send_policy"]["real_send_implemented"])
            self.assertIn("send_enabled is true but send_driver is not implemented", report["warnings"])

    def test_preflight_reports_write_access_when_send_driver_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.mode = "confirm"
            config.send_enabled = True
            config.send_driver = "bridge_outbox"

            report = build_preflight_report(config)

            self.assertTrue(report["send_policy"]["real_send_implemented"])
            self.assertFalse(report["wechat_access"]["read_only"])
            self.assertTrue(report["wechat_access"]["write_access_configured"])

    def test_preflight_bridge_outbox_uses_channel_registry_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.mode = "confirm"
            config.send_enabled = True
            config.send_driver = "bridge_outbox"

            report = build_preflight_report(config)

            self.assertNotIn("manual_bound_channels", report["wechat_access"])


if __name__ == "__main__":
    unittest.main()
