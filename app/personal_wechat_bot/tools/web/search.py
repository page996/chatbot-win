from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.web.http_safety import (
    PublicHttpUrlError,
    guarded_urlopen,
    read_response_with_deadline,
)


SEARCH_LEVELS: dict[str, dict[str, Any]] = {
    "light": {
        "max_results": 5,
        "fetch_top": 1,
        "timeout_seconds": 6.0,
        "max_workers": 3,
        "min_score": 0.45,
        "fetch_min_score": 0.55,
    },
    "standard": {
        "max_results": 8,
        "fetch_top": 3,
        "timeout_seconds": 8.0,
        "max_workers": 4,
        "min_score": 0.38,
        "fetch_min_score": 0.48,
    },
    "deep": {
        "max_results": 12,
        "fetch_top": 6,
        "timeout_seconds": 12.0,
        "max_workers": 6,
        "min_score": 0.30,
        "fetch_min_score": 0.40,
    },
}


class SearchProvider(Protocol):
    def search(self, query: str, *, max_results: int, timeout_seconds: float) -> list[dict[str, Any]]: ...


class PageFetcher(Protocol):
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ScoredCandidate:
    title: str
    url: str
    domain: str
    snippet: str
    score: float
    authority_score: float
    relevance_score: float
    quality_tier: str
    source_type: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "authority_score": round(self.authority_score, 4),
            "relevance_score": round(self.relevance_score, 4),
            "quality_tier": self.quality_tier,
            "source_type": self.source_type,
            "reasons": list(self.reasons),
        }


class WebSearchTool:
    manifest = ToolManifest(
        name="web.search",
        description="Search public web pages, score/filter results, fetch high-value pages, and save an evidence note.",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        *,
        provider: SearchProvider | None = None,
        fetcher: PageFetcher | None = None,
        blocklist: list[str] | None = None,
        max_fetch_bytes: int = 512 * 1024,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.provider = provider or DuckDuckGoHtmlSearchProvider()
        self.fetcher = fetcher or SimplePageFetcher()
        self.blocklist = [item.lower().strip() for item in (blocklist or []) if str(item).strip()]
        self.max_fetch_bytes = max_fetch_bytes

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        query = str(request.arguments.get("query") or request.arguments.get("q") or "").strip()
        if not query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="web.search requires a query",
                error="missing_query",
            )
        level = _search_level(request.arguments.get("level") or request.arguments.get("strength"))
        level_spec = dict(SEARCH_LEVELS[level])
        max_results = _bounded_int(request.arguments.get("max_results"), level_spec["max_results"], 1, 20)
        fetch_top = _bounded_int(request.arguments.get("fetch_top"), level_spec["fetch_top"], 0, max_results)
        timeout_seconds = float(level_spec["timeout_seconds"])

        try:
            raw_candidates = self.provider.search(query, max_results=max_results * 3, timeout_seconds=timeout_seconds)
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary="web.search provider failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        scored, filtered = score_and_filter_candidates(query, raw_candidates, blocklist=self.blocklist)
        scored, low_score_filtered = _drop_low_score(scored, minimum_score=float(level_spec["min_score"]))
        filtered.extend(low_score_filtered)
        kept = scored[:max_results]
        fetch_candidates = _fetch_candidates(kept, fetch_top=fetch_top, minimum_score=float(level_spec["fetch_min_score"]))
        fetched = self._fetch_top(fetch_candidates, timeout_seconds=timeout_seconds, max_workers=int(level_spec["max_workers"]))
        annotation_text = _render_search_annotation(
            query=query,
            level=level,
            kept=kept,
            fetched=fetched,
            filtered=filtered,
        )
        output = self.output_dir / f"{_slug(request.call_id or query)}.md"
        output.write_text(annotation_text, encoding="utf-8")
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        summary = _summary_line(query=query, level=level, kept=kept, fetched=fetched, filtered=filtered)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed" if kept else "blocked",
            summary=summary,
            output_refs=[str(output)],
            error=None if kept else "no_search_results_after_filter",
            payload={
                "file_id": file_id,
                "query": query,
                "level": level,
                "result_count": len(kept),
                "fetched_count": len([item for item in fetched if item.get("status") == "completed"]),
                "results": [item.to_dict() for item in kept],
                "fetched": fetched,
                "filtered": filtered[:30],
                "annotation_text": annotation_text,
            },
        )

    def _fetch_top(
        self,
        candidates: list[ScoredCandidate],
        *,
        timeout_seconds: float,
        max_workers: int,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        fetched_by_url: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {
                pool.submit(self.fetcher.fetch, candidate.url, timeout_seconds=timeout_seconds, max_bytes=self.max_fetch_bytes): candidate
                for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                fetched_by_url[candidate.url] = _fetched_payload(candidate, result)
        return [fetched_by_url.get(candidate.url, _fetched_payload(candidate, {"status": "failed"})) for candidate in candidates]


class DuckDuckGoHtmlSearchProvider:
    def search(self, query: str, *, max_results: int, timeout_seconds: float) -> list[dict[str, Any]]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "wechat-agent-local/0.1",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        with guarded_urlopen(request, timeout_seconds=timeout_seconds) as response:
            raw = read_response_with_deadline(
                response,
                max_bytes=1024 * 1024,
                deadline=deadline,
                truncate=True,
            )
            charset = _charset(response.headers.get("content-type", "")) or "utf-8"
        parser = _DuckDuckGoParser()
        parser.feed(raw.decode(charset, errors="replace"))
        return parser.results[:max_results]


class SimplePageFetcher:
    def __init__(self, *, allow_private_network: bool = False) -> None:
        self.allow_private_network = allow_private_network

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> dict[str, Any]:
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return {"status": "blocked", "error": "invalid_url"}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"status": "blocked", "error": "invalid_url"}
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "wechat-agent-local/0.1",
                    "Accept": "text/html,text/plain,application/xhtml+xml,application/json",
                },
            )
            deadline = time.monotonic() + max(0.2, float(timeout_seconds))
            with guarded_urlopen(
                request,
                timeout_seconds=timeout_seconds,
                allow_private_network=self.allow_private_network,
            ) as response:
                content_type = response.headers.get("content-type", "")
                raw = read_response_with_deadline(
                    response,
                    max_bytes=max_bytes + 1,
                    deadline=deadline,
                    truncate=True,
                )
        except PublicHttpUrlError as exc:
            return {"status": "blocked", "error": str(exc)}
        except (UnicodeError, ValueError):
            return {"status": "blocked", "error": "invalid_url"}
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        if _binary_content(content_type, raw):
            return {"status": "blocked", "error": "binary_or_file_content", "content_type": content_type}
        charset = _charset(content_type) or "utf-8"
        text = raw.decode(charset, errors="replace")
        if "html" in content_type.lower() or "<html" in text[:1000].lower():
            text = _html_to_text(text)
        else:
            text = _normalize_text(text)
        block_reason = fetched_text_block_reason(text)
        if block_reason:
            return {"status": "blocked", "error": block_reason, "content_type": content_type}
        return {
            "status": "completed",
            "content_type": content_type,
            "text": _compact(text, 1800),
            "truncated": truncated,
        }


def score_and_filter_candidates(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    blocklist: list[str] | None = None,
) -> tuple[list[ScoredCandidate], list[dict[str, Any]]]:
    scored: list[ScoredCandidate] = []
    filtered: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    blocklist = [item.lower().strip() for item in (blocklist or []) if str(item).strip()]
    for raw in candidates:
        title = _clean_text(raw.get("title"))
        url = _normalize_result_url(str(raw.get("url") or ""))
        snippet = _clean_text(raw.get("snippet"))
        domain = _domain(url)
        reason = _filter_reason(title=title, url=url, domain=domain, snippet=snippet, blocklist=blocklist)
        if not reason and url in seen_urls:
            reason = "duplicate_url"
        if reason:
            filtered.append({"title": title, "url": url, "domain": domain, "reason": reason})
            continue
        seen_urls.add(url)
        scored.append(_score_candidate(query, title=title, url=url, domain=domain, snippet=snippet))
    scored.sort(key=lambda item: (item.score, item.authority_score, item.relevance_score), reverse=True)
    return scored, filtered


def _drop_low_score(
    candidates: list[ScoredCandidate],
    *,
    minimum_score: float,
) -> tuple[list[ScoredCandidate], list[dict[str, Any]]]:
    kept: list[ScoredCandidate] = []
    filtered: list[dict[str, Any]] = []
    for item in candidates:
        if item.score >= minimum_score:
            kept.append(item)
            continue
        filtered.append(
            {
                "title": item.title,
                "url": item.url,
                "domain": item.domain,
                "reason": "low_relevance_or_quality",
                "score": round(item.score, 4),
                "relevance_score": round(item.relevance_score, 4),
            }
        )
    return kept, filtered


def _fetch_candidates(
    candidates: list[ScoredCandidate],
    *,
    fetch_top: int,
    minimum_score: float,
) -> list[ScoredCandidate]:
    if fetch_top <= 0:
        return []
    return [item for item in candidates if item.score >= minimum_score][:fetch_top]


def _score_candidate(query: str, *, title: str, url: str, domain: str, snippet: str) -> ScoredCandidate:
    authority_score, source_type, authority_reasons = _authority_score(domain, url)
    relevance_score, relevance_reasons = _relevance_score(query, title, snippet, url)
    quality_adjustment, quality_reasons = _quality_adjustment(domain, title, snippet, url)
    score = max(0.0, min(1.0, 0.18 + authority_score * 0.38 + relevance_score * 0.34 + quality_adjustment))
    quality_tier = "high" if score >= 0.72 else ("medium" if score >= 0.48 else "low")
    return ScoredCandidate(
        title=title,
        url=url,
        domain=domain,
        snippet=snippet,
        score=score,
        authority_score=authority_score,
        relevance_score=relevance_score,
        quality_tier=quality_tier,
        source_type=source_type,
        reasons=[*authority_reasons, *relevance_reasons, *quality_reasons][:8],
    )


def _authority_score(domain: str, url: str) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    score = 0.18
    source_type = "web"
    if domain.endswith(".gov") or ".gov." in domain or domain.endswith(".gov.cn"):
        score = 0.98
        source_type = "government"
        reasons.append("government_domain")
    elif domain.endswith(".edu") or ".edu." in domain:
        score = 0.82
        source_type = "education"
        reasons.append("education_domain")
    elif domain in _OFFICIAL_DOC_DOMAINS or any(marker in url.lower() for marker in ("/docs/", "/documentation/", "/reference/")):
        score = 0.78
        source_type = "official_docs"
        reasons.append("official_docs")
    elif domain in _RESEARCH_DOMAINS or domain.endswith(".ac.uk"):
        score = 0.76
        source_type = "research"
        reasons.append("research_source")
    elif domain in {"github.com", "gitlab.com"}:
        score = 0.64
        source_type = "source_repository"
        reasons.append("source_repository")
    elif domain in _CHINESE_COMMUNITY_DOMAINS:
        score = 0.42
        source_type = "community"
        reasons.append("community_platform")
    if not domain.endswith(".cn") and source_type not in {"government", "education"}:
        score = min(1.0, score + 0.04)
        reasons.append("non_cn_source")
    return score, source_type, reasons


def _relevance_score(query: str, title: str, snippet: str, url: str) -> tuple[float, list[str]]:
    query_terms = _terms(query)
    haystack = f"{title} {snippet} {url}".lower()
    if not query_terms:
        return 0.1, []
    hits = sum(1 for term in query_terms if term in haystack)
    score = min(1.0, hits / max(1, len(query_terms)))
    reasons = [f"query_term_hits={hits}/{len(query_terms)}"]
    if _FRESHNESS_MARKER_RE.search(haystack):
        score = min(1.0, score + 0.14)
        reasons.append("freshness_marker")
    return score, reasons


def _quality_adjustment(domain: str, title: str, snippet: str, url: str) -> tuple[float, list[str]]:
    text = f"{domain} {title} {snippet} {url}".lower()
    reasons: list[str] = []
    adjustment = 0.0
    if any(marker in text for marker in ("official", "docs", "reference", "whitepaper", "specification", "标准", "官方")):
        adjustment += 0.12
        reasons.append("quality_marker")
    if any(marker in text for marker in ("forum", "thread", "community", "问答", "回答")):
        adjustment -= 0.04
        reasons.append("community_penalty")
    if any(marker in text for marker in ("download free", "coupon", "top 10", "推广", "广告")):
        adjustment -= 0.18
        reasons.append("spam_marker")
    return adjustment, reasons


def _filter_reason(*, title: str, url: str, domain: str, snippet: str, blocklist: list[str]) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "invalid_or_non_http_url"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "invalid_or_non_http_url"
    if _domain_blocked(domain, blocklist):
        return "operator_blocklist"
    text = f"{domain} {parsed.path} {title} {snippet}".lower()
    if any(pattern.search(text) for pattern in _HARD_FILTER_PATTERNS):
        return "unsafe_or_spam_content"
    if any(marker in text for marker in _LOGIN_MARKERS):
        return "login_or_paywall_required"
    if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in _AD_TRACKING_DOMAINS):
        return "ad_or_tracking_domain"
    return ""


def _domain_blocked(domain: str, blocklist: list[str]) -> bool:
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in blocklist)


def _fetched_payload(candidate: ScoredCandidate, result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("status") or "failed")
    return {
        "url": candidate.url,
        "title": candidate.title,
        "domain": candidate.domain,
        "status": status,
        "error": str(result.get("error") or ""),
        "content_type": str(result.get("content_type") or ""),
        "text": _compact(str(result.get("text") or ""), 1600),
        "truncated": bool(result.get("truncated")),
    }


def _render_search_annotation(
    *,
    query: str,
    level: str,
    kept: list[ScoredCandidate],
    fetched: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
) -> str:
    lines = [
        "# Web Search Evidence",
        "",
        f"query: {query}",
        f"level: {level}",
        f"generated_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"kept_results: {len(kept)}",
        f"filtered_results: {len(filtered)}",
        "",
        "## Selected Results",
        "",
    ]
    fetched_by_url = {item.get("url"): item for item in fetched}
    for index, item in enumerate(kept, 1):
        lines.extend(
            [
                f"{index}. [{item.quality_tier} score={item.score:.2f} type={item.source_type}] {item.title}",
                f"   url: {item.url}",
                f"   domain: {item.domain}",
                f"   snippet: {_compact(item.snippet, 500)}",
                f"   reasons: {', '.join(item.reasons)}",
            ]
        )
        fetched_item = fetched_by_url.get(item.url)
        if fetched_item:
            if fetched_item.get("status") == "completed":
                lines.append(f"   fetched_excerpt: {_compact(str(fetched_item.get('text') or ''), 1000)}")
            else:
                lines.append(f"   fetch_status: {fetched_item.get('status')} {fetched_item.get('error')}")
        else:
            lines.append("   fetch_status: skipped_not_in_top_fetch_set")
        lines.append("")
    if filtered:
        lines.extend(["## Filtered Results", ""])
        for item in filtered[:20]:
            lines.append(f"- {item.get('reason')}: {item.get('domain')} {item.get('title')} {item.get('url')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _summary_line(
    *,
    query: str,
    level: str,
    kept: list[ScoredCandidate],
    fetched: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
) -> str:
    if not kept:
        return f"web.search found no usable results for {query!r}; filtered={len(filtered)}"
    top = kept[0]
    fetched_ok = len([item for item in fetched if item.get("status") == "completed"])
    return (
        f"web.search level={level} usable_results={len(kept)} fetched={fetched_ok} "
        f"top={top.title} ({top.domain}, score={top.score:.2f})"
    )


class _DuckDuckGoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_link = False
        self._in_snippet = False
        self._current: dict[str, str] = {}
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        css = attrs_dict.get("class", "")
        if tag == "a" and ("result__a" in css or "result-link" in css):
            href = attrs_dict.get("href", "")
            self._current = {"url": _normalize_result_url(href), "title": "", "snippet": ""}
            self._parts = []
            self._in_link = True
        elif "result__snippet" in css:
            self._parts = []
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            self._current["title"] = _clean_text(" ".join(self._parts))
            self._in_link = False
            self._parts = []
            if self._current.get("url") and self._current.get("title"):
                self.results.append(dict(self._current))
        elif self._in_snippet and tag in {"a", "div"}:
            snippet = _clean_text(" ".join(self._parts))
            if snippet and self.results:
                self.results[-1]["snippet"] = snippet
            self._in_snippet = False
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_link or self._in_snippet:
            self._parts.append(data)


class _HtmlTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.parts.append(data.strip())


def _normalize_result_url(url: str) -> str:
    text = html.unescape(str(url or "").strip())
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return ""
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        target = query.get("uddg", [""])[0]
        if target:
            return target
    if text.startswith("//"):
        return "https:" + text
    return text


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split("@")[-1]
    except ValueError:
        return ""
    host = host.split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _clean_text(value: Any) -> str:
    return _normalize_text(html.unescape(str(value or "")))


def _normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", str(text or ""))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def _html_to_text(raw_html: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(raw_html)
    return _normalize_text(" ".join(parser.parts))


def _terms(query: str) -> list[str]:
    terms = re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower())
    stop = {"what", "when", "where", "which", "with", "about", "latest", "current", "怎么", "什么", "如何", "现在"}
    return [term for term in terms if term not in stop][:12]


def _search_level(value: Any) -> str:
    text = str(value or "standard").strip().lower()
    if text in {"1", "low", "quick", "fast", "light", "minimal"}:
        return "light"
    if text in {"3", "high", "strong", "deep", "aggressive"}:
        return "deep"
    return "standard"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = int(default)
    return max(minimum, min(maximum, raw))


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    return match.group(1).strip("\"'") if match else ""


def _binary_content(content_type: str, raw: bytes) -> bool:
    lowered = content_type.lower()
    if lowered.startswith(("image/", "audio/", "video/")) or "application/pdf" in lowered:
        return True
    if b"\x00" in raw[:4096]:
        return True
    return False


def _requires_login_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _LOGIN_MARKERS)


def fetched_text_block_reason(text: str) -> str:
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return "login_or_paywall_detected"
    if any(marker in lowered for marker in _ANTI_BOT_MARKERS):
        return "anti_bot_or_verification_wall"
    if any(pattern.search(str(text or "")) for pattern in _HARD_FILTER_PATTERNS):
        return "unsafe_or_spam_content"
    return ""


def _compact(text: str, max_chars: int) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 1)].rstrip() + "..."


def _slug(value: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if raw:
        return raw[:80]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


_OFFICIAL_DOC_DOMAINS = {
    "docs.python.org",
    "developer.mozilla.org",
    "learn.microsoft.com",
    "support.google.com",
    "docs.github.com",
    "openai.com",
    "platform.openai.com",
    "react.dev",
    "nodejs.org",
    "typescriptlang.org",
}
_RESEARCH_DOMAINS = {"arxiv.org", "pubmed.ncbi.nlm.nih.gov", "nature.com", "science.org", "acm.org", "ieee.org"}
_CHINESE_COMMUNITY_DOMAINS = {"zhihu.com", "csdn.net", "xiaohongshu.com", "juejin.cn", "segmentfault.com"}
_AD_TRACKING_DOMAINS = {
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "taboola.com",
    "outbrain.com",
}
_LOGIN_MARKERS = (
    "login required",
    "sign in to continue",
    "please log in",
    "log in to continue",
    "sign up to continue",
    "register to continue",
    "subscribe to continue",
    "paywall",
    "登录后",
    "请登录",
    "登录以继续",
    "需要登录",
    "扫码登录",
    "打开app查看",
    "打开 app 查看",
    "app内打开",
    "在app内查看",
    "下载app查看",
    "关注后查看",
    "订阅后继续",
    "付费阅读",
)
_ANTI_BOT_MARKERS = (
    "enable javascript",
    "checking your browser",
    "captcha",
    "verify you are human",
    "验证你是真人",
    "安全验证",
    "访问过于频繁",
    "请完成验证",
)
_HARD_FILTER_PATTERNS = (
    re.compile(r"\b(ad|ads|advertorial|sponsored|casino|gambling|betting|porn|xxx|escort)\b", re.I),
    re.compile(r"(博彩|赌博|现金网|色情|成人视频|约炮|外挂|破解版|广告推广)"),
)
_FRESHNESS_MARKER_RE = re.compile(
    r"\b(202[4-9]|latest|current|release|changelog|today|recent)\b|最新|当前|现在|今天|更新|版本",
    re.I,
)
