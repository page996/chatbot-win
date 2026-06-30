from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.bootstrap import build_runtime
from app.personal_wechat_bot.control.commands import (
    accept_contact_channel,
    accept_group_channel,
    change_group_name,
    init_config,
    set_deepseek_api,
    set_chat_api,
    whitelist_contact,
    whitelist_group,
)
from app.personal_wechat_bot.control.audit import (
    build_artifact_cleanup_report,
    build_plan_audit,
)
from app.personal_wechat_bot.control.preflight import build_preflight_report
from app.personal_wechat_bot.control.sidebar_api import cleanup_sidebar_channels, delete_sidebar_channel
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.control.sidebar_server import run_sidebar_server
from app.personal_wechat_bot.control.sidebar_window import run_sidebar_window
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    send_approved_confirm_item,
    set_send_controls,
)
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.replay.runner import ReplayRunner
from app.personal_wechat_bot.runtime.agent_runner import AgentRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.memory.maintainer import MemoryMaintainer, result_payload
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.vision.ocr import RapidOcrSubprocessEngine
from app.personal_wechat_bot.vision.window_capture import Win32WindowCapture
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.wechat_driver.fake import FakeWeChatDriver
from app.personal_wechat_bot.wechat_driver.backend_events import (
    BackendEventJsonlDriver,
    append_backend_event,
)
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_ack, bridge_state
from app.personal_wechat_bot.wechat_driver.backend_file_watcher import BackendFileWatcher
from app.personal_wechat_bot.wechat_driver.snapshot_provider import (
    FileSnapshotProvider,
    WindowsClipboardSnapshotProvider,
    WindowsUIAutomationSnapshotProvider,
)
from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    WindowsWeChatReadOnlyDriver,
    Win32WindowProbe,
    find_wechat_processes,
    foreground_window_info,
)
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe
from app.personal_wechat_bot.wechat_driver.window_binding import WeChatWindowBindingStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("--data-dir", default="data")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    preflight = sub.add_parser("preflight")
    preflight.add_argument("--show-accepted", action="store_true")
    preflight.add_argument("--show-whitelist", action="store_true")

    sub.add_parser("send-readiness")

    run_agent = sub.add_parser("run-agent")
    run_agent.add_argument("--loops", type=int, default=None)
    run_agent.add_argument("--interval", type=float, default=1.0)
    run_agent.add_argument("--backend-event-file", default=None)
    run_agent.add_argument("--no-backend-events", action="store_true")
    run_agent.add_argument(
        "--no-wechat-ocr",
        action="store_true",
        help="deprecated compatibility flag; WeChat page OCR ingestion is always disabled",
    )
    run_agent.add_argument("--ocr-output", default=None, help=argparse.SUPPRESS)
    run_agent.add_argument("--ocr-capture-mode", choices=["window", "screen", "auto"], default="auto", help=argparse.SUPPRESS)
    run_agent.add_argument("--verbose", action="store_true")

    sidebar = sub.add_parser("send-sidebar")
    sidebar.add_argument("--host", default="127.0.0.1")
    sidebar.add_argument("--port", type=int, default=8765)

    sidebar_window = sub.add_parser("send-sidebar-window")
    sidebar_window.add_argument("--interval-ms", type=int, default=2000)

    send_driver_probe = sub.add_parser("send-driver-probe")
    send_driver_probe.add_argument("--driver", default=None)
    send_driver_probe.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="wait before probing so you can switch focus to the target WeChat chat window",
    )

    send_controls = sub.add_parser("set-send-controls")
    send_controls.add_argument("--mode", choices=["dry_run", "confirm", "auto"], default=None)
    send_controls.add_argument("--enable", action="store_true")
    send_controls.add_argument("--disable", action="store_true")
    send_controls.add_argument("--driver", default=None)
    send_controls.add_argument("--confirm-required", choices=["true", "false"], default=None)
    send_controls.add_argument("--max-chars", type=int, default=None)
    send_controls.add_argument("--min-interval-seconds", type=int, default=None)

    confirm_list = sub.add_parser("confirm-list")
    confirm_list.add_argument("--status", default="pending")

    send_audit = sub.add_parser("send-audit")
    send_audit.add_argument("--limit", type=int, default=20)
    send_audit.add_argument("--status", default=None)

    bridge_status = sub.add_parser("send-bridge-state")
    bridge_status.add_argument("--limit", type=int, default=30)

    bridge_ack_parser = sub.add_parser("send-bridge-ack")
    bridge_ack_parser.add_argument("bridge_id")
    bridge_ack_parser.add_argument("--status", choices=["sent", "failed", "blocked"], required=True)
    bridge_ack_parser.add_argument("--reason", default="")
    bridge_ack_parser.add_argument("--external-message-id", default="")

    confirm_approve = sub.add_parser("confirm-approve")
    confirm_approve.add_argument("queue_id")
    confirm_approve.add_argument("--reviewer", default="local_user")
    confirm_approve.add_argument("--note", default="")

    confirm_reject = sub.add_parser("confirm-reject")
    confirm_reject.add_argument("queue_id")
    confirm_reject.add_argument("--reviewer", default="local_user")
    confirm_reject.add_argument("--note", default="")

    confirm_send = sub.add_parser("confirm-send-approved")
    confirm_send.add_argument("queue_id")
    confirm_send.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="wait before sending so you can switch focus to the target WeChat chat window",
    )

    audit_plan = sub.add_parser("audit-plan")
    audit_plan.add_argument("--plan-path", default=None)

    cleanup_artifacts = sub.add_parser("cleanup-artifacts")
    cleanup_artifacts.add_argument("--apply", action="store_true")

    maintain_memory = sub.add_parser("maintain-memory")
    maintain_memory.add_argument("--conversation-id", required=True)
    maintain_memory.add_argument("--session-id", default="session_default")

    sub.add_parser("maintain-memory-all")

    accept_contact = sub.add_parser("accept-contact")
    accept_contact.add_argument("wechat_id")

    accept_group = sub.add_parser("accept-group")
    accept_group.add_argument("group_name")

    add_contact = sub.add_parser("add-contact")
    add_contact.add_argument("wechat_id")

    add_group = sub.add_parser("add-group")
    add_group.add_argument("group_name")

    rename = sub.add_parser("rename-group")
    rename.add_argument("old_name")
    rename.add_argument("new_name")

    provider = sub.add_parser("set-chat-provider")
    provider.add_argument("--base-url", required=True)
    provider.add_argument("--model", default="gpt-5.5")
    provider.add_argument("--api-key-env", default="OPENAI_API_KEY")
    provider.add_argument("--max-wait-seconds", type=int, default=None)

    deepseek = sub.add_parser("set-deepseek-provider")
    deepseek.add_argument("--base-url", default="https://api.deepseek.com")
    deepseek.add_argument("--model", default="deepseek-v4-flash")
    deepseek.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    deepseek.add_argument("--max-wait-seconds", type=int, default=60)

    replay = sub.add_parser("replay")
    replay.add_argument("fixture")
    replay.add_argument("--mode", default=None)

    poll_fake = sub.add_parser("poll-fake")
    poll_fake.add_argument("fixture")
    poll_fake.add_argument("--mode", default=None)
    poll_fake.add_argument("--loops", type=int, default=1)
    poll_fake.add_argument("--interval", type=float, default=1.0)
    poll_fake.add_argument("--verbose", action="store_true")

    append_backend = sub.add_parser("append-backend-event")
    append_backend.add_argument("--event-file", default=None)
    append_backend.add_argument("--chat-title", required=True)
    append_backend.add_argument("--sender-name", required=True)
    append_backend.add_argument("--sender-wechat-id", default="")
    append_backend.add_argument("--text", default="")
    append_backend.add_argument("--self", action="store_true", dest="is_self")
    append_backend.add_argument("--group", action="store_true")
    append_backend.add_argument("--observed-at", default="")
    append_backend.add_argument("--attachment", action="append", default=[])
    append_backend.add_argument("--voice-text", default="")
    append_backend.add_argument("--voice-duration", default="")
    append_backend.add_argument("--quote-text", default="")
    append_backend.add_argument("--quote-message-id", default="")
    append_backend.add_argument("--quote-sender-name", default="")
    append_backend.add_argument("--quote-received-at", default="")
    append_backend.add_argument("--history-json", default="")
    append_backend.add_argument("--history-file", default="")

    poll_backend = sub.add_parser("poll-backend-events")
    poll_backend.add_argument("--event-file", default=None)
    poll_backend.add_argument("--mode", default=None)
    poll_backend.add_argument("--loops", type=int, default=1)
    poll_backend.add_argument("--interval", type=float, default=1.0)
    poll_backend.add_argument("--verbose", action="store_true")
    poll_backend.add_argument("--extra-root", action="append", default=[])

    scan_backend = sub.add_parser("scan-backend-files")
    scan_backend.add_argument("--event-file", default=None)
    scan_backend.add_argument("--root", action="append", default=[])
    scan_backend.add_argument("--chat-title", required=True)
    scan_backend.add_argument("--sender-name", required=True)
    scan_backend.add_argument("--sender-wechat-id", default="")
    scan_backend.add_argument("--group", action="store_true")
    scan_backend.add_argument("--text-prefix", default="收到后台文件")
    scan_backend.add_argument("--recursive", action="store_true")
    scan_backend.add_argument("--since-minutes", type=int, default=None)
    scan_backend.add_argument("--max-files", type=int, default=None)

    sub.add_parser("wechat-health")

    poll_snapshot = sub.add_parser("poll-snapshot")
    poll_snapshot.add_argument("snapshot")
    poll_snapshot.add_argument("--mode", default=None)
    poll_snapshot.add_argument("--loops", type=int, default=1)
    poll_snapshot.add_argument("--interval", type=float, default=1.0)
    poll_snapshot.add_argument("--verbose", action="store_true")

    poll_clipboard = sub.add_parser("poll-clipboard")
    poll_clipboard.add_argument("--mode", default=None)
    poll_clipboard.add_argument("--loops", type=int, default=1)
    poll_clipboard.add_argument("--interval", type=float, default=1.0)
    poll_clipboard.add_argument("--verbose", action="store_true")

    wechat_snapshot = sub.add_parser("wechat-snapshot")
    wechat_snapshot.add_argument("--title-keyword", action="append", default=None)
    wechat_snapshot.add_argument("--max-nodes", type=int, default=500)
    wechat_snapshot.add_argument("--max-depth", type=int, default=8)
    wechat_snapshot.add_argument("--output", default=None)
    wechat_snapshot.add_argument("--probe-handles", action="store_true")

    bind_window = sub.add_parser("bind-wechat-window")
    bind_window.add_argument("--chat-title", required=True)
    bind_window.add_argument("--group", action="store_true")
    bind_window.add_argument("--conversation-id", default="")
    bind_window.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="wait before binding so you can switch focus to the target WeChat chat window",
    )

    sub.add_parser("wechat-window-bindings")

    delete_channel = sub.add_parser("delete-channel")
    delete_channel.add_argument("conversation_id")

    sub.add_parser("cleanup-hidden-channels")

    capture = sub.add_parser("wechat-capture")
    capture.add_argument("--hwnd", type=int, default=None)
    capture.add_argument("--output", default="data/wechat_window.bmp")
    capture.add_argument("--mode", choices=["window", "screen", "auto"], default="auto")

    ocr_image = sub.add_parser("ocr-image")
    ocr_image.add_argument("image")

    ocr_snapshot = sub.add_parser("ocr-snapshot")
    ocr_snapshot.add_argument("image")
    ocr_snapshot.add_argument("--chat-title", default="")

    poll_ocr = sub.add_parser("poll-ocr-window")
    poll_ocr.add_argument("--chat-title", default="")
    poll_ocr.add_argument("--output", default="data/wechat_window.bmp")
    poll_ocr.add_argument("--mode", default=None)
    poll_ocr.add_argument("--verbose", action="store_true")
    poll_ocr.add_argument("--loops", type=int, default=1)
    poll_ocr.add_argument("--interval", type=float, default=1.0)
    poll_ocr.add_argument("--capture-mode", choices=["window", "screen", "auto"], default="auto")
    poll_ocr.add_argument("--delay-seconds", type=float, default=0.0)

    diagnose_ocr = sub.add_parser("ocr-window-diagnose")
    diagnose_ocr.add_argument("--chat-title", default="")
    diagnose_ocr.add_argument("--output", default="data/wechat_window_diagnose.bmp")
    diagnose_ocr.add_argument("--capture-mode", choices=["window", "screen", "auto"], default="auto")
    diagnose_ocr.add_argument("--delay-seconds", type=float, default=0.0)
    diagnose_ocr.add_argument("--show-ocr-text", action="store_true")

    sub.add_parser("capabilities")
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.command == "init":
        init_config(args.data_dir)
        print(f"initialized config in {args.data_dir}")
        return
    if args.command == "preflight":
        config = load_config(args.data_dir)
        result = build_preflight_report(config, show_accepted=args.show_accepted or args.show_whitelist)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "send-readiness":
        result = build_send_readiness_report(args.data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "run-agent":
        config = load_config(args.data_dir)
        runtime = build_runtime(config)
        runners = []
        if not args.no_backend_events:
            event_file = args.backend_event_file or str(Path(args.data_dir) / "backend_events.jsonl")
            runners.append(
                (
                    "backend-events",
                    PollingRunner(
                        runtime,
                        BackendEventJsonlDriver(
                            event_file,
                            runtime.file_index,
                            allowed_input_roots=resolve_allowed_roots(config.data_dir, config.file_read_roots),
                            allowed_extensions=config.file_allowed_extensions,
                            max_input_bytes=config.file_max_bytes,
                            file_workspace=runtime.file_workspace,
                            session_store=runtime.session_store,
                        ),
                        poll_interval_seconds=0,
                    ),
                )
            )
        if not runners:
            raise SystemExit("run-agent requires at least one input source")
        result = AgentRunner(runners, poll_interval_seconds=args.interval).run_forever(max_loops=args.loops)
        if not args.verbose:
            for item in result.get("runners", []):
                if isinstance(item, dict):
                    detail = item.get("detail")
                    if isinstance(detail, dict):
                        detail.pop("snapshot", None)
                        detail.pop("capture", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "send-sidebar":
        run_sidebar_server(args.data_dir, host=args.host, port=args.port)
        return
    if args.command == "send-sidebar-window":
        run_sidebar_window(args.data_dir, poll_interval_ms=args.interval_ms)
        return
    if args.command == "send-driver-probe":
        _delay_for_foreground_switch(args.delay_seconds)
        result = probe_send_controls(args.data_dir, driver=args.driver)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "set-send-controls":
        if args.enable and args.disable:
            raise SystemExit("--enable and --disable cannot both be used")
        enabled = True if args.enable else (False if args.disable else None)
        confirm_required = (
            None
            if args.confirm_required is None
            else args.confirm_required == "true"
        )
        result = set_send_controls(
            args.data_dir,
            mode=args.mode,
            enabled=enabled,
            driver=args.driver,
            confirm_required=confirm_required,
            max_chars=args.max_chars,
            min_interval_seconds=args.min_interval_seconds,
        )
        print(json.dumps({"status": "ok", "send_controls": result}, ensure_ascii=False, indent=2))
        return
    if args.command == "confirm-list":
        result = list_confirm_queue(args.data_dir, status=args.status)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "send-audit":
        result = list_send_audit(args.data_dir, limit=args.limit, status=args.status)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "send-bridge-state":
        print(json.dumps(bridge_state(args.data_dir, limit=args.limit), ensure_ascii=False, indent=2))
        return
    if args.command == "send-bridge-ack":
        result = bridge_ack(
            args.data_dir,
            args.bridge_id,
            status=args.status,
            reason=args.reason,
            external_message_id=args.external_message_id,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "confirm-approve":
        result = approve_confirm_item(args.data_dir, args.queue_id, reviewer=args.reviewer, note=args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "confirm-reject":
        result = reject_confirm_item(args.data_dir, args.queue_id, reviewer=args.reviewer, note=args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "confirm-send-approved":
        _delay_for_foreground_switch(args.delay_seconds)
        result = send_approved_confirm_item(args.data_dir, args.queue_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "audit-plan":
        result = build_plan_audit(args.data_dir, plan_path=args.plan_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "cleanup-artifacts":
        result = build_artifact_cleanup_report(args.data_dir, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "maintain-memory":
        maintainer = MemoryMaintainer(ConversationLedgerStore(args.data_dir))
        result = maintainer.maintain(args.conversation_id, session_id=args.session_id)
        print(json.dumps(result_payload(result), ensure_ascii=False, indent=2))
        return
    if args.command == "maintain-memory-all":
        maintainer = MemoryMaintainer(ConversationLedgerStore(args.data_dir))
        results = [result_payload(item) for item in maintainer.maintain_all()]
        print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False, indent=2))
        return
    if args.command == "accept-contact":
        accept_contact_channel(args.data_dir, args.wechat_id)
        print(f"accepted contact channel {args.wechat_id}")
        return
    if args.command == "accept-group":
        accept_group_channel(args.data_dir, args.group_name)
        print(f"accepted group channel {args.group_name}")
        return
    if args.command == "add-contact":
        whitelist_contact(args.data_dir, args.wechat_id)
        print(f"accepted contact channel {args.wechat_id} (legacy add-contact alias)")
        return
    if args.command == "add-group":
        whitelist_group(args.data_dir, args.group_name)
        print(f"accepted group channel {args.group_name} (legacy add-group alias)")
        return
    if args.command == "rename-group":
        change_group_name(args.data_dir, args.old_name, args.new_name)
        print(f"renamed group {args.old_name} -> {args.new_name}")
        return
    if args.command == "set-chat-provider":
        set_chat_api(args.data_dir, args.base_url, args.model, args.api_key_env, args.max_wait_seconds)
        print(f"set chat provider model={args.model} base_url={args.base_url} api_key_env={args.api_key_env}")
        return
    if args.command == "set-deepseek-provider":
        set_deepseek_api(args.data_dir, args.base_url, args.model, args.api_key_env, args.max_wait_seconds)
        print(f"set DeepSeek provider model={args.model} base_url={args.base_url} api_key_env={args.api_key_env}")
        return
    if args.command == "replay":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        result = ReplayRunner(config).run(args.fixture)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "poll-fake":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        driver = FakeWeChatDriver(args.fixture)
        runner = PollingRunner(runtime, driver, poll_interval_seconds=args.interval)
        result = runner.run_forever(max_loops=args.loops)
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "append-backend-event":
        event_file = args.event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        raw_id = append_backend_event(
            event_file,
            chat_title=args.chat_title,
            sender_name=args.sender_name,
            sender_wechat_id=args.sender_wechat_id,
            text=args.text,
            is_self=args.is_self,
            is_group=args.group,
            observed_at=args.observed_at,
            attachments=args.attachment,
            voice=_voice_payload(args),
            quote=_quote_payload(args),
            history=_history_payload(args),
        )
        print(json.dumps({"status": "ok", "event_file": event_file, "raw_id": raw_id, "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "poll-backend-events":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        event_file = args.event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        driver = BackendEventJsonlDriver(
            event_file,
            runtime.file_index,
            allowed_input_roots=resolve_allowed_roots(config.data_dir, config.file_read_roots + args.extra_root),
            allowed_extensions=config.file_allowed_extensions,
            max_input_bytes=config.file_max_bytes,
            file_workspace=runtime.file_workspace,
            session_store=runtime.session_store,
        )
        runner = PollingRunner(runtime, driver, poll_interval_seconds=args.interval)
        result = runner.run_forever(max_loops=args.loops)
        result["event_file"] = event_file
        result["send_enabled"] = False
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "scan-backend-files":
        config = load_config(args.data_dir)
        event_file = args.event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        watcher = BackendFileWatcher(Path(args.data_dir) / "backend_file_watcher.sqlite", event_file)
        roots = resolve_allowed_roots(config.data_dir, args.root or config.file_read_roots)
        created = watcher.scan_once(
            roots,
            chat_title=args.chat_title,
            sender_name=args.sender_name,
            sender_wechat_id=args.sender_wechat_id,
            is_group=args.group,
            text_prefix=args.text_prefix,
            recursive=args.recursive,
            since_seconds=args.since_minutes * 60 if args.since_minutes is not None else None,
            max_files=args.max_files,
            allowed_extensions=config.file_allowed_extensions,
        )
        result = {
            "status": "ok",
            "event_file": event_file,
            "created_count": len(created),
            "created": [item.__dict__ for item in created],
            "send_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "wechat-health":
        windows = Win32WindowProbe(include_invisible=True).find_wechat_windows()
        processes = find_wechat_processes()
        foreground = foreground_window_info()
        status = "ok" if windows else ("process_only" if processes else "not_found")
        result = {
            "status": status,
            "windows": [item.__dict__ for item in windows],
            "processes": processes,
            "foreground": foreground,
            "send_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "poll-snapshot":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        driver = WindowsWeChatReadOnlyDriver(snapshot_provider=FileSnapshotProvider(args.snapshot))
        runner = PollingRunner(runtime, driver, poll_interval_seconds=args.interval)
        result = runner.run_forever(max_loops=args.loops)
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "poll-clipboard":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        driver = WindowsWeChatReadOnlyDriver(snapshot_provider=WindowsClipboardSnapshotProvider())
        runner = PollingRunner(runtime, driver, poll_interval_seconds=args.interval)
        result = runner.run_forever(max_loops=args.loops)
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "wechat-snapshot":
        provider = WindowsUIAutomationSnapshotProvider(
            title_keywords=args.title_keyword,
            max_nodes=args.max_nodes,
            max_depth=args.max_depth,
        )
        text = provider.read_text()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text)
        result = {
            "status": "ok" if text else "empty",
            "line_count": len([line for line in text.splitlines() if line.strip()]),
            "text": text,
            "output": args.output,
            "window_probe": build_wechat_window_probe(max_controls=args.max_nodes, max_depth=args.max_depth)
            if args.probe_handles
            else None,
            "send_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "bind-wechat-window":
        _delay_for_foreground_switch(args.delay_seconds)
        store = WeChatWindowBindingStore(args.data_dir)
        result = store.bind_foreground(
            chat_title=args.chat_title,
            conversation_type="group" if args.group else "private",
            conversation_id=args.conversation_id,
        )
        result["send_enabled"] = False
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "wechat-window-bindings":
        store = WeChatWindowBindingStore(args.data_dir)
        print(json.dumps({"status": "ok", "bindings": store.list_bindings(), "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "delete-channel":
        result = delete_sidebar_channel(args.data_dir, args.conversation_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "cleanup-hidden-channels":
        result = cleanup_sidebar_channels(args.data_dir, hidden_only=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "wechat-capture":
        hwnd = args.hwnd
        if hwnd is None:
            windows = Win32WindowProbe().find_wechat_windows()
            hwnd = windows[0].hwnd if windows else 0
        if not hwnd:
            result = {"status": "not_found", "send_enabled": False}
        else:
            capture_result = Win32WindowCapture().capture(hwnd, args.output, mode=args.mode)
            result = {
                "status": "ok" if capture_result.ok else "failed",
                "capture": capture_result.__dict__,
                "send_enabled": False,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "capabilities":
        ocr = RapidOcrSubprocessEngine().health()
        office = LibreOfficeRuntime().health()
        result = {
            "ocr": ocr.__dict__,
            "libreoffice": office.__dict__,
            "send_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "ocr-image":
        engine = RapidOcrSubprocessEngine()
        try:
            text = engine.read_text(args.image)
            result = {"status": "ok", "text": text}
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "ocr-snapshot":
        _ = (args.image, args.chat_title)
        print(json.dumps(_deprecated_page_ocr_payload("ocr-snapshot"), ensure_ascii=False, indent=2))
        return
    if args.command == "poll-ocr-window":
        _ = (args.chat_title, args.output, args.mode, args.verbose, args.loops, args.interval, args.capture_mode, args.delay_seconds)
        print(json.dumps(_deprecated_page_ocr_payload("poll-ocr-window"), ensure_ascii=False, indent=2))
        return
    if args.command == "ocr-window-diagnose":
        _ = (args.chat_title, args.output, args.capture_mode, args.delay_seconds, args.show_ocr_text)
        print(json.dumps(_deprecated_page_ocr_payload("ocr-window-diagnose"), ensure_ascii=False, indent=2))
        return


def _quote_payload(args: argparse.Namespace) -> dict[str, str] | None:
    quote = {
        "text": str(getattr(args, "quote_text", "")).strip(),
        "message_id": str(getattr(args, "quote_message_id", "")).strip(),
        "sender_name": str(getattr(args, "quote_sender_name", "")).strip(),
        "received_at": str(getattr(args, "quote_received_at", "")).strip(),
    }
    cleaned = {key: value for key, value in quote.items() if value}
    if not cleaned:
        return None
    return {**cleaned, "source": "append_backend_event_cli"}


def _voice_payload(args: argparse.Namespace) -> dict[str, str] | None:
    text = str(getattr(args, "voice_text", "")).strip()
    if not text:
        return None
    duration = str(getattr(args, "voice_duration", "")).strip()
    voice = {
        "status": "transcribed",
        "source": "wechat_builtin_voice_to_text",
        "text": text,
        "duration": duration,
    }
    return {key: value for key, value in voice.items() if value}


def _history_payload(args: argparse.Namespace) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    raw_json = str(getattr(args, "history_json", "") or "").strip()
    if raw_json:
        parsed = json.loads(raw_json)
        history.extend(_history_items(parsed, source="append_backend_event_cli"))
    raw_file = str(getattr(args, "history_file", "") or "").strip()
    if raw_file:
        parsed = json.loads(Path(raw_file).read_text(encoding="utf-8"))
        history.extend(_history_items(parsed, source=f"append_backend_event_cli:{raw_file}"))
    return history


def _history_items(value: Any, *, source: str) -> list[dict[str, Any]]:
    raw_items = value if isinstance(value, list) else [value]
    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("history items must be JSON objects")
        cleaned = {key: item[key] for key in item if item[key] not in (None, "")}
        cleaned.setdefault("source", source)
        items.append(cleaned)
    return items


def _ocr_parse_payload(parse_result) -> dict[str, object] | None:
    if parse_result is None:
        return None
    return {
        "status": parse_result.status,
        "reason": parse_result.reason,
        "message": parse_result.message,
        "attachments": list(parse_result.attachments),
        "evidence": list(parse_result.evidence),
    }


def _deprecated_page_ocr_payload(command: str) -> dict[str, object]:
    return {
        "status": "deprecated",
        "command": command,
        "reason": "WeChat page OCR ingestion is disabled. Use backend events or pure wechat-capture for page/window acquisition; OCR is reserved for file-layer tools such as ocr-image and vision.ocr.",
        "will_write_ledger": False,
        "processed_count": 0,
        "send_enabled": False,
    }


def _delay_for_foreground_switch(seconds: float) -> None:
    if seconds <= 0:
        return
    whole_seconds = int(seconds)
    fractional_seconds = seconds - whole_seconds
    for remaining in range(whole_seconds, 0, -1):
        print(
            f"Switch focus to the target WeChat chat window: {remaining}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(1)
    if fractional_seconds > 0:
        time.sleep(fractional_seconds)
