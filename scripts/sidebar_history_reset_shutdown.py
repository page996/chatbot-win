from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WEFLOW_DIR = ROOT / "vendor" / "reference" / "WeFlow-gitcode"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.personal_wechat_bot.runtime.process_lock import blocking_process_lock, process_start_marker
from app.personal_wechat_bot.control.sidebar_browser_runtime import (
    inspect_sidebar_browser_runtime,
    sidebar_browser_process_inventory,
)


class ShutdownVerificationError(RuntimeError):
    def __init__(self, message: str, *, checks: dict[str, Any]):
        super().__init__(message)
        self.checks = checks


def _lexical_absolute_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not raw or not os.path.isabs(raw):
        raise ValueError("path must be absolute")
    return Path(os.path.abspath(os.path.normpath(raw)))


def _path_is_reparse_or_symlink(path: Path, path_stat: os.stat_result | None = None) -> bool:
    try:
        current_stat = path.lstat() if path_stat is None else path_stat
    except OSError:
        return False
    return bool(
        stat.S_ISLNK(current_stat.st_mode)
        or int(getattr(current_stat, "st_file_attributes", 0) or 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_plain_directory(path: Path, *, allow_missing: bool = False) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise ValueError(f"missing directory: {path}")
    if _path_is_reparse_or_symlink(path, path_stat) or not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"unsafe directory: {path}")


def _validated_data_dir(value: str | os.PathLike[str]) -> Path:
    data_dir = _lexical_absolute_path(value)
    anchor = Path(data_dir.anchor) if data_dir.anchor else None
    if anchor is not None and os.path.normcase(str(data_dir)) == os.path.normcase(str(anchor)):
        raise ValueError("data directory cannot be a filesystem root")
    _require_plain_directory(data_dir)
    _require_plain_directory(data_dir / "runtime", allow_missing=True)
    _require_plain_directory(data_dir / "runtime_locks", allow_missing=True)
    return data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stop sidebar/WeFlow around a history reset.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-process-start", default="")
    parser.add_argument("--shutdown-owner-token", default="")
    parser.add_argument("--weflow", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--weflow-port", type=int, default=5031)
    parser.add_argument("--weflow-pid", type=int, default=0)
    parser.add_argument("--weflow-process-start", default="")
    parser.add_argument("--response-delay-seconds", type=float, default=1.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data_dir = _validated_data_dir(args.data_dir)
    except (OSError, ValueError):
        return 2
    parent_process_start = str(args.parent_process_start or "").strip()
    shutdown_owner_token = str(args.shutdown_owner_token or "").strip()
    parent_preflight = _preflight_sidebar_parent(
        args.parent_pid,
        data_dir=data_dir,
        expected_process_start=parent_process_start,
    )
    time.sleep(max(0.1, float(args.response_delay_seconds)))
    authorization = _verify_shutdown_authorization(
        data_dir,
        parent_pid=args.parent_pid,
        parent_process_start=parent_process_start,
        shutdown_owner_token=shutdown_owner_token,
    )
    if not bool(authorization.get("authorized")):
        if bool(authorization.get("lock_owned_by_helper")):
            _remove_shutdown_lock(data_dir, shutdown_owner_token=shutdown_owner_token)
        return 2
    shutdown_checks: dict[str, Any] = {
        "authorization": authorization,
        "sidebar_parent_preflight": parent_preflight,
    }
    if not bool(parent_preflight.get("verified_identity")):
        _write_status(
            data_dir,
            {
                "status": "error",
                "phase": "parent_preflight_failed",
                "manual_reopen_required": True,
                "shutdown_checks": shutdown_checks,
            },
            shutdown_owner_token=shutdown_owner_token,
        )
        _remove_shutdown_lock(data_dir, shutdown_owner_token=shutdown_owner_token)
        return 2
    browser_preflight = _preflight_sidebar_browser(
        data_dir,
        parent_pid=args.parent_pid,
        parent_process_start=parent_process_start,
    )
    shutdown_checks["sidebar_browser_preflight"] = browser_preflight
    if not bool(browser_preflight.get("verified_identity")):
        _write_status(
            data_dir,
            {
                "status": "error",
                "phase": "browser_preflight_failed",
                "manual_reopen_required": True,
                "shutdown_checks": shutdown_checks,
            },
            shutdown_owner_token=shutdown_owner_token,
        )
        _remove_shutdown_lock(data_dir, shutdown_owner_token=shutdown_owner_token)
        return 2

    try:
        runtime_locks_dir = data_dir / "runtime_locks"
        try:
            runtime_locks_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _require_plain_directory(runtime_locks_dir)
        with blocking_process_lock(
            runtime_locks_dir / "weflow_lifecycle.lock",
            label="weflow_lifecycle_history_reset",
            stale_after_seconds=3600.0,
            wait_timeout_seconds=120.0,
        ):
            _validated_data_dir(data_dir)
            _write_status(
                data_dir,
                {"status": "running", "phase": "scheduled", "parent_pid": args.parent_pid},
                shutdown_owner_token=shutdown_owner_token,
            )
            _write_status(
                data_dir,
                {"status": "running", "phase": "closing_sidebar_window"},
                shutdown_owner_token=shutdown_owner_token,
            )
            browser_processes = (
                browser_preflight.get("runtime", {}).get("browser_process_tree", [])
                if isinstance(browser_preflight.get("runtime"), dict)
                else []
            )
            _close_sidebar_windows(
                tuple(
                    int(item.get("pid") or 0)
                    for item in browser_processes
                    if isinstance(item, dict)
                    and bool(item.get("identity_verified"))
                    and int(item.get("pid") or 0) > 0
                )
            )

            _write_status(
                data_dir,
                {"status": "running", "phase": "stopping_sidebar_server"},
                shutdown_owner_token=shutdown_owner_token,
            )
            parent_result = _stop_sidebar_parent(
                args.parent_pid,
                data_dir=data_dir,
                expected_creation_date=str(parent_preflight.get("creation_date") or ""),
                expected_process_start=parent_process_start,
            )
            shutdown_checks["sidebar_parent"] = parent_result
            _write_status(
                data_dir,
                {"status": "running", "phase": "stopping_sidebar_server", "sidebar_parent_stop": parent_result},
                shutdown_owner_token=shutdown_owner_token,
            )
            if not bool(parent_result.get("verified_stopped")):
                raise ShutdownVerificationError("Sidebar parent shutdown could not be verified", checks=shutdown_checks)

            _write_status(
                data_dir,
                {"status": "running", "phase": "stopping_sidebar_browser"},
                shutdown_owner_token=shutdown_owner_token,
            )
            browser_result = _stop_sidebar_browser_profile_processes(
                data_dir,
                expected_processes=tuple(
                    item
                    for item in browser_processes
                    if isinstance(item, dict) and bool(item.get("identity_verified"))
                ),
            )
            shutdown_checks["sidebar_browser"] = browser_result
            if not bool(browser_result.get("verified_stopped")):
                raise ShutdownVerificationError("Sidebar browser shutdown could not be verified", checks=shutdown_checks)

            _write_status(
                data_dir,
                {"status": "running", "phase": "stopping_weflow"},
                shutdown_owner_token=shutdown_owner_token,
            )
            stop_result = _stop_weflow(
                args.weflow_pid,
                args.weflow_port,
                str(args.weflow_process_start or ""),
            )
            shutdown_checks["weflow"] = stop_result
            start_lock_result = _finalize_weflow_start_lock(data_dir, stop_result)
            shutdown_checks["weflow_start_lock"] = start_lock_result
            _write_status(
                data_dir,
                {
                    "status": "running",
                    "phase": "stopping_weflow",
                    "weflow_stop": stop_result,
                    "weflow_start_lock": start_lock_result,
                },
                shutdown_owner_token=shutdown_owner_token,
            )
            if not bool(stop_result.get("verified_stopped")) or not bool(start_lock_result.get("verified")):
                raise ShutdownVerificationError("WeFlow shutdown could not be verified", checks=shutdown_checks)

            _write_status(
                data_dir,
                {"status": "running", "phase": "clearing_history"},
                shutdown_owner_token=shutdown_owner_token,
            )
            from app.personal_wechat_bot.control.sidebar_api import clear_sidebar_history_data

            clear_result = clear_sidebar_history_data(data_dir)
            clear_status = str(clear_result.get("status") or "")
            if clear_status == "ok":
                final_status, final_phase, return_code = "ok", "stopped_after_clear", 0
            elif clear_status == "blocked":
                final_status, final_phase, return_code = "blocked", "clear_blocked", 4
            else:
                final_status, final_phase, return_code = "partial_error", "clear_incomplete", 3
            _write_status(
                data_dir,
                {
                    "status": final_status,
                    "phase": final_phase,
                    "manual_reopen_required": True,
                    "clear_result": clear_result,
                },
                shutdown_owner_token=shutdown_owner_token,
            )
            return return_code
    except Exception as exc:
        checks = exc.checks if isinstance(exc, ShutdownVerificationError) else shutdown_checks
        _write_status(
            data_dir,
            {
                "status": "error",
                "phase": "shutdown_verification_failed" if isinstance(exc, ShutdownVerificationError) else "failed",
                "manual_reopen_required": True,
                "error": f"{type(exc).__name__}: {exc}",
                "shutdown_checks": checks,
            },
            shutdown_owner_token=shutdown_owner_token,
        )
        return 2
    finally:
        _remove_shutdown_lock(data_dir, shutdown_owner_token=shutdown_owner_token)


def _close_sidebar_windows(browser_pids: tuple[int, ...] = ()) -> None:
    try:
        from app.personal_wechat_bot.control.sidebar_window import _close_existing_sidebar_windows

        _close_existing_sidebar_windows(browser_pids)
    except Exception:
        return


def _preflight_sidebar_browser(
    data_dir: Path,
    *,
    parent_pid: int,
    parent_process_start: str,
) -> dict[str, Any]:
    status = inspect_sidebar_browser_runtime(data_dir, require_launch_state=True)
    launch_state = status.get("launch_state") if isinstance(status.get("launch_state"), dict) else {}
    recorded_parent_pid = int(launch_state.get("pid") or 0)
    recorded_parent_start = str(launch_state.get("process_start") or "").strip()
    if recorded_parent_pid != int(parent_pid) or recorded_parent_start != str(parent_process_start or "").strip():
        return {
            "verified_identity": False,
            "state": "launch_state_parent_identity_mismatch",
            "expected_parent_pid": int(parent_pid),
            "recorded_parent_pid": recorded_parent_pid,
            "expected_parent_process_start": str(parent_process_start or ""),
            "recorded_parent_process_start": recorded_parent_start,
            "runtime": status,
        }
    if not bool(status.get("verified")) or not bool(status.get("inventory_verified")):
        return {
            "verified_identity": False,
            "state": "browser_runtime_identity_unavailable",
            "runtime": status,
        }
    recorded_process = status.get("recorded_process") if isinstance(status.get("recorded_process"), dict) else {}
    if recorded_process.get("state") == "identity_unavailable":
        return {
            "verified_identity": False,
            "state": "recorded_browser_identity_unavailable",
            "recorded_process": recorded_process,
            "runtime": status,
        }
    unverified = [
        item
        for item in status.get("browser_process_tree", [])
        if isinstance(item, dict) and not bool(item.get("identity_verified"))
    ]
    unverified.extend(
        item
        for item in status.get("recorded_descendant_errors", [])
        if isinstance(item, dict)
    )
    if unverified:
        return {
            "verified_identity": False,
            "state": "browser_profile_process_identity_unavailable",
            "unverified_processes": unverified,
            "runtime": status,
        }
    if status.get("profile_processes") and not bool(launch_state.get("browser_owned")):
        return {
            "verified_identity": False,
            "state": "unowned_browser_profile_processes_active",
            "runtime": status,
        }
    return {
        "verified_identity": True,
        "state": "matched",
        "runtime": status,
    }


def _stop_sidebar_browser_profile_processes(
    data_dir: Path,
    *,
    expected_processes: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    terminated: list[dict[str, Any]] = []
    expected_by_pid = {
        int(item.get("pid") or 0): item
        for item in expected_processes
        if int(item.get("pid") or 0) > 0
    }
    expected_inventory: dict[int, dict[str, Any]] = {}
    if expected_by_pid:
        try:
            expected_inventory = {
                int(item.get("pid") or 0): item
                for item in sidebar_browser_process_inventory()
                if int(item.get("pid") or 0) > 0
            }
        except RuntimeError as exc:
            return {
                "verified_stopped": False,
                "state": "browser_process_inventory_unavailable",
                "reason": str(exc),
                "terminated": terminated,
            }
    for pid, recorded in sorted(
        expected_by_pid.items(),
        key=lambda pair: int(pair[1].get("tree_depth") or 0),
        reverse=True,
    ):
        failure = _terminate_recorded_sidebar_browser_process(
            pid,
            recorded,
            terminated,
            current=expected_inventory.get(pid),
        )
        if failure is not None:
            return failure
    for _attempt in range(3):
        status = inspect_sidebar_browser_runtime(data_dir, require_launch_state=True)
        if not bool(status.get("verified")) or not bool(status.get("inventory_verified")):
            return {
                "verified_stopped": False,
                "state": "browser_runtime_identity_unavailable",
                "runtime": status,
                "terminated": terminated,
            }
        processes = [item for item in status.get("browser_process_tree", []) if isinstance(item, dict)]
        if not processes:
            return {
                "verified_stopped": True,
                "state": "stopped",
                "runtime": status,
                "terminated": terminated,
            }
        for recorded in processes:
            if not bool(recorded.get("identity_verified")):
                return {
                    "verified_stopped": False,
                    "state": "browser_process_identity_unavailable",
                    "process": recorded,
                    "runtime": status,
                    "terminated": terminated,
                }
            pid = int(recorded.get("pid") or 0)
            expected_start = str(recorded.get("process_start") or "").strip()
            expected_image = _normalized_executable_path(str(recorded.get("image") or ""))
            current = recorded
            current_start = process_start_marker(pid)
            current_image = _normalized_executable_path(str(current.get("image") or ""))
            if not current_start and _pid_state(pid) is False:
                continue
            if (
                not expected_start
                or not current_start
                or current_start != expected_start
                or not expected_image
                or current_image != expected_image
            ):
                return {
                    "verified_stopped": False,
                    "state": "browser_process_identity_changed_before_terminate",
                    "pid": pid,
                    "expected_process_start": expected_start,
                    "current_process_start": current_start,
                    "expected_image": expected_image,
                    "current_image": current_image,
                    "terminated": terminated,
                }
            termination = _terminate_pid(
                pid,
                tree=False,
                expected_process_start=expected_start,
            )
            exited = _termination_exited(pid, termination, timeout_seconds=8.0)
            record = {
                "pid": pid,
                "process_start": expected_start,
                "image": str(current.get("image") or ""),
                "termination": termination,
                "exited": exited,
            }
            terminated.append(record)
            if not exited:
                return {
                    "verified_stopped": False,
                    "state": "browser_process_still_running",
                    "process": record,
                    "terminated": terminated,
                }
        time.sleep(0.1)
    final_status = inspect_sidebar_browser_runtime(data_dir, require_launch_state=True)
    remaining = [item for item in final_status.get("browser_process_tree", []) if isinstance(item, dict)]
    return {
        "verified_stopped": not remaining and bool(final_status.get("verified")),
        "state": "stopped" if not remaining and bool(final_status.get("verified")) else "profile_processes_remain",
        "runtime": final_status,
        "remaining": remaining,
        "terminated": terminated,
    }


def _terminate_recorded_sidebar_browser_process(
    pid: int,
    recorded: dict[str, Any],
    terminated: list[dict[str, Any]],
    *,
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    expected_start = str(recorded.get("process_start") or "").strip()
    expected_image = _normalized_executable_path(
        str(recorded.get("image") or recorded.get("executable") or "")
    )
    if current is None:
        current_start = process_start_marker(pid)
        if current_start and current_start != expected_start:
            return None
        if current_start == expected_start or _pid_state(pid) is not False:
            return {
                "verified_stopped": False,
                "state": "recorded_browser_process_missing_from_inventory",
                "pid": pid,
                "expected_process_start": expected_start,
                "current_process_start": current_start,
                "terminated": terminated,
            }
        return None
    current_start = process_start_marker(pid)
    current_image = _normalized_executable_path(str(current.get("image") or ""))
    if not current_start and _pid_state(pid) is False:
        return None
    if current_start and current_start != expected_start:
        return None
    if (
        not expected_start
        or not current_start
        or not expected_image
        or current_image != expected_image
    ):
        return {
            "verified_stopped": False,
            "state": "browser_process_identity_changed_before_terminate",
            "pid": pid,
            "expected_process_start": expected_start,
            "current_process_start": current_start,
            "expected_image": expected_image,
            "current_image": current_image,
            "terminated": terminated,
        }
    termination = _terminate_pid(
        pid,
        tree=False,
        expected_process_start=expected_start,
    )
    exited = _termination_exited(pid, termination, timeout_seconds=8.0)
    record = {
        "pid": pid,
        "process_start": expected_start,
        "image": str(current.get("image") or ""),
        "termination": termination,
        "exited": exited,
    }
    terminated.append(record)
    if not exited:
        return {
            "verified_stopped": False,
            "state": "browser_process_still_running",
            "process": record,
            "terminated": terminated,
        }
    return None
def _normalized_executable_path(value: str) -> str:
    if not str(value or "").strip():
        return ""
    return os.path.normcase(os.path.abspath(os.path.normpath(value)))


def _stop_weflow(known_pid: int, port: int, known_process_start: str = "") -> dict[str, Any]:
    sources: dict[int, set[str]] = {}
    if known_pid > 0:
        sources.setdefault(known_pid, set()).add("known_pid")
    initial_port_pids = _pids_listening_on_port(port)
    for pid in initial_port_pids:
        sources.setdefault(pid, set()).add("port_listener")
    discovery_error = ""
    try:
        project_pids = _project_weflow_pids()
    except RuntimeError as exc:
        project_pids = []
        discovery_error = str(exc)
    for pid in project_pids:
        sources.setdefault(pid, set()).add("project_scan")

    inspected: list[dict[str, Any]] = []
    terminated_pids: list[int] = []
    blocked_pids: list[dict[str, Any]] = []
    waited: list[dict[str, Any]] = []
    for pid, pid_sources in sorted(sources.items()):
        item, should_terminate = _inspect_weflow_pid(
            pid,
            pid_sources,
            known_process_start=known_process_start if "known_pid" in pid_sources else "",
        )
        inspected.append(item)
        if item["state"] == "identity_unavailable":
            blocked_pids.append(item)
            continue
        if item["state"] == "identity_mismatch":
            blocked_pids.append(item)
            continue
        if not should_terminate:
            continue
        child_result = _stop_verified_weflow_child_tree(
            pid,
            expected_root_process_start=str(item.get("process_start") or ""),
        )
        item["child_processes"] = child_result
        for child_pid in child_result.get("terminated_pids", []):
            child_pid = int(child_pid)
            if child_pid > 0 and child_pid not in terminated_pids:
                terminated_pids.append(child_pid)
        if not bool(child_result.get("verified_stopped")):
            blocked_pids.append(
                {
                    "pid": pid,
                    "state": "weflow_child_tree_unverified",
                    "child_processes": child_result,
                }
            )
        item["termination"] = _terminate_pid(
            pid,
            tree=False,
            expected_process_start=str(item.get("process_start") or ""),
        )
        exited = _termination_exited(pid, item["termination"], timeout_seconds=8.0)
        waited.append({"pid": pid, "exited": exited})
        if exited and bool(item["termination"].get("attempted")):
            terminated_pids.append(pid)
        elif not exited:
            blocked_pids.append({"pid": pid, "state": "still_running_after_terminate"})

    port_released = _wait_for_port_release(port, timeout_seconds=15.0)
    if not port_released:
        late_pids = [pid for pid in _pids_listening_on_port(port) if pid not in sources]
        for pid in late_pids:
            item, should_terminate = _inspect_weflow_pid(pid, {"late_port_listener"})
            inspected.append(item)
            if not should_terminate:
                blocked_pids.append(item)
                continue
            child_result = _stop_verified_weflow_child_tree(
                pid,
                expected_root_process_start=str(item.get("process_start") or ""),
            )
            item["child_processes"] = child_result
            for child_pid in child_result.get("terminated_pids", []):
                child_pid = int(child_pid)
                if child_pid > 0 and child_pid not in terminated_pids:
                    terminated_pids.append(child_pid)
            if not bool(child_result.get("verified_stopped")):
                blocked_pids.append(
                    {
                        "pid": pid,
                        "state": "weflow_child_tree_unverified",
                        "child_processes": child_result,
                    }
                )
            item["termination"] = _terminate_pid(
                pid,
                tree=False,
                expected_process_start=str(item.get("process_start") or ""),
            )
            exited = _termination_exited(pid, item["termination"], timeout_seconds=8.0)
            waited.append({"pid": pid, "exited": exited})
            if exited and bool(item["termination"].get("attempted")):
                terminated_pids.append(pid)
            elif not exited:
                blocked_pids.append({"pid": pid, "state": "still_running_after_terminate"})
        port_released = _wait_for_port_release(port, timeout_seconds=8.0)
    remaining_port_pids = _pids_listening_on_port(port)
    remaining_project_pids: list[int] = []
    try:
        remaining_project_pids = _project_weflow_pids()
    except RuntimeError as exc:
        discovery_error = discovery_error or str(exc)
    verified_stopped = bool(
        port_released
        and not remaining_port_pids
        and not remaining_project_pids
        and not blocked_pids
        and not discovery_error
    )
    return {
        "verified_stopped": verified_stopped,
        "terminated_pids": terminated_pids,
        "inspected": inspected,
        "waited": waited,
        "port": port,
        "port_released": port_released,
        "remaining_port_pids": remaining_port_pids,
        "project_scan_pids": project_pids,
        "remaining_project_pids": remaining_project_pids,
        "discovery_error": discovery_error,
        "blocked_pids": blocked_pids,
    }


def _inspect_weflow_pid(
    pid: int,
    sources: set[str],
    *,
    known_process_start: str = "",
) -> tuple[dict[str, Any], bool]:
    if "known_pid" in sources and not str(known_process_start or "").strip():
        info = _process_info(pid)
        item = {"pid": pid, "sources": sorted(sources)}
        if str(info.get("status") or "") == "absent":
            item["state"] = "already_exited"
            return item, False
        item["state"] = "identity_unavailable"
        item["reason"] = "missing_known_process_start"
        return item, False
    info = _stable_process_sample(pid)
    item: dict[str, Any] = {"pid": pid, "sources": sorted(sources)}
    status = str(info.get("status") or "error")
    if status == "absent":
        item["state"] = "already_exited"
        return item, False
    if status != "running":
        item["state"] = "identity_unavailable"
        item["reason"] = str(info.get("reason") or "process_query_failed")
        return item, False
    current_process_start = str(info.get("process_start") or "")
    item["process_start"] = current_process_start
    marker_matches = False
    if "known_pid" in sources and known_process_start:
        item["recorded_process_start"] = known_process_start
        item["current_process_start"] = current_process_start
        if not current_process_start:
            item["state"] = "identity_unavailable"
            item["reason"] = "process_start_query_failed"
            return item, False
        marker_matches = current_process_start == known_process_start
        if not marker_matches and "port_listener" not in sources and "project_scan" not in sources:
            item["state"] = "start_marker_mismatch"
            item["identity"] = "stale_known_pid"
            return item, False
    matched, identity = _matches_weflow_process(info)
    item["image"] = str(info.get("image") or "")
    if matched:
        item["identity"] = identity
        item["state"] = "matched"
    elif marker_matches and ("port_listener" in sources or "late_port_listener" in sources):
        item["identity"] = "weflow_launch_marker_and_port_listener"
        item["state"] = "matched"
    else:
        item["identity"] = identity
        item["state"] = "identity_mismatch"
        if marker_matches:
            item["reason"] = "known_pid_missing_project_or_listener_identity"
    return item, item["state"] == "matched"


def _stable_process_sample(pid: int) -> dict[str, Any]:
    process_start_before = process_start_marker(pid)
    info = _process_info(pid)
    if str(info.get("status") or "") == "absent":
        return info
    if str(info.get("status") or "") != "running":
        return info
    process_start_after = process_start_marker(pid)
    if not process_start_before or not process_start_after:
        return {"status": "error", "pid": pid, "reason": "process_start_query_failed"}
    if process_start_before != process_start_after:
        return {"status": "error", "pid": pid, "reason": "process_changed_during_identity_query"}
    result = dict(info)
    result["process_start"] = process_start_before
    return result


def _project_weflow_pids() -> list[int]:
    matches: set[int] = set()
    for item in _process_table():
        pid = int(item.get("pid", 0) or 0)
        matched, _identity = _matches_weflow_process(item)
        if pid > 0 and matched:
            matches.add(pid)
    return sorted(matches)


def _stop_verified_weflow_child_tree(
    root_pid: int,
    *,
    expected_root_process_start: str,
) -> dict[str, Any]:
    root_start_before = process_start_marker(root_pid)
    if not root_start_before or root_start_before != expected_root_process_start:
        return {
            "verified_stopped": False,
            "state": "root_process_start_changed_before_child_inventory",
            "expected_root_process_start": expected_root_process_start,
            "current_root_process_start": root_start_before,
            "terminated_pids": [],
        }
    try:
        descendants = _process_descendants(root_pid)
    except RuntimeError as exc:
        return {
            "verified_stopped": False,
            "state": "child_inventory_failed",
            "reason": str(exc),
            "terminated_pids": [],
        }
    root_start_after = process_start_marker(root_pid)
    if not root_start_after or root_start_after != root_start_before:
        return {
            "verified_stopped": False,
            "state": "root_process_changed_during_child_inventory",
            "expected_root_process_start": expected_root_process_start,
            "current_root_process_start": root_start_after,
            "terminated_pids": [],
        }

    by_pid = {int(item.get("pid", 0) or 0): item for item in descendants if int(item.get("pid", 0) or 0) > 0}

    def depth(pid: int) -> int:
        seen: set[int] = set()
        current = pid
        result = 0
        while current in by_pid and current not in seen:
            seen.add(current)
            parent_pid = int(by_pid[current].get("parent_pid", 0) or 0)
            if parent_pid not in by_pid:
                break
            current = parent_pid
            result += 1
        return result

    terminated: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    terminated_pids: list[int] = []
    for child_pid in sorted(by_pid, key=lambda item: (depth(item), item), reverse=True):
        inventory = by_pid[child_pid]
        info = _stable_process_sample(child_pid)
        status = str(info.get("status") or "error")
        if status == "absent":
            continue
        if status != "running":
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "identity_unavailable",
                    "reason": str(info.get("reason") or "process_query_failed"),
                }
            )
            continue
        expected_creation_date = str(inventory.get("creation_date") or "").strip()
        current_creation_date = str(info.get("creation_date") or "").strip()
        if not expected_creation_date or not current_creation_date:
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "creation_time_unavailable",
                    "expected_creation_date": expected_creation_date,
                    "current_creation_date": current_creation_date,
                }
            )
            continue
        if current_creation_date != expected_creation_date:
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "pid_reused_or_creation_time_changed",
                    "expected_creation_date": expected_creation_date,
                    "current_creation_date": current_creation_date,
                }
            )
            continue
        child_process_start = str(info.get("process_start") or "")
        termination = _terminate_pid(
            child_pid,
            tree=False,
            expected_process_start=child_process_start,
        )
        exited = _termination_exited(child_pid, termination, timeout_seconds=8.0)
        record = {"pid": child_pid, "termination": termination, "exited": exited}
        terminated.append(record)
        if exited:
            if bool(termination.get("attempted")):
                terminated_pids.append(child_pid)
        else:
            blocked.append({"pid": child_pid, "state": "still_running_after_terminate"})
    return {
        "verified_stopped": not blocked,
        "state": "stopped" if not blocked else "blocked",
        "terminated": terminated,
        "terminated_pids": terminated_pids,
        "blocked": blocked,
    }


def _preflight_sidebar_parent(
    pid: int,
    *,
    data_dir: Path,
    expected_process_start: str,
) -> dict[str, Any]:
    expected_process_start = str(expected_process_start or "").strip()
    if not expected_process_start:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "missing_parent_process_start",
        }
    sampled_process_start = process_start_marker(pid)
    if not sampled_process_start:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "process_start_query_failed",
        }
    if sampled_process_start != expected_process_start:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "parent_process_start_mismatch",
            "expected_process_start": expected_process_start,
            "current_process_start": sampled_process_start,
        }
    info = _process_info(pid)
    status = str(info.get("status") or "error")
    if status != "running":
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "identity_unavailable",
            "reason": str(info.get("reason") or "process_query_failed"),
        }
    sampled_process_start_after = process_start_marker(pid)
    if not sampled_process_start_after or sampled_process_start_after != sampled_process_start:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "parent_process_changed_during_preflight",
            "expected_process_start": expected_process_start,
            "current_process_start": sampled_process_start_after,
        }
    creator_pid = os.getppid()
    if creator_pid > 0 and creator_pid != pid:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "not_helper_parent",
            "helper_parent_pid": creator_pid,
        }
    matched, identity = _matches_sidebar_process(info, data_dir=data_dir)
    if not matched:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "identity_mismatch",
            "image": str(info.get("image") or ""),
            "identity": identity,
        }
    creation_date = str(info.get("creation_date") or "").strip()
    if not creation_date:
        return {
            "pid": pid,
            "verified_identity": False,
            "state": "creation_time_unavailable",
            "image": str(info.get("image") or ""),
            "identity": identity,
        }
    return {
        "pid": pid,
        "verified_identity": True,
        "state": "matched",
        "image": str(info.get("image") or ""),
        "identity": identity,
        "creation_date": creation_date,
        "process_start": sampled_process_start,
    }


def _verify_shutdown_authorization(
    data_dir: Path,
    *,
    parent_pid: int,
    parent_process_start: str,
    shutdown_owner_token: str,
) -> dict[str, Any]:
    try:
        data_dir = _validated_data_dir(data_dir)
    except (OSError, ValueError) as exc:
        return {
            "authorized": False,
            "lock_owned_by_helper": False,
            "reason": f"unsafe_data_dir: {type(exc).__name__}",
        }
    parent_process_start = str(parent_process_start or "").strip()
    shutdown_owner_token = str(shutdown_owner_token or "").strip()
    if not parent_process_start:
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "missing_parent_process_start"}
    if not shutdown_owner_token:
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "missing_shutdown_owner_token"}
    config_path = data_dir / "config.json"
    lock_path = data_dir / "runtime" / "history_reset_shutdown.lock"
    expected_status_path = data_dir / "runtime" / "history_reset_shutdown.json"
    try:
        payload, _lock_identity = _read_private_json_file(lock_path)
    except FileNotFoundError:
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "missing_shutdown_lock"}
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "authorized": False,
            "lock_owned_by_helper": False,
            "reason": f"invalid_shutdown_lock: {type(exc).__name__}",
        }
    if not isinstance(payload, dict):
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "invalid_shutdown_lock_payload"}
    try:
        helper_pid = int(payload.get("helper_pid") or 0)
    except (TypeError, ValueError):
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "invalid_helper_pid"}
    payload_owner_token = str(payload.get("owner_token") or "").strip()
    helper_process_start = str(payload.get("helper_process_start") or "").strip()
    current_helper_process_start = process_start_marker(helper_pid) if helper_pid == os.getpid() else ""
    lock_owned_by_helper = bool(
        helper_pid == os.getpid()
        and helper_process_start
        and current_helper_process_start == helper_process_start
        and payload_owner_token == shutdown_owner_token
    )
    status_file_text = str(payload.get("status_file") or "").strip()
    lock_data_dir_text = str(payload.get("data_dir") or "").strip()
    if not status_file_text or not lock_data_dir_text:
        return {
            "authorized": False,
            "lock_owned_by_helper": lock_owned_by_helper,
            "reason": "missing_shutdown_lock_fields",
        }
    try:
        owner_pid = int(payload.get("owner_pid") or 0)
        updated_at_epoch = float(payload.get("updated_at_epoch") or 0.0)
        status_path = _lexical_absolute_path(status_file_text)
        lock_data_dir = _lexical_absolute_path(lock_data_dir_text)
    except (OSError, TypeError, ValueError):
        return {
            "authorized": False,
            "lock_owned_by_helper": lock_owned_by_helper,
            "reason": "invalid_shutdown_lock_fields",
        }
    if helper_pid != os.getpid():
        return {"authorized": False, "lock_owned_by_helper": False, "reason": "helper_pid_mismatch"}
    if not helper_process_start or current_helper_process_start != helper_process_start:
        return {
            "authorized": False,
            "lock_owned_by_helper": lock_owned_by_helper,
            "reason": "helper_process_start_mismatch",
        }
    if payload_owner_token != shutdown_owner_token:
        return {
            "authorized": False,
            "lock_owned_by_helper": False,
            "reason": "shutdown_owner_token_mismatch",
        }
    try:
        _config_payload, _config_identity = _read_private_json_file(config_path)
    except FileNotFoundError:
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "missing_owned_config"}
    except (OSError, UnicodeError, ValueError):
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "unsafe_config"}
    if owner_pid != parent_pid or owner_pid != os.getppid():
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "owner_pid_mismatch"}
    owner_process_start = str(payload.get("owner_process_start") or "").strip()
    if owner_process_start != parent_process_start:
        return {
            "authorized": False,
            "lock_owned_by_helper": lock_owned_by_helper,
            "reason": "owner_process_start_mismatch",
        }
    current_parent_start = process_start_marker(parent_pid)
    if not current_parent_start or current_parent_start != parent_process_start:
        return {
            "authorized": False,
            "lock_owned_by_helper": lock_owned_by_helper,
            "reason": "live_parent_process_start_mismatch",
        }
    if os.path.normcase(str(lock_data_dir)) != os.path.normcase(str(data_dir)):
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "data_dir_mismatch"}
    if os.path.normcase(str(status_path)) != os.path.normcase(str(expected_status_path)):
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "status_path_mismatch"}
    if status_path.exists():
        try:
            status_stat = status_path.lstat()
            if (
                _path_is_reparse_or_symlink(status_path, status_stat)
                or not stat.S_ISREG(status_stat.st_mode)
                or int(getattr(status_stat, "st_nlink", 1) or 1) != 1
            ):
                return {
                    "authorized": False,
                    "lock_owned_by_helper": lock_owned_by_helper,
                    "reason": "unsafe_status_file",
                }
        except OSError:
            return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "status_stat_failed"}
    age_seconds = time.time() - updated_at_epoch
    if updated_at_epoch <= 0 or age_seconds < -5.0 or age_seconds > 120.0:
        return {"authorized": False, "lock_owned_by_helper": lock_owned_by_helper, "reason": "stale_shutdown_lock"}
    return {
        "authorized": True,
        "lock_owned_by_helper": True,
        "owner_pid": owner_pid,
        "helper_pid": helper_pid,
        "lock_age_seconds": round(max(0.0, age_seconds), 3),
    }


def _stop_sidebar_parent(
    pid: int,
    *,
    data_dir: Path,
    expected_creation_date: str,
    expected_process_start: str,
) -> dict[str, Any]:
    info = _stable_process_sample(pid)
    status = str(info.get("status") or "error")
    if status == "absent":
        try:
            descendants = _process_descendants(pid)
        except RuntimeError as exc:
            return {
                "pid": pid,
                "verified_stopped": False,
                "state": "child_inventory_failed",
                "reason": str(exc),
            }
        parent_recheck = _process_info(pid)
        if str(parent_recheck.get("status") or "error") != "absent":
            return {
                "pid": pid,
                "verified_stopped": False,
                "state": "parent_pid_reappeared_after_exit",
                "reason": str(parent_recheck.get("reason") or "parent_identity_changed"),
            }
        child_result = _stop_verified_sidebar_children(descendants, data_dir=data_dir)
        verified_stopped = bool(child_result.get("verified_stopped"))
        return {
            "pid": pid,
            "verified_stopped": verified_stopped,
            "state": "already_exited" if verified_stopped else "child_processes_active",
            "child_processes": child_result,
        }
    if status != "running":
        return {
            "pid": pid,
            "verified_stopped": False,
            "state": "identity_unavailable",
            "reason": str(info.get("reason") or "process_query_failed"),
        }
    current_creation_date = str(info.get("creation_date") or "").strip()
    if not expected_creation_date or current_creation_date != expected_creation_date:
        return {
            "pid": pid,
            "verified_stopped": False,
            "state": "pid_reused_or_creation_time_changed",
        }
    matched, identity = _matches_sidebar_process(info, data_dir=data_dir)
    if not matched:
        return {
            "pid": pid,
            "verified_stopped": False,
            "state": "identity_mismatch",
            "image": str(info.get("image") or ""),
            "identity": identity,
        }
    try:
        descendants = _process_descendants(pid)
    except RuntimeError as exc:
        return {
            "pid": pid,
            "verified_stopped": False,
            "state": "child_inventory_failed",
            "image": str(info.get("image") or ""),
            "identity": identity,
            "reason": str(exc),
        }
    before_terminate = _stable_process_sample(pid)
    before_terminate_status = str(before_terminate.get("status") or "error")
    if before_terminate_status == "absent":
        termination = {"attempted": False, "reason": "already_exited_before_terminate"}
        exited = True
    elif before_terminate_status != "running":
        return {
            "pid": pid,
            "verified_stopped": False,
            "state": "identity_unavailable_before_terminate",
            "image": str(info.get("image") or ""),
            "identity": identity,
            "reason": str(before_terminate.get("reason") or "process_query_failed"),
        }
    else:
        before_terminate_creation_date = str(before_terminate.get("creation_date") or "").strip()
        if not expected_creation_date or before_terminate_creation_date != expected_creation_date:
            return {
                "pid": pid,
                "verified_stopped": False,
                "state": "pid_reused_or_creation_time_changed_before_terminate",
                "image": str(before_terminate.get("image") or ""),
            }
        before_terminate_matched, before_terminate_identity = _matches_sidebar_process(
            before_terminate,
            data_dir=data_dir,
        )
        if not before_terminate_matched:
            return {
                "pid": pid,
                "verified_stopped": False,
                "state": "identity_mismatch_before_terminate",
                "image": str(before_terminate.get("image") or ""),
                "identity": before_terminate_identity,
            }
        current_process_start = str(before_terminate.get("process_start") or "")
        if not expected_process_start or current_process_start != expected_process_start:
            return {
                "pid": pid,
                "verified_stopped": False,
                "state": "process_start_changed_before_terminate",
                "image": str(before_terminate.get("image") or ""),
                "expected_process_start": expected_process_start,
                "current_process_start": current_process_start,
            }
        termination = _terminate_pid(
            pid,
            tree=False,
            expected_process_start=expected_process_start,
        )
        exited = _termination_exited(pid, termination, timeout_seconds=8.0)
    if exited:
        try:
            post_exit_descendants = _process_descendants(pid)
        except RuntimeError as exc:
            child_result = {
                "verified_stopped": False,
                "state": "post_exit_child_inventory_failed",
                "reason": str(exc),
            }
        else:
            parent_recheck = _process_info(pid)
            if str(parent_recheck.get("status") or "error") != "absent":
                child_result = {
                    "verified_stopped": False,
                    "state": "parent_pid_reappeared_after_exit",
                    "reason": str(parent_recheck.get("reason") or "parent_identity_changed"),
                }
            else:
                combined: dict[int, dict[str, Any]] = {}
                for item in (*descendants, *post_exit_descendants):
                    child_pid = int(item.get("pid", 0) or 0)
                    if child_pid > 0:
                        combined.setdefault(child_pid, item)
                child_result = _stop_verified_sidebar_children(
                    [item for child_pid, item in combined.items() if child_pid > 0],
                    data_dir=data_dir,
                )
    else:
        child_result = {"verified_stopped": False, "state": "parent_still_running"}
    verified_stopped = bool(exited and child_result.get("verified_stopped"))
    return {
        "pid": pid,
        "verified_stopped": verified_stopped,
        "state": "stopped" if verified_stopped else ("child_processes_active" if exited else "still_running_after_terminate"),
        "image": str(info.get("image") or ""),
        "identity": identity,
        "termination": termination,
        "child_processes": child_result,
    }


def _process_descendants(parent_pid: int) -> list[dict[str, Any]]:
    table = _process_table()
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for item in table:
        by_parent.setdefault(int(item.get("parent_pid", 0) or 0), []).append(item)

    def collect(root_pid: int) -> dict[int, dict[str, Any]]:
        found: dict[int, dict[str, Any]] = {}
        pending = [root_pid]
        while pending:
            current = pending.pop()
            for child in by_parent.get(current, []):
                child_pid = int(child.get("pid", 0) or 0)
                if child_pid <= 0 or child_pid in found:
                    continue
                found[child_pid] = child
                pending.append(child_pid)
        return found

    descendants = collect(parent_pid)
    helper_tree = collect(os.getpid())
    excluded = {os.getpid(), *helper_tree.keys()}
    return [item for child_pid, item in descendants.items() if child_pid not in excluded]


def _process_table() -> list[dict[str, Any]]:
    if os.name == "nt":
        query = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -ErrorAction Stop | "
            "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate | "
            "ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except Exception as exc:
            raise RuntimeError(f"process tree query failed: {type(exc).__name__}: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"process tree query failed: rc={completed.returncode}")
        try:
            payload = json.loads(completed.stdout.strip() or "[]")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid process tree query output: {type(exc).__name__}") from exc
        rows = payload if isinstance(payload, list) else [payload]
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            process_pid = int(row.get("ProcessId") or 0)
            if process_pid <= 0:
                continue
            result.append(
                {
                    "status": "running",
                    "pid": process_pid,
                    "parent_pid": int(row.get("ParentProcessId") or 0),
                    "image": str(row.get("ExecutablePath") or ""),
                    "command_line": str(row.get("CommandLine") or ""),
                    "creation_date": str(row.get("CreationDate") or ""),
                }
            )
        return result

    result = []
    for proc_root in Path("/proc").iterdir():
        if not proc_root.name.isdigit():
            continue
        try:
            raw_stat = (proc_root / "stat").read_text(encoding="utf-8")
            fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
            process_pid = int(proc_root.name)
            parent = int(fields[1])
            creation_date = f"proc_start_ticks:{fields[19]}"
            command_line = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
            image = os.readlink(proc_root / "exe")
        except (OSError, ValueError, IndexError):
            continue
        result.append(
            {
                "status": "running",
                "pid": process_pid,
                "parent_pid": parent,
                "image": image,
                "command_line": command_line,
                "creation_date": creation_date,
            }
        )
    return result


def _stop_verified_sidebar_children(children: list[dict[str, Any]], *, data_dir: Path) -> dict[str, Any]:
    pending = {int(item.get("pid", 0) or 0): item for item in children if int(item.get("pid", 0) or 0) > 0}
    deadline = time.monotonic() + 3.0
    while pending and time.monotonic() < deadline:
        exited = [child_pid for child_pid in pending if _pid_state(child_pid) is False]
        for child_pid in exited:
            pending.pop(child_pid, None)
        if pending:
            time.sleep(0.1)

    terminated: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for child_pid in sorted(pending):
        info = _stable_process_sample(child_pid)
        if str(info.get("status") or "") == "absent":
            continue
        if str(info.get("status") or "") != "running":
            blocked.append({"pid": child_pid, "state": "identity_unavailable"})
            continue
        if not _normalized_process_text(info).strip():
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "identity_unavailable",
                    "reason": "process_identity_fields_unavailable",
                }
            )
            continue
        matched, identity = _matches_sidebar_child_writer(info, data_dir=data_dir)
        if not matched:
            ignored.append(
                {
                    "pid": child_pid,
                    "state": "ignored_non_writer",
                    "image": str(info.get("image") or ""),
                    "identity": identity,
                }
            )
            continue
        expected_creation_date = str(pending[child_pid].get("creation_date") or "").strip()
        current_creation_date = str(info.get("creation_date") or "").strip()
        if not expected_creation_date or not current_creation_date:
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "creation_time_unavailable",
                    "identity": identity,
                    "expected_creation_date": expected_creation_date,
                    "current_creation_date": current_creation_date,
                }
            )
            continue
        if current_creation_date != expected_creation_date:
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "pid_reused_or_creation_time_changed",
                    "identity": identity,
                    "expected_creation_date": expected_creation_date,
                    "current_creation_date": current_creation_date,
                }
            )
            continue
        current_process_start = str(info.get("process_start") or "")
        if not current_process_start:
            blocked.append(
                {
                    "pid": child_pid,
                    "state": "process_start_query_failed",
                    "identity": identity,
                }
            )
            continue
        termination = _terminate_pid(
            child_pid,
            tree=False,
            expected_process_start=current_process_start,
        )
        exited = _termination_exited(child_pid, termination, timeout_seconds=8.0)
        record = {
            "pid": child_pid,
            "identity": identity,
            "termination": termination,
            "exited": exited,
        }
        terminated.append(record)
        if not exited:
            blocked.append({"pid": child_pid, "state": "still_running_after_terminate", "identity": identity})
    return {
        "verified_stopped": not blocked,
        "state": "stopped" if not blocked else "blocked",
        "terminated": terminated,
        "blocked": blocked,
        "ignored": ignored,
    }


def _matches_sidebar_child_writer(info: dict[str, Any], *, data_dir: Path) -> tuple[bool, str]:
    text = _normalized_process_text(info)
    root = _normalized_path(ROOT)
    data_root = _normalized_path(data_dir)
    if root and _normalized_path_is_present(text, root):
        return True, "project_child_path"
    if data_root and _normalized_path_is_present(text, data_root):
        return True, "data_writer_path"
    writer_markers = (
        "app.personal_wechat_bot",
        "app\\personal_wechat_bot",
        "scripts\\send_bridge_worker.py",
        "scripts/send_bridge_worker.py",
    )
    if any(marker in text for marker in writer_markers):
        return True, "project_writer_command"
    return False, "unverified_child_process"


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
    except Exception as exc:
        raise RuntimeError(f"failed to query TCP listeners on port {port}: {type(exc).__name__}: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"netstat failed while querying TCP listeners on port {port}: rc={completed.returncode}")
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local_address, state, pid_text = parts[1], parts[3].upper(), parts[-1]
        if state != "LISTENING" or _endpoint_port(local_address) != port:
            continue
        try:
            pids.add(int(pid_text))
        except ValueError:
            continue
    return sorted(pids)


def _endpoint_port(address: str) -> int | None:
    try:
        return int(str(address).rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None


def _termination_exited(pid: int, termination: dict[str, Any], *, timeout_seconds: float) -> bool:
    handle_exit = termination.get("exited")
    if isinstance(handle_exit, bool):
        return handle_exit
    if str(termination.get("reason") or "") == "already_exited":
        return True
    return _wait_for_pid_exit(pid, timeout_seconds=timeout_seconds)


def _terminate_pid(
    pid: int,
    *,
    tree: bool,
    expected_process_start: str,
) -> dict[str, Any]:
    if pid <= 0 or pid == os.getpid():
        return {"attempted": False, "reason": "invalid_or_self_pid"}
    expected_process_start = str(expected_process_start or "").strip()
    if not expected_process_start:
        return {"attempted": False, "reason": "missing_expected_process_start"}
    if os.name == "nt":
        return _terminate_windows_process_handle(
            pid,
            expected_process_start=expected_process_start,
            tree_requested=tree,
        )
    current_process_start = process_start_marker(pid)
    if not current_process_start:
        return {"attempted": False, "reason": "process_start_query_failed"}
    if current_process_start != expected_process_start:
        return {
            "attempted": False,
            "reason": "process_start_mismatch",
            "expected_process_start": expected_process_start,
            "current_process_start": current_process_start,
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"attempted": True, "returncode": 0, "reason": "already_exited"}
    except OSError as exc:
        return {"attempted": True, "returncode": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"attempted": True, "returncode": 0}


def _terminate_windows_process_handle(
    pid: int,
    *,
    expected_process_start: str,
    tree_requested: bool,
) -> dict[str, Any]:
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError) as exc:
        return {"attempted": False, "reason": f"win32_api_unavailable: {type(exc).__name__}"}

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError as exc:
        return {"attempted": False, "reason": f"kernel32_unavailable: {type(exc).__name__}"}

    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    wait_object_0 = 0
    wait_timeout = 258
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    access = process_terminate | process_query_limited_information | synchronize
    try:
        handle = kernel32.OpenProcess(access, False, pid)
    except OSError as exc:
        return {"attempted": False, "reason": f"open_process_failed: {type(exc).__name__}: {exc}"}
    if not handle:
        error_code = int(ctypes.get_last_error())
        if error_code in {87, 1168}:
            return {"attempted": False, "returncode": 0, "reason": "already_exited", "exited": True}
        return {"attempted": False, "reason": "open_process_failed", "winerror": error_code}

    result: dict[str, Any] = {
        "attempted": False,
        "tree_requested": bool(tree_requested),
        "termination_scope": "single_verified_process",
        "expected_process_start": expected_process_start,
    }
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            times_ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
        except OSError as exc:
            result["reason"] = f"get_process_times_failed: {type(exc).__name__}: {exc}"
            return result
        if not times_ok:
            result["reason"] = "get_process_times_failed"
            result["winerror"] = int(ctypes.get_last_error())
            return result
        current_process_start = f"win:{(int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)}"
        result["current_process_start"] = current_process_start
        if current_process_start != expected_process_start:
            result["reason"] = "process_start_mismatch"
            return result

        wait_before = int(kernel32.WaitForSingleObject(handle, 0))
        if wait_before == wait_object_0:
            result.update({"returncode": 0, "reason": "already_exited", "exited": True})
            return result
        try:
            terminated = kernel32.TerminateProcess(handle, 1)
        except OSError as exc:
            result.update(
                {
                    "attempted": True,
                    "returncode": None,
                    "reason": f"terminate_process_failed: {type(exc).__name__}: {exc}",
                }
            )
            return result
        result["attempted"] = True
        if not terminated:
            result.update(
                {
                    "returncode": None,
                    "reason": "terminate_process_failed",
                    "winerror": int(ctypes.get_last_error()),
                }
            )
            return result
        wait_after = int(kernel32.WaitForSingleObject(handle, 8000))
        result["returncode"] = 0
        result["exited"] = wait_after == wait_object_0
        if wait_after not in {wait_object_0, wait_timeout}:
            result["wait_result"] = wait_after
        return result
    finally:
        try:
            kernel32.CloseHandle(handle)
        except OSError:
            pass


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    while time.monotonic() < deadline:
        state = _pid_state(pid)
        if state is False:
            return True
        time.sleep(0.1)
    return _pid_state(pid) is False


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
    return _pid_state(pid) is not False


def _pid_state(pid: int) -> bool | None:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) < 2:
                continue
            try:
                if int(row[1]) == pid:
                    return True
            except (TypeError, ValueError):
                continue
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _process_info(pid: int) -> dict[str, Any]:
    if pid <= 0:
        return {"status": "absent", "pid": pid}
    if os.name != "nt":
        state = _pid_state(pid)
        if state is False:
            return {"status": "absent", "pid": pid}
        if state is None:
            return {"status": "error", "pid": pid, "reason": "process_state_unknown"}
        proc_root = Path("/proc") / str(pid)
        try:
            command_line = (proc_root / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            image = os.readlink(proc_root / "exe")
            raw_stat = (proc_root / "stat").read_text(encoding="utf-8")
            stat_fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
            creation_date = f"proc_start_ticks:{stat_fields[19]}"
        except (OSError, IndexError) as exc:
            return {"status": "error", "pid": pid, "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "status": "running",
            "pid": pid,
            "image": image,
            "command_line": command_line,
            "creation_date": creation_date,
        }

    query = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' -ErrorAction Stop; "
        "if ($null -eq $p) { exit 3 }; "
        "$p | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except Exception as exc:
        return {"status": "error", "pid": pid, "reason": f"{type(exc).__name__}: {exc}"}
    if completed.returncode == 3:
        return {"status": "absent", "pid": pid}
    if completed.returncode != 0:
        return {"status": "error", "pid": pid, "reason": f"process_query_rc_{completed.returncode}"}
    try:
        payload = json.loads(completed.stdout.strip())
    except (TypeError, ValueError) as exc:
        return {"status": "error", "pid": pid, "reason": f"invalid_process_query_output: {type(exc).__name__}"}
    if not isinstance(payload, dict) or int(payload.get("ProcessId") or 0) != pid:
        return {"status": "error", "pid": pid, "reason": "process_query_pid_mismatch"}
    return {
        "status": "running",
        "pid": pid,
        "parent_pid": int(payload.get("ParentProcessId") or 0),
        "image": str(payload.get("ExecutablePath") or ""),
        "command_line": str(payload.get("CommandLine") or ""),
        "creation_date": str(payload.get("CreationDate") or ""),
    }


def _matches_sidebar_process(info: dict[str, Any], *, data_dir: Path) -> tuple[bool, str]:
    text = _normalized_process_text(info)
    root = _normalized_path(ROOT)
    data_root = _normalized_path(data_dir)
    if "scripts\\start_sidebar_frontend.py" in text or "scripts/start_sidebar_frontend.py" in text:
        return True, "start_sidebar_frontend"
    module_markers = (
        "app.personal_wechat_bot.main",
        "app.personal_wechat_bot.control.cli",
        "app\\personal_wechat_bot\\main.py",
        "app/personal_wechat_bot/main.py",
        "app\\personal_wechat_bot\\control\\cli.py",
        "app/personal_wechat_bot/control/cli.py",
    )
    sidebar_commands = ("send-sidebar", "send-sidebar-window")
    if any(marker in text for marker in module_markers) and any(command in text for command in sidebar_commands):
        return True, "personal_wechat_bot_sidebar_cli"
    if (root and root in text) or (data_root and data_root in text):
        return False, "project_process_without_sidebar_signature"
    return False, "not_sidebar_process"


def _matches_weflow_process(info: dict[str, Any]) -> tuple[bool, str]:
    text = _normalized_process_text(info)
    weflow_root = _normalized_path(ROOT / "vendor" / "reference" / "WeFlow-gitcode")
    if weflow_root and _normalized_path_is_present(text, weflow_root):
        return True, "weflow_project_path"
    return False, "not_weflow_process"


def _normalized_process_text(info: dict[str, Any]) -> str:
    return " ".join((str(info.get("image") or ""), str(info.get("command_line") or ""))).replace("/", "\\").casefold()


def _normalized_path(path: Path) -> str:
    return os.path.abspath(os.path.normpath(os.fspath(path))).replace("/", "\\").casefold()


def _normalized_path_is_present(process_text: str, expected_path: str) -> bool:
    start = 0
    while True:
        index = process_text.find(expected_path, start)
        if index < 0:
            return False
        before = process_text[index - 1] if index > 0 else ""
        end = index + len(expected_path)
        after = process_text[end] if end < len(process_text) else ""
        if before in {"", " ", "\t", '"', "'", "="} and after in {"", " ", "\t", "\\", '"', "'"}:
            return True
        start = index + 1


def _read_private_json_file(path: Path) -> tuple[dict[str, Any], tuple[int, int, int, int]]:
    path_stat = path.lstat()
    if (
        _path_is_reparse_or_symlink(path, path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
        or int(getattr(path_stat, "st_nlink", 1) or 1) != 1
    ):
        raise ValueError(f"unsafe private file: {path}")
    fd = os.open(str(path), os.O_RDONLY)
    try:
        opened_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or int(getattr(opened_stat, "st_nlink", 1) or 1) != 1
            or (int(path_stat.st_dev), int(path_stat.st_ino))
            != (int(opened_stat.st_dev), int(opened_stat.st_ino))
        ):
            raise ValueError(f"private file changed while opening: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid private JSON object: {path}")
    identity = (
        int(opened_stat.st_dev),
        int(opened_stat.st_ino),
        int(opened_stat.st_size),
        int(getattr(opened_stat, "st_mtime_ns", 0) or 0),
    )
    return payload, identity


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _require_plain_directory(path.parent)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd: int | None = None
    try:
        fd = os.open(str(temporary_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private JSON write made no progress")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        _require_plain_directory(path.parent)
        os.replace(temporary_path, path)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_status(
    data_dir: Path,
    payload: dict[str, Any],
    *,
    shutdown_owner_token: str,
) -> None:
    runtime_dir = data_dir / "runtime"
    _require_plain_directory(data_dir)
    try:
        runtime_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _require_plain_directory(runtime_dir)
    payload = dict(payload)
    helper_pid = os.getpid()
    payload["helper_pid"] = helper_pid
    payload["helper_process_start"] = process_start_marker(helper_pid)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_private_json(runtime_dir / "history_reset_shutdown.json", payload)
    _refresh_shutdown_lock(data_dir, shutdown_owner_token=shutdown_owner_token)


def _remove_shutdown_lock(data_dir: Path, *, shutdown_owner_token: str) -> None:
    shutdown_owner_token = str(shutdown_owner_token or "").strip()
    if not shutdown_owner_token:
        return
    path = data_dir / "runtime" / "history_reset_shutdown.lock"
    try:
        payload, identity = _read_private_json_file(path)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError):
        return
    expected_start = str(payload.get("helper_process_start") or "")
    try:
        helper_pid = int(payload.get("helper_pid") or 0)
    except (TypeError, ValueError):
        return
    if (
        str(payload.get("owner_token") or "") != shutdown_owner_token
        or helper_pid != os.getpid()
        or not expected_start
        or process_start_marker(os.getpid()) != expected_start
    ):
        return
    try:
        current_payload, current_identity = _read_private_json_file(path)
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return
    if current_identity != identity or current_payload != payload:
        return
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


def _refresh_shutdown_lock(data_dir: Path, *, shutdown_owner_token: str) -> None:
    shutdown_owner_token = str(shutdown_owner_token or "").strip()
    if not shutdown_owner_token:
        return
    lock_path = data_dir / "runtime" / "history_reset_shutdown.lock"
    try:
        payload, identity = _read_private_json_file(lock_path)
    except (OSError, UnicodeError, ValueError):
        return
    expected_start = str(payload.get("helper_process_start") or "")
    try:
        helper_pid = int(payload.get("helper_pid") or 0)
    except (TypeError, ValueError):
        return
    if (
        helper_pid != os.getpid()
        or not expected_start
        or process_start_marker(os.getpid()) != expected_start
        or str(payload.get("owner_token") or "") != shutdown_owner_token
    ):
        return
    original_payload = dict(payload)
    payload["updated_at_epoch"] = time.time()
    try:
        current_payload, current_identity = _read_private_json_file(lock_path)
        if current_identity != identity or current_payload != original_payload:
            return
        _atomic_write_private_json(lock_path, payload)
    except (OSError, UnicodeError, ValueError):
        return


def _finalize_weflow_start_lock(data_dir: Path, stop_result: dict[str, Any]) -> dict[str, Any]:
    path = data_dir / "runtime" / "weflow_start.lock"
    try:
        payload, identity = _read_private_json_file(path)
    except FileNotFoundError:
        return {"verified": True, "state": "absent", "removed": False}
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "verified": False,
            "state": "unsafe_start_lock",
            "removed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return {"verified": False, "state": "invalid_start_lock_pid", "removed": False}
    process_start = str(payload.get("process_start") or "")
    pid_state = _pid_state(pid)
    current_start = process_start_marker(pid) if pid_state is True else ""
    stale_pid = bool(pid_state is True and process_start and current_start and current_start != process_start)
    # A PID appearing in the stop result is not sufficient after the handle is
    # closed: Windows may already have reused it for a new, valid lock owner.
    removable = pid <= 0 or pid_state is False or stale_pid
    if not removable:
        return {
            "verified": False,
            "state": "live_unverified_owner" if pid_state is True else "owner_state_unknown",
            "removed": False,
            "pid": pid,
            "recorded_process_start": process_start,
            "current_process_start": current_start,
        }
    try:
        current, current_identity = _read_private_json_file(path)
    except (OSError, UnicodeError, ValueError):
        return {"verified": False, "state": "start_lock_changed", "removed": False, "pid": pid}
    if current != payload or current_identity != identity:
        return {"verified": False, "state": "start_lock_changed", "removed": False, "pid": pid}
    try:
        path.unlink()
    except FileNotFoundError:
        return {"verified": True, "state": "already_removed", "removed": False, "pid": pid}
    except OSError as exc:
        return {
            "verified": False,
            "state": "remove_failed",
            "removed": False,
            "pid": pid,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"verified": True, "state": "removed", "removed": True, "pid": pid}


if __name__ == "__main__":
    raise SystemExit(main())
