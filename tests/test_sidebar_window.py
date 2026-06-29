from __future__ import annotations

import unittest

from app.personal_wechat_bot.control import sidebar_window


class SidebarWindowTest(unittest.TestCase):
    def test_sidebar_window_entrypoint_is_importable(self) -> None:
        self.assertTrue(callable(sidebar_window.run_sidebar_window))

    def test_queue_helpers_flatten_counts_and_fingerprint(self) -> None:
        state = {
            "config": {"mode": "confirm", "send_enabled": True, "send_driver": "windows_guarded"},
            "readiness": {"status": "ready"},
            "driver_probe": {"foreground": {"title": "微信"}},
            "queues": {
                "pending": {
                    "count": 1,
                    "items": [
                        {
                            "queue_id": "q1",
                            "status": "pending",
                            "reply": {"conversation_id": "c1", "text": "hello"},
                        }
                    ],
                },
                "approved": {"count": 0, "items": []},
                "failed": {"count": 0, "items": []},
            },
        }

        self.assertEqual(sidebar_window.queue_counts(state)["pending"], 1)
        self.assertEqual(sidebar_window.flatten_queue_items(state)[0]["queue_id"], "q1")
        self.assertEqual(
            sidebar_window.sidebar_state_fingerprint(state),
            sidebar_window.sidebar_state_fingerprint(dict(state)),
        )

    def test_wechat_foreground_check_does_not_match_sidebar_title(self) -> None:
        self.assertFalse(
            sidebar_window._looks_like_wechat_foreground(
                {"title": "WeChat Agent Queue", "process_name": "python.exe"}
            )
        )
        self.assertTrue(
            sidebar_window._looks_like_wechat_foreground(
                {"title": "微信", "process_name": "WeChatAppEx.exe"}
            )
        )


if __name__ == "__main__":
    unittest.main()
