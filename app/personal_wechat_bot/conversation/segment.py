"""Canonical conversation directory-segment naming.

Directory names for a conversation are ``chat_title_hashPrefix`` (human
readable) rather than the raw 24-hex-char ``conversation_id``. The segment is
NOT a pure function of ``conversation_id`` alone -- it needs the chat_title.
Callers that only have a ``conversation_id`` (e.g. the out-of-process send
worker, or the session store) resolve the segment deterministically by reading
``conversation_channels/index.json``, which maps ``conversation_id`` ->
the stable directory segment. The display ``chat_title`` may change over time;
once a channel exists, the segment must not drift with it.

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


def chat_title_from_index(data_dir: str | Path, conversation_id: str) -> str:
    """Look up a conversation's chat_title from conversation_channels/index.json.

    Falls back to scanning channel.json files when the index is missing or
    stale. Returns "" when no matching channel exists.
    """
    registered = _registry_payload(data_dir, conversation_id)
    if registered:
        return str(registered.get("chat_title", "") or "")
    root = Path(data_dir) / "conversation_channels"
    index_path = root / "index.json"
    if not index_path.exists():
        return _scan_chat_title(root, conversation_id)
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _scan_chat_title(root, conversation_id)
    channels = index.get("channels") if isinstance(index, dict) else None
    if isinstance(channels, list):
        for item in channels:
            if isinstance(item, dict) and item.get("conversation_id") == conversation_id:
                return str(item.get("chat_title", "") or "")
    return _scan_chat_title(root, conversation_id)


def segment_from_index(data_dir: str | Path, conversation_id: str) -> str:
    """Look up the stable directory segment from conversation_channels.

    New channel indexes store ``segment`` explicitly so display-title updates
    do not move ledger/session/workspace data. Legacy indexes may only have
    ``chat_title``; in that case we reconstruct the historical segment from the
    title, then fall back to scanning channel directories.
    """
    registered = _registry_payload(data_dir, conversation_id)
    if registered:
        segment = str(registered.get("segment", "") or "").strip()
        if segment:
            return segment
        title = str(registered.get("chat_title", "") or "")
        if title:
            return conversation_segment(conversation_id, title)
    root = Path(data_dir) / "conversation_channels"
    index_path = root / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index = {}
        channels = index.get("channels") if isinstance(index, dict) else None
        if isinstance(channels, list):
            for item in channels:
                if not isinstance(item, dict) or item.get("conversation_id") != conversation_id:
                    continue
                segment = str(item.get("segment", "") or "").strip()
                if segment:
                    return segment
                title = str(item.get("chat_title", "") or "")
                if title:
                    return conversation_segment(conversation_id, title)
    return _scan_segment(root, conversation_id)


def _scan_chat_title(root: Path, conversation_id: str) -> str:
    if not root.exists():
        return ""
    for channel_json in root.glob("*/channel.json"):
        try:
            payload = json.loads(channel_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("conversation_id") == conversation_id:
            return str(payload.get("chat_title", "") or "")
    return ""


def _scan_segment(root: Path, conversation_id: str) -> str:
    if not root.exists():
        return ""
    for channel_json in root.glob("*/channel.json"):
        try:
            payload = json.loads(channel_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("conversation_id") == conversation_id:
            segment = str(payload.get("segment", "") or "").strip()
            return segment or channel_json.parent.name
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
    segment = segment_from_index(data_dir, conversation_id)
    if segment:
        return segment
    return conversation_segment(conversation_id, chat_title)
