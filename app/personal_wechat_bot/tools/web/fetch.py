from __future__ import annotations

import hashlib
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from app.personal_wechat_bot.domain.models import ToolCallRequest, ToolCallResult
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.tools.base import ToolManifest


class WebFetchTool:
    manifest = ToolManifest(
        name="web.fetch",
        description="Fetch a public http(s) page and save extracted text for ledger annotation.",
        supports_async=False,
    )

    def __init__(
        self,
        output_dir: str | Path,
        file_index: FileIndex,
        *,
        timeout_seconds: float = 20.0,
        max_bytes: int = 2 * 1024 * 1024,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file_index = file_index
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        url = str(request.arguments.get("url") or request.arguments.get("input_url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="blocked",
                summary="web.fetch requires an http(s) url",
                error="invalid_url",
            )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wechat-agent-local/0.1"})
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_bytes + 1)
                content_type = response.headers.get("content-type", "")
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=self.manifest.name,
                status="failed",
                summary="web.fetch failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        truncated = len(raw) > self.max_bytes
        raw = raw[: self.max_bytes]
        charset = _charset(content_type) or "utf-8"
        text = raw.decode(charset, errors="replace")
        if "html" in content_type.lower() or "<html" in text[:1000].lower():
            text = _html_to_text(text)
        else:
            text = _normalize_text(text)

        url_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        output = self.output_dir / f"{url_id}.md"
        output.write_text(
            f"# Web Fetch\n\nurl: {url}\ntruncated: {str(truncated).lower()}\n\n{text}\n",
            encoding="utf-8",
        )
        file_id = self.file_index.add(output, source=self.manifest.name, original_name=output.name)
        preview = text[:1200].strip() or "(empty page text)"
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=self.manifest.name,
            status="completed",
            summary=f"Fetched page text: {preview}",
            output_refs=[str(output)],
            payload={"file_id": file_id, "url_id": url_id, "url": url, "truncated": truncated, "text": text},
        )


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1
        if tag.lower() in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self.skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self.parts.append(stripped)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return _normalize_text(" ".join(parser.parts))


def _normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    return match.group(1).strip("\"'") if match else ""
