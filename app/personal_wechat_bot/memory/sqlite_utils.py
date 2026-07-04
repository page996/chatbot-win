"""Shared SQLite connection helper.

All local state DBs (file index, deduper, cooldowns, backend-file-watcher) are
opened per-operation and are touched by several actors at once: the cross-talker
capture ThreadPoolExecutor, the background WeFlow worker, the sidebar process,
and the standalone tool/attachment workers. With SQLite's default rollback
journal and a 0-second lock timeout, a concurrent writer raises
``sqlite3.OperationalError: database is locked`` immediately.

``connect`` centralizes two mitigations:

* ``PRAGMA journal_mode=WAL`` — readers don't block the writer and vice versa,
  which is the right mode for many short concurrent transactions on one file.
* ``PRAGMA busy_timeout`` — a writer that still hits a held write lock waits and
  retries internally for up to ``timeout_ms`` instead of failing instantly.

WAL is a persistent property of the database file, so setting it on every
connect is cheap and idempotent. Both pragmas degrade gracefully: if WAL can't
be enabled (e.g. a network filesystem), the connection is still usable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000


def connect(db_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + busy_timeout applied.

    ``timeout`` (seconds) is also passed to ``sqlite3.connect`` so the Python
    driver's own wait matches the busy_timeout, belt-and-suspenders.
    """
    conn = sqlite3.connect(str(db_path), timeout=max(0.0, busy_timeout_ms / 1000.0))
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # A pragma failure (unusual filesystem) must not make the DB unusable;
        # the connection still works with default journaling.
        pass
    return conn
