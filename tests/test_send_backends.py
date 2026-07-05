from __future__ import annotations

import unittest

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.wechat_driver.send_backends import WcfSendBackend, build_send_backend


class SendBackendsTest(unittest.TestCase):
    def test_wcf_backend_returns_unknown_delivery_timeout_without_blocking_worker(self) -> None:
        def timeout_runner(payload, timeout_seconds):
            raise TimeoutError("hung rpc")

        backend = WcfSendBackend(timeout_seconds=1.5, child_runner=timeout_runner)

        result = backend.send_text("wxid_a", "hello")

        self.assertFalse(result.ok)
        self.assertIn("wcf_rpc_timeout:1.5s", result.reason)

    def test_wcf_backend_maps_success_and_failure_from_child(self) -> None:
        ok_backend = WcfSendBackend(child_runner=lambda payload, timeout: {"ok": True, "ret": 0})
        fail_backend = WcfSendBackend(child_runner=lambda payload, timeout: {"ok": False, "ret": 37})

        ok = ok_backend.send_text("wxid_a", "hello")
        failed = fail_backend.send_file("wxid_a", "report.pdf")

        self.assertTrue(ok.ok)
        self.assertEqual(ok.reason, "wcf_send_text")
        self.assertFalse(failed.ok)
        self.assertEqual(failed.reason, "wcf_send_file_failed:code=37")

    def test_build_send_backend_passes_configured_wcf_timeout(self) -> None:
        backend = build_send_backend(
            BotConfig(send_backend="wcf", wcf_host="127.0.0.1", wcf_port=10086, wcf_send_timeout_seconds=3.25)
        )

        self.assertIsInstance(backend, WcfSendBackend)
        self.assertEqual(backend.timeout_seconds, 3.25)


if __name__ == "__main__":
    unittest.main()
