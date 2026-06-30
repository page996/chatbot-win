from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import (
    accept_contact,
    create_default_config,
    load_config,
    set_deepseek_provider,
)
from app.personal_wechat_bot.control.preflight import build_preflight_report


class PreflightTest(unittest.TestCase):
    def test_preflight_reports_safe_send_policy_and_channel_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)

            report = build_preflight_report(config)

            self.assertEqual(report["mode"], "dry_run")
            self.assertFalse(report["send_policy"]["send_enabled"])
            self.assertFalse(report["send_policy"]["real_send_implemented"])
            self.assertTrue(report["wechat_access"]["read_only"])
            self.assertEqual(report["accepted_conversations"]["mode"], "channel_auto_accept")
            self.assertEqual(report["legacy_whitelist"]["mode"], "compatibility_alias_not_used_for_routing")
            self.assertEqual(
                report["conversation_channels"]["policy"],
                "auto_accept_wechat_contacts_and_groups",
            )
            self.assertNotIn("poll-ocr-window", report["wechat_access"]["fallback_inputs"])
            self.assertNotIn("poll-ocr-window", report["wechat_access"]["debug_inputs"])
            self.assertIn("wechat-capture", report["wechat_access"]["debug_inputs"])
            self.assertIn("poll-ocr-window", report["wechat_access"]["deprecated_inputs"])
            self.assertEqual(report["wechat_access"]["page_ocr_ingestion"], "disabled")
            self.assertEqual(report["tools"]["ocr"]["name"], "vision.ocr")
            self.assertEqual(report["tools"]["ocr"]["scope"], "tool_layer_file_workspace_only")
            self.assertTrue(report["conversation_channels"]["auto_register_private"])
            self.assertTrue(report["conversation_channels"]["auto_register_groups"])

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
            legacy_shown = build_preflight_report(config, show_whitelist=True)

            self.assertIsNone(hidden["accepted_conversations"]["contacts"])
            self.assertEqual(shown["accepted_conversations"]["contacts"], ["wxid_xiaoming"])
            self.assertEqual(legacy_shown["accepted_conversations"]["contacts"], ["wxid_xiaoming"])
            self.assertEqual(shown["legacy_whitelist"]["contacts"], ["wxid_xiaoming"])
            self.assertIn("api_key_env", shown["model"])
            self.assertIn("api_key_present", shown["model"])

    def test_legacy_whitelist_files_are_migrated_to_accepted_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            (data_dir / "accepted_contacts.json").unlink()
            (data_dir / "contacts_whitelist.json").write_text(json.dumps(["wxid_legacy"]), encoding="utf-8")

            config = load_config(data_dir)

            self.assertEqual(config.accepted_contacts, {"wxid_legacy"})
            self.assertEqual(config.contacts_whitelist, {"wxid_legacy"})

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

    def test_preflight_reports_write_access_when_guarded_driver_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            config = load_config(data_dir)
            config.mode = "confirm"
            config.send_enabled = True
            config.send_driver = "windows_guarded"

            report = build_preflight_report(config)

            self.assertTrue(report["send_policy"]["real_send_implemented"])
            self.assertFalse(report["wechat_access"]["read_only"])
            self.assertTrue(report["wechat_access"]["write_access_configured"])


if __name__ == "__main__":
    unittest.main()
