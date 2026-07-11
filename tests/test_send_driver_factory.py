from __future__ import annotations

import unittest
from unittest import mock

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.wechat_driver.bridge_send import BRIDGE_OUTBOX_SEND_DRIVER
from app.personal_wechat_bot.wechat_driver.send_driver_factory import (
    build_send_driver,
    implemented_send_drivers,
    is_real_send_driver_implemented,
    probe_send_driver,
    registered_send_drivers,
)


class SendDriverFactoryTest(unittest.TestCase):
    def test_bridge_outbox_is_registered_and_real_send_capable(self) -> None:
        config = BotConfig(send_driver=BRIDGE_OUTBOX_SEND_DRIVER)

        driver = build_send_driver(config)
        registered = registered_send_drivers()

        self.assertIsNotNone(driver)
        self.assertTrue(any(item["name"] == BRIDGE_OUTBOX_SEND_DRIVER for item in registered))
        self.assertTrue(is_real_send_driver_implemented(BRIDGE_OUTBOX_SEND_DRIVER))
        self.assertIn(BRIDGE_OUTBOX_SEND_DRIVER, implemented_send_drivers())

    def test_probe_reports_registered_driver_blocked_when_send_disabled(self) -> None:
        config = BotConfig(send_enabled=False, send_driver=BRIDGE_OUTBOX_SEND_DRIVER)

        probe = probe_send_driver(config)

        self.assertTrue(probe["registered"])
        self.assertTrue(probe["real_send_implemented"])
        self.assertTrue(probe["driver_present"])
        self.assertEqual(probe["driver_probe"]["driver"], BRIDGE_OUTBOX_SEND_DRIVER)
        self.assertIn("send_enabled_false", probe["driver_probe"]["blockers"])

    def test_unknown_driver_probe_reports_missing_driver(self) -> None:
        config = BotConfig(send_enabled=True, send_driver="unknown")

        probe = probe_send_driver(config)

        self.assertFalse(probe["registered"])
        self.assertFalse(probe["driver_present"])
        self.assertIsNone(probe["driver_probe"])

    def test_passive_probe_never_connects_to_backend(self) -> None:
        config = BotConfig(
            send_enabled=True,
            send_driver=BRIDGE_OUTBOX_SEND_DRIVER,
            send_backend="wechat_native_http",
        )

        with mock.patch(
            "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
            side_effect=AssertionError("passive probe must stay local"),
        ) as backend_status:
            probe = probe_send_driver(config, active_backend_probe=False)

        backend_status.assert_not_called()
        self.assertFalse(probe["driver_probe"]["backend"]["active_backend_probe"])
        self.assertEqual(probe["wechat_native_http"], {})

    def test_active_probe_calls_selected_backend_once(self) -> None:
        config = BotConfig(
            send_enabled=True,
            send_driver=BRIDGE_OUTBOX_SEND_DRIVER,
            send_backend="wechat_native_http",
        )
        status = {"available": True, "reason": "", "health": {"IsLogin": 1}}

        with mock.patch(
            "app.personal_wechat_bot.wechat_driver.bridge_send.wechat_native_http_status",
            return_value=status,
        ) as backend_status:
            probe = probe_send_driver(config)

        backend_status.assert_called_once()
        self.assertEqual(probe["wechat_native_http"], status)

if __name__ == "__main__":
    unittest.main()
