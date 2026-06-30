from __future__ import annotations

import unittest

from app.personal_wechat_bot.wechat_driver import window_introspection
from app.personal_wechat_bot.wechat_driver.windows_readonly import WindowInfo


class WindowIntrospectionTest(unittest.TestCase):
    def test_probe_returns_stable_payload_shape(self) -> None:
        payload = window_introspection.build_wechat_window_probe(max_children=2, max_controls=2)

        self.assertIn(payload["status"], {"ok", "not_found"})
        self.assertEqual(payload["strategy"], "win32_hwnd_plus_ui_automation")
        self.assertIn("developer_tools_note", payload)
        self.assertIn("foreground", payload)
        self.assertIn("windows", payload)
        self.assertIn("ignored_windows", payload)
        self.assertIn("ui_automation", payload)

    def test_uia_dependency_status_reports_available_or_install_hint(self) -> None:
        status = window_introspection._uia_dependency_status()

        self.assertIn("available", status)
        if not status["available"]:
            self.assertIn("install", status)

    def test_candidate_chat_window_filters_offscreen_tray_window(self) -> None:
        self.assertFalse(
            window_introspection._is_candidate_chat_window(
                WindowInfo(hwnd=1, title="微信", width=157, height=25, left=-16000, top=-16000)
            )
        )
        self.assertTrue(
            window_introspection._is_candidate_chat_window(
                WindowInfo(hwnd=2, title="微信", width=1000, height=700, left=100, top=100)
            )
        )


if __name__ == "__main__":
    unittest.main()
