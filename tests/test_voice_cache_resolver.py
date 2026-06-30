from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import WeChatVoiceCacheResolver


class VoiceCacheResolverTest(unittest.TestCase):
    def test_resolves_readable_audio_by_name_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "voice_abc123.m4a"
            audio.write_bytes(b"fake audio")

            result = WeChatVoiceCacheResolver([root], allowed_extensions=[".m4a"]).resolve(
                {"audio_name": "voice_abc123"},
            )

            self.assertEqual(result.status, "resolved")
            self.assertEqual(Path(result.path), audio)
            self.assertEqual(result.reason, "matched_readable_audio_cache")

    def test_resolves_readable_audio_by_observed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "latest.silk"
            audio.write_bytes(b"fake audio")
            observed_at = datetime.fromtimestamp(audio.stat().st_mtime, tz=timezone.utc).isoformat()

            result = WeChatVoiceCacheResolver([root], allowed_extensions=[".silk"]).resolve(
                {},
                observed_at=observed_at,
            )

            self.assertEqual(result.status, "resolved")
            self.assertEqual(Path(result.path), audio)
            self.assertIn("time_window", result.candidates[0].reasons[0])

    def test_does_not_scan_without_hints_or_observed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "voice.m4a").write_bytes(b"fake audio")

            result = WeChatVoiceCacheResolver([root], allowed_extensions=[".m4a"]).resolve({})

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "insufficient_voice_cache_hints")


if __name__ == "__main__":
    unittest.main()
