from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest
from app.personal_wechat_bot.tools.web.http_safety import (
    HttpContentEncodingError,
    PublicHttpUrlError,
    decode_http_content,
    guarded_urlopen,
    read_response_with_deadline,
)


SEARCH_LEVELS: dict[str, dict[str, Any]] = {
    "light": {
        "max_results": 5,
        "fetch_top": 1,
        "timeout_seconds": 4.5,
        "total_timeout_seconds": 6.0,
        "max_workers": 3,
        "query_variants": 1,
        "min_score": 0.42,
        "min_relevance": 0.18,
        "fetch_min_score": 0.50,
    },
    "standard": {
        "max_results": 8,
        "fetch_top": 3,
        "timeout_seconds": 7.0,
        "total_timeout_seconds": 10.0,
        "max_workers": 4,
        "query_variants": 2,
        "min_score": 0.36,
        "min_relevance": 0.14,
        "fetch_min_score": 0.44,
    },
    "deep": {
        "max_results": 12,
        "fetch_top": 6,
        "timeout_seconds": 10.0,
        "total_timeout_seconds": 15.0,
        "max_workers": 6,
        "query_variants": 3,
        "min_score": 0.30,
        "min_relevance": 0.08,
        "fetch_min_score": 0.36,
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
    intent_required: bool = False
    intent_matched: bool = False
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
            "intent_required": self.intent_required,
            "intent_matched": self.intent_matched,
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
        self.blocklist = [item.lower().strip() for item in (blocklist or []) if str(item).strip()]
        self.provider = provider or FallbackSearchProvider(
            BingRssSearchProvider(),
            DuckDuckGoHtmlSearchProvider(),
            blocklist=self.blocklist,
        )
        self.fetcher = fetcher or SimplePageFetcher()
        self.max_fetch_bytes = max_fetch_bytes

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        raw_query = str(request.arguments.get("query") or request.arguments.get("q") or "").strip()
        if not raw_query:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="web.search requires a query",
                error="missing_query",
            )
        query, redacted_fields = sanitize_search_query(raw_query)
        if not query or not _terms(query):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="web.search query contains no safe public search terms",
                error="sensitive_or_empty_query",
                payload={"redacted_fields": redacted_fields},
            )
        level = _search_level(request.arguments.get("level") or request.arguments.get("strength"))
        level_spec = dict(SEARCH_LEVELS[level])
        max_results = _bounded_int(request.arguments.get("max_results"), level_spec["max_results"], 1, 20)
        fetch_top = _bounded_int(request.arguments.get("fetch_top"), level_spec["fetch_top"], 0, max_results)
        timeout_seconds = float(level_spec["timeout_seconds"])
        total_deadline = time.monotonic() + float(level_spec["total_timeout_seconds"])

        query_variants = _query_variants(
            query,
            level=level,
            limit=int(level_spec["query_variants"]),
        )
        scoring_query = _expand_query_aliases(query)
        raw_candidates, search_runs = self._search_queries(
            query_variants,
            max_results=max_results * 3,
            timeout_seconds=timeout_seconds,
            quality_min_score=float(level_spec["fetch_min_score"]),
            quality_min_relevance=float(level_spec["min_relevance"]),
        )
        if not raw_candidates and any(item.get("status") == "failed" for item in search_runs):
            errors = "; ".join(str(item.get("error") or "") for item in search_runs if item.get("error"))
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary="web.search provider failed",
                error=errors or "all_search_queries_failed",
                payload={"query": query, "level": level, "search_runs": search_runs},
            )

        scored, filtered = score_and_filter_candidates(scoring_query, raw_candidates, blocklist=self.blocklist)
        scored, low_score_filtered = _drop_low_score(
            scored,
            minimum_score=float(level_spec["min_score"]),
            minimum_relevance=float(level_spec["min_relevance"]),
        )
        filtered.extend(low_score_filtered)
        scored = _intent_primary_first(scored)
        kept = scored[:max_results]
        fetch_candidates = _fetch_candidates(
            kept,
            fetch_top=fetch_top,
            minimum_score=float(level_spec["fetch_min_score"]),
            include_reserves=True,
        )
        fetch_timeout = max(0.2, total_deadline - time.monotonic())
        fetched = self._fetch_top(
            fetch_candidates,
            query=scoring_query,
            target_successes=fetch_top,
            timeout_seconds=fetch_timeout,
            max_workers=int(level_spec["max_workers"]),
        )
        evidence = _evidence_summary(kept, fetched)
        annotation_text = _render_search_annotation(
            query=query,
            level=level,
            kept=kept,
            fetched=fetched,
            filtered=filtered,
            evidence=evidence,
            search_runs=search_runs,
        )
        output = self.output_dir / f"{_slug(request.call_id or query)}.md"
        output.write_text(annotation_text, encoding="utf-8")
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        summary = _summary_line(query=query, level=level, kept=kept, fetched=fetched, filtered=filtered)
        fetched_count = int(evidence["readable_source_count"])
        completed = fetched_count > 0
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed" if completed else "blocked",
            summary=summary,
            output_refs=[str(output)],
            error=None if completed else ("no_readable_pages_after_filter" if kept else "no_search_results_after_filter"),
            payload={
                "file_id": file_id,
                "query": query,
                "query_redacted": bool(redacted_fields),
                "redacted_fields": redacted_fields,
                "level": level,
                "generated_at": evidence["generated_at"],
                "result_count": len(kept),
                "fetched_count": fetched_count,
                "results": [item.to_dict() for item in kept],
                "fetched": fetched,
                "filtered": filtered[:30],
                "evidence": evidence,
                "search_runs": search_runs,
                "annotation_text": annotation_text,
            },
        )

    def _search_queries(
        self,
        queries: list[str],
        *,
        max_results: int,
        timeout_seconds: float,
        quality_min_score: float,
        quality_min_relevance: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not queries:
            return [], []
        results_by_query: dict[str, list[dict[str, Any]]] = {}
        runs_by_query: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(3, len(queries))) as pool:
            futures = {}
            for query in queries:
                search_kwargs: dict[str, Any] = {
                    "max_results": max_results,
                    "timeout_seconds": timeout_seconds,
                }
                if (
                    isinstance(self.provider, FallbackSearchProvider)
                    and type(self.provider).search is FallbackSearchProvider.search
                ):
                    search_kwargs.update(
                        {
                            "quality_min_score": quality_min_score,
                            "quality_min_relevance": quality_min_relevance,
                            "blocklist": self.blocklist,
                        }
                    )
                    search_method = self.provider._search_with_quality
                else:
                    search_method = self.provider.search
                futures[pool.submit(search_method, query, **search_kwargs)] = query
            for future in as_completed(futures):
                query = futures[future]
                try:
                    found = [dict(item) for item in future.result() if isinstance(item, dict)]
                    results_by_query[query] = found
                    runs_by_query[query] = {"query": query, "status": "completed", "result_count": len(found)}
                except Exception as exc:
                    results_by_query[query] = []
                    runs_by_query[query] = {
                        "query": query,
                        "status": "failed",
                        "result_count": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        combined: list[dict[str, Any]] = []
        for query in queries:
            for item in results_by_query.get(query, []):
                combined.append({**item, "search_query": query})
        return combined, [runs_by_query[query] for query in queries]

    def _fetch_top(
        self,
        candidates: list[ScoredCandidate],
        *,
        query: str,
        target_successes: int,
        timeout_seconds: float,
        max_workers: int,
    ) -> list[dict[str, Any]]:
        if not candidates or target_successes <= 0:
            return []
        fetched: list[dict[str, Any]] = []
        remaining = list(candidates)
        success_count = 0
        deadline = time.monotonic() + max(0.2, timeout_seconds)
        while remaining and success_count < target_successes:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0.2:
                break
            needed = max(1, target_successes - success_count)
            primary = [item for item in remaining if not item.intent_required or item.intent_matched]
            candidate_pool = primary or remaining
            batch_size = min(len(candidate_pool), max(1, min(max_workers, needed)))
            batch = candidate_pool[:batch_size]
            selected_urls = {item.url for item in batch}
            remaining = [item for item in remaining if item.url not in selected_urls]
            batch_by_url: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=batch_size) as pool:
                futures = {
                    pool.submit(
                        self.fetcher.fetch,
                        candidate.url,
                        timeout_seconds=remaining_seconds,
                        max_bytes=self.max_fetch_bytes,
                    ): candidate
                    for candidate in batch
                }
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                    batch_by_url[candidate.url] = _fetched_payload(candidate, result, query=query)
            ordered = [
                batch_by_url.get(candidate.url, _fetched_payload(candidate, {"status": "failed"}, query=query))
                for candidate in batch
            ]
            fetched.extend(ordered)
            success_count += sum(1 for item in ordered if item.get("status") == "completed")
        return fetched


class FallbackSearchProvider:
    """Fan out to lightweight providers, then merge/filter/rank their leads."""

    def __init__(self, *providers: SearchProvider, blocklist: list[str] | None = None):
        self.providers = [provider for provider in providers if provider is not None]
        self.blocklist = [item.lower().strip() for item in (blocklist or []) if str(item).strip()]

    def search(self, query: str, *, max_results: int, timeout_seconds: float) -> list[dict[str, Any]]:
        return self._search_with_quality(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
        )

    def _search_with_quality(
        self,
        query: str,
        *,
        max_results: int,
        timeout_seconds: float,
        quality_min_score: float = 0.36,
        quality_min_relevance: float = 0.14,
        blocklist: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        errors: list[str] = []
        effective_blocklist = _unique_keep_order([*self.blocklist, *(blocklist or [])])
        scoring_query = _expand_query_aliases(query)
        if not self.providers:
            return []

        # Search providers run in parallel. The level-specific thresholds are
        # applied by WebSearchTool after all leads are merged, so a weak first
        # source cannot suppress a stronger result from another source.
        del quality_min_score, quality_min_relevance
        provider_results: dict[int, list[dict[str, Any]]] = {}
        provider_timeout = max(0.2, float(timeout_seconds))
        with ThreadPoolExecutor(max_workers=len(self.providers)) as pool:
            futures = {
                pool.submit(
                    provider.search,
                    query,
                    max_results=max_results,
                    timeout_seconds=provider_timeout,
                ): index
                for index, provider in enumerate(self.providers)
            }
            try:
                completed = as_completed(futures, timeout=provider_timeout)
                for future in completed:
                    index = futures[future]
                    try:
                        provider_results[index] = [
                            dict(item) for item in (future.result() or []) if isinstance(item, dict)
                        ]
                    except Exception as exc:
                        provider_results[index] = []
                        provider = self.providers[index]
                        errors.append(f"{type(provider).__name__}:{type(exc).__name__}:{exc}")
            except TimeoutError:
                for future, index in futures.items():
                    if not future.done():
                        future.cancel()
                        provider_results[index] = []
                        provider = self.providers[index]
                        errors.append(f"{type(provider).__name__}:TimeoutError:provider deadline exceeded")

        all_results: list[dict[str, Any]] = []
        for index in range(len(self.providers)):
            all_results.extend(provider_results.get(index, []))
        if all_results:
            # score_and_filter_candidates deliberately filters before scoring;
            # this is the single cross-provider ranking point.
            scored, _filtered = score_and_filter_candidates(
                scoring_query,
                all_results,
                blocklist=effective_blocklist,
            )
            return _scored_results_as_raw(scored, all_results)[:max_results]
        if errors:
            raise RuntimeError(" | ".join(errors))
        return []


class BingRssSearchProvider:
    """Parse Bing's small RSS result feed; snippets remain untrusted leads."""

    def search(self, query: str, *, max_results: int, timeout_seconds: float) -> list[dict[str, Any]]:
        language = "zh-CN" if re.search(r"[\u4e00-\u9fff]", query) else "en-US"
        url = "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "format": "rss", "setlang": language}
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "wechat-agent-local/0.1",
                "Accept": "application/rss+xml,application/xml,text/xml",
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
            content_encoding = response.headers.get("content-encoding", "")
        raw, _truncated = decode_http_content(
            raw,
            content_encoding=content_encoding,
            max_bytes=1024 * 1024,
        )
        root = ET.fromstring(raw.decode(charset, errors="replace"))
        results: list[dict[str, Any]] = []
        for item in root.findall(".//item"):
            title = _clean_text(item.findtext("title") or "")
            link = _normalize_result_url(item.findtext("link") or "")
            snippet = _html_to_text(item.findtext("description") or "")
            if not title or not link:
                continue
            results.append(
                {
                    "title": title,
                    "url": link,
                    "snippet": snippet,
                    # Bing's pubDate is an index/feed signal, not a page publication date.
                    "index_date": _clean_text(item.findtext("pubDate") or ""),
                    "provider": "bing_rss",
                }
            )
            if len(results) >= max_results:
                break
        return results


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
            content_encoding = response.headers.get("content-encoding", "")
        raw, _truncated = decode_http_content(
            raw,
            content_encoding=content_encoding,
            max_bytes=1024 * 1024,
        )
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
                content_encoding = response.headers.get("content-encoding", "")
                raw = read_response_with_deadline(
                    response,
                    max_bytes=max_bytes + 1,
                    deadline=deadline,
                    truncate=True,
                )
        except PublicHttpUrlError as exc:
            return {"status": "blocked", "error": str(exc)}
        except HttpContentEncodingError as exc:
            return {"status": "blocked", "error": str(exc)}
        except (UnicodeError, ValueError):
            return {"status": "blocked", "error": "invalid_url"}
        wire_truncated = len(raw) > max_bytes
        try:
            raw, decoded_truncated = decode_http_content(
                raw,
                content_encoding=content_encoding,
                max_bytes=max_bytes,
            )
        except HttpContentEncodingError as exc:
            return {"status": "blocked", "error": str(exc), "content_type": content_type}
        truncated = wire_truncated or decoded_truncated
        if _binary_content(content_type, raw):
            return {"status": "blocked", "error": "binary_or_file_content", "content_type": content_type}
        charset = _charset(content_type) or "utf-8"
        text = raw.decode(charset, errors="replace")
        if "html" in content_type.lower() or "<html" in text[:1000].lower():
            text = _html_to_text(text)
        else:
            text = _normalize_text(text)
        text, extraction_warnings = _neutralize_conflicting_live_state(text)
        block_reason = fetched_text_block_reason(text)
        if block_reason:
            return {"status": "blocked", "error": block_reason, "content_type": content_type}
        return {
            "status": "completed",
            "content_type": content_type,
            "text": _compact(text, 1800),
            "truncated": truncated,
            "warnings": extraction_warnings,
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


def _scored_results_as_raw(
    scored: list[ScoredCandidate],
    raw_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_by_url: dict[str, dict[str, Any]] = {}
    for raw in raw_results:
        url = _normalize_result_url(str(raw.get("url") or ""))
        if url and url not in raw_by_url:
            raw_by_url[url] = dict(raw)
    ranked: list[dict[str, Any]] = []
    for item in scored:
        raw = dict(raw_by_url.get(item.url) or {})
        raw.update({"title": item.title, "url": item.url, "snippet": item.snippet})
        ranked.append(raw)
    return ranked


def _drop_low_score(
    candidates: list[ScoredCandidate],
    *,
    minimum_score: float,
    minimum_relevance: float = 0.0,
) -> tuple[list[ScoredCandidate], list[dict[str, Any]]]:
    kept: list[ScoredCandidate] = []
    filtered: list[dict[str, Any]] = []
    for item in candidates:
        if item.score >= minimum_score and item.relevance_score >= minimum_relevance:
            kept.append(item)
            continue
        filter_dimension = "relevance" if item.relevance_score < minimum_relevance else "combined_score"
        filtered.append(
            {
                "title": item.title,
                "url": item.url,
                "domain": item.domain,
                "reason": "low_relevance_or_quality",
                "filter_dimension": filter_dimension,
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
    include_reserves: bool = False,
) -> list[ScoredCandidate]:
    if fetch_top <= 0:
        return []
    eligible = [item for item in candidates if item.score >= minimum_score]
    diverse = _intent_primary_first(eligible, diversify=True)
    return diverse if include_reserves else diverse[:fetch_top]


def _intent_primary_first(
    candidates: list[ScoredCandidate],
    *,
    diversify: bool = False,
) -> list[ScoredCandidate]:
    primary = [item for item in candidates if not item.intent_required or item.intent_matched]
    reserves = [item for item in candidates if item.intent_required and not item.intent_matched]
    if diversify:
        primary = _diversify_candidates(primary)
        reserves = _diversify_candidates(reserves)
    return [*primary, *reserves]


def _diversify_candidates(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Prefer independent domains before taking a second page from one site."""

    first_by_domain: list[ScoredCandidate] = []
    repeated: list[ScoredCandidate] = []
    seen: set[str] = set()
    for item in candidates:
        if item.domain not in seen:
            first_by_domain.append(item)
            seen.add(item.domain)
        else:
            repeated.append(item)
    return [*first_by_domain, *repeated]


def _score_candidate(query: str, *, title: str, url: str, domain: str, snippet: str) -> ScoredCandidate:
    authority_score, source_type, authority_reasons = _authority_score(domain, url)
    relevance_score, relevance_reasons = _relevance_score(query, title, snippet, url)
    query_intents = _matched_intent_groups(query)
    result_intents = _matched_intent_groups(f"{title} {snippet} {url}")
    matched_intents = query_intents.intersection(result_intents)
    intent_reasons = [f"intent_required={','.join(sorted(query_intents))}"] if query_intents else []
    if query_intents:
        intent_reasons.append(
            f"intent_match={','.join(sorted(matched_intents))}" if matched_intents else "intent_miss"
        )
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
        intent_required=bool(query_intents),
        intent_matched=bool(matched_intents),
        reasons=[*authority_reasons, *relevance_reasons, *intent_reasons, *quality_reasons][:10],
    )


def _authority_score(domain: str, url: str) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    score = 0.18
    source_type = "web"
    if _is_government_domain(domain):
        score = 0.98
        source_type = "government"
        reasons.append("government_domain")
    elif _is_education_domain(domain):
        score = 0.82
        source_type = "education"
        reasons.append("education_domain")
    elif _domain_in(domain, _OFFICIAL_DOC_DOMAINS):
        score = 0.86
        source_type = "official_docs"
        reasons.append("official_docs")
    elif _domain_in(domain, _STANDARDS_DOMAINS):
        score = 0.88
        source_type = "standards_body"
        reasons.append("standards_body")
    elif _domain_in(domain, _RESEARCH_DOMAINS) or _is_academic_domain(domain):
        score = 0.80
        source_type = "research"
        reasons.append("research_source")
    elif _domain_in(domain, _SOURCE_REPOSITORY_DOMAINS):
        score = 0.64
        source_type = "source_repository"
        reasons.append("source_repository")
    elif _domain_in(domain, _ESTABLISHED_NEWS_DOMAINS):
        score = 0.70
        source_type = "established_news"
        reasons.append("established_news")
    elif _domain_in(domain, _REFERENCE_DOMAINS):
        score = 0.66
        source_type = "reference"
        reasons.append("reference_source")
    elif _domain_in(domain, _COMMUNITY_DOMAINS):
        score = 0.46
        source_type = "community"
        reasons.append("community_platform")
    elif any(marker in url.lower() for marker in ("/docs/", "/documentation/", "/reference/")):
        score = 0.34
        source_type = "unverified_documentation"
        reasons.append("documentation_path_unverified")
    if not domain.endswith(".cn") and source_type in {
        "official_docs",
        "standards_body",
        "research",
        "source_repository",
        "established_news",
        "reference",
        "community",
    }:
        score = min(1.0, score + 0.03)
        reasons.append("non_cn_source")
    return score, source_type, reasons


def _relevance_score(query: str, title: str, snippet: str, url: str) -> tuple[float, list[str]]:
    query_terms = _terms(query)
    haystack = f"{title} {snippet} {url}".lower()
    if not query_terms:
        return 0.1, []
    hits = sum(1 for term in query_terms if _term_matches(term, haystack))
    denominator = min(4, len(query_terms))
    score = min(1.0, hits / max(1, denominator))
    reasons = [f"query_term_hits={hits}/{len(query_terms)}", f"relevance_denominator={denominator}"]
    if _FRESHNESS_MARKER_RE.search(haystack):
        score = min(1.0, score + 0.14)
        reasons.append("freshness_marker")
    return score, reasons


def _quality_adjustment(domain: str, title: str, snippet: str, url: str) -> tuple[float, list[str]]:
    text = f"{domain} {title} {snippet} {url}".lower()
    reasons: list[str] = []
    adjustment = 0.0
    if any(marker in text for marker in ("whitepaper", "specification", "technical standard", "白皮书", "技术标准")):
        adjustment += 0.06
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
    if parsed.username is not None or parsed.password is not None:
        return "url_credentials_not_allowed"
    if not domain or not title:
        return "missing_domain_or_title"
    if _domain_blocked(domain, blocklist):
        return "operator_blocklist"
    text = f"{domain} {parsed.path} {title} {snippet}".lower()
    if any(pattern.search(text) for pattern in _HARD_FILTER_PATTERNS):
        return "unsafe_or_spam_content"
    if any(marker in text for marker in _LOGIN_MARKERS):
        return "login_or_paywall_required"
    if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in _AD_TRACKING_DOMAINS):
        return "ad_or_tracking_domain"
    if any(marker in parsed.path.lower() for marker in _AD_PATH_MARKERS):
        return "ad_or_affiliate_page"
    if _SEARCH_REDIRECT_RE.search(parsed.path.lower()):
        return "search_redirect_or_cached_page"
    return ""


def _domain_blocked(domain: str, blocklist: list[str]) -> bool:
    return any(domain == blocked or domain.endswith(f".{blocked}") for blocked in blocklist)


def _fetched_payload(
    candidate: ScoredCandidate,
    result: dict[str, Any],
    *,
    query: str,
) -> dict[str, Any]:
    status = str(result.get("status") or "failed")
    text = _compact(str(result.get("text") or ""), 1800)
    error = str(result.get("error") or "")
    content_relevance = 0.0
    if status == "completed":
        block_reason = fetched_text_block_reason(text)
        content_relevance, _reasons = _relevance_score(query, "", text, "")
        if block_reason:
            status = "blocked"
            error = block_reason
            text = ""
        elif _matched_intent_groups(query) and not _matched_intent_groups(query).intersection(
            _matched_intent_groups(text)
        ):
            status = "blocked"
            error = "fetched_content_intent_mismatch"
            text = ""
        elif content_relevance <= 0.0:
            status = "blocked"
            error = "fetched_content_unrelated"
            text = ""
    return {
        "url": candidate.url,
        "title": candidate.title,
        "domain": candidate.domain,
        "score": round(candidate.score, 4),
        "authority_score": round(candidate.authority_score, 4),
        "source_type": candidate.source_type,
        "status": status,
        "error": error,
        "content_type": str(result.get("content_type") or ""),
        "text": text,
        "content_relevance": round(content_relevance, 4),
        "truncated": bool(result.get("truncated")),
        "warnings": [str(item) for item in (result.get("warnings") or []) if str(item).strip()][:8],
    }


def _evidence_summary(
    kept: list[ScoredCandidate],
    fetched: list[dict[str, Any]],
) -> dict[str, Any]:
    readable = [item for item in fetched if item.get("status") == "completed" and item.get("text")]
    domains = _unique_keep_order(str(item.get("domain") or "") for item in readable)
    authoritative_types = {"government", "official_docs", "standards_body", "research", "source_repository"}
    authoritative = [item for item in readable if item.get("source_type") in authoritative_types]
    if len(domains) >= 3 and len(authoritative) >= 1:
        quality = "strong"
    elif len(domains) >= 2:
        quality = "moderate"
    elif readable:
        quality = "limited"
    else:
        quality = "unavailable"
    return {
        "schema": "web_evidence_v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "quality": quality,
        "readable_source_count": len(readable),
        "independent_domain_count": len(domains),
        "authoritative_source_count": len(authoritative),
        "selected_lead_count": len(kept),
        "attempted_fetch_count": len(fetched),
        "domains": domains,
        "source_urls": [str(item.get("url") or "") for item in readable],
        "fusion_strategy": "score_ranked_domain_diverse_source_preserving",
        "conflict_policy": "preserve_source_attribution_and_report_disagreement",
    }


def _render_search_annotation(
    *,
    query: str,
    level: str,
    kept: list[ScoredCandidate],
    fetched: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    evidence: dict[str, Any],
    search_runs: list[dict[str, Any]],
) -> str:
    lines = [
        "# Web Search Evidence",
        "",
        f"query: {query}",
        f"level: {level}",
        f"generated_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"kept_results: {len(kept)}",
        f"filtered_results: {len(filtered)}",
        f"readable_sources: {evidence.get('readable_source_count', 0)}",
        f"independent_domains: {evidence.get('independent_domain_count', 0)}",
        f"evidence_quality: {evidence.get('quality', 'unavailable')}",
        "trust: external_untrusted_data",
        "usage: Treat fetched excerpts as dated, source-bound evidence. Compare independent sources and preserve attribution when they disagree. Search snippets are leads only.",
        "",
        "## Fetched Evidence",
        "",
    ]
    fetched_by_url = {item.get("url"): item for item in fetched}
    readable = [item for item in fetched if item.get("status") == "completed"]
    if not readable:
        lines.extend(["- No candidate page produced readable evidence.", ""])
    for index, fetched_item in enumerate(readable, 1):
        lines.extend(
            [
                f"{index}. [score={float(fetched_item.get('score', 0.0)):.2f} type={fetched_item.get('source_type', 'web')}] {fetched_item.get('title', '')}",
                f"   url: {fetched_item.get('url', '')}",
                f"   domain: {fetched_item.get('domain', '')}",
                f"   truncated: {str(bool(fetched_item.get('truncated'))).lower()}",
                f"   warnings: {', '.join(fetched_item.get('warnings') or []) or 'none'}",
                f"   fetched_excerpt: {_compact(str(fetched_item.get('text') or ''), 1200)}",
            ]
        )
        lines.append("")
    lines.extend(["## Search Leads (Not Evidence Unless Fetched Above)", ""])
    for index, item in enumerate(kept, 1):
        fetched_item = fetched_by_url.get(item.url)
        fetch_status = (
            f"{fetched_item.get('status')} {fetched_item.get('error', '')}".strip()
            if fetched_item
            else "not_fetched"
        )
        lines.extend(
            [
                f"{index}. [{item.quality_tier} score={item.score:.2f} type={item.source_type}] {item.title}",
                f"   url: {item.url}",
                f"   snippet_unverified: {_compact(item.snippet, 500)}",
                f"   fetch_status: {fetch_status}",
                f"   reasons: {', '.join(item.reasons)}",
                "",
            ]
        )
    if search_runs:
        lines.extend(["## Search Runs", ""])
        for item in search_runs:
            lines.append(
                f"- status={item.get('status')} results={item.get('result_count', 0)} query={item.get('query', '')}"
            )
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
    if fetched_ok == 0:
        return (
            f"web.search level={level} found {len(kept)} leads but no readable evidence pages; "
            f"top={top.title} ({top.domain}, score={top.score:.2f})"
        )
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
        if tag.lower() in {"script", "style", "noscript", "svg", "iframe", "template", "form", "nav", "footer"}:
            self.skip_depth += 1
        if tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "iframe", "template", "form", "nav", "footer"} and self.skip_depth:
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
            text = target
    if text.startswith("//"):
        text = "https:" + text
    try:
        parsed = urllib.parse.urlsplit(text)
        if parsed.username is not None or parsed.password is not None:
            return ""
        query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        clean_query = urllib.parse.urlencode(
            [
                (key, value)
                for key, value in query_items
                if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
            ],
            doseq=True,
        )
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), host, parsed.path or "/", clean_query, "")
        )
    except (UnicodeError, ValueError):
        return ""


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
    lowered = str(query or "").lower()
    ascii_terms = re.findall(r"[a-z0-9][a-z0-9_.+-]{1,}", lowered)
    for stop in _CJK_QUERY_STOP_PHRASES:
        lowered = lowered.replace(stop, " ")
    cjk_terms: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) <= 4:
            cjk_terms.append(chunk)
            continue
        cjk_terms.extend(chunk[index : index + 2] for index in range(0, len(chunk) - 1, 2))
        if len(chunk) % 2:
            cjk_terms.append(chunk[-2:])
    stop = {"what", "when", "where", "which", "with", "about", "latest", "current", "today", "please"}
    return _unique_keep_order(term for term in [*ascii_terms, *cjk_terms] if term not in stop)[:16]


def _term_matches(term: str, text: str) -> bool:
    if re.fullmatch(r"[a-z0-9_.+-]+", term, re.I):
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text, re.I))
    return term in text


def _matched_intent_groups(text: str) -> set[str]:
    normalized = str(text or "").lower()
    matched: set[str] = set()
    for group, markers in _INTENT_TERM_GROUPS.items():
        if any(_term_matches(marker.lower(), normalized) for marker in markers):
            matched.add(group)
    return matched


def _query_variants(query: str, *, level: str, limit: int) -> list[str]:
    base = re.sub(r"\s+", " ", str(query or "")).strip()
    if not base or limit <= 1:
        return [base] if base else []
    is_cjk = bool(re.search(r"[\u4e00-\u9fff]", base))
    variants = [base]
    expanded = _expand_query_aliases(base)
    alias_only = _query_alias_text(base)
    authority_query = alias_only or expanded
    variants.append(f"{authority_query} {'官方 资料 official source' if is_cjk else 'official source'}")
    if level == "deep":
        variants.append(f"{base} {'研究 报告 数据' if is_cjk else 'research report data'}")
    return _unique_keep_order(variants)[: max(1, limit)]


def _expand_query_aliases(query: str) -> str:
    aliases = _query_alias_text(query)
    return " ".join([str(query or "").strip(), aliases]).strip()


def _query_alias_text(query: str) -> str:
    aliases = [alias for marker, alias in _CROSS_LANGUAGE_QUERY_ALIASES if marker in query]
    return " ".join(aliases).strip()


def _unique_keep_order(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _domain_in(domain: str, domains: set[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in domains)


def _is_government_domain(domain: str) -> bool:
    return bool(_GOVERNMENT_DOMAIN_RE.search(domain)) or _domain_in(domain, _EXPLICIT_GOVERNMENT_DOMAINS)


def _is_education_domain(domain: str) -> bool:
    return bool(_EDUCATION_DOMAIN_RE.search(domain))


def _is_academic_domain(domain: str) -> bool:
    return bool(_ACADEMIC_DOMAIN_RE.search(domain))


def _search_level(value: Any) -> str:
    text = str(value or "standard").strip().lower()
    if text in {"1", "low", "quick", "fast", "light", "minimal"}:
        return "light"
    if text in {"3", "high", "strong", "deep", "aggressive"}:
        return "deep"
    return "standard"


def sanitize_search_query(value: str) -> tuple[str, list[str]]:
    query = str(value or "")[:500]
    redacted: list[str] = []
    for label, pattern in _SENSITIVE_QUERY_PATTERNS:
        query, count = pattern.subn(" ", query)
        if count:
            redacted.append(label)
    query = re.sub(r"\s+", " ", query).strip(" ：:，,。?\t\r\n")
    return query[:240], redacted


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
    normalized = _normalize_text(str(text or ""))
    lowered = normalized.lower()
    if len(normalized) < 8:
        return "empty_or_thin_content"
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return "login_or_paywall_detected"
    if any(marker in lowered for marker in _ANTI_BOT_MARKERS):
        return "anti_bot_or_verification_wall"
    if _SOFT_ERROR_RE.search(normalized) and len(normalized) < 800:
        return "error_or_access_denied_page"
    if _PROMPT_INJECTION_RE.search(normalized):
        return "prompt_injection_content"
    if any(pattern.search(normalized) for pattern in _HARD_FILTER_PATTERNS):
        return "unsafe_or_spam_content"
    return ""


def _neutralize_conflicting_live_state(text: str) -> tuple[str, list[str]]:
    normalized = _normalize_text(text)
    pairs = (
        (r"\b(?:now|currently)\s+open\b", r"\b(?:now|currently)\s+closed\b"),
        (r"营业中|正在营业", r"已打烊|停止营业"),
        (r"开放中|正在开放", r"已关闭|暂停开放"),
    )
    for open_pattern, closed_pattern in pairs:
        if not re.search(open_pattern, normalized, re.I) or not re.search(closed_pattern, normalized, re.I):
            continue
        cleaned = re.sub(open_pattern, "", normalized, flags=re.I)
        cleaned = re.sub(closed_pattern, "", cleaned, flags=re.I)
        warning = (
            "[extraction_warning: live_state_conflict; the page contained both open and closed state placeholders. "
            "Do not infer the current live status.]"
        )
        return _normalize_text(f"{warning}\n{cleaned}"), ["live_state_conflict"]
    return normalized, []


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
    "python.org",
    "docs.python.org",
    "developer.mozilla.org",
    "learn.microsoft.com",
    "support.microsoft.com",
    "support.google.com",
    "developers.google.com",
    "docs.github.com",
    "openai.com",
    "platform.openai.com",
    "help.openai.com",
    "react.dev",
    "nodejs.org",
    "typescriptlang.org",
    "docs.oracle.com",
    "docs.aws.amazon.com",
    "cloud.google.com",
    "kubernetes.io",
    "pytorch.org",
    "tensorflow.org",
    "museivaticani.va",
    "basilicasanpietro.va",
    "turismoroma.it",
}
_STANDARDS_DOMAINS = {
    "w3.org",
    "ietf.org",
    "rfc-editor.org",
    "nist.gov",
    "iso.org",
    "itu.int",
    "unicode.org",
}
_RESEARCH_DOMAINS = {
    "arxiv.org",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "nature.com",
    "science.org",
    "acm.org",
    "ieee.org",
    "springer.com",
    "sciencedirect.com",
    "thelancet.com",
    "nejm.org",
}
_SOURCE_REPOSITORY_DOMAINS = {"github.com", "gitlab.com", "codeberg.org", "gitee.com"}
_ESTABLISHED_NEWS_DOMAINS = {
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "ft.com",
    "economist.com",
    "caixin.com",
    "thepaper.cn",
    "xinhuanet.com",
    "people.com.cn",
}
_REFERENCE_DOMAINS = {
    "wikipedia.org",
    "britannica.com",
    "worldbank.org",
    "who.int",
    "un.org",
}
_COMMUNITY_DOMAINS = {
    "zhihu.com",
    "csdn.net",
    "xiaohongshu.com",
    "juejin.cn",
    "segmentfault.com",
    "stackoverflow.com",
    "stackexchange.com",
    "reddit.com",
}
_EXPLICIT_GOVERNMENT_DOMAINS = {
    "gov.cn",
    "europa.eu",
    "gc.ca",
    "gouv.fr",
    "bund.de",
    "vatican.va",
}
_AD_TRACKING_DOMAINS = {
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "taboola.com",
    "outbrain.com",
    "adnxs.com",
    "criteo.com",
    "clickbank.net",
}
_AD_PATH_MARKERS = ("/sponsored/", "/advertorial/", "/affiliate/", "/coupon/", "/promo/")
_TRACKING_QUERY_KEYS = {"gclid", "fbclid", "msclkid", "yclid", "mc_cid", "mc_eid", "ref", "referrer"}
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
    re.compile(r"\b(advertorial|sponsored\s+(?:post|content|result)|casino|gambling|betting|porn|xxx|escort)\b", re.I),
    re.compile(r"(博彩|赌博|现金网|色情|成人视频|约炮|外挂|破解版|广告推广|赞助内容|推广软文)"),
)
_SEARCH_REDIRECT_RE = re.compile(r"/(?:search|redirect|out|click)(?:/|$)", re.I)
_GOVERNMENT_DOMAIN_RE = re.compile(r"(?:^|\.)gov(?:\.[a-z]{2})?$", re.I)
_EDUCATION_DOMAIN_RE = re.compile(r"(?:^|\.)edu(?:\.[a-z]{2})?$", re.I)
_ACADEMIC_DOMAIN_RE = re.compile(r"(?:^|\.)ac\.(?:uk|cn|jp|kr|nz|za)$", re.I)
_CJK_QUERY_STOP_PHRASES = (
    "能不能",
    "可以吗",
    "帮我",
    "麻烦",
    "请问",
    "查一下",
    "搜一下",
    "搜索",
    "检索",
    "核实",
    "查证",
    "是什么",
    "是不是",
    "为什么",
    "怎么样",
    "怎么",
    "如何",
    "哪些",
    "哪个",
    "多少",
    "现在",
    "当前",
    "最新",
    "今天",
    "今日",
    "请",
    "吗",
    "呢",
)
_SENSITIVE_QUERY_PATTERNS = (
    ("api_key", re.compile(r"\b(?:sk|rk|pk|api)[-_][A-Za-z0-9_-]{12,}\b", re.I)),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("wechat_id", re.compile(r"\bwxid_[A-Za-z0-9_-]{5,}\b", re.I)),
    ("windows_path", re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")),
)
_INTENT_TERM_GROUPS: dict[str, tuple[str, ...]] = {
    "attractions": (
        "景点",
        "好玩",
        "参观",
        "游览",
        "旅游",
        "attraction",
        "attractions",
        "sightseeing",
        "things to do",
        "visit",
        "visitor",
        "museum",
        "museums",
        "basilica",
        "cathedral",
    ),
    "hours": (
        "开放时间",
        "营业时间",
        "开放",
        "营业",
        "opening hours",
        "hours",
        "timetable",
        "schedule",
    ),
    "booking": (
        "预约",
        "预订",
        "门票",
        "购票",
        "booking",
        "reservation",
        "ticket",
        "tickets",
        "admission",
    ),
    "price": ("价格", "票价", "费用", "多少钱", "price", "pricing", "cost", "fee", "fees", "free"),
    "release": ("最新", "版本", "发布", "更新", "latest", "current", "version", "release", "changelog"),
    "news": ("新闻", "消息", "动态", "news", "announcement", "announced"),
    "law_policy": ("政策", "法规", "法律", "条例", "policy", "regulation", "law", "legal"),
    "medical": ("医学", "药物", "诊疗", "治疗", "medical", "medicine", "drug", "treatment", "guideline"),
}
_CROSS_LANGUAGE_QUERY_ALIASES = (
    ("梵蒂冈", "Vatican Vatican Museums St Peter Basilica"),
    ("博物馆", "museum museums"),
    ("景点", "attractions sightseeing"),
    ("开放时间", "opening hours"),
    ("门票", "tickets admission"),
    ("预约", "reservation booking"),
)
_FRESHNESS_MARKER_RE = re.compile(
    r"\b(202[4-9]|latest|current|release|changelog|today|recent)\b|最新|当前|现在|今天|更新|版本",
    re.I,
)
_SOFT_ERROR_RE = re.compile(
    r"\b(?:404\s+not\s+found|403\s+forbidden|access\s+denied|page\s+not\s+found|service\s+unavailable)\b|"
    r"页面不存在|访问被拒绝|暂无内容|内容已删除",
    re.I,
)
_PROMPT_INJECTION_RE = re.compile(
    r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|system)\s+instructions.{0,160}"
    r"(?:reveal|print|return|follow|execute|system\s+prompt)",
    re.I | re.S,
)
