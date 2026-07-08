from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WEFLOW_DIR = ROOT / "vendor" / "reference" / "WeFlow-gitcode"
WEFLOW_START_LOCK_STALE_SECONDS = 180.0
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    data_dir = Path(args.data_dir)
    weflow_result: dict[str, object] = {"status": "skipped", "component": "weflow", "reason": "disabled"}
    if args.weflow != "off":
        weflow_result = ensure_weflow_started(
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
    _write_sidebar_launch_state(data_dir, args, weflow_result)

    if args.mode == "server":
        from app.personal_wechat_bot.control.sidebar_server import run_sidebar_server

        print(f"Starting sidebar server at http://{args.host}:{args.port}", flush=True)
        print("Close this window or press Ctrl+C to stop.", flush=True)
        run_sidebar_server(data_dir, host=args.host, port=args.port)
        return 0

    from app.personal_wechat_bot.control.sidebar_window import run_sidebar_window

    print("Starting sidebar app window...", flush=True)
    print("Close this window or press Ctrl+C to stop the local frontend.", flush=True)
    run_sidebar_window(data_dir, poll_interval_ms=args.interval_ms)
    return 0


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
    base_url = f"http://{host}:{port}"
    health = weflow_health(base_url)
    if health.get("status") == "ok":
        return {"status": "ok", "component": "weflow", "state": "already_running", "base_url": base_url, "health": health}

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
    existing_start = _existing_weflow_start(lock_path, base_url=base_url, token=token, wait_seconds=wait_seconds)
    if existing_start is not None:
        return existing_start
    if not _try_acquire_weflow_start_lock(lock_path):
        existing_start = _existing_weflow_start(lock_path, base_url=base_url, token=token, wait_seconds=wait_seconds)
        if existing_start is not None:
            return existing_start
        _remove_weflow_start_lock(lock_path)
        if not _try_acquire_weflow_start_lock(lock_path):
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
        _remove_weflow_start_lock(lock_path)
        return {
            "status": "error" if required else "skipped",
            "component": "weflow",
            "reason": f"failed to launch WeFlow: {type(exc).__name__}: {exc}",
            "stdout": str(out),
            "stderr": str(err),
        }
    _write_weflow_start_lock(
        lock_path,
        {
            "pid": process.pid,
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
            _remove_weflow_start_lock(lock_path)
            return {
                "status": "error" if required else "starting",
                "component": "weflow",
                "state": "exited",
                "pid": process.pid,
                "returncode": process.returncode,
                "stdout": str(out),
                "stderr": str(err),
            }
        last_health = weflow_health(base_url, token=token)
        if last_health.get("status") == "ok":
            _remove_weflow_start_lock(lock_path)
            return {
                "status": "ok",
                "component": "weflow",
                "state": "started",
                "pid": process.pid,
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
        "base_url": base_url,
        "last_health": last_health,
        "stdout": str(out),
        "stderr": str(err),
        "lock": str(lock_path),
    }


def _weflow_start_lock_path(data_dir: Path) -> Path:
    return data_dir / "runtime" / "weflow_start.lock"


def _try_acquire_weflow_start_lock(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": 0, "updated_at_epoch": time.time()}, handle, ensure_ascii=False)
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
    age = _lock_age_seconds(lock if isinstance(lock, dict) else {})
    health = weflow_health(base_url, token=token)
    if health.get("status") == "ok":
        _remove_weflow_start_lock(lock_path)
        return {"status": "ok", "component": "weflow", "state": "already_running", "base_url": base_url, "health": health}
    if pid <= 0 and age <= 20.0:
        return {
            "status": "starting",
            "component": "weflow",
            "state": "start_lock_pending",
            "base_url": base_url,
            "last_health": health,
            "lock": str(lock_path),
        }
    if pid > 0 and _pid_exists(pid) and age <= WEFLOW_START_LOCK_STALE_SECONDS:
        deadline = time.monotonic() + max(1.0, wait_seconds)
        last_health = health
        while time.monotonic() < deadline:
            last_health = weflow_health(base_url, token=token)
            if last_health.get("status") == "ok":
                _remove_weflow_start_lock(lock_path)
                return {
                    "status": "ok",
                    "component": "weflow",
                    "state": "started_by_existing_launcher",
                    "pid": pid,
                    "base_url": base_url,
                    "health": last_health,
                }
            if not _pid_exists(pid):
                _remove_weflow_start_lock(lock_path)
                return None
            time.sleep(1.0)
        return {
            "status": "starting",
            "component": "weflow",
            "state": "start_in_progress",
            "pid": pid,
            "base_url": base_url,
            "last_health": last_health,
            "lock": str(lock_path),
        }
    if pid > 0 and _pid_exists(pid):
        _terminate_pid(pid, tree=True)
    _remove_weflow_start_lock(lock_path)
    return None


def _write_weflow_start_lock(lock_path: Path, payload: dict[str, object]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_weflow_start_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


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
        os.kill(pid, 15)
    except OSError:
        return


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
    payload = {
        "pid": os.getpid(),
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
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (runtime_dir / "sidebar_launch.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
