from __future__ import annotations

import unittest

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.wechat_driver.send_driver_factory import (
    build_send_driver,
    implemented_send_drivers,
    is_real_send_driver_implemented,
    probe_send_driver,
    registered_send_drivers,
)
from app.personal_wechat_bot.wechat_driver.windows_guarded_send import WINDOWS_GUARDED_SEND_DRIVER


class SendDriverFactoryTest(unittest.TestCase):
    def test_windows_guarded_is_registered_and_real_send_capable(self) -> None:
        config = BotConfig(send_driver=WINDOWS_GUARDED_SEND_DRIVER)

        driver = build_send_driver(config)
        registered = registered_send_drivers()

        self.assertIsNotNone(driver)
        self.assertTrue(any(item["name"] == WINDOWS_GUARDED_SEND_DRIVER for item in registered))
        self.assertTrue(is_real_send_driver_implemented(WINDOWS_GUARDED_SEND_DRIVER))
        self.assertIn(WINDOWS_GUARDED_SEND_DRIVER, implemented_send_drivers())

    def test_probe_reports_registered_driver_blocked_when_send_disabled(self) -> None:
        config = BotConfig(send_enabled=False, send_driver=WINDOWS_GUARDED_SEND_DRIVER)

        probe = probe_send_driver(config)

        self.assertTrue(probe["registered"])
        self.assertTrue(probe["real_send_implemented"])
        self.assertTrue(probe["driver_present"])
        self.assertEqual(probe["driver_probe"]["driver"], WINDOWS_GUARDED_SEND_DRIVER)
        self.assertIn("send_enabled_false", probe["driver_probe"]["blockers"])

    def test_unknown_driver_probe_reports_missing_driver(self) -> None:
        config = BotConfig(send_enabled=True, send_driver="unknown")

        probe = probe_send_driver(config)

        self.assertFalse(probe["registered"])
        self.assertFalse(probe["driver_present"])
        self.assertIsNone(probe["driver_probe"])


if __name__ == "__main__":
    unittest.main()
