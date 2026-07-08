from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stop sidebar/WeFlow around a history reset.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--weflow", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--weflow-port", type=int, default=5031)
    parser.add_argument("--weflow-pid", type=int, default=0)
    parser.add_argument("--response-delay-seconds", type=float, default=1.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    _write_status(data_dir, {"status": "running", "phase": "scheduled", "parent_pid": args.parent_pid})
    time.sleep(max(0.1, float(args.response_delay_seconds)))

    try:
        _write_status(data_dir, {"status": "running", "phase": "closing_sidebar_window"})
        _close_sidebar_windows()

        if args.weflow != "off":
            _write_status(data_dir, {"status": "running", "phase": "stopping_weflow"})
            stop_result = _stop_weflow(args.weflow_pid, args.weflow_port)
            _remove_weflow_start_lock(data_dir)
            _write_status(data_dir, {"status": "running", "phase": "stopping_weflow", "weflow_stop": stop_result})

        _write_status(data_dir, {"status": "running", "phase": "stopping_sidebar_server"})
        _terminate_pid(args.parent_pid, tree=False)
        _wait_for_pid_exit(args.parent_pid, timeout_seconds=8.0)

        _write_status(data_dir, {"status": "running", "phase": "clearing_history"})
        from app.personal_wechat_bot.control.sidebar_api import clear_sidebar_history_data

        clear_result = clear_sidebar_history_data(data_dir, {"source": "shutdown_helper", "shutdown_processes": False})
        final_status = "ok" if clear_result.get("status") == "ok" else "partial_error"
        _write_status(
            data_dir,
            {
                "status": final_status,
                "phase": "stopped_after_clear",
                "manual_reopen_required": True,
                "clear_result": clear_result,
            },
        )
        return 0 if final_status == "ok" else 3
    except Exception as exc:
        _write_status(
            data_dir,
            {
                "status": "error",
                "phase": "failed",
                "manual_reopen_required": True,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return 2
    finally:
        _remove_shutdown_lock(data_dir)


def _close_sidebar_windows() -> None:
    try:
        from app.personal_wechat_bot.control.sidebar_window import _close_existing_sidebar_windows

        _close_existing_sidebar_windows()
    except Exception:
        return


def _stop_weflow(known_pid: int, port: int) -> dict[str, Any]:
    pids: list[int] = []
    if known_pid > 0:
        pids.append(known_pid)
    pids.extend(pid for pid in _pids_listening_on_port(port) if pid not in pids)
    for pid in pids:
        _terminate_pid(pid, tree=True)
    waited = []
    for pid in pids:
        _wait_for_pid_exit(pid, timeout_seconds=8.0)
        waited.append({"pid": pid, "exited": not _pid_exists(pid)})
    port_released = _wait_for_port_release(port, timeout_seconds=15.0)
    if not port_released:
        late_pids = [pid for pid in _pids_listening_on_port(port) if pid not in pids]
        for pid in late_pids:
            _terminate_pid(pid, tree=True)
        for pid in late_pids:
            _wait_for_pid_exit(pid, timeout_seconds=8.0)
        pids.extend(late_pids)
        port_released = _wait_for_port_release(port, timeout_seconds=8.0)
    return {
        "terminated_pids": pids,
        "waited": waited,
        "port": port,
        "port_released": port_released,
        "remaining_port_pids": _pids_listening_on_port(port),
    }


def _pids_listening_on_port(port: int) -> list[int]:
    if port <= 0 or os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []
    pids: set[int] = set()
    marker = f":{int(port)}"
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local_address, state, pid_text = parts[1], parts[3].upper(), parts[-1]
        if state != "LISTENING" or marker not in local_address:
            continue
        try:
            pids.add(int(pid_text))
        except ValueError:
            continue
    return sorted(pids)


def _terminate_pid(pid: int, *, tree: bool) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/F"]
        if tree:
            command.append("/T")
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.1)


def _wait_for_port_release(port: int, *, timeout_seconds: float) -> bool:
    if port <= 0 or os.name != "nt":
        return True
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        if not _pids_listening_on_port(port):
            return True
        time.sleep(0.2)
    return not _pids_listening_on_port(port)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_status(data_dir: Path, payload: dict[str, Any]) -> None:
    runtime_dir = data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (runtime_dir / "history_reset_shutdown.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_shutdown_lock(data_dir: Path) -> None:
    try:
        (data_dir / "runtime" / "history_reset_shutdown.lock").unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _remove_weflow_start_lock(data_dir: Path) -> None:
    try:
        (data_dir / "runtime" / "weflow_start.lock").unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
