"""Capture native WeChat file-send diagnostic events.

Typical flow:
    python scripts/capture_wechat_file_send_diagnostics.py --clear --enable --wait 60

Then manually send one small ordinary file in WeChat while the command waits.
The script saves the raw diagnostic JSON under data/native_diagnostics.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:30001"
WXID_RE = re.compile(r"^(?:wxid_[0-9a-z_]+|gh_[0-9a-z_]+|filehelper)$", re.IGNORECASE)
ROOMID_RE = re.compile(r"^\d+@chatroom$", re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:\\")
LINUX_STYLE_PATH_RE = re.compile(r"^/")
FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,12}$")
HEXISH_RE = re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE)
MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
EXPECTED_DIAGNOSTIC_WRAPPERS = (
    "sendfile_batch_entry",
    "sendfile_request_builder",
    "sendfile_vector_processor",
    "sendfile_context_dispatch",
    "sendfile_vector_submit",
    "sendfile_string_prepare",
    "sendfile_submit_factory",
    "sendfile_task_factory",
    "sendfile_owner_factory",
    "sendfile_owner_ctor",
    "sendfile_request_ctor",
    "sendfile_request_derived_ctor",
    "sendfile_high_entry",
    "sendfile_task_entry",
    "sendfile_parent_build",
    "fileitem_default_ctor",
    "fileitem_base_ctor",
    "fileitem_business_ctor_a",
    "fileitem_business_ctor_b",
    "fileitem_shared_ctor",
    "winapi_create_file_w",
    "winapi_copy_file_w",
    "winapi_move_file_ex_w",
    "winapi_create_hard_link_w",
    "sendfileuploadmsg_init",
    "sendfileuploadmsg_short",
)


def request_json(base_url: str, path: str, *, method: str = "GET") -> dict:
    url = base_url.rstrip("/") + path
    req = Request(url, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: http_{exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
    return json.loads(body)


def event_count(payload: dict) -> int:
    try:
        return int(payload.get("event_count", 0))
    except (TypeError, ValueError):
        return 0


def summarize_events(payload: dict) -> list[dict]:
    summary: list[dict] = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        strings: list[dict] = []
        pointer_fields: list[dict] = []
        numeric_fields: list[dict] = []
        args = event.get("args", {})
        if isinstance(args, dict):
            for arg_name, arg in args.items():
                if not isinstance(arg, dict):
                    continue
                candidates = []
                for key in ("string_candidates", "raw_string_candidates"):
                    values = arg.get(key, [])
                    if isinstance(values, list):
                        candidates.extend(values)
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    strings.append(
                        {
                            "arg": arg_name,
                            "offset": candidate.get("offset", ""),
                            "encoding": candidate.get("encoding", ""),
                            "valid_utf8": candidate.get("valid_utf8", False),
                            "value": candidate.get("value", ""),
                            "ascii_preview": candidate.get("ascii_preview", ""),
                            "value_hex": candidate.get("value_hex", ""),
                        }
                    )
                for field in arg.get("pointer_fields", []):
                    if not isinstance(field, dict):
                        continue
                    field_strings = []
                    for field_key in ("string_candidates", "raw_string_candidates"):
                        values = field.get(field_key, [])
                        if not isinstance(values, list):
                            continue
                        for candidate in values:
                            if not isinstance(candidate, dict):
                                continue
                            field_strings.append(
                                {
                                    "source": field_key,
                                    "offset": candidate.get("offset", ""),
                                    "encoding": candidate.get("encoding", ""),
                                    "value": candidate.get("value", ""),
                                    "ascii_preview": candidate.get("ascii_preview", ""),
                                    "value_hex": candidate.get("value_hex", ""),
                                }
                            )
                    if field_strings:
                        pointer_fields.append(
                            {
                                "arg": arg_name,
                                "offset": field.get("offset", ""),
                                "ptr": field.get("ptr", ""),
                                "weixin_offset": field.get("weixin_offset", ""),
                                "strings": field_strings,
                            }
                        )
                for field in arg.get("numeric_fields", []):
                    if not isinstance(field, dict):
                        continue
                    numeric_fields.append(
                        {
                            "arg": arg_name,
                            "offset": field.get("offset", ""),
                            "u64": field.get("u64", 0),
                            "i64": field.get("i64", 0),
                            "u32": field.get("u32", 0),
                            "weixin_offset": field.get("weixin_offset", ""),
                        }
                    )
        summary.append(
            {
                "schema": event.get("schema", ""),
                "timestamp": event.get("timestamp", ""),
                "wrapper": event.get("wrapper", ""),
                "phase": event.get("phase", ""),
                "thread_id": event.get("thread_id", 0),
                "return_address": event.get("return_address", ""),
                "return_weixin_offset": event.get("return_weixin_offset", ""),
                "result_value": event.get("result_value", ""),
                "result_signed": event.get("result_signed", ""),
                "path": event.get("path", ""),
                "path2": event.get("path2", ""),
                "last_error": event.get("last_error", ""),
                "strings": strings,
                "pointer_fields": pointer_fields,
                "numeric_fields": numeric_fields,
            }
        )
    return summary


def candidate_value(candidate: dict) -> str:
    value = str(candidate.get("value", "") or "")
    if value:
        return value
    return str(candidate.get("ascii_preview", "") or "").strip(".")


def classify_value(value: str) -> tuple[str, int]:
    text = str(value or "").strip()
    lower = text.lower()
    if not text:
        return "", 0
    if WXID_RE.match(text):
        return "receiver_wxid", 100
    if ROOMID_RE.match(text):
        return "receiver_roomid", 100
    if text.startswith("<msg><appmsg") or "<appmsg" in lower:
        return "appmsg_xml", 105
    if MD5_RE.match(text):
        return "file_md5", 90
    if WINDOWS_PATH_RE.match(text) or (LINUX_STYLE_PATH_RE.match(text) and FILE_EXT_RE.search(text)):
        return "file_path", 95
    if "\\" in text and FILE_EXT_RE.search(text):
        return "file_path_fragment", 80
    if FILE_EXT_RE.search(text) and len(text) <= 260 and not any(sep in text for sep in (":", "://", "\\", "/", "\n", "\r")):
        return "file_name", 75
    if "uploadappattach" in lower:
        return "upload_endpoint", 70
    if "sendfileuploadmsg" in lower:
        return "sendfile_endpoint", 70
    if "appattach" in lower:
        return "appattach_field", 60
    if "file" in lower and len(text) <= 256:
        return "file_related", 45
    if HEXISH_RE.match(text):
        return "hex_id_or_token", 35
    if len(text) >= 16 and any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text):
        return "id_or_token", 30
    return "", 0


def add_hint(
    hints: list[dict],
    *,
    event: dict,
    arg: str,
    source: str,
    offset: str,
    encoding: str,
    value: str,
    value_hex: str = "",
    pointer_offset: str = "",
    pointer: str = "",
) -> None:
    kind, score = classify_value(value)
    if not kind:
        return
    wrapper = str(event.get("wrapper", "") or "")
    if wrapper.endswith("_init") and kind in {"file_path", "file_path_fragment", "file_name"}:
        score += 10
    if wrapper.endswith("_short") and kind in {"upload_endpoint", "sendfile_endpoint"}:
        score += 5
    if wrapper.startswith("winapi_") and kind in {"file_path", "file_path_fragment", "file_name"}:
        score += 20
    if str(event.get("return_weixin_offset", "") or ""):
        score += 3
    hints.append(
        {
            "score": score,
            "kind": kind,
            "value": value,
            "wrapper": wrapper,
            "return_weixin_offset": event.get("return_weixin_offset", ""),
            "arg": arg,
            "source": source,
            "offset": offset,
            "pointer_offset": pointer_offset,
            "pointer": pointer,
            "encoding": encoding,
            "value_hex": value_hex,
        }
    )


def extract_hints(payload: dict) -> list[dict]:
    hints: list[dict] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        for key in ("path", "path2"):
            value = str(event.get(key, "") or "")
            if not value:
                continue
            identity = (
                str(event.get("wrapper", "")),
                key,
                value,
            )
            if identity in seen:
                continue
            seen.add(identity)
            add_hint(
                hints,
                event=event,
                arg=key,
                source=f"event.{key}",
                offset="",
                encoding="utf16le",
                value=value,
            )
        args = event.get("args", {})
        if not isinstance(args, dict):
            continue
        for arg_name, arg in args.items():
            if not isinstance(arg, dict):
                continue
            for key in ("string_candidates", "raw_string_candidates"):
                values = arg.get(key, [])
                if not isinstance(values, list):
                    continue
                for candidate in values:
                    if not isinstance(candidate, dict):
                        continue
                    value = candidate_value(candidate)
                    identity = (
                        str(event.get("wrapper", "")),
                        str(arg_name),
                        key,
                        str(candidate.get("offset", "")),
                        str(candidate.get("encoding", "")),
                        value,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    add_hint(
                        hints,
                        event=event,
                        arg=str(arg_name),
                        source=key,
                        offset=str(candidate.get("offset", "")),
                        encoding=str(candidate.get("encoding", "")),
                        value=value,
                        value_hex=str(candidate.get("value_hex", "")),
                    )
            pointer_fields = arg.get("pointer_fields", [])
            if not isinstance(pointer_fields, list):
                continue
            for field in pointer_fields:
                if not isinstance(field, dict):
                    continue
                for field_key in ("string_candidates", "raw_string_candidates"):
                    values = field.get(field_key, [])
                    if not isinstance(values, list):
                        continue
                    for candidate in values:
                        if not isinstance(candidate, dict):
                            continue
                        value = candidate_value(candidate)
                        identity = (
                            str(event.get("wrapper", "")),
                            str(arg_name),
                            f"pointer_fields.{field_key}",
                            str(field.get("offset", "")),
                            str(candidate.get("offset", "")),
                            value,
                        )
                        if identity in seen:
                            continue
                        seen.add(identity)
                        add_hint(
                            hints,
                            event=event,
                            arg=str(arg_name),
                            source=f"pointer_fields.{field_key}",
                            offset=str(candidate.get("offset", "")),
                            pointer_offset=str(field.get("offset", "")),
                            pointer=str(field.get("ptr", "")),
                            encoding=str(candidate.get("encoding", "")),
                            value=value,
                            value_hex=str(candidate.get("value_hex", "")),
                        )
    hints.sort(key=lambda item: (-int(item.get("score", 0)), str(item.get("kind", "")), str(item.get("value", ""))))
    return hints[:80]


def default_output_path(data_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return data_dir / "native_diagnostics" / f"file-send-events-{stamp}.json"


def copy_persistent_log(payload: dict, out_path: Path) -> str:
    source = str(payload.get("persistent_log_path", "") or "")
    if not source:
        return ""
    source_path = Path(source)
    if not source_path.exists():
        return ""
    target = out_path.with_suffix(".jsonl")
    shutil.copyfile(source_path, target)
    return str(target)


def load_persistent_events(payload: dict) -> list[dict]:
    source = str(payload.get("persistent_log_path", "") or "")
    if not source:
        return []
    source_path = Path(source)
    if not source_path.exists():
        return []

    events: list[dict] = []
    for line in source_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_input_payload(path: Path) -> dict:
    if path.suffix.lower() == ".jsonl":
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return {"event_count": len(events), "events": events, "events_source": "input_jsonl"}

    payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if isinstance(payload, list):
        return {"event_count": len(payload), "events": payload, "events_source": "input_json_list"}
    if not isinstance(payload, dict):
        raise RuntimeError(f"unsupported input payload in {path}")
    if "events" not in payload:
        payload["events"] = []
    payload["event_count"] = len(payload.get("events", [])) if isinstance(payload.get("events"), list) else 0
    payload.setdefault("events_source", "input_json")
    return payload


def default_analyzed_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.analyzed.json")


def report_path_for(out_path: Path) -> Path:
    return out_path.with_suffix(".report.md")


def markdown_escape(value: object) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def expected_wrapper_status(events: list[dict]) -> dict[str, bool]:
    wrappers = {str(event.get("wrapper", "") or "") for event in events if isinstance(event, dict)}
    return {wrapper: wrapper in wrappers for wrapper in EXPECTED_DIAGNOSTIC_WRAPPERS}


def top_hints_by_kind(hints: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    priority = [
        "receiver_wxid",
        "receiver_roomid",
        "file_path",
        "file_name",
        "file_md5",
        "appmsg_xml",
        "sendfile_endpoint",
        "upload_endpoint",
        "hex_id_or_token",
        "id_or_token",
    ]
    for kind in priority:
        candidates = [hint for hint in hints if str(hint.get("kind", "")) == kind]
        candidates.sort(key=lambda hint: hint_preference(kind, hint))
        for hint in candidates:
            value = str(hint.get("value", ""))
            key = f"{kind}\0{value}"
            if key in seen:
                continue
            seen.add(key)
            selected.append(hint)
            break
    return selected


def hint_preference(kind: str, hint: dict) -> tuple[int, int, int, str]:
    value = str(hint.get("value", "") or "")
    lower = value.lower()
    penalty = 0
    bonus = 0
    if kind == "file_path":
        if lower.startswith("c:\\windows\\") or "\\system32\\" in lower:
            penalty += 50
        if "\\xwechat_files\\" in lower or "\\wechat files\\" in lower or "\\wechat-doc\\" in lower:
            bonus += 30
        if "\\cache\\" in lower or "\\msg\\file\\" in lower:
            bonus += 20
    if kind == "file_name":
        if "t" in value and ":" in value:
            penalty += 50
        if value.lower().endswith((".txt", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".pdf", ".xlsx", ".zip", ".md")):
            bonus += 20
    source = str(hint.get("source", "") or "")
    if source == "raw_string_candidates":
        bonus += 2
    score = int(hint.get("score", 0) or 0)
    return (penalty, -bonus, -score, value)


def generate_markdown_report(payload: dict) -> str:
    events = payload.get("events", [])
    if not isinstance(events, list):
        events = []
    hints = payload.get("hints", [])
    if not isinstance(hints, list):
        hints = []

    wrapper_counts: dict[str, int] = {}
    callsites: dict[str, set[str]] = {}
    phases: dict[str, set[str]] = {}
    results: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        wrapper = str(event.get("wrapper", "") or "unknown")
        wrapper_counts[wrapper] = wrapper_counts.get(wrapper, 0) + 1
        phase = str(event.get("phase", "") or "")
        if phase:
            phases.setdefault(wrapper, set()).add(phase)
        callsite = str(event.get("return_weixin_offset", "") or event.get("return_address", "") or "")
        if callsite:
            callsites.setdefault(wrapper, set()).add(callsite)
        result = str(event.get("result_value", "") or "")
        if result:
            results.setdefault(wrapper, set()).add(result)

    lines = [
        "# WeChat File Send Diagnostics",
        "",
        "## Overview",
        "",
        f"- events: {len(events)}",
        f"- hints: {len(hints)}",
        f"- schema: {payload.get('schema', '')}",
        f"- events_source: {payload.get('events_source', 'live_http')}",
        f"- persistent_log_path: {payload.get('persistent_log_path', '')}",
        "",
        "## Wrappers",
        "",
    ]

    if wrapper_counts:
        lines.extend(["| wrapper | count | phases | callsites | results |", "|---|---:|---|---|---|"])
        for wrapper, count in sorted(wrapper_counts.items()):
            site_text = ", ".join(sorted(callsites.get(wrapper, set())))
            phase_text = ", ".join(sorted(phases.get(wrapper, set())))
            result_text = ", ".join(sorted(results.get(wrapper, set())))
            lines.append(
                f"| {markdown_escape(wrapper)} | {count} | {markdown_escape(phase_text)} | "
                f"{markdown_escape(site_text)} | {markdown_escape(result_text)} |"
            )
    else:
        lines.append("_No events captured._")

    lines.extend(["", "## Expected Diagnostic Wrappers", ""])
    wrapper_status = expected_wrapper_status(events)
    lines.extend(["| wrapper | captured |", "|---|---|"])
    for wrapper, captured in wrapper_status.items():
        lines.append(f"| {markdown_escape(wrapper)} | {'yes' if captured else 'no'} |")

    key_hints = top_hints_by_kind(hints)
    lines.extend(["", "## Key Hints", ""])
    if key_hints:
        lines.extend(
            [
                "| kind | value | wrapper | arg | source | offset | pointer_offset |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for hint in key_hints:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(hint.get("kind", "")),
                        markdown_escape(hint.get("value", "")),
                        markdown_escape(hint.get("wrapper", "")),
                        markdown_escape(hint.get("arg", "")),
                        markdown_escape(hint.get("source", "")),
                        markdown_escape(hint.get("offset", "")),
                        markdown_escape(hint.get("pointer_offset", "")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No key hints extracted._")

    lines.extend(["", "## Top Hints", ""])
    if hints:
        lines.extend(
            [
                "| score | kind | value | wrapper | return_weixin_offset | arg | source | offset | pointer_offset |",
                "|---:|---|---|---|---|---|---|---|---|",
            ]
        )
        for hint in hints[:30]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_escape(hint.get("score", "")),
                        markdown_escape(hint.get("kind", "")),
                        markdown_escape(hint.get("value", "")),
                        markdown_escape(hint.get("wrapper", "")),
                        markdown_escape(hint.get("return_weixin_offset", "")),
                        markdown_escape(hint.get("arg", "")),
                        markdown_escape(hint.get("source", "")),
                        markdown_escape(hint.get("offset", "")),
                        markdown_escape(hint.get("pointer_offset", "")),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No hints extracted._")

    lines.extend(["", "## Next Checks", ""])
    if events:
        lines.extend(
            [
                "- If `sendfile_submit_factory` or `sendfile_task_factory` is missing, the capture started below the high-level task creation path.",
                "- Confirm the top receiver hint matches the target wxid/roomid.",
                "- Confirm the top file path/name hints match the manually sent file.",
                "- Compare uploadappattach and sendfileuploadmsg callsites and shared ids/tokens.",
            ]
        )
    else:
        lines.append("- Run capture while manually sending one ordinary small file in WeChat.")

    lines.append("")
    return "\n".join(lines)


def read_events_with_fallback(base_url: str) -> dict:
    status = request_json(base_url, "/debug/file-send/status")
    try:
        payload = request_json(base_url, "/debug/file-send/events")
    except RuntimeError as exc:
        payload = dict(status)
        payload["events"] = []
        payload["events_error"] = str(exc)
        payload["events_source"] = "status_fallback"

    if "persistent_log_path" not in payload and "persistent_log_path" in status:
        payload["persistent_log_path"] = status["persistent_log_path"]

    persistent_events = load_persistent_events(payload)
    if persistent_events:
        payload["persistent_event_count"] = len(persistent_events)
        if len(persistent_events) > event_count(payload):
            payload["events"] = persistent_events
            payload["event_count"] = len(persistent_events)
            payload["events_source"] = "persistent_log"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture WeChat native file-send diagnostics")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--out", default="")
    parser.add_argument("--input", default="", help="analyze an existing diagnostic .json/.jsonl instead of querying WeChat")
    parser.add_argument("--clear", action="store_true", help="clear old in-memory diagnostic events first")
    parser.add_argument("--enable", action="store_true", help="enable native diagnostic hooks first")
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to wait for events")
    parser.add_argument("--min-events", type=int, default=6, help="minimum events to wait for")
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else None
    if input_path:
        payload = load_input_payload(input_path)
    else:
        if args.clear:
            request_json(args.base_url, "/debug/file-send/clear", method="POST")
        if args.enable:
            request_json(args.base_url, "/debug/file-send/enable", method="POST")

        deadline = time.monotonic() + max(0.0, args.wait)
        payload = read_events_with_fallback(args.base_url)
        while args.wait > 0 and event_count(payload) < args.min_events and time.monotonic() < deadline:
            time.sleep(0.5)
            payload = read_events_with_fallback(args.base_url)

    payload["summary"] = summarize_events(payload)
    payload["hints"] = extract_hints(payload)

    if args.out:
        out_path = Path(args.out)
    elif input_path:
        out_path = default_analyzed_output_path(input_path)
    else:
        out_path = default_output_path(Path(args.data_dir))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    persistent_copy = "" if input_path else copy_persistent_log(payload, out_path)
    if persistent_copy:
        payload["persistent_log_copy"] = persistent_copy
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = report_path_for(out_path)
    report_path.write_text(generate_markdown_report(payload), encoding="utf-8")

    print(f"events={event_count(payload)}")
    print(f"hints={len(payload['hints'])}")
    print(f"saved={out_path}")
    print(f"report={report_path}")
    if persistent_copy:
        print(f"persistent_log_copy={persistent_copy}")
    if event_count(payload) < args.min_events and not input_path:
        print("warning: no enough events captured; send one ordinary file manually while diagnostics are enabled", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
