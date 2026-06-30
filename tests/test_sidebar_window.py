from __future__ import annotations

import unittest

from app.personal_wechat_bot.control import sidebar_window


class SidebarWindowTest(unittest.TestCase):
    def test_sidebar_window_entrypoint_is_importable(self) -> None:
        self.assertTrue(callable(sidebar_window.run_sidebar_window))

    def test_queue_helpers_flatten_counts(self) -> None:
        state = {
            "queues": {
                "pending": {
                    "count": 1,
                    "items": [{"queue_id": "q1", "reply": {"conversation_id": "c1", "text": "hello"}}],
                },
                "approved": {"count": 0, "items": []},
                "failed": {"count": 0, "items": []},
            },
        }

        self.assertEqual(sidebar_window.queue_counts(state)["pending"], 1)
        self.assertEqual(sidebar_window.flatten_queue_items(state)[0]["queue_id"], "q1")

    def test_sidebar_geometry_uses_default_when_wechat_missing(self) -> None:
        original = sidebar_window._wechat_anchor
        sidebar_window._wechat_anchor = lambda: None
        try:
            geometry = sidebar_window._sidebar_geometry(width=420, height=700)
        finally:
            sidebar_window._wechat_anchor = original

        self.assertEqual(geometry, {"x": 80, "y": 80, "width": 420, "height": 700})

    def test_launch_result_json_is_stable(self) -> None:
        payload = sidebar_window.result_as_json(
            sidebar_window.SidebarLaunchResult(
                status="ok",
                url="http://127.0.0.1:1/",
                host="127.0.0.1",
                port=1,
                browser="chrome",
                pid=123,
                geometry={"x": 1, "y": 2, "width": 3, "height": 4},
            )
        )

        self.assertIn('"status": "ok"', payload)


if __name__ == "__main__":
    unittest.main()
