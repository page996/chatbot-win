"""Entry point for the non-foreground send bridge.

Consumes ``<data_dir>/send_bridge/outbox.jsonl`` and delivers each queued
message to WeChat via the configured send backend (WeChatFerry in production,
dry-run otherwise), then writes acks and syncs the confirm queue + ledger.

Usage:
    python scripts/send_bridge_worker.py --data-dir data
    python scripts/send_bridge_worker.py --data-dir data --once
    python scripts/send_bridge_worker.py --data-dir data --interval 2

The send backend is chosen by ``send_backend`` in config.json (default
``dry_run``). Set it to ``wcf`` and ensure a WeChatFerry RPC server is running
to deliver for real.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.personal_wechat_bot.runtime.process_lock import ProcessLockError
from app.personal_wechat_bot.runtime.send_bridge_worker import run_bridge_worker


def main() -> int:
    parser = argparse.ArgumentParser(description="Non-foreground WeChat send bridge worker")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    parser.add_argument("--once", action="store_true", help="deliver current backlog then exit")
    parser.add_argument("--no-lock", action="store_true", help="skip single-instance lock (tests only)")
    parser.add_argument(
        "--strict-data-dir",
        action="store_true",
        help="refuse to start if the data dir has no config.json (likely the wrong dir)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Guard against the classic footgun: the worker and the app reading/writing
    # DIFFERENT data dirs (so the app queues into one outbox while the worker
    # drains another), which silently delivers nothing with no error. The app's
    # data dir always contains a config.json; a data dir without one is almost
    # certainly wrong. Warn loudly, and refuse under --strict-data-dir.
    resolved = Path(args.data_dir).resolve()
    config_path = resolved / "config.json"
    if not config_path.exists():
        message = (
            f"data dir {resolved} has no config.json; the app likely uses a different "
            f"data dir. Queued replies there will never be delivered from here."
        )
        if args.strict_data_dir:
            print(f"refusing to start: {message}", file=sys.stderr)
            return 3
        print(f"warning: {message}", file=sys.stderr)
    else:
        print(f"send bridge worker using data dir: {resolved}", file=sys.stderr)

    try:
        stats = run_bridge_worker(
            str(resolved),
            poll_interval_seconds=args.interval,
            once=args.once,
            lock_enabled=not args.no_lock,
        )
    except ProcessLockError:
        print("another send bridge worker is already running", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("send bridge worker stopped", file=sys.stderr)
        return 0

    print(
        f"delivered={stats.delivered} failed={stats.failed} skipped={stats.skipped} ticks={stats.ticks}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
