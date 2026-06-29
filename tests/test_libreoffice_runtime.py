from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.personal_wechat_bot.tools.document import libreoffice
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeHealth, LibreOfficeRuntime


class LibreOfficeRuntimeTest(unittest.TestCase):
    def test_convert_command_is_non_interactive_and_uses_isolated_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = libreoffice._convert_command(
                "soffice.exe",
                root / "input.docx",
                root / "out",
                root / "out" / "profile",
            )

            self.assertIn("--headless", command)
            self.assertIn("--invisible", command)
            self.assertIn("--nodefault", command)
            self.assertIn("--nolockcheck", command)
            self.assertIn("--norestore", command)
            self.assertTrue(any(item.startswith("-env:UserInstallation=file:///") for item in command))

    def test_convert_to_pdf_closes_stdin_and_hides_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.txt"
            input_path.write_text("hello", encoding="utf-8")
            runtime = LibreOfficeRuntime(executable="soffice.exe")

            def fake_run(*args, **kwargs):
                self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["creationflags"], libreoffice._creationflags())
                return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

            with patch.object(
                runtime,
                "health",
                return_value=LibreOfficeHealth(True, executable=str(root / "program" / "soffice.exe"), version="test"),
            ), patch("app.personal_wechat_bot.tools.document.libreoffice.subprocess.run", side_effect=fake_run):
                output = runtime.convert_to_pdf(input_path, root / "out")

            self.assertEqual(output, root / "out" / "input.pdf")
            self.assertTrue((root / "out" / "profile").exists())


if __name__ == "__main__":
    unittest.main()
