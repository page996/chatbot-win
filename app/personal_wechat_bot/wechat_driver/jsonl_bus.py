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
