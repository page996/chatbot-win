from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.runtime.process_lock import process_pid_alive, process_start_marker


BROWSER_PROCESS_NAMES = {"chrome.exe", "msedge.exe"}
SIDEBAR_BROWSER_PROFILE_RELATIVE = Path("runtime") / "sidebar_browser_profile"
SIDEBAR_LAUNCH_STATE_RELATIVE = Path("runtime") / "sidebar_launch.json"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def sidebar_browser_profile_path(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / SIDEBAR_BROWSER_PROFILE_RELATIVE


def sidebar_browser_runtime_blockers(data_dir: str | Path) -> list[dict[str, Any]]:
    return list(inspect_sidebar_browser_runtime(data_dir).get("blockers") or [])


def inspect_sidebar_browser_runtime(
    data_dir: str | Path,
    *,
    require_launch_state: bool = False,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    profile = sidebar_browser_profile_path(root)
    profile_stat = _safe_lstat(profile)
    profile_exists = profile_stat is not None
    blockers: list[dict[str, Any]] = []
    if profile_stat is not None and (_is_reparse_point(profile_stat) or not stat.S_ISDIR(profile_stat.st_mode)):
        blockers.append(_browser_blocker(profile, reason="unsafe_sidebar_browser_profile"))

    launch_state, launch_error = read_sidebar_browser_launch_state(root)
    if require_launch_state and launch_state is None:
        launch_error = launch_error or "missing_sidebar_launch_state"
    state_validation = _validate_launch_browser_state(root, launch_state)
    if require_launch_state and (launch_error or not state_validation["schema_valid"]):
        reason = launch_error or str(state_validation.get("reason") or "invalid_sidebar_launch_state")
        blockers.append(_browser_blocker(profile, reason=reason))

    if (
        not profile_exists
        and not require_launch_state
        and (launch_state is None or not bool(launch_state.get("browser_owned")))
    ):
        return {
            "verified": not blockers,
            "inventory_verified": True,
            "profile": str(profile),
            "profile_exists": False,
            "launch_state": launch_state,
            "launch_state_error": launch_error,
            "state_validation": state_validation,
            "profile_processes": [],
            "browser_process_tree": [],
            "blockers": blockers,
        }

    try:
        inventory = sidebar_browser_process_inventory()
    except RuntimeError as exc:
        inventory = []
        if profile_exists or require_launch_state or bool((launch_state or {}).get("browser_owned")):
            blockers.append(_browser_blocker(profile, reason=f"browser_process_inventory_unavailable:{exc}"))
        return {
            "verified": False,
            "inventory_verified": False,
            "profile": str(profile),
            "profile_exists": profile_exists,
            "launch_state": launch_state,
            "launch_state_error": launch_error,
            "state_validation": state_validation,
            "profile_processes": [],
            "browser_process_tree": [],
            "blockers": blockers,
        }

    profile_processes: list[dict[str, Any]] = []
    by_pid = {int(item.get("pid") or 0): item for item in inventory}
    for item in inventory:
        if not command_uses_sidebar_profile(item, profile):
            continue
        sampled = _sample_browser_record(item)
        if sampled is None:
            continue
        profile_processes.append(sampled)
        reason = "active_sidebar_browser_profile"
        if not bool(sampled.get("identity_verified")):
            reason = str(sampled.get("identity_reason") or "sidebar_browser_identity_unavailable")
        blockers.append(
            _browser_blocker(
                profile,
                reason=reason,
                pid=int(sampled.get("pid") or 0),
                process_start=str(sampled.get("process_start") or ""),
                executable=str(sampled.get("image") or ""),
            )
        )

    recorded = _recorded_browser_process_status(launch_state, by_pid, profile)
    if recorded.get("state") == "identity_unavailable":
        blockers.append(
            _browser_blocker(
                profile,
                reason=str(recorded.get("reason") or "recorded_browser_identity_unavailable"),
                pid=int(recorded.get("pid") or 0),
                process_start=str(recorded.get("process_start") or ""),
                executable=str(recorded.get("image") or ""),
            )
        )
    recorded_descendants, recorded_descendant_errors = _recorded_browser_descendants(
        launch_state,
        by_pid,
    )
    blockers.extend(
        _browser_blocker(
            profile,
            reason="active_sidebar_browser_descendant",
            pid=int(item.get("pid") or 0),
            process_start=str(item.get("process_start") or ""),
            executable=str(item.get("image") or ""),
        )
        for item in recorded_descendants
    )
    blockers.extend(
        _browser_blocker(
            profile,
            reason=str(item.get("reason") or "recorded_browser_descendant_identity_unavailable"),
            pid=int(item.get("pid") or 0),
            process_start=str(item.get("process_start") or ""),
            executable=str(item.get("image") or ""),
        )
        for item in recorded_descendant_errors
    )
    browser_process_tree = _browser_process_tree(inventory, profile_processes)
    tree_by_pid = {int(item.get("pid") or 0): item for item in browser_process_tree}
    for item in recorded_descendants:
        tree_by_pid.setdefault(int(item.get("pid") or 0), item)
    browser_process_tree = [tree_by_pid[pid] for pid in sorted(tree_by_pid) if pid > 0]
    verified = (not require_launch_state or (not launch_error and bool(state_validation.get("schema_valid")))) and not any(
        str(item.get("reason") or "").startswith(("unsafe_", "browser_process_inventory_unavailable"))
        for item in blockers
    )
    if require_launch_state:
        verified = bool(verified and launch_state is not None)
    return {
        "verified": verified,
        "inventory_verified": True,
        "profile": str(profile),
        "profile_exists": profile_exists,
        "launch_state": launch_state,
        "launch_state_error": launch_error,
        "state_validation": state_validation,
        "recorded_process": recorded,
        "recorded_descendant_errors": recorded_descendant_errors,
        "profile_processes": profile_processes,
        "browser_process_tree": browser_process_tree,
        "blockers": blockers,
    }


def read_sidebar_browser_launch_state(data_dir: str | Path) -> tuple[dict[str, Any] | None, str]:
    path = Path(data_dir).resolve() / SIDEBAR_LAUNCH_STATE_RELATIVE
    path_stat = _safe_lstat(path)
    if path_stat is None:
        return None, ""
    if _is_reparse_point(path_stat) or not stat.S_ISREG(path_stat.st_mode) or int(path_stat.st_nlink) != 1:
        return None, "unsafe_sidebar_launch_state"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"invalid_sidebar_launch_state:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "invalid_sidebar_launch_state_payload"
    return payload, ""


def sidebar_browser_process_inventory() -> list[dict[str, Any]]:
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
            raise RuntimeError(f"process query failed: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"process query failed: rc={completed.returncode}")
        try:
            payload = json.loads(completed.stdout.strip() or "[]")
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid process query output: {type(exc).__name__}") from exc
        rows = payload if isinstance(payload, list) else [payload]
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = int(row.get("ProcessId") or 0)
            if pid <= 0:
                continue
            command_line = str(row.get("CommandLine") or "")
            result.append(
                {
                    "pid": pid,
                    "parent_pid": int(row.get("ParentProcessId") or 0),
                    "image": str(row.get("ExecutablePath") or ""),
                    "command_line": command_line,
                    "argv": _windows_command_line_argv(command_line),
                    "creation_date": str(row.get("CreationDate") or ""),
                }
            )
        return result

    result = []
    try:
        proc_roots = list(Path("/proc").iterdir())
    except OSError as exc:
        raise RuntimeError(f"process query failed: {type(exc).__name__}") from exc
    for proc_root in proc_roots:
        if not proc_root.name.isdigit():
            continue
        try:
            raw = (proc_root / "cmdline").read_bytes()
            argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
            image = os.readlink(proc_root / "exe")
            raw_stat = (proc_root / "stat").read_text(encoding="utf-8")
            fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
            parent_pid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        result.append(
            {
                "pid": int(proc_root.name),
                "parent_pid": parent_pid,
                "image": image,
                "command_line": " ".join(argv),
                "argv": argv,
                "creation_date": "",
            }
        )
    return result


def command_uses_sidebar_profile(record: dict[str, Any], profile: str | Path) -> bool:
    expected = _normalized_path(profile)
    argv = record.get("argv") if isinstance(record.get("argv"), list) else []
    for index, raw in enumerate(argv):
        argument = str(raw or "")
        lowered = argument.lower()
        value = ""
        if lowered.startswith("--user-data-dir="):
            value = argument.split("=", 1)[1]
        elif lowered == "--user-data-dir" and index + 1 < len(argv):
            value = str(argv[index + 1] or "")
        if value and _normalized_path(value) == expected:
            return True
    return False


def _validate_launch_browser_state(root: Path, payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {"schema_valid": False, "reason": "missing_sidebar_launch_state"}
    try:
        recorded_root_raw = str(payload.get("data_dir") or "").strip()
        profile_raw = str(payload.get("browser_profile") or "").strip()
        recorded_root = _normalized_path(recorded_root_raw)
        expected_root = _normalized_path(root)
        profile = _normalized_path(profile_raw)
        expected_profile = _normalized_path(sidebar_browser_profile_path(root))
        owned = bool(payload.get("browser_owned"))
        pid = int(payload.get("browser_pid") or 0)
    except (TypeError, ValueError, OverflowError, OSError):
        return {"schema_valid": False, "reason": "invalid_sidebar_launch_state_identity"}
    if (
        not os.path.isabs(recorded_root_raw)
        or not os.path.isabs(profile_raw)
        or not recorded_root
        or recorded_root != expected_root
        or profile != expected_profile
    ):
        return {"schema_valid": False, "reason": "sidebar_launch_state_path_mismatch"}
    if not owned:
        if pid > 0 or payload.get("browser_process_start") or payload.get("browser_executable"):
            return {"schema_valid": False, "reason": "external_browser_state_claims_process"}
        return {"schema_valid": True, "owned": False}
    executable = str(payload.get("browser_executable") or "").strip()
    process_start = str(payload.get("browser_process_start") or "").strip()
    job_owned = bool(payload.get("browser_job_owned"))
    descendants = payload.get("browser_descendants")
    if (
        pid <= 0
        or not process_start
        or not os.path.isabs(executable)
        or Path(executable).name.lower() not in BROWSER_PROCESS_NAMES
        or not job_owned
        or not isinstance(descendants, list)
    ):
        return {"schema_valid": False, "reason": "incomplete_sidebar_browser_identity"}
    descendant_pids: set[int] = set()
    normalized_executable = _normalized_path(executable)
    for item in descendants:
        if not isinstance(item, dict):
            return {"schema_valid": False, "reason": "invalid_sidebar_browser_descendant_identity"}
        try:
            descendant_pid = int(item.get("pid") or 0)
        except (TypeError, ValueError, OverflowError):
            return {"schema_valid": False, "reason": "invalid_sidebar_browser_descendant_identity"}
        if (
            descendant_pid <= 0
            or descendant_pid in descendant_pids
            or not str(item.get("process_start") or "").strip()
            or not os.path.isabs(str(item.get("executable") or ""))
            or _normalized_path(str(item.get("executable") or "")) != normalized_executable
        ):
            return {"schema_valid": False, "reason": "invalid_sidebar_browser_descendant_identity"}
        descendant_pids.add(descendant_pid)
    if pid not in descendant_pids:
        return {"schema_valid": False, "reason": "missing_sidebar_browser_root_identity"}
    return {
        "schema_valid": True,
        "owned": True,
        "pid": pid,
        "process_start": process_start,
        "executable": executable,
        "descendants": descendants,
    }


def _recorded_browser_process_status(
    launch_state: dict[str, Any] | None,
    by_pid: dict[int, dict[str, Any]],
    profile: Path,
) -> dict[str, Any]:
    validation = _validate_launch_browser_state(profile.parents[1], launch_state)
    if not validation.get("schema_valid") or not validation.get("owned"):
        return {"state": "not_owned"}
    pid = int(validation.get("pid") or 0)
    row = by_pid.get(pid)
    if row is None:
        current_start = process_start_marker(pid)
        recorded_start = str(validation.get("process_start") or "")
        if current_start and current_start != recorded_start:
            return {"state": "pid_reused", "pid": pid, "process_start": current_start}
        if current_start == recorded_start or process_pid_alive(pid):
            return {
                "state": "identity_unavailable",
                "reason": "recorded_browser_missing_from_process_inventory",
                "pid": pid,
                "process_start": current_start,
            }
        return {"state": "absent", "pid": pid}
    current_start = process_start_marker(pid)
    recorded_start = str(validation.get("process_start") or "")
    recorded_executable = _normalized_path(str(validation.get("executable") or ""))
    current_executable = _normalized_path(str(row.get("image") or ""))
    if not current_start:
        return {
            "state": "identity_unavailable",
            "reason": "recorded_browser_process_start_unavailable",
            "pid": pid,
            "image": str(row.get("image") or ""),
        }
    if current_start != recorded_start:
        return {"state": "pid_reused", "pid": pid, "process_start": current_start}
    if not str(row.get("command_line") or "").strip() or not row.get("argv"):
        return {
            "state": "identity_unavailable",
            "reason": "recorded_browser_command_line_unavailable",
            "pid": pid,
            "process_start": current_start,
            "image": str(row.get("image") or ""),
        }
    if current_executable != recorded_executable or not command_uses_sidebar_profile(row, profile):
        return {
            "state": "identity_mismatch",
            "pid": pid,
            "process_start": current_start,
            "image": str(row.get("image") or ""),
        }
    return {
        "state": "verified",
        "pid": pid,
        "process_start": current_start,
        "image": str(row.get("image") or ""),
    }


def _sample_browser_record(record: dict[str, Any]) -> dict[str, Any] | None:
    pid = int(record.get("pid") or 0)
    before = process_start_marker(pid)
    if not before:
        if not process_pid_alive(pid):
            return None
        sampled = dict(record)
        sampled.update(identity_verified=False, identity_reason="browser_process_start_unavailable")
        return sampled
    after = process_start_marker(pid)
    if not after or after != before:
        sampled = dict(record)
        sampled.update(identity_verified=False, identity_reason="browser_process_identity_changed")
        return sampled
    sampled = dict(record)
    image_name = Path(str(record.get("image") or "")).name.lower()
    sampled.update(
        process_start=before,
        identity_verified=image_name in BROWSER_PROCESS_NAMES,
        identity_reason="" if image_name in BROWSER_PROCESS_NAMES else "profile_process_executable_mismatch",
    )
    return sampled


def _recorded_browser_descendants(
    launch_state: dict[str, Any] | None,
    by_pid: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(launch_state, dict) or not bool(launch_state.get("browser_owned")):
        return [], []
    raw_descendants = launch_state.get("browser_descendants")
    if not isinstance(raw_descendants, list):
        return [], []
    active: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for recorded in raw_descendants:
        if not isinstance(recorded, dict):
            continue
        pid = int(recorded.get("pid") or 0)
        expected_start = str(recorded.get("process_start") or "").strip()
        expected_image = _normalized_path(str(recorded.get("executable") or ""))
        row = by_pid.get(pid)
        current_start = process_start_marker(pid)
        if row is None:
            if current_start and current_start != expected_start:
                continue
            if current_start == expected_start or process_pid_alive(pid):
                errors.append(
                    {
                        "pid": pid,
                        "process_start": current_start,
                        "image": "",
                        "reason": "recorded_browser_descendant_missing_from_inventory",
                    }
                )
            continue
        if not current_start:
            if process_pid_alive(pid):
                errors.append(
                    {
                        "pid": pid,
                        "process_start": "",
                        "image": str(row.get("image") or ""),
                        "reason": "recorded_browser_descendant_start_unavailable",
                    }
                )
            continue
        if current_start != expected_start:
            continue
        current_image = _normalized_path(str(row.get("image") or ""))
        if not current_image or current_image != expected_image:
            errors.append(
                {
                    "pid": pid,
                    "process_start": current_start,
                    "image": str(row.get("image") or ""),
                    "reason": "recorded_browser_descendant_executable_mismatch",
                }
            )
            continue
        item = dict(row)
        item.update(
            process_start=current_start,
            identity_verified=True,
            identity_reason="",
            root_pid=int(recorded.get("root_pid") or 0),
            recorded_descendant=True,
        )
        active.append(item)
    return active, errors


def _browser_process_tree(
    inventory: list[dict[str, Any]],
    profile_processes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for item in inventory:
        by_parent.setdefault(int(item.get("parent_pid") or 0), []).append(item)
    roots = {
        int(item.get("pid") or 0): item
        for item in profile_processes
        if int(item.get("pid") or 0) > 0
    }
    found: dict[int, dict[str, Any]] = {}
    pending: list[tuple[int, int, str]] = [
        (root_pid, root_pid, str(root.get("image") or ""))
        for root_pid, root in roots.items()
    ]
    for root_pid, root in roots.items():
        copied = dict(root)
        copied.update(root_pid=root_pid, tree_depth=0)
        found[root_pid] = copied
    while pending:
        parent_pid, root_pid, root_image = pending.pop()
        parent_depth = int(found.get(parent_pid, {}).get("tree_depth", 0) or 0)
        for child in by_parent.get(parent_pid, []):
            child_pid = int(child.get("pid") or 0)
            if child_pid <= 0 or child_pid in found:
                continue
            sampled = _sample_browser_descendant(child, root_image=root_image)
            if sampled is None:
                continue
            sampled.update(root_pid=root_pid, tree_depth=parent_depth + 1)
            found[child_pid] = sampled
            pending.append((child_pid, root_pid, root_image))
    return [found[pid] for pid in sorted(found)]


def _sample_browser_descendant(record: dict[str, Any], *, root_image: str) -> dict[str, Any] | None:
    pid = int(record.get("pid") or 0)
    before = process_start_marker(pid)
    if not before:
        if not process_pid_alive(pid):
            return None
        sampled = dict(record)
        sampled.update(identity_verified=False, identity_reason="browser_descendant_start_unavailable")
        return sampled
    after = process_start_marker(pid)
    if not after or after != before:
        sampled = dict(record)
        sampled.update(identity_verified=False, identity_reason="browser_descendant_identity_changed")
        return sampled
    sampled = dict(record)
    same_executable = _normalized_path(str(record.get("image") or "")) == _normalized_path(root_image)
    sampled.update(
        process_start=before,
        identity_verified=same_executable,
        identity_reason="" if same_executable else "browser_descendant_executable_mismatch",
    )
    return sampled


def _windows_command_line_argv(command_line: str) -> list[str]:
    if not command_line:
        return []
    if os.name != "nt":
        return [command_line]
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [str(argv[index]) for index in range(max(0, int(argc.value)))]
    finally:
        kernel32.LocalFree(argv)


def _browser_blocker(
    profile: Path,
    *,
    reason: str,
    pid: int = 0,
    process_start: str = "",
    executable: str = "",
) -> dict[str, Any]:
    return {
        "worker": "sidebar_browser_profile",
        "source": "sidebar_browser_runtime",
        "reason": reason,
        "pid": pid,
        "process_start": process_start,
        "executable": executable,
        "profile": str(profile),
    }


def _normalized_path(value: str | Path) -> str:
    raw = os.fspath(value)
    if not raw:
        return ""
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(path_stat.st_mode)
        or int(getattr(path_stat, "st_file_attributes", 0) or 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )
