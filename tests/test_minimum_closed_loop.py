from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from app.personal_wechat_bot.config.loader import add_contact, add_group, create_default_config, load_config
from app.personal_wechat_bot.domain.models import ToolCallResult
from app.personal_wechat_bot.replay.runner import ReplayRunner


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "messages"


class MinimumClosedLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        create_default_config(self.data_dir)
        for wechat_id in [
            "wxid_xiaoming",
            "wxid_xiaogang",
            "wxid_symbols",
            "wxid_sakura",
            "wxid_minjun",
            "wxid_alice",
        ]:
            add_contact(self.data_dir, wechat_id)
        for group_name in ["学习群", "群+！@#￥%……&*", "研究会さくら", "스터디", "Study Group"]:
            add_group(self.data_dir, group_name)
        self.config = load_config(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def replay(self, name: str) -> dict:
        return ReplayRunner(self.config).run(FIXTURES / name)

    def test_private_replay_generates_dry_run_reply(self) -> None:
        result = self.replay("private_basic.json")
        item = result["processed"][0]
        self.assertEqual(item["route"]["action"], "process")
        self.assertEqual(item["speak"]["decision"], "speak")
        self.assertNotIn("SUMMARY:", item["reply"]["text"])
        self.assertIn("收到", item["reply"]["text"])
        self.assertEqual(item["send"]["status"], "skipped")

    def test_replay_restart_marks_same_message_as_duplicate(self) -> None:
        first = ReplayRunner(self.config).run(FIXTURES / "private_basic.json")
        second = ReplayRunner(self.config).run(FIXTURES / "private_basic.json")

        self.assertEqual(first["processed"][0]["route"]["action"], "process")
        self.assertEqual(second["processed"][0]["route"]["action"], "duplicate")
        self.assertNotIn("reply", second["processed"][0])

    def test_group_topic_miss_stays_silent(self) -> None:
        result = self.replay("group_topic_miss.json")
        item = result["processed"][0]
        self.assertEqual(item["route"]["action"], "process")
        self.assertEqual(item["speak"]["decision"], "silent")
        self.assertNotIn("reply", item)

    def test_group_topic_match_generates_reply(self) -> None:
        result = self.replay("group_topic_match.json")
        item = result["processed"][0]
        self.assertEqual(item["speak"]["decision"], "speak")
        self.assertEqual(item["send"]["reason"], "dry_run")

    def test_group_cooldown_waits_on_second_group_reply(self) -> None:
        first = self.replay("group_topic_match.json")
        second = self.replay("group_topic_match_second.json")

        self.assertEqual(first["processed"][0]["speak"]["decision"], "speak")
        self.assertEqual(second["processed"][0]["speak"]["decision"], "wait")
        self.assertIn("group_cooldown", second["processed"][0]["speak"]["reason"])
        self.assertNotIn("reply", second["processed"][0])

    def test_special_names_and_groups_are_supported(self) -> None:
        private_result = self.replay("private_special_names.json")
        group_result = self.replay("group_special_names.json")
        self.assertEqual(len(private_result["processed"]), 4)
        self.assertTrue(all(item["route"]["action"] == "process" for item in private_result["processed"]))
        self.assertEqual(len(group_result["processed"]), 4)
        self.assertTrue(all(item["route"]["action"] == "process" for item in group_result["processed"]))

    def test_document_tool_returns_docx_reference_and_indexes_file(self) -> None:
        result = self.replay("tool_document_translate.json")
        tool = result["processed"][0]["reply"]["tool_result"]
        self.assertEqual(tool["status"], "completed")
        self.assertTrue(tool["output_refs"][0].endswith("文本翻译.docx"))
        self.assertTrue((self.data_dir / "file_index.sqlite").exists())
        tasks = list((self.data_dir / "agent_workspace" / "tasks").iterdir())
        self.assertEqual(len(tasks), 1)
        status = json.loads((tasks[0] / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "completed")

    def test_external_search_returns_url_summary_and_source_reference(self) -> None:
        output = self.data_dir / "tool_outputs" / "web_search" / "minimum-closed-loop.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("https://arxiv.org/abs/1234.5678\nResearch evidence.", encoding="utf-8")
        tool_result = ToolCallResult(
            call_id="minimum-search",
            tool_name="web.search",
            status="completed",
            summary="web.search usable result: https://arxiv.org/abs/1234.5678",
            output_refs=[str(output)],
            payload={"result_count": 1, "filtered": [{"title": "Unrelated shopping"}]},
        )
        with mock.patch(
            "app.personal_wechat_bot.agent.tool_orchestrator.ToolTaskOrchestrator.execute",
            return_value=tool_result,
        ):
            result = self.replay("tool_external_search.json")
        tool = result["processed"][0]["reply"]["tool_result"]
        self.assertEqual(tool["status"], "completed")
        self.assertIn("https://arxiv.org/abs/1234.5678", tool["summary"])
        self.assertNotIn("Unrelated shopping", tool["summary"])
        self.assertTrue(tool["output_refs"][0].endswith(".md"))


if __name__ == "__main__":
    unittest.main()
