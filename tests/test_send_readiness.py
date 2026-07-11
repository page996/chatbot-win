from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import create_default_config
from app.personal_wechat_bot.control.send_commands import set_send_controls
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report


ROOT = Path(__file__).resolve().parent


class SendReadinessTest(unittest.TestCase):
    def test_current_project_defaults_to_real_bridge_driver_but_send_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            report = build_send_readiness_report(data_dir)
            blockers = {item["id"] for item in report["checks"] if item["status"] == "blocker"}

            self.assertEqual(report["status"], "blocked")
            self.assertIn("send_enabled", blockers)
            self.assertIn("wechat_write_access", blockers)
            self.assertNotIn("real_send_driver", blockers)
            self.assertNotIn("send_driver_name", blockers)
            self.assertFalse(report["send_policy"]["send_enabled"])

    def test_send_readiness_cli_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            completed = subprocess.run(
                [sys.executable, "-m", "app.personal_wechat_bot.main", "--data-dir", str(data_dir), "init"],
                cwd=ROOT.parent,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            self.assertIn("initialized", completed.stdout)

            output = subprocess.run(
                [sys.executable, "-m", "app.personal_wechat_bot.main", "--data-dir", str(data_dir), "send-readiness"],
                cwd=ROOT.parent,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
            )
            payload = json.loads(output.stdout)

            self.assertEqual(payload["status"], "blocked")
            self.assertFalse(payload["send_policy"]["send_enabled"])

    def test_bridge_outbox_driver_removes_send_driver_blockers_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")

            report = build_send_readiness_report(data_dir)
            blockers = {item["id"] for item in report["checks"] if item["status"] == "blocker"}

            self.assertNotIn("real_send_driver", blockers)
            self.assertNotIn("send_enabled", blockers)
            self.assertNotIn("wechat_write_access", blockers)
            self.assertNotIn("send_driver_name", blockers)
            # bridge_outbox no longer requires a manual foreground binding; it delivers by wxid/roomid.
            self.assertNotIn("manual_bridge_channels", blockers)
            self.assertIn("keep confirm mode active", " ".join(report["recommended_rollout"]))
            rollout = next(item for item in report["checks"] if item["id"] == "rollout_mode")
            self.assertEqual(rollout["status"], "warn")
            self.assertIn("confirm mode is active", rollout["detail"])
            # The bridge worker must be started to deliver queued replies; this
            # required next-step must actually appear for the bridge_outbox driver
            # (previously it was keyed on a blocker id no check ever emitted).
            self.assertIn(
                "start the send bridge worker",
                " ".join(report["required_next_steps"]),
            )

    def test_bridge_outbox_does_not_require_worker_start_when_lock_is_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "label": "send_bridge_worker",
                        "acquired_at": time.time(),
                        "heartbeat_at": time.time(),
                    }
                ),
                encoding="utf-8",
            )

            report = build_send_readiness_report(data_dir)

            self.assertNotIn(
                "start the send bridge worker",
                " ".join(report["required_next_steps"]),
            )

    def test_bridge_outbox_requires_worker_start_when_lock_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox")
            lock_path = data_dir / "send_bridge" / ".bridge_worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            old = time.time() - 120.0
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 12345,
                        "label": "send_bridge_worker",
                        "acquired_at": old,
                        "heartbeat_at": old,
                    }
                ),
                encoding="utf-8",
            )

            report = build_send_readiness_report(data_dir)

            self.assertIn(
                "start the send bridge worker",
                " ".join(report["required_next_steps"]),
            )

    def test_dry_run_backend_warns_when_real_send_intended(self) -> None:
        # send_enabled + bridge_outbox + dry_run backend means the
        # worker acks 'sent' without delivering. The send_backend check must warn
        # so the operator doesn't mistake dry-run acks for real delivery.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="dry_run")

            report = build_send_readiness_report(data_dir)
            by_id = {item["id"]: item for item in report["checks"]}

            self.assertEqual(by_id["send_backend"]["status"], "warn")
            self.assertIn("not a real delivery backend", by_id["send_backend"]["detail"])

    def test_weflow_http_backend_passes_when_bridge_is_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.weflow_http_status",
                return_value={"available": True, "token_present": True, "reason": ""},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["send_backend"]["status"], "pass")
            self.assertEqual(by_id["weflow_http_send_bridge"]["status"], "pass")

    def test_weflow_http_backend_blocks_when_token_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="weflow_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.weflow_http_status",
                return_value={"available": False, "token_present": False, "reason": ""},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["weflow_http_send_bridge"]["status"], "blocker")
            self.assertEqual(report["status"], "blocked")
            self.assertIn("WeFlow HTTP service", " ".join(report["required_next_steps"]))

    def test_wechat_native_http_backend_passes_when_native_bridge_is_logged_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.wechat_native_http_status",
                return_value={"available": True, "reason": "", "health": {"IsLogin": 1}},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["send_backend"]["status"], "pass")
            self.assertEqual(by_id["wechat_native_http_send_bridge"]["status"], "pass")

    def test_wechat_native_http_warns_for_unverified_default_file_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.wechat_native_http_status",
                return_value={
                    "available": True,
                    "reason": "",
                    "health": {"IsLogin": 1},
                    "send_capabilities": {
                        "file": {"status": "default_route_accepts_unverified_native_file"},
                    },
                },
            ), mock.patch(
                "app.personal_wechat_bot.control.send_readiness.weflow_http_status",
                return_value={"available": False, "token_present": False},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["wechat_native_http_send_bridge"]["status"], "pass")
            self.assertEqual(by_id["wechat_native_file_delivery_verification"]["status"], "warn")

    def test_wechat_native_http_passes_file_verification_when_readback_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.wechat_native_http_status",
                return_value={
                    "available": True,
                    "reason": "",
                    "health": {"IsLogin": 1},
                    "send_capabilities": {
                        "file": {"status": "default_route_accepts_unverified_native_file"},
                    },
                },
            ), mock.patch(
                "app.personal_wechat_bot.control.send_readiness.weflow_http_status",
                return_value={"available": True, "token_present": True},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["wechat_native_file_delivery_verification"]["status"], "pass")

    def test_wechat_native_http_backend_blocks_when_native_bridge_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="bridge_outbox", backend="wechat_native_http")

            with mock.patch(
                "app.personal_wechat_bot.control.send_readiness.wechat_native_http_status",
                return_value={"available": False, "reason": "wechat_native_not_login", "health": {"IsLogin": 0}},
            ):
                report = build_send_readiness_report(data_dir)

            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["wechat_native_http_send_bridge"]["status"], "blocker")
            self.assertEqual(report["status"], "blocked")
            self.assertIn("PC WeChat Native HTTP service", " ".join(report["required_next_steps"]))

    def test_unregistered_driver_name_blocks_consistently_with_real_send_check(self) -> None:
        # Regression: send_driver_name must key off the driver registry, not a
        # weak "!= not_implemented" test, so an unregistered driver name blocks
        # consistently with the real_send_driver check (no contradiction).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, mode="confirm", enabled=True, driver="some_unregistered_driver")

            report = build_send_readiness_report(data_dir)
            by_id = {item["id"]: item for item in report["checks"]}

            self.assertEqual(by_id["send_driver_name"]["status"], "blocker")
            self.assertEqual(by_id["real_send_driver"]["status"], "blocker")

    def test_passive_readiness_never_probes_send_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(
                data_dir,
                mode="confirm",
                enabled=True,
                driver="bridge_outbox",
                backend="wechat_native_http",
            )

            with (
                mock.patch(
                    "app.personal_wechat_bot.control.send_readiness.wechat_native_http_status",
                    side_effect=AssertionError("passive readiness must stay local"),
                ) as native_status,
                mock.patch(
                    "app.personal_wechat_bot.control.send_readiness.weflow_http_status",
                    side_effect=AssertionError("passive readiness must stay local"),
                ) as weflow_status,
            ):
                report = build_send_readiness_report(data_dir, active_backend_probe=False)

            native_status.assert_not_called()
            weflow_status.assert_not_called()
            self.assertFalse(report["active_backend_probe"])
            by_id = {item["id"]: item for item in report["checks"]}
            self.assertEqual(by_id["wechat_native_http_send_bridge"]["status"], "warn")


if __name__ == "__main__":
    unittest.main()
