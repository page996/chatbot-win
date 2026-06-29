from __future__ import annotations

import unittest

from app.personal_wechat_bot.runtime.agent_runner import AgentRunner


class AgentRunnerTest(unittest.TestCase):
    def test_agent_runner_combines_multiple_input_sources(self) -> None:
        runner = AgentRunner(
            [
                ("a", _Runner({"status": "ok", "processed": [1, 2]})),
                ("b", _Runner({"status": "unchanged", "processed_count": 0})),
            ],
            poll_interval_seconds=0,
        )

        result = runner.run_forever(max_loops=1)

        self.assertEqual(result["processed_count"], 2)
        self.assertEqual(result["runners"][0]["name"], "a")
        self.assertEqual(result["runners"][0]["processed_count"], 2)

    def test_agent_runner_keeps_running_when_one_source_errors(self) -> None:
        runner = AgentRunner(
            [
                ("bad", _FailingRunner()),
                ("good", _Runner({"status": "ok", "processed_count": 1})),
            ],
            poll_interval_seconds=0,
        )

        result = runner.run_forever(max_loops=1)

        self.assertEqual(result["processed_count"], 1)
        self.assertEqual(result["runners"][0]["status"], "error")
        self.assertEqual(result["runners"][1]["status"], "ok")


class _Runner:
    def __init__(self, result):
        self.result = result

    def run_once(self):
        return self.result


class _FailingRunner:
    def run_once(self):
        raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
