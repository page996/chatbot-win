from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.snapshot_provider import (
    AutomationTextNode,
    FileSnapshotProvider,
    StaticSnapshotProvider,
    WindowsUIAutomationSnapshotProvider,
    format_automation_text_nodes,
)


class _Collector:
    def collect_text_nodes(self, max_nodes: int, max_depth: int) -> list[AutomationTextNode]:
        return [
            AutomationTextNode("微信", "Window", 0),
            AutomationTextNode("小明", "Text", 1),
        ]


class SnapshotProviderTest(unittest.TestCase):
    def test_file_snapshot_provider_reads_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.txt"
            path.write_text("[private] 小明 | 小明 | wxid_xiaoming | 你好", encoding="utf-8")

            self.assertIn("小明", FileSnapshotProvider(path).read_text())

    def test_static_snapshot_provider_returns_text(self) -> None:
        self.assertEqual(StaticSnapshotProvider("hello").read_text(), "hello")

    def test_format_automation_text_nodes_dedupes_and_normalizes_whitespace(self) -> None:
        text = format_automation_text_nodes(
            [
                AutomationTextNode("  微信  ", "Window", 0),
                AutomationTextNode("小明   你好", "Text", 1),
                AutomationTextNode("小明 你好", "Text", 2),
                AutomationTextNode("", "Text", 0),
            ]
        )

        self.assertEqual(text, "微信 [Window]\n  小明 你好 [Text]")

    def test_uia_snapshot_provider_uses_injected_collector(self) -> None:
        provider = WindowsUIAutomationSnapshotProvider(collector=_Collector())

        text = provider.read_text()

        self.assertIn("微信", text)
        self.assertIn("小明", text)


if __name__ == "__main__":
    unittest.main()
