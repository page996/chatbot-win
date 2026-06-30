from __future__ import annotations

import unittest

from app.personal_wechat_bot.control import sidebar_window
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo


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
        sidebar_window._wechat_anchor = lambda data_dir=None: None
        try:
            geometry = sidebar_window._sidebar_geometry(width=420, height=700)
        finally:
            sidebar_window._wechat_anchor = original

        self.assertEqual(geometry, {"x": 80, "y": 80, "width": 420, "height": 700})

    def test_sidebar_geometry_places_inside_wechat_when_right_side_has_no_room(self) -> None:
        original_anchor = sidebar_window._wechat_anchor
        original_work_area = sidebar_window._work_area
        sidebar_window._wechat_anchor = lambda data_dir=None: {"left": 100, "top": 80, "right": 1000, "bottom": 780}
        sidebar_window._work_area = lambda: {"left": 0, "top": 0, "right": 1024, "bottom": 768}
        try:
            geometry = sidebar_window._sidebar_geometry(width=420, height=700)
        finally:
            sidebar_window._wechat_anchor = original_anchor
            sidebar_window._work_area = original_work_area

        self.assertEqual(geometry["x"], 572)
        self.assertEqual(geometry["y"], 68)
        self.assertEqual(geometry["height"], 700)

    def test_wechat_anchor_filters_offscreen_tray_windows(self) -> None:
        class _Probe:
            def find_wechat_windows(self):
                return [
                    WindowInfo(hwnd=1, title="微信", width=157, height=25, left=-16000, top=-16000, right=-15843, bottom=-15975, process_name="Weixin.exe"),
                    WindowInfo(hwnd=2, title="微信", width=1000, height=700, left=100, top=100, right=1100, bottom=800, process_name="Weixin.exe"),
                ]

        original_probe = sidebar_window.Win32WindowProbe
        sidebar_window.Win32WindowProbe = lambda include_invisible=False: _Probe()
        try:
            anchor = sidebar_window._wechat_anchor()
        finally:
            sidebar_window.Win32WindowProbe = original_probe

        self.assertEqual(anchor, {"left": 100, "top": 100, "right": 1100, "bottom": 800})

    def test_wechat_anchor_prefers_active_window_binding(self) -> None:
        class _Store:
            def __init__(self, data_dir):
                self.data_dir = data_dir

            def list_bindings(self):
                return [{"conversation_id": "private-page", "status": "active"}]

            def resolve_status(self, conversation_id):
                return {
                    "status": "ok",
                    "window": WindowInfo(
                        hwnd=9,
                        title="微信",
                        width=900,
                        height=700,
                        left=200,
                        top=120,
                        right=1100,
                        bottom=820,
                        process_name="Weixin.exe",
                    ),
                }

        original_store = sidebar_window.WeChatWindowBindingStore
        sidebar_window.WeChatWindowBindingStore = _Store
        try:
            anchor = sidebar_window._wechat_anchor(data_dir="data")
        finally:
            sidebar_window.WeChatWindowBindingStore = original_store

        self.assertEqual(anchor, {"left": 200, "top": 120, "right": 1100, "bottom": 820})

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
