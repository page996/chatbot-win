from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WEFLOW_DIR = ROOT / "vendor" / "reference" / "WeFlow-gitcode"
HISTORY_RESET_ACTIVE_SECONDS = 3600.0
_HISTORY_RESET_TERMINAL_STATUSES = frozenset(
    {"ok", "partial_error", "blocked", "error", "failed", "interrupted"}
)
_HISTORY_RESET_NONTERMINAL_STATUSES = frozenset({"running", "scheduled", "shutdown_scheduled"})
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.personal_wechat_bot.runtime.process_lock import (
    ProcessLockError,
    blocking_process_lock,
    process_pid_alive,
    process_start_marker,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local sidebar frontend.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--mode", choices=["window", "server"], default="window")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval-ms", type=int, default=2000)
    parser.add_argument("--weflow", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--weflow-port", type=int, default=5031)
    parser.add_argument("--weflow-host", default="127.0.0.1")
    parser.add_argument("--install-weflow-deps", choices=["auto", "never", "always"], default="auto")
    parser.add_argument("--weflow-wait-seconds", type=float, default=25.0)
    parser.add_argument("--weflow-window", choices=["hidden", "normal"], default="hidden")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    if _history_reset_in_progress(data_dir):
        print(json.dumps(_history_reset_blocked_result(), ensure_ascii=False, indent=2), flush=True)
        return 3
    try:
        with _sidebar_frontend_lifecycle_lock(data_dir):
            return _run_frontend_lifecycle(args, data_dir)
    except ProcessLockError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "component": "sidebar_startup",
                    "state": "sidebar_frontend_already_running",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3


def _run_frontend_lifecycle(args: argparse.Namespace, data_dir: Path) -> int:
    try:
        with _weflow_lifecycle_lock(data_dir, wait_timeout_seconds=max(30.0, args.weflow_wait_seconds + 5.0)):
            if _history_reset_in_progress(data_dir):
                print(json.dumps(_history_reset_blocked_result(), ensure_ascii=False, indent=2), flush=True)
                return 3
            weflow_result: dict[str, object] = {
                "status": "skipped",
                "component": "weflow",
                "reason": "disabled",
            }
            if args.weflow != "off":
                weflow_result = _ensure_weflow_started_locked(
                    data_dir=data_dir,
                    host=args.weflow_host,
                    port=args.weflow_port,
                    install_deps=args.install_weflow_deps,
                    wait_seconds=args.weflow_wait_seconds,
                    required=args.weflow == "on",
                    hidden=args.weflow_window == "hidden",
                )
                print(json.dumps(weflow_result, ensure_ascii=False, indent=2), flush=True)
                if args.weflow == "on" and weflow_result.get("status") == "error":
                    return 2
            if _history_reset_in_progress(data_dir):
                print(json.dumps(_history_reset_blocked_result(), ensure_ascii=False, indent=2), flush=True)
                return 3
            _write_sidebar_launch_state(data_dir, args, weflow_result)
            bridge_result = ensure_send_bridge_worker(data_dir)
            if bridge_result.get("status") not in {"skipped", "ok"}:
                print(json.dumps(bridge_result, ensure_ascii=False, indent=2), flush=True)
            if _history_reset_in_progress(data_dir):
                return 3
    except ProcessLockError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "component": "sidebar_startup",
                    "state": "weflow_lifecycle_contended",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 3

    if args.mode == "server":
        from app.personal_wechat_bot.control.sidebar_server import run_sidebar_server

        print(f"Starting sidebar server at http://{args.host}:{args.port}", flush=True)
        print("Close this window or press Ctrl+C to stop.", flush=True)
        run_sidebar_server(data_dir, host=args.host, port=args.port)
        return 0

    from app.personal_wechat_bot.control.sidebar_window import run_sidebar_window

    print("Starting sidebar app window...", flush=True)
    print("Close this window or press Ctrl+C to stop the local frontend.", flush=True)
    run_sidebar_window(
        data_dir,
        poll_interval_ms=args.interval_ms,
        browser_state_callback=lambda state: _merge_sidebar_browser_launch_state(data_dir, state),
    )
    return 0


def ensure_send_bridge_worker(data_dir: Path) -> dict[str, object]:
    try:
        from app.personal_wechat_bot.control.sidebar_api import ensure_sidebar_bridge_worker

        worker = ensure_sidebar_bridge_worker(data_dir, {"source": "start_sidebar_frontend"})
        return {"status": "ok", "component": "send_bridge_worker", "worker": worker}
    except Exception as exc:
        return {
            "status": "error",
            "component": "send_bridge_worker",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def ensure_weflow_started(
    *,
    data_dir: Path,
    host: str,
    port: int,
    install_deps: str,
    wait_seconds: float,
    required: bool,
    hidden: bool,
) -> dict[str, object]:
    data_dir = Path(data_dir).resolve()
    try:
        with _weflow_lifecycle_lock(data_dir, wait_timeout_seconds=max(30.0, wait_seconds + 5.0)):
            if _history_reset_in_progress(data_dir):
                return _history_reset_blocked_result(required=required)
            return _ensure_weflow_started_locked(
                data_dir=data_dir,
                host=host,
                port=port,
                install_deps=install_deps,
                wait_seconds=wait_seconds,
                required=required,
                hidden=hidden,
            )
    except ProcessLockError as exc:
        return {
            "status": "error" if required else "starting",
            "component": "weflow",
            "state": "weflow_lifecycle_contended",
            "reason": str(exc),
        }


def _ensure_weflow_started_locked(
    *,
    data_dir: Path,
    host: str,
    port: int,
    install_deps: str,
    wait_seconds: float,
    required: bool,
    hidden: bool,
) -> dict[str, object]:
    base_url = f"http://{host}:{port}"
    health = weflow_health(base_url)
    if health.get("status") == "ok":
        known_launch = _known_weflow_launch(data_dir)
        return {
            "status": "ok",
            "component": "weflow",
            "state": "already_running",
            "base_url": base_url,
            "health": health,
            **known_launch,
        }

    if not WEFLOW_DIR.exists():
        return {"status": "error" if required else "skipped", "component": "weflow", "reason": f"missing WeFlow source: {WEFLOW_DIR}"}

    package_json = WEFLOW_DIR / "package.json"
    if not package_json.exists():
        return {"status": "error" if required else "skipped", "component": "weflow", "reason": f"missing package.json: {package_json}"}

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        return {"status": "error" if required else "skipped", "component": "weflow", "reason": "npm was not found in PATH"}

    data_dir.mkdir(parents=True, exist_ok=True)
    node_modules = WEFLOW_DIR / "node_modules"
    if install_deps == "never" and not node_modules.exists():
        return {
            "status": "error" if required else "skipped",
            "component": "weflow",
            "reason": f"WeFlow dependencies are missing: {node_modules}",
            "hint": "run start_sidebar.cmd --weflow on --install-weflow-deps always",
        }
    if install_deps == "always" or (install_deps == "auto" and not node_modules.exists()):
        install = install_weflow_dependencies(npm, data_dir)
        if install.get("status") != "ok":
            return install

    token = weflow_token()
    if not token:
        return {"status": "error" if required else "skipped", "component": "weflow", "reason": "WEFLOW_API_TOKEN is not configured"}

    lock_path = _weflow_start_lock_path(data_dir)
    owner_token = uuid.uuid4().hex
    existing_start = _existing_weflow_start(lock_path, base_url=base_url, token=token, wait_seconds=wait_seconds)
    if existing_start is not None:
        return existing_start
    if not _try_acquire_weflow_start_lock(lock_path, owner_token=owner_token):
        existing_start = _existing_weflow_start(lock_path, base_url=base_url, token=token, wait_seconds=wait_seconds)
        if existing_start is not None:
            return existing_start
        stale_lock = _read_json(lock_path, {})
        _remove_weflow_start_lock(lock_path, expected=stale_lock if isinstance(stale_lock, dict) else {})
        if not _try_acquire_weflow_start_lock(lock_path, owner_token=owner_token):
            return {
                "status": "starting",
                "component": "weflow",
                "state": "start_lock_contended",
                "base_url": base_url,
                "lock": str(lock_path),
            }

    env = os.environ.copy()
    env.update(
        {
            "WEFLOW_API_TOKEN": token,
            "WEFLOW_HTTP_TOKEN": token,
            "WEFLOW_HTTP_AUTOSTART": "1",
            "WEFLOW_HTTP_HOST": host,
            "WEFLOW_HTTP_PORT": str(port),
            "WEFLOW_API_HOST": host,
            "WEFLOW_API_PORT": str(port),
            "WEFLOW_PROJECT_NAME": "WeFlow",
            "WEFLOW_START_HIDDEN": "1" if hidden else "0",
            "AUTO_UPDATE_ENABLED": "0",
        }
    )
    out = data_dir / "weflow_process.out.log"
    err = data_dir / "weflow_process.err.log"
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000
    try:
        with out.open("ab") as stdout, err.open("ab") as stderr:
            process = subprocess.Popen(
                [npm, "run", "electron:dev"],
                cwd=str(WEFLOW_DIR),
                env=env,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                creationflags=creationflags,
            )
    except Exception as exc:
        _remove_weflow_start_lock(lock_path, expected_owner_token=owner_token)
        return {
            "status": "error" if required else "skipped",
            "component": "weflow",
            "reason": f"failed to launch WeFlow: {type(exc).__name__}: {exc}",
            "stdout": str(out),
            "stderr": str(err),
        }
    process_start = process_start_marker(process.pid)
    _write_weflow_start_lock(
        lock_path,
        {
            "pid": process.pid,
            "process_start": process_start,
            "owner_token": owner_token,
            "launcher_pid": os.getpid(),
            "launcher_process_start": process_start_marker(os.getpid()),
            "base_url": base_url,
            "updated_at_epoch": time.time(),
            "stdout": str(out),
            "stderr": str(err),
        },
    )

    deadline = time.monotonic() + max(1.0, wait_seconds)
    last_health: dict[str, object] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _remove_weflow_start_lock(lock_path, expected_owner_token=owner_token)
            return {
                "status": "error" if required else "starting",
                "component": "weflow",
                "state": "exited",
                "pid": process.pid,
                "process_start": process_start,
                "returncode": process.returncode,
                "stdout": str(out),
                "stderr": str(err),
            }
        last_health = weflow_health(base_url, token=token)
        if last_health.get("status") == "ok":
            _remove_weflow_start_lock(lock_path, expected_owner_token=owner_token)
            return {
                "status": "ok",
                "component": "weflow",
                "state": "started",
                "pid": process.pid,
                "process_start": process_start,
                "base_url": base_url,
                "health": last_health,
                "stdout": str(out),
                "stderr": str(err),
            }
        time.sleep(1.0)

    return {
        "status": "starting",
        "component": "weflow",
        "state": "launched_waiting_for_health",
        "pid": process.pid,
        "process_start": process_start,
        "base_url": base_url,
        "last_health": last_health,
        "stdout": str(out),
        "stderr": str(err),
        "lock": str(lock_path),
    }


def _weflow_start_lock_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "weflow_start.lock"


def _sidebar_frontend_lifecycle_lock(data_dir: Path):
    return blocking_process_lock(
        data_dir / "runtime_locks" / "sidebar_frontend_lifecycle.lock",
        label="sidebar_frontend_lifecycle",
        stale_after_seconds=HISTORY_RESET_ACTIVE_SECONDS,
        wait_timeout_seconds=0.0,
    )


def _weflow_lifecycle_lock(data_dir: Path, *, wait_timeout_seconds: float):
    return blocking_process_lock(
        data_dir / "runtime_locks" / "weflow_lifecycle.lock",
        label="weflow_lifecycle_start",
        stale_after_seconds=HISTORY_RESET_ACTIVE_SECONDS,
        wait_timeout_seconds=wait_timeout_seconds,
    )


def _history_reset_in_progress(data_dir: Path) -> bool:
    lock_path = data_dir / "runtime" / "history_reset_shutdown.lock"
    try:
        lock_stat = os.lstat(lock_path)
    except FileNotFoundError:
        return _history_reset_status_in_progress(data_dir)
    except OSError:
        return True
    if (
        _is_reparse_point(lock_stat)
        or not stat.S_ISREG(lock_stat.st_mode)
        or int(getattr(lock_stat, "st_nlink", 1) or 1) != 1
    ):
        return True
    lock = _read_json(lock_path, None)
    if not isinstance(lock, dict):
        return True
    age = _lock_age_seconds(lock)
    helper_pid = _int_value(lock.get("helper_pid"), 0)
    if helper_pid <= 0:
        return age <= 20.0 or _history_reset_status_in_progress(data_dir)
    helper_process_start = str(lock.get("helper_process_start") or "")
    if not _pid_exists(helper_pid):
        return _history_reset_status_in_progress(data_dir)
    if not helper_process_start:
        return True
    current_process_start = process_start_marker(helper_pid)
    if not current_process_start:
        return True
    if current_process_start == helper_process_start:
        return True
    return _history_reset_status_in_progress(data_dir)


def _history_reset_status_in_progress(data_dir: Path) -> bool:
    status_path = data_dir / "runtime" / "history_reset_shutdown.json"
    try:
        status_stat = os.lstat(status_path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if (
        _is_reparse_point(status_stat)
        or not stat.S_ISREG(status_stat.st_mode)
        or int(getattr(status_stat, "st_nlink", 1) or 1) != 1
    ):
        return True
    payload = _read_json(status_path, None)
    if not isinstance(payload, dict):
        return True
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in _HISTORY_RESET_TERMINAL_STATUSES:
        return False
    if raw_status not in _HISTORY_RESET_NONTERMINAL_STATUSES:
        return True
    helper_pid = _int_value(payload.get("helper_pid"), 0)
    helper_process_start = str(payload.get("helper_process_start") or "").strip()
    if helper_pid <= 0 or not helper_process_start:
        return True
    if not _pid_exists(helper_pid):
        return False
    current_process_start = process_start_marker(helper_pid)
    if not current_process_start:
        return True
    return current_process_start == helper_process_start


def _history_reset_blocked_result(*, required: bool = True) -> dict[str, object]:
    return {
        "status": "error" if required else "skipped",
        "component": "weflow",
        "state": "history_reset_in_progress",
        "reason": "history reset is stopping local writers; reopen after cleanup completes",
    }


def _known_weflow_launch(data_dir: Path) -> dict[str, object]:
    candidates: list[tuple[str, dict[str, object]]] = []
    start_lock = _read_json(_weflow_start_lock_path(data_dir), {})
    if isinstance(start_lock, dict):
        candidates.append(("weflow_start_lock", start_lock))
    launch_state = _read_json(data_dir / "runtime" / "sidebar_launch.json", {})
    if isinstance(launch_state, dict):
        nested = launch_state.get("weflow_result")
        nested = nested if isinstance(nested, dict) else {}
        candidates.append(
            (
                "sidebar_launch",
                {
                    "pid": launch_state.get("weflow_pid") or nested.get("pid"),
                    "process_start": launch_state.get("weflow_process_start") or nested.get("process_start"),
                },
            )
        )
    for source, candidate in candidates:
        pid = _int_value(candidate.get("pid"), 0)
        recorded_start = str(candidate.get("process_start") or "")
        if (
            pid > 0
            and recorded_start
            and _pid_exists(pid)
            and process_start_marker(pid) == recorded_start
        ):
            return {"pid": pid, "process_start": recorded_start, "launch_identity_source": source}
    return {}


def _try_acquire_weflow_start_lock(lock_path: Path, *, owner_token: str = "") -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner_token = str(owner_token or uuid.uuid4().hex)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": 0,
                "owner_token": owner_token,
                "launcher_pid": os.getpid(),
                "launcher_process_start": process_start_marker(os.getpid()),
                "updated_at_epoch": time.time(),
            },
            handle,
            ensure_ascii=False,
        )
    return True


def _existing_weflow_start(
    lock_path: Path,
    *,
    base_url: str,
    token: str,
    wait_seconds: float,
) -> dict[str, object] | None:
    if not lock_path.exists():
        return None
    lock = _read_json(lock_path, {})
    pid = _int_value(lock.get("pid") if isinstance(lock, dict) else 0, 0)
    recorded_process_start = str((lock.get("process_start") if isinstance(lock, dict) else "") or "")
    owner_token = str((lock.get("owner_token") if isinstance(lock, dict) else "") or "")
    age = _lock_age_seconds(lock if isinstance(lock, dict) else {})
    health = weflow_health(base_url, token=token)
    if health.get("status") == "ok":
        _remove_weflow_start_lock(lock_path, expected=lock if isinstance(lock, dict) else {})
        launch = {}
        if pid > 0 and recorded_process_start and process_start_marker(pid) == recorded_process_start:
            launch = {"pid": pid, "process_start": recorded_process_start}
        return {
            "status": "ok",
            "component": "weflow",
            "state": "already_running",
            "base_url": base_url,
            "health": health,
            **launch,
        }
    if pid <= 0 and age <= 20.0:
        return {
            "status": "starting",
            "component": "weflow",
            "state": "start_lock_pending",
            "base_url": base_url,
            "last_health": health,
            "lock": str(lock_path),
        }
    pid_alive = pid > 0 and _pid_exists(pid)
    current_process_start = process_start_marker(pid) if pid_alive else ""
    if pid_alive:
        if not recorded_process_start:
            return {
                "status": "starting",
                "component": "weflow",
                "state": "start_identity_unverified",
                "pid": pid,
                "base_url": base_url,
                "last_health": health,
                "lock": str(lock_path),
            }
        if not current_process_start:
            return {
                "status": "starting",
                "component": "weflow",
                "state": "start_identity_unverified",
                "pid": pid,
                "process_start": recorded_process_start,
                "base_url": base_url,
                "last_health": health,
                "lock": str(lock_path),
            }
        if current_process_start != recorded_process_start:
            _remove_weflow_start_lock(lock_path, expected=lock if isinstance(lock, dict) else {})
            return None
        deadline = time.monotonic() + max(1.0, wait_seconds)
        last_health = health
        while time.monotonic() < deadline:
            last_health = weflow_health(base_url, token=token)
            if last_health.get("status") == "ok":
                _remove_weflow_start_lock(lock_path, expected_owner_token=owner_token)
                return {
                    "status": "ok",
                    "component": "weflow",
                    "state": "started_by_existing_launcher",
                    "pid": pid,
                    "process_start": recorded_process_start,
                    "base_url": base_url,
                    "health": last_health,
                }
            if not _pid_exists(pid):
                _remove_weflow_start_lock(lock_path, expected_owner_token=owner_token)
                return None
            time.sleep(1.0)
        return {
            "status": "starting",
            "component": "weflow",
            "state": "start_in_progress",
            "pid": pid,
            "process_start": recorded_process_start,
            "base_url": base_url,
            "last_health": last_health,
            "lock": str(lock_path),
        }
    _remove_weflow_start_lock(lock_path, expected=lock if isinstance(lock, dict) else {})
    return None


def _write_weflow_start_lock(lock_path: Path, payload: dict[str, object]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_weflow_start_lock(
    lock_path: Path,
    *,
    expected_owner_token: str = "",
    expected: dict[str, object] | None = None,
) -> bool:
    current = _read_json(lock_path, None)
    if current is None:
        return True
    if not isinstance(current, dict):
        return False
    if expected_owner_token and str(current.get("owner_token") or "") != expected_owner_token:
        return False
    if expected is not None and current != expected:
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _lock_age_seconds(lock: dict[str, object]) -> float:
    try:
        updated_at = float(lock.get("updated_at_epoch") or 0)
    except Exception:
        updated_at = 0.0
    if updated_at <= 0:
        return float("inf")
    return max(0.0, time.time() - updated_at)


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _pid_exists(pid: int) -> bool:
    return process_pid_alive(pid)


def install_weflow_dependencies(npm: str, data_dir: Path) -> dict[str, object]:
    out = data_dir / "weflow_npm_ci.out.log"
    err = data_dir / "weflow_npm_ci.err.log"
    print("Installing WeFlow dependencies with npm ci. This may take a while...", flush=True)
    with out.open("ab") as stdout, err.open("ab") as stderr:
        completed = subprocess.run(
            [npm, "ci"],
            cwd=str(WEFLOW_DIR),
            stdout=stdout,
            stderr=stderr,
            timeout=1800,
            check=False,
        )
    if completed.returncode != 0:
        return {
            "status": "error",
            "component": "weflow",
            "reason": "npm ci failed",
            "returncode": completed.returncode,
            "stdout": str(out),
            "stderr": str(err),
        }
    return {"status": "ok", "component": "weflow", "state": "dependencies_installed", "stdout": str(out), "stderr": str(err)}


def weflow_health(base_url: str, *, token: str = "") -> dict[str, object]:
    url = base_url.rstrip("/") + "/api/v1/health"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if isinstance(payload, dict):
            return {"status": "ok", **payload}
        return {"status": "error", "message": "health response was not an object"}
    except Exception as exc:
        return {"status": "error", "type": type(exc).__name__, "message": str(exc)}


def weflow_token() -> str:
    for name in ("WEFLOW_API_TOKEN", "WEFLOW_HTTP_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as env_key:
            for name in ("WEFLOW_API_TOKEN", "WEFLOW_HTTP_TOKEN"):
                try:
                    value, _ = winreg.QueryValueEx(env_key, name)
                except FileNotFoundError:
                    continue
                text = str(value or "").strip()
                if text:
                    return text
    except Exception:
        return ""
    return ""


def _write_sidebar_launch_state(data_dir: Path, args: argparse.Namespace, weflow_result: dict[str, object]) -> None:
    runtime_dir = data_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    process_start = process_start_marker(os.getpid())
    if not process_start:
        raise RuntimeError("sidebar process identity is unavailable")
    payload = {
        "pid": os.getpid(),
        "process_start": process_start,
        "python": sys.executable,
        "script": str(Path(__file__).resolve()),
        "root": str(ROOT),
        "data_dir": str(data_dir.resolve()),
        "argv": sys.argv[1:],
        "mode": args.mode,
        "host": args.host,
        "port": args.port,
        "interval_ms": args.interval_ms,
        "weflow": args.weflow,
        "weflow_port": args.weflow_port,
        "weflow_host": args.weflow_host,
        "install_weflow_deps": args.install_weflow_deps,
        "weflow_wait_seconds": args.weflow_wait_seconds,
        "weflow_window": args.weflow_window,
        "weflow_result": weflow_result,
        "weflow_pid": weflow_result.get("pid") if isinstance(weflow_result, dict) else None,
        "weflow_process_start": weflow_result.get("process_start") if isinstance(weflow_result, dict) else None,
        "browser_status": "pending",
        "browser_pid": None,
        "browser_process_start": "",
        "browser_executable": "",
        "browser_profile": os.path.abspath(data_dir / "runtime" / "sidebar_browser_profile"),
        "browser_owned": False,
        "browser_job_owned": False,
        "browser_descendants": [],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_private_json_atomic(runtime_dir / "sidebar_launch.json", payload)


def _merge_sidebar_browser_launch_state(data_dir: Path, browser_state: dict[str, object]) -> None:
    data_dir = Path(data_dir).resolve()
    state_path = data_dir / "runtime" / "sidebar_launch.json"
    try:
        state_stat = os.lstat(state_path)
    except OSError as exc:
        raise RuntimeError("sidebar launch state is unavailable") from exc
    if (
        _is_reparse_point(state_stat)
        or not stat.S_ISREG(state_stat.st_mode)
        or int(state_stat.st_nlink) != 1
    ):
        raise RuntimeError("sidebar launch state is not private")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("sidebar launch state is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("sidebar launch state is invalid")
    current_start = process_start_marker(os.getpid())
    if (
        int(payload.get("pid") or 0) != os.getpid()
        or not current_start
        or str(payload.get("process_start") or "") != current_start
        or Path(str(payload.get("data_dir") or "")).resolve() != data_dir
    ):
        raise RuntimeError("sidebar launch state owner changed")
    expected_profile = Path(os.path.abspath(data_dir / "runtime" / "sidebar_browser_profile"))
    recorded_profile = Path(str(browser_state.get("browser_profile") or "")).resolve()
    if recorded_profile != expected_profile:
        raise RuntimeError("sidebar browser profile identity mismatch")
    owned = bool(browser_state.get("browser_owned"))
    browser_pid = int(browser_state.get("browser_pid") or 0)
    browser_start = str(browser_state.get("browser_process_start") or "").strip()
    browser_executable = str(browser_state.get("browser_executable") or "").strip()
    browser_job_owned = bool(browser_state.get("browser_job_owned"))
    browser_descendants = browser_state.get("browser_descendants")
    if owned:
        if (
            browser_pid <= 0
            or not browser_start
            or not os.path.isabs(browser_executable)
            or Path(browser_executable).name.lower() not in {"chrome.exe", "msedge.exe"}
            or not browser_job_owned
            or not isinstance(browser_descendants, list)
        ):
            raise RuntimeError("sidebar browser process identity is incomplete")
        current_browser_start = process_start_marker(browser_pid)
        if not current_browser_start or current_browser_start != browser_start:
            raise RuntimeError("sidebar browser process identity changed before recording")
        descendant_pids: set[int] = set()
        for item in browser_descendants:
            if not isinstance(item, dict):
                raise RuntimeError("sidebar browser descendant identity is invalid")
            descendant_pid = int(item.get("pid") or 0)
            descendant_start = str(item.get("process_start") or "").strip()
            descendant_executable = str(item.get("executable") or "").strip()
            if (
                descendant_pid <= 0
                or descendant_pid in descendant_pids
                or not descendant_start
                or not os.path.isabs(descendant_executable)
                or os.path.normcase(os.path.abspath(descendant_executable))
                != os.path.normcase(os.path.abspath(browser_executable))
            ):
                raise RuntimeError("sidebar browser descendant identity is invalid")
            descendant_pids.add(descendant_pid)
        if browser_pid not in descendant_pids:
            raise RuntimeError("sidebar browser root is missing from descendant identity")
    elif browser_pid > 0 or browser_start or browser_executable or browser_job_owned or browser_descendants not in (None, []):
        raise RuntimeError("external browser state must not claim an owned process")
    allowed_keys = {
        "browser_status",
        "browser_pid",
        "browser_process_start",
        "browser_executable",
        "browser_profile",
        "browser_owned",
        "browser_job_owned",
        "browser_descendants",
        "browser_url",
        "browser_state_updated_at_epoch",
    }
    payload.update({key: browser_state[key] for key in allowed_keys if key in browser_state})
    payload["browser_profile"] = str(expected_profile)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_private_json_atomic(state_path, payload)


def _write_private_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_stat = os.lstat(path.parent)
    if not stat.S_ISDIR(parent_stat.st_mode) or _is_reparse_point(parent_stat):
        raise RuntimeError(f"unsafe launch state directory: {path.parent}")

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(str(tmp), flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_stat = os.lstat(tmp)
        if _is_reparse_point(tmp_stat) or not stat.S_ISREG(tmp_stat.st_mode) or int(tmp_stat.st_nlink) != 1:
            raise RuntimeError("launch state temporary file is not private")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        final_stat = os.lstat(path)
        if _is_reparse_point(final_stat) or not stat.S_ISREG(final_stat.st_mode) or int(final_stat.st_nlink) != 1:
            raise RuntimeError("launch state file is not private")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400)
    return bool(attributes & marker)


if __name__ == "__main__":
    raise SystemExit(main())
