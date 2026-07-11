from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.personal_wechat_bot.runtime.process_lock import short_process_lock


_INDEX_VERSION = 2
_FINGERPRINT_WINDOW_BYTES = 64 * 1024


def append_jsonl(path: str | Path, payload: dict[str, Any], *, timeout_seconds: float = 10.0) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with _file_lock(lock_path, timeout_seconds=timeout_seconds):
        _append_jsonl_record(target, payload)


def append_jsonl_once(
    path: str | Path,
    payload: dict[str, Any],
    *,
    key_field: str = "raw_id",
    index_path: str | Path | None = None,
    timeout_seconds: float = 10.0,
) -> bool:
    """Append one JSONL item unless its stable key already exists.

    Returns True when a line was appended. The sidecar index is rebuilt from
    the JSONL file when missing, so existing files gain dedupe without a manual
    migration step.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = str(payload.get(key_field) or "").strip()
    if not key:
        append_jsonl(target, payload, timeout_seconds=timeout_seconds)
        return True
    index = Path(index_path) if index_path is not None else target.with_suffix(target.suffix + ".index.json")
    lock_path = target.with_suffix(target.suffix + ".lock")
    with _file_lock(lock_path, timeout_seconds=timeout_seconds):
        state = _read_index(index)
        source_identity = _file_identity(target)
        rebuilt = False
        if not state or not _index_matches_source(state, source_identity, key_field=key_field):
            state = _build_index(target, key_field)
            rebuilt = True
        keys = set(_string_list(state.get("keys")))
        if key in keys:
            if rebuilt:
                _write_index(index, state)
            return False
        _append_jsonl_record(target, payload)
        keys.add(key)
        _write_index(
            index,
            {
                **_file_identity(target),
                "version": _INDEX_VERSION,
                "key_field": key_field,
                "keys": sorted(keys),
            },
        )
        return True


def _append_jsonl_record(path: Path, payload: dict[str, Any]) -> None:
    newline = os.linesep.encode("ascii")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") + newline
    with path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            last_byte = f.read(1)
            if last_byte == b"\r":
                f.seek(0, os.SEEK_END)
                f.write(b"\n")
            elif last_byte != b"\n":
                # Keep a crash-truncated tail isolated from the next record so
                # consumers can skip that malformed line without losing both.
                f.seek(0, os.SEEK_END)
                f.write(newline)
        f.seek(0, os.SEEK_END)
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())


@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    with short_process_lock(
        path,
        timeout_seconds=timeout_seconds,
        stale_after_seconds=30.0,
        timeout_label="JSONL lock",
    ):
        yield


def _read_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_index(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _build_index(path: Path, key_field: str) -> dict[str, Any]:
    keys: set[str] = set()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        key = str(payload.get(key_field) or "").strip()
                        if key:
                            keys.add(key)
        except OSError:
            keys = set()
    return {
        **_file_identity(path),
        "version": _INDEX_VERSION,
        "key_field": key_field,
        "keys": sorted(keys),
    }


def _file_identity(path: Path) -> dict[str, int | str]:
    try:
        stat = path.stat()
    except OSError:
        return {
            "source_size": 0,
            "source_mtime_ns": 0,
            "source_ctime_ns": 0,
            "source_device": 0,
            "source_file_id": 0,
            "source_fingerprint": "missing",
        }
    return {
        "source_size": int(stat.st_size),
        "source_mtime_ns": int(stat.st_mtime_ns),
        "source_ctime_ns": int(stat.st_ctime_ns),
        "source_device": int(getattr(stat, "st_dev", 0) or 0),
        "source_file_id": int(getattr(stat, "st_ino", 0) or 0),
        "source_fingerprint": _sampled_file_fingerprint(path, source_size=int(stat.st_size)),
    }


def _sampled_file_fingerprint(path: Path, *, source_size: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"jsonl-sidecar-v{_INDEX_VERSION}:{source_size}:".encode("ascii"))
    try:
        with path.open("rb") as source:
            head = source.read(_FINGERPRINT_WINDOW_BYTES)
            digest.update(head)
            if source_size > _FINGERPRINT_WINDOW_BYTES:
                source.seek(max(0, source_size - _FINGERPRINT_WINDOW_BYTES))
                digest.update(b"\0tail\0")
                digest.update(source.read(_FINGERPRINT_WINDOW_BYTES))
    except OSError:
        return ""
    return digest.hexdigest()


def _index_matches_source(
    state: dict[str, Any],
    identity: dict[str, int | str],
    *,
    key_field: str,
) -> bool:
    try:
        version = int(state.get("version", 0) or 0)
    except (TypeError, ValueError):
        return False
    if version != _INDEX_VERSION:
        return False
    if str(state.get("key_field") or "") != key_field:
        return False
    fingerprint = str(identity.get("source_fingerprint") or "")
    if not fingerprint or str(state.get("source_fingerprint") or "") != fingerprint:
        return False
    numeric_fields = (
        "source_size",
        "source_mtime_ns",
        "source_ctime_ns",
        "source_device",
        "source_file_id",
    )
    try:
        return all(
            int(state.get(field, -1)) == int(identity.get(field, 0))
            for field in numeric_fields
        )
    except (TypeError, ValueError):
        return False


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
