from __future__ import annotations

import unittest

from app.personal_wechat_bot.control import sidebar_window


class SidebarWindowTest(unittest.TestCase):
    def test_sidebar_window_entrypoint_is_importable(self) -> None:
        self.assertTrue(callable(sidebar_window.run_sidebar_window))


if __name__ == "__main__":
    unittest.main()
