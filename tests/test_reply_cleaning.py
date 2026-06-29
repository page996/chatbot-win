from __future__ import annotations

import unittest

from app.personal_wechat_bot.conversation.engine import _clean_visible_reply


class ReplyCleaningTest(unittest.TestCase):
    def test_clean_visible_reply_removes_plan_monitor_summary_lines(self) -> None:
        raw = "\n".join(
            [
                "【计划】先判断语气",
                "【监控】准备回复",
                "（发送消息）",
                "“嘿，刚加上啦，之后可以直接找我聊～”",
                "【总结】已回复",
            ]
        )

        self.assertEqual(_clean_visible_reply(raw), "嘿，刚加上啦，之后可以直接找我聊～")


if __name__ == "__main__":
    unittest.main()
