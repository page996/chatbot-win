from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.personal_wechat_bot.domain.models import ToolCallRequest
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.web.search import (
    SimplePageFetcher,
    WebSearchTool,
    fetched_text_block_reason,
    score_and_filter_candidates,
)


class WebSearchToolTest(unittest.TestCase):
    def test_scores_authority_filters_spam_and_fetches_top_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = WebSearchTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                provider=_FakeSearchProvider(),
                fetcher=_FakeFetcher(),
                blocklist=["blocked.example"],
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="web.search",
                    call_id="search-call",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"query": "python latest release", "level": "standard"},
                )
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.payload["level"], "standard")
            self.assertEqual(result.payload["result_count"], 3)
            self.assertEqual(result.payload["fetched_count"], 3)
            self.assertEqual(result.payload["results"][0]["domain"], "docs.python.org")
            self.assertIn("operator_blocklist", {item["reason"] for item in result.payload["filtered"]})
            self.assertIn("unsafe_or_spam_content", {item["reason"] for item in result.payload["filtered"]})
            self.assertIn("Official docs say Python 3.14 is current.", result.payload["annotation_text"])
            self.assertTrue(Path(result.output_refs[0]).exists())

    def test_deep_level_fetches_more_results_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetcher = _SlowFetcher()
            tool = WebSearchTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                provider=_ManyResultProvider(),
                fetcher=fetcher,
            )

            start = time.monotonic()
            result = tool.run(
                ToolCallRequest(
                    tool_name="web.search",
                    call_id="deep-call",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"query": "release notes", "level": "deep"},
                )
            )
            elapsed = time.monotonic() - start

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.payload["fetched_count"], 6)
            self.assertGreaterEqual(fetcher.max_active, 2)
            self.assertLess(elapsed, 0.9)

    def test_score_filter_blocks_login_only_result(self) -> None:
        scored, filtered = score_and_filter_candidates(
            "private beta docs",
            [
                {
                    "title": "Private beta docs",
                    "url": "https://example.com/private",
                    "snippet": "Please log in to continue",
                }
            ],
        )

        self.assertEqual(scored, [])
        self.assertEqual(filtered[0]["reason"], "login_or_paywall_required")

    def test_score_filter_blocks_chinese_app_login_result(self) -> None:
        scored, filtered = score_and_filter_candidates(
            "小红书 配置教程",
            [
                {
                    "title": "配置教程",
                    "url": "https://www.xiaohongshu.com/explore/abc",
                    "snippet": "打开App查看完整内容，请登录后继续",
                }
            ],
        )

        self.assertEqual(scored, [])
        self.assertEqual(filtered[0]["reason"], "login_or_paywall_required")

    def test_low_score_results_are_not_kept_or_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetcher = _CountingFetcher()
            tool = WebSearchTool(
                root / "outputs",
                FileIndex(root / "files.sqlite"),
                provider=_UnrelatedResultProvider(),
                fetcher=fetcher,
            )

            result = tool.run(
                ToolCallRequest(
                    tool_name="web.search",
                    call_id="low-score-call",
                    conversation_id="conv1",
                    requested_by="test",
                    arguments={"query": "python latest release", "level": "standard"},
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.payload["result_count"], 0)
            self.assertEqual(fetcher.calls, 0)
            self.assertIn("low_relevance_or_quality", {item["reason"] for item in result.payload["filtered"]})

    def test_fetched_text_block_reason_catches_restricted_and_unsafe_pages(self) -> None:
        self.assertEqual(fetched_text_block_reason("请登录后查看全文"), "login_or_paywall_detected")
        self.assertEqual(fetched_text_block_reason("请完成验证，验证你是真人"), "anti_bot_or_verification_wall")
        self.assertEqual(fetched_text_block_reason("现金网 博彩 广告推广"), "unsafe_or_spam_content")


    def test_malformed_search_result_is_filtered_without_raising(self) -> None:
        scored, filtered = score_and_filter_candidates(
            "release notes",
            [{"title": "Release notes", "url": "http://[::1", "snippet": "current release notes"}],
        )

        self.assertEqual(scored, [])
        self.assertEqual(filtered[0]["reason"], "invalid_or_non_http_url")

    def test_page_fetcher_blocks_malformed_url(self) -> None:
        result = SimplePageFetcher().fetch("http://[::1", timeout_seconds=1.0, max_bytes=1024)

        self.assertEqual(result, {"status": "blocked", "error": "invalid_url"})


class _FakeSearchProvider:
    def search(self, query: str, *, max_results: int, timeout_seconds: float):
        return [
            {
                "title": "Python 3.14.0 documentation",
                "url": "https://docs.python.org/3/whatsnew/3.14.html",
                "snippet": "Latest release notes and current documentation.",
            },
            {
                "title": "Community answer about Python release",
                "url": "https://zhihu.com/question/123",
                "snippet": "A discussion with a concise summary.",
            },
            {
                "title": "Source code release tag",
                "url": "https://github.com/python/cpython/releases",
                "snippet": "Release tags and changelog.",
            },
            {
                "title": "Sponsored Python casino bonus",
                "url": "https://ads.example.com/python",
                "snippet": "Sponsored casino bonus.",
            },
            {
                "title": "Blocked result",
                "url": "https://blocked.example/python",
                "snippet": "Should be blocked by operator policy.",
            },
        ][:max_results]


class _ManyResultProvider:
    def search(self, query: str, *, max_results: int, timeout_seconds: float):
        return [
            {
                "title": f"Official release notes {index}",
                "url": f"https://docs.python.org/3/release-{index}.html",
                "snippet": "release notes current changelog",
            }
            for index in range(10)
        ][:max_results]


class _UnrelatedResultProvider:
    def search(self, query: str, *, max_results: int, timeout_seconds: float):
        return [
            {
                "title": "Weekend cooking ideas",
                "url": "https://example.com/cooking",
                "snippet": "Dinner notes and kitchen tips.",
            }
        ][:max_results]


class _FakeFetcher:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int):
        return {"status": "completed", "content_type": "text/html", "text": "Official docs say Python 3.14 is current."}


class _CountingFetcher:
    def __init__(self):
        self.calls = 0

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int):
        self.calls += 1
        return {"status": "completed", "content_type": "text/html", "text": f"Fetched {url}"}


class _SlowFetcher:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.15)
            return {"status": "completed", "content_type": "text/html", "text": f"Fetched {url}"}
        finally:
            with self.lock:
                self.active -= 1


if __name__ == "__main__":
    unittest.main()
