"""Append-with-rotation for JSONL log files.

`logs.jsonl` (EventLogger) and `send_audit.jsonl` (SendAuditLog) are append-only
and otherwise grow without bound over a long-running local deployment. This
helper appends a line and, when the file exceeds ``max_bytes``, rotates it:
``<name>`` → ``<name>.1`` → ``<name>.2`` … keeping ``keep`` generations and
discarding the oldest. Rotation is best-effort: a rotation failure must never
prevent the line from being written (observability beats tidiness).
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB per active log file
DEFAULT_KEEP = 3  # number of rotated generations to retain


def append_line_with_rotation(
    path: Path,
    line: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> None:
    """Append ``line`` (a single record, no trailing newline) then rotate if big."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            _rotate(path, keep=keep)
    except OSError:
        # Rotation is best-effort; never lose the just-written record over it.
        pass


def _rotate(path: Path, *, keep: int) -> None:
    keep = max(1, keep)
    # Drop the oldest generation, then shift each down by one.
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
