from __future__ import annotations

import re


URL_RE = re.compile(r"https?://[^\s<>'\")\]]+", re.IGNORECASE)


def redact_file_internal_urls(text: str, *, replacement: str = "[file-internal-url-redacted]") -> str:
    """Hide URLs that came from parsed file content.

    User-authored chat URLs are handled by the ledger/link pipeline. Parsed file
    text is different: URLs inside a document should not be pinned into normal
    context or fetched unless file.read is called with an explicit opt-in.
    """

    return URL_RE.sub(replacement, str(text or ""))


def count_urls(text: str) -> int:
    return len(URL_RE.findall(str(text or "")))
