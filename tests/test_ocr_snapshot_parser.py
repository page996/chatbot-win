from __future__ import annotations

import unittest

from app.personal_wechat_bot.wechat_driver.ocr_snapshot_parser import (
    ocr_text_to_snapshots,
    ocr_text_to_snapshot,
    parse_ocr_snapshot,
)


class OcrSnapshotParserTest(unittest.TestCase):
    def test_ocr_text_to_snapshot_extracts_page_message(self) -> None:
        text = "\n".join(
            [
                "Q搜索",
                "PAGE",
                "文件传输助手",
                "21:56",
                "PAGE",
                "21:53",
                "猪思",
                "我通过了你的朋友验证请求，",
                "我通过了你的朋友验证请求，现在我们可以开始聊天了",
                "猪思",
            ]
        )

        snapshot = ocr_text_to_snapshot(text, preferred_chat_title="PAGE")

        self.assertEqual(snapshot, "[private] PAGE | PAGE |  | 我通过了你的朋友验证请求，现在我们可以开始聊天了")

    def test_prefers_longer_duplicate_message_over_truncated_prefix(self) -> None:
        text = "\n".join(
            [
                "Q搜索",
                "PAGE",
                "21:53",
                "我通过了你的朋友验证请求，",
                "我通过了你的朋友验证请求，现在我们可以开始聊天了",
            ]
        )

        result = parse_ocr_snapshot(text, preferred_chat_title="PAGE")

        self.assertIsNotNone(result)
        self.assertEqual(result.message, "我通过了你的朋友验证请求，现在我们可以开始聊天了")

    def test_ignores_single_character_artifact_after_message(self) -> None:
        text = "\n".join(
            [
                "PAGE",
                "刚才那个文件我看到了，先整理重点给你",
                "六",
            ]
        )

        snapshot = ocr_text_to_snapshot(text, preferred_chat_title="PAGE")

        self.assertEqual(snapshot, "[private] PAGE | PAGE |  | 刚才那个文件我看到了，先整理重点给你")

    def test_guesses_chat_title_without_preferred_title(self) -> None:
        text = "\n".join(
            [
                "搜索",
                "PAGE",
                "文件传输助手",
                "21:56",
                "PAGE",
                "今天可以继续测试 OCR 吗",
            ]
        )

        snapshot = ocr_text_to_snapshot(text)

        self.assertEqual(snapshot, "[private] PAGE | PAGE |  | 今天可以继续测试 OCR 吗")

    def test_returns_empty_when_no_meaningful_message(self) -> None:
        text = "\n".join(["Q搜索", "PAGE", "文件传输助手", "21:56", "猪思", "六"])

        snapshot = ocr_text_to_snapshot(text, preferred_chat_title="PAGE")

        self.assertEqual(snapshot, "")

    def test_allows_extra_ignored_names(self) -> None:
        text = "\n".join(["PAGE", "测试账号", "这个截图里的最后一句才是消息", "测试账号"])

        snapshot = ocr_text_to_snapshot(
            text,
            preferred_chat_title="PAGE",
            ignored_names={"测试账号"},
        )

        self.assertEqual(snapshot, "[private] PAGE | PAGE |  | 这个截图里的最后一句才是消息")

    def test_ignores_file_card_after_last_text_message(self) -> None:
        text = "\n".join(
            [
                "搜索",
                "PAGE",
                "昨天23:51",
                "猪思",
                "如果可以接收到我的消息，回复第一行加括号",
                "Checklist.pdf",
                "PDF",
                "2.4M",
            ]
        )

        snapshot = ocr_text_to_snapshot(text, preferred_chat_title="PAGE", ignored_names={"猪思"})

        self.assertEqual(snapshot, "[private] PAGE | PAGE |  | 如果可以接收到我的消息，回复第一行加括号")

    def test_blocks_truncated_left_list_preview_with_stale_visual_content(self) -> None:
        text = "\n".join(
            [
                "PAGE",
                "猪思",
                "如果收到了这条消息，无视上.",
                "我通过了你的朋友验证请求，现在我们可以开始聊天了",
                "Checklist.pdf",
                "PDF",
                "2.4M",
                "微信电脑版",
                "Congratulations...",
            ]
        )

        result = parse_ocr_snapshot(text, preferred_chat_title="PAGE", ignored_names={"猪思"})

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "ambiguous_or_truncated")
        self.assertEqual(result.to_snapshot(), "")
        self.assertEqual(ocr_text_to_snapshot(text, preferred_chat_title="PAGE", ignored_names={"猪思"}), "")
        self.assertEqual(ocr_text_to_snapshots(text, preferred_chat_title="PAGE", ignored_names={"猪思"}), [])
        self.assertIn("如果收到了这条消息", result.evidence[0])

    def test_blocks_truncated_preview_even_when_chat_title_appears_multiple_times(self) -> None:
        text = "\n".join(
            [
                "Q",
                "搜索",
                "PAGE",
                "文件传输助手",
                "昨天21:56",
                "PAGE",
                "14:53",
                "猪思",
                "如果收到了这条消息，无视上.",
                "我通过了你的朋友验证请求，现在我们可以开始聊天了",
                "猪思",
                "Checklist.pdf",
                "猪思",
                "PDF",
                "2.4M",
                "微信电脑版",
                "Congratulations! We are pleased to offer you admission to the following programme in 2026/27:",
                "猪思",
                "Programme Code & Title",
                "P70 MSc Data Science",
                "ource of Funding",
                "Non-govement funded",
            ]
        )

        result = parse_ocr_snapshot(text, preferred_chat_title="PAGE", ignored_names={"猪思"})

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "ambiguous_or_truncated")
        self.assertEqual(result.to_snapshots(), [])
        self.assertIn("如果收到了这条消息", result.evidence[0])


if __name__ == "__main__":
    unittest.main()
