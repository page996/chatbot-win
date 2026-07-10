"""Canonical conversation directory-segment naming.

Directory names for a conversation are ``chat_title_hashPrefix`` (human
readable) rather than the raw ``conversation_id``. The SQLite channel registry
owns the stable segment; JSON channel files are readable projections only.

This module is dependency-free (only stdlib) so every store and the send
bridge can share one implementation and never drift.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def conversation_segment(conversation_id: str, chat_title: str = "") -> str:
    """Build the canonical directory segment: ``chat_title_hashPrefix``.

    Uses the sanitized chat_title (capped at 64 chars) plus ``_`` plus the
    first 8 hex digits of ``conversation_id``. Falls back to the 8-char hash
    prefix when chat_title is empty or sanitizes to empty.

    Examples:
        ("abc12345...", "文件传输助手") -> "文件传输助手_abc12345"
        ("abc12345...", "")            -> "abc12345"
        ("abc12345...", "Group/Name")  -> "Group_Name_abc12345"
    """
    hash_prefix = conversation_id[:8] if conversation_id else "default"
    if not chat_title:
        return hash_prefix
    sanitized = re.sub(r"[^\w\s.-]+", "_", chat_title, flags=re.UNICODE)
    sanitized = re.sub(r"_+", "_", sanitized).strip("._")[:64]
    if not sanitized:
        return hash_prefix
    return f"{sanitized}_{hash_prefix}"


def chat_title_from_registry(data_dir: str | Path, conversation_id: str) -> str:
    """Look up a conversation title from the SQLite channel registry."""

    registered = _registry_payload(data_dir, conversation_id)
    return str(registered.get("chat_title", "") or "")


def segment_from_registry(data_dir: str | Path, conversation_id: str) -> str:
    """Look up the stable directory segment from the SQLite registry."""

    registered = _registry_payload(data_dir, conversation_id)
    if registered:
        segment = str(registered.get("segment", "") or "").strip()
        if segment:
            return segment
        title = str(registered.get("chat_title", "") or "")
        if title:
            return conversation_segment(conversation_id, title)
    return ""


def _registry_payload(data_dir: str | Path, conversation_id: str) -> dict[str, object]:
    path = Path(data_dir) / "conversation_channels.sqlite"
    if not path.exists():
        return {}
    try:
        db = sqlite3.connect(path, timeout=1)
        try:
            row = db.execute(
                "SELECT payload_json FROM conversation_channels WHERE conversation_id = ?",
                (str(conversation_id or ""),),
            ).fetchone()
        finally:
            db.close()
    except (sqlite3.DatabaseError, OSError):
        return {}
    if row is None:
        return {}
    try:
        payload = json.loads(str(row[0] or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_segment(data_dir: str | Path, conversation_id: str, chat_title: str = "") -> str:
    """Deterministically resolve the directory segment for a conversation_id.

    Once a channel exists, its stored segment is authoritative. Before first
    registration, callers that have a message may pass ``chat_title`` so the
    first writes land in the same readable directory the channel store will
    create later in the same processing tick. Falls back to the hash-only prefix
    when neither channel metadata nor title is available.
    """
    segment = segment_from_registry(data_dir, conversation_id)
    if segment:
        return segment
    return conversation_segment(conversation_id, chat_title)
