from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def append_jsonl(path: str | Path, payload: dict[str, Any], *, timeout_seconds: float = 10.0) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    with _file_lock(lock_path, timeout_seconds=timeout_seconds):
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


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
        current_size = _file_size(target)
        rebuilt = False
        if not state or int(state.get("source_size", 0) or 0) != current_size:
            state = _build_index(target, key_field)
            rebuilt = True
        keys = set(_string_list(state.get("keys")))
        if key in keys:
            if rebuilt:
                _write_index(index, state)
            return False
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        keys.add(key)
        _write_index(index, {"version": 1, "key_field": key_field, "source_size": _file_size(target), "keys": sorted(keys)})
        return True


@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _stale_lock(path):
                try:
                    path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for JSONL lock: {path}")
            time.sleep(0.025)
    try:
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _stale_lock(path: Path, *, max_age_seconds: float = 30.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > max_age_seconds
    except OSError:
        return False


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
    return {"version": 1, "key_field": key_field, "source_size": _file_size(path), "keys": sorted(keys)}


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
