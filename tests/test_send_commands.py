from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    send_approved_confirm_item,
    set_send_controls,
)
from app.personal_wechat_bot.domain.models import ReplyCandidate, SendResult
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_state


ROOT = Path(__file__).resolve().parent


class SendCommandsTest(unittest.TestCase):
    def test_set_send_controls_persists_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            payload = set_send_controls(
                data_dir,
                enabled=True,
                driver="fake",
                confirm_required=False,
                max_chars=64,
                min_interval_seconds=2,
            )
            config = load_config(data_dir)

            self.assertTrue(payload["send_enabled"])
            self.assertEqual(payload["send_driver"], "fake")
            self.assertFalse(config.send_confirm_required)
            self.assertEqual(config.send_max_chars, 64)
            self.assertEqual(config.send_min_interval_seconds, 2)

    def test_confirm_helpers_approve_reject_and_send_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            first_id = queue.enqueue(_reply("message-1", "hello"))
            second_id = queue.enqueue(_reply("message-2", "bye"))

            pending = list_confirm_queue(data_dir)
            approved = approve_confirm_item(data_dir, first_id, reviewer="tester", note="ok")
            rejected = reject_confirm_item(data_dir, second_id, reviewer="tester", note="no")
            send_result = send_approved_confirm_item(data_dir, first_id)

            self.assertEqual(pending["count"], 2)
            self.assertEqual(approved["item"]["status"], "approved")
            self.assertEqual(rejected["item"]["status"], "rejected")
            self.assertEqual(send_result["status"], "failed")
            self.assertEqual(send_result["send_result"]["reason"], "send_enabled_false")
            self.assertEqual(queue.get(first_id)["status"], "failed")
            audit = list_send_audit(data_dir, limit=10)
            actions = [item["action"] for item in audit["items"]]
            self.assertIn("confirm_approve", actions)
            self.assertIn("confirm_reject", actions)
            self.assertIn("confirm_send_attempt", actions)

    def test_send_approved_confirm_item_calls_driver_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="fake")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            queue.approve(queue_id, reviewer="tester")
            driver = _SendingDriver()

            result = send_approved_confirm_item(data_dir, queue_id, driver=driver)

            self.assertEqual(result["status"], "sent")
            self.assertEqual(driver.sent_texts, ["hello"])
            self.assertEqual(queue.get(queue_id)["status"], "sent")

    def test_send_approved_confirm_item_blocks_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))

            result = send_approved_confirm_item(data_dir, queue_id, driver=_SendingDriver())

            self.assertEqual(result["status"], "blocked")
            self.assertIn("pending", result["reason"])
            self.assertEqual(queue.get(queue_id)["status"], "pending")

    def test_send_approved_confirm_item_uses_factory_and_blocks_unknown_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="unknown-real-driver")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello"))
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["send_result"]["reason"], "send_driver_missing")
            self.assertEqual(queue.get(queue_id)["status"], "failed")

    def test_send_approved_confirm_item_can_queue_to_bridge_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(_reply("message-1", "hello bridge"))
            queue.approve(queue_id, reviewer="tester")

            result = send_approved_confirm_item(data_dir, queue_id)
            bridge = bridge_state(data_dir, limit=10)

            self.assertEqual(result["status"], "queued_to_bridge")
            self.assertEqual(result["send_result"]["status"], "queued_to_bridge")
            self.assertEqual(queue.get(queue_id)["status"], "queued_to_bridge")
            self.assertEqual(bridge["pending_count"], 1)
            self.assertEqual(bridge["items"][0]["text"], "hello bridge")

    def test_probe_send_controls_reports_configured_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=False, driver="windows_guarded")

            result = probe_send_controls(data_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["probe"]["normalized_driver"], "windows_guarded")
            self.assertTrue(result["probe"]["registered"])
            self.assertTrue(result["probe"]["real_send_implemented"])

    def test_probe_send_controls_can_override_driver_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)

            result = probe_send_controls(data_dir, driver="windows_guarded")
            config = load_config(data_dir)

            self.assertEqual(result["probe"]["normalized_driver"], "windows_guarded")
            self.assertEqual(config.send_driver, "not_implemented")

    def test_set_send_controls_cli_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "set-send-controls",
                    "--enable",
                    "--driver",
                    "fake",
                    "--confirm-required",
                    "false",
                    "--max-chars",
                    "64",
                    "--min-interval-seconds",
                    "2",
                )
            )
            config = load_config(data_dir)

            self.assertEqual(payload["status"], "ok")
            self.assertTrue(config.send_enabled)
            self.assertEqual(config.send_driver, "fake")
            self.assertFalse(config.send_confirm_required)
            self.assertEqual(config.send_max_chars, 64)
            self.assertEqual(config.send_min_interval_seconds, 2)

    def test_confirm_cli_approve_reject_and_send_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            first_id = queue.enqueue(_reply("message-1", "hello"))
            second_id = queue.enqueue(_reply("message-2", "bye"))

            pending = json.loads(self._run("--data-dir", str(data_dir), "confirm-list"))
            approved = json.loads(
                self._run("--data-dir", str(data_dir), "confirm-approve", first_id, "--reviewer", "tester")
            )
            rejected = json.loads(
                self._run("--data-dir", str(data_dir), "confirm-reject", second_id, "--reviewer", "tester")
            )
            send_result = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "confirm-send-approved",
                    first_id,
                    "--delay-seconds",
                    "0",
                )
            )
            failed = json.loads(self._run("--data-dir", str(data_dir), "confirm-list", "--status", "failed"))

            self.assertEqual(pending["count"], 2)
            self.assertEqual(approved["item"]["status"], "approved")
            self.assertEqual(rejected["item"]["status"], "rejected")
            self.assertEqual(send_result["status"], "failed")
            self.assertEqual(send_result["send_result"]["reason"], "send_enabled_false")
            self.assertEqual(failed["count"], 1)
            audit = json.loads(self._run("--data-dir", str(data_dir), "send-audit", "--limit", "5"))
            self.assertGreaterEqual(audit["count"], 3)

    def test_send_driver_probe_cli_returns_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")
            self._run("--data-dir", str(data_dir), "set-send-controls", "--driver", "windows_guarded")

            payload = json.loads(
                self._run("--data-dir", str(data_dir), "send-driver-probe", "--delay-seconds", "0")
            )

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["probe"]["normalized_driver"], "windows_guarded")
            self.assertTrue(payload["probe"]["registered"])

    def test_send_driver_probe_cli_can_override_driver_without_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            payload = json.loads(
                self._run("--data-dir", str(data_dir), "send-driver-probe", "--driver", "windows_guarded")
            )
            config = load_config(data_dir)

            self.assertEqual(payload["probe"]["normalized_driver"], "windows_guarded")
            self.assertEqual(config.send_driver, "not_implemented")

    def test_send_bridge_cli_returns_state_and_accepts_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            self._run("--data-dir", str(data_dir), "init")

            state = json.loads(self._run("--data-dir", str(data_dir), "send-bridge-state"))
            ack = json.loads(
                self._run(
                    "--data-dir",
                    str(data_dir),
                    "send-bridge-ack",
                    "bridge:test",
                    "--status",
                    "sent",
                    "--reason",
                    "manual",
                )
            )
            updated = json.loads(self._run("--data-dir", str(data_dir), "send-bridge-state"))

            self.assertEqual(state["status"], "ok")
            self.assertEqual(ack["status"], "ok")
            self.assertEqual(updated["ack_count"], 1)

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", *args],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )
        return completed.stdout


def _reply(message_id: str, text: str) -> ReplyCandidate:
    return ReplyCandidate(
        message_id=message_id,
        conversation_id="private-1",
        text=text,
        send_mode="confirm",
        model="fake",
    )


class _SendingDriver:
    def __init__(self) -> None:
        self.sent_texts: list[str] = []

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        self.sent_texts.append(text)
        return SendResult(message_id="sent-id", conversation_id=conversation_id, status="sent", reason="fake_sent")


if __name__ == "__main__":
    unittest.main()
