from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.vision.ocr import PlaceholderGpuOcrEngine


ROOT = Path(__file__).resolve().parent


class CapabilitiesTest(unittest.TestCase):
    def test_ocr_health_is_structured(self) -> None:
        health = PlaceholderGpuOcrEngine().health()

        self.assertEqual(health.backend, "gpu_ocr_placeholder")
        self.assertIsInstance(health.available, bool)
        self.assertIsInstance(health.gpu_available, bool)

    def test_libreoffice_health_is_structured(self) -> None:
        health = LibreOfficeRuntime(executable="Z:\\missing\\soffice.exe").health()

        self.assertFalse(health.available)
        self.assertIsInstance(health.executable, str)
        self.assertIsInstance(health.version, str)

    def test_default_libreoffice_runtime_can_use_project_vendor_install(self) -> None:
        health = LibreOfficeRuntime().health()

        self.assertIsInstance(health.available, bool)
        if health.available:
            self.assertIn("vendor", health.executable.lower())

    def test_capabilities_cli_returns_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "app.personal_wechat_bot.main", "capabilities"],
            cwd=ROOT.parent,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
        )

        payload = json.loads(completed.stdout)

        self.assertIn("ocr", payload)
        self.assertIn("libreoffice", payload)
        self.assertIn("asr", payload)
        self.assertIn("wechat_voice_cache", payload)
        self.assertFalse(payload["send_enabled"])


if __name__ == "__main__":
    unittest.main()
