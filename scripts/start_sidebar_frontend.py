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
    if args.weflow != "off":
        result = ensure_weflow_started(
            data_dir=data_dir,
            host=args.weflow_host,
            port=args.weflow_port,
            install_deps=args.install_weflow_deps,
            wait_seconds=args.weflow_wait_seconds,
            required=args.weflow == "on",
            hidden=args.weflow_window == "hidden",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if args.weflow == "on" and result.get("status") == "error":
            return 2

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
    with out.open("ab") as stdout, err.open("ab") as stderr:
        process = subprocess.Popen(
            [npm, "run", "electron:dev"],
            cwd=str(WEFLOW_DIR),
            env=env,
            stdout=stdout,
            stderr=stderr,
            shell=False,
        )

    deadline = time.monotonic() + max(1.0, wait_seconds)
    last_health: dict[str, object] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
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
    }


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


if __name__ == "__main__":
    raise SystemExit(main())
