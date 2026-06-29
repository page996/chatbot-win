from __future__ import annotations

import unittest

from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    SnapshotMessageParser,
    WindowsWeChatReadOnlyDriver,
    Win32WindowProbe,
    WindowInfo,
    find_wechat_processes,
    foreground_window_info,
)
from app.personal_wechat_bot.wechat_driver.snapshot_provider import StaticSnapshotProvider


class SnapshotMessageParserTest(unittest.TestCase):
    def test_parser_extracts_private_and_group_messages(self) -> None:
        snapshot = "\n".join(
            [
                "[private] 小明 | 小明 | wxid_xiaoming | 今天复习得有点累",
                "[group] 学习群 | 小刚 | wxid_xiaogang | 我们继续聊 AI",
                "unrelated line",
            ]
        )

        messages = SnapshotMessageParser().parse(snapshot, observed_at="2026-06-28T01:00:00+00:00")

        self.assertEqual(len(messages), 2)
        self.assertFalse(messages[0].is_group)
        self.assertEqual(messages[0].chat_title, "小明")
        self.assertEqual(messages[0].sender_wechat_id, "wxid_xiaoming")
        self.assertTrue(messages[1].is_group)
        self.assertEqual(messages[1].chat_title, "学习群")

    def test_parser_ignores_incomplete_lines(self) -> None:
        messages = SnapshotMessageParser().parse("[private] 小明 | 小明 | missing text")

        self.assertEqual(messages, [])


class WindowsWeChatReadOnlyDriverTest(unittest.TestCase):
    def test_driver_reads_snapshot_once_and_dedupes_raw_ids(self) -> None:
        snapshot = "[private] 小明 | 小明 | wxid_xiaoming | 今天复习得有点累"
        driver = WindowsWeChatReadOnlyDriver(text_provider=lambda: snapshot)

        first = driver.read_new_messages()
        second = driver.read_new_messages()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_driver_reads_snapshot_provider(self) -> None:
        snapshot = "[private] 小明 | 小明 | wxid_xiaoming | 今天复习得有点累"
        driver = WindowsWeChatReadOnlyDriver(snapshot_provider=StaticSnapshotProvider(snapshot))

        messages = driver.read_new_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].chat_title, "小明")

    def test_parser_marks_ocr_context_only_attachment_cards(self) -> None:
        snapshot = "[private] PAGE | PAGE |  | [OCR_CONTEXT][OCR附件卡片] Checklist.pdf kind=pdf size=2.4M"

        messages = SnapshotMessageParser().parse(snapshot, observed_at="2026-06-29T01:00:00+00:00")

        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].driver_meta["context_only"])
        self.assertEqual(messages[0].text, "[OCR附件卡片] Checklist.pdf kind=pdf size=2.4M")
        self.assertEqual(messages[0].driver_meta["attachments"][0]["name"], "Checklist.pdf")

    def test_driver_never_sends(self) -> None:
        driver = WindowsWeChatReadOnlyDriver(text_provider=lambda: "")

        result = driver.send_message("conversation", "hello")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "windows_readonly_driver_never_sends")

    def test_health_check_uses_window_probe_without_text_provider(self) -> None:
        driver = WindowsWeChatReadOnlyDriver(window_probe=_Probe([WindowInfo(hwnd=1, title="微信")]))

        self.assertTrue(driver.health_check())

    def test_health_check_fails_when_no_window_found(self) -> None:
        driver = WindowsWeChatReadOnlyDriver(window_probe=_Probe([]))

        self.assertFalse(driver.health_check())

    def test_find_wechat_processes_returns_list(self) -> None:
        self.assertIsInstance(find_wechat_processes(), list)

    def test_foreground_window_info_returns_dict(self) -> None:
        self.assertIsInstance(foreground_window_info(), dict)


class _Probe(Win32WindowProbe):
    def __init__(self, windows: list[WindowInfo]):
        self.windows = windows

    def find_wechat_windows(self) -> list[WindowInfo]:
        return self.windows


if __name__ == "__main__":
    unittest.main()
