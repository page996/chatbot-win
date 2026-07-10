from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.config.loader import accept_contact, create_default_config, load_config
from app.personal_wechat_bot.domain.models import RawWeChatMessage, SendResult
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.wechat_driver.fake import FakeWeChatDriver


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "messages"


class PollingRunnerTest(unittest.TestCase):
    def test_run_once_processes_driver_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_xiaoming")
            runtime = build_runtime(load_config(data_dir))
            driver = FakeWeChatDriver(FIXTURES / "private_basic.json")

            result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_once()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["processed"]), 1)
            self.assertEqual(result["processed"][0]["route"]["action"], "process")
            self.assertEqual(result["max_running_seen"], 1)
            self.assertEqual(result["resource_schedule"]["workload"], "interactive")

    def test_run_forever_stops_after_max_loops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_xiaoming")
            runtime = build_runtime(load_config(data_dir))
            driver = FakeWeChatDriver(FIXTURES / "private_basic.json")

            result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_forever(max_loops=2)

            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["loops"], 2)
            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(len(result["processed"]), 1)

    def test_run_once_uses_scheduler_for_multiple_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            accept_contact(data_dir, "wxid_a")
            accept_contact(data_dir, "wxid_b")
            runtime = build_runtime(load_config(data_dir))
            driver = _ListDriver(
                [
                    RawWeChatMessage(
                        raw_id="a1",
                        chat_title="A",
                        sender_name="A",
                        sender_wechat_id="wxid_a",
                        text="hello",
                    ),
                    RawWeChatMessage(
                        raw_id="b1",
                        chat_title="B",
                        sender_name="B",
                        sender_wechat_id="wxid_b",
                        text="hello",
                    ),
                ]
            )

            result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_once()

            self.assertEqual(len(result["processed"]), 2)
            self.assertLessEqual(result["max_running_seen"], 2)
            self.assertEqual(result["resource_schedule"]["workload"], "interactive")
            self.assertEqual(result["resource_schedule"]["max_parallel_conversations"], 4)

    def test_context_only_batch_uses_background_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            driver = _ListDriver(
                [
                    RawWeChatMessage(
                        raw_id=f"history-{index}",
                        chat_title=f"C{index}",
                        sender_name=f"C{index}",
                        text="history",
                        driver_meta={"context_only": True},
                    )
                    for index in range(5)
                ]
            )

            result = PollingRunner(runtime, driver, poll_interval_seconds=0).run_once()

            self.assertEqual(result["resource_schedule"]["workload"], "background")
            self.assertEqual(result["resource_schedule"]["max_parallel_conversations"], 2)
            self.assertLessEqual(result["max_running_seen"], 2)

    def test_explicit_background_workload_uses_background_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            runtime = build_runtime(load_config(data_dir))
            driver = _ListDriver(
                [
                    RawWeChatMessage(
                        raw_id=f"bg-{index}",
                        chat_title=f"C{index}",
                        sender_name=f"C{index}",
                        text="background",
                    )
                    for index in range(5)
                ]
            )

            result = PollingRunner(runtime, driver, poll_interval_seconds=0, workload="background").run_once()

            self.assertEqual(result["resource_schedule"]["workload"], "background")
            self.assertEqual(result["resource_schedule"]["max_parallel_conversations"], 2)
            self.assertLessEqual(result["max_running_seen"], 2)


class _ListDriver:
    def __init__(self, messages: list[RawWeChatMessage]):
        self.messages = messages
        self.read = False

    def health_check(self) -> bool:
        return True

    def read_new_messages(self) -> list[RawWeChatMessage]:
        if self.read:
            return []
        self.read = True
        return self.messages

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        return SendResult(message_id="unused", conversation_id=conversation_id, status="failed", reason="unused")


if __name__ == "__main__":
    unittest.main()
