from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.config.schema import BotConfig
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
    build_storage_migration_status,
)
from app.personal_wechat_bot.control.preflight import build_preflight_report
from app.personal_wechat_bot.control.sidebar_api import (
    cleanup_sidebar_channels,
    delete_sidebar_channel,
    run_weflow_backfill_sync,
    sidebar_diagnostics_export,
    sidebar_native_migration_probe,
)
from app.personal_wechat_bot.control.send_readiness import build_send_readiness_report
from app.personal_wechat_bot.control.sidebar_server import run_sidebar_server
from app.personal_wechat_bot.control.sidebar_window import run_sidebar_window
from app.personal_wechat_bot.control.send_commands import (
    approve_confirm_item,
    list_confirm_queue,
    list_send_audit,
    probe_send_controls,
    reject_confirm_item,
    retry_bridge_item,
    send_approved_confirm_item,
    set_send_controls,
)
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.replay.runner import ReplayRunner
from app.personal_wechat_bot.runtime.agent_runner import AgentRunner
from app.personal_wechat_bot.runtime.conversation_migration import (
    ConversationMigration,
    load_migration_map,
    migrate_conversations,
)
from app.personal_wechat_bot.runtime.hook_pull_runner import HookMessagePullRunner
from app.personal_wechat_bot.runtime.polling_runner import PollingRunner
from app.personal_wechat_bot.memory.maintainer import MemoryMaintainer, result_payload
from app.personal_wechat_bot.domain.errors import ConfigError
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.vision.ocr import build_default_ocr_engine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine
from app.personal_wechat_bot.vision.window_capture import Win32WindowCapture
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.wechat_driver.fake import FakeWeChatDriver
from app.personal_wechat_bot.wechat_driver.backend_events import (
    BackendEventJsonlDriver,
    append_backend_event,
)
from app.personal_wechat_bot.wechat_driver.backend_attachment_parser import BackendAttachmentParser
from app.personal_wechat_bot.wechat_driver.bridge_send import bridge_ack, bridge_state
from app.personal_wechat_bot.wechat_driver.backend_file_watcher import BackendFileWatcher
from app.personal_wechat_bot.wechat_driver.snapshot_provider import (
    FileSnapshotProvider,
    WindowsUIAutomationSnapshotProvider,
)
from app.personal_wechat_bot.wechat_driver.windows_readonly import (
    WindowsWeChatReadOnlyDriver,
    Win32WindowProbe,
    find_wechat_processes,
)
from app.personal_wechat_bot.wechat_driver.window_introspection import build_wechat_window_probe
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import (
    WeChatVoiceCacheResolver,
    default_wechat_voice_roots,
    voice_cache_capability,
)
from app.personal_wechat_bot.wechat_driver.hook_events import HookEventJsonlImporter
from app.personal_wechat_bot.wechat_driver.hook_source_bridge import (
    WeFlowHttpBridge,
    append_hook_source_event,
    require_weflow_ready,
    weflow_health_status,
)


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
    run_agent.add_argument("--hook-event-file", default=None)
    run_agent.add_argument("--hook-state-file", default=None)
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

    diagnostics_export = sub.add_parser("diagnostics-export")
    diagnostics_export.add_argument("--limit", type=int, default=50)
    diagnostics_export.add_argument("--no-persist", action="store_true")

    native_migration_probe = sub.add_parser("native-migration-probe")
    native_migration_probe.add_argument("--no-persist", action="store_true")
    native_migration_probe.add_argument("--force-scan", action="store_true")
    native_migration_probe.add_argument("--timeout-seconds", type=float, default=None)
    native_migration_probe.add_argument("--max-depth", type=int, default=None)
    native_migration_probe.add_argument("--max-entries", type=int, default=None)
    native_migration_probe.add_argument("--limit", type=int, default=None)
    native_migration_probe.add_argument("--no-cleanup-sizes", action="store_true")

    send_controls = sub.add_parser("set-send-controls")
    send_controls.add_argument("--mode", choices=["dry_run", "confirm", "auto"], default=None)
    send_controls.add_argument("--enable", action="store_true")
    send_controls.add_argument("--disable", action="store_true")
    send_controls.add_argument("--driver", default=None)
    send_controls.add_argument("--backend", default=None)
    send_controls.add_argument("--weflow-base-url", default=None)
    send_controls.add_argument("--weflow-token-env", default=None)
    send_controls.add_argument("--weflow-send-text-path", default=None)
    send_controls.add_argument("--weflow-send-file-path", default=None)
    send_controls.add_argument("--weflow-send-timeout-seconds", type=float, default=None)
    send_controls.add_argument("--wechat-native-base-url", default=None)
    send_controls.add_argument("--wechat-native-send-text-path", default=None)
    send_controls.add_argument("--wechat-native-send-image-path", default=None)
    send_controls.add_argument("--wechat-native-send-file-path", default=None)
    send_controls.add_argument("--wechat-native-status-path", default=None)
    send_controls.add_argument("--wechat-native-timeout-seconds", type=float, default=None)
    send_controls.add_argument("--wechat-native-verify-timeout-seconds", type=float, default=None)
    send_controls.add_argument("--wechat-native-file-verify-timeout-seconds", type=float, default=None)
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
    bridge_ack_parser.add_argument("--status", choices=["sent", "accepted", "failed", "blocked"], required=True)
    bridge_ack_parser.add_argument("--reason", default="")
    bridge_ack_parser.add_argument("--external-message-id", default="")

    bridge_retry_parser = sub.add_parser("send-bridge-retry")
    bridge_retry_parser.add_argument("bridge_id")
    bridge_retry_parser.add_argument("--reviewer", default="cli")
    bridge_retry_parser.add_argument("--note", default="")

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

    audit_plan = sub.add_parser("audit-plan")
    audit_plan.add_argument("--plan-path", default=None)

    cleanup_artifacts = sub.add_parser("cleanup-artifacts")
    cleanup_artifacts.add_argument("--apply", action="store_true")

    storage_status = sub.add_parser("storage-status")
    storage_status.add_argument("--no-sizes", action="store_true")
    storage_status.add_argument("--max-entries-per-component", type=int, default=5000)

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
    poll_fake.add_argument("--forever", action="store_true")
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
    append_backend.add_argument("--voice-pending", action="store_true")
    append_backend.add_argument("--voice-audio", default="")
    append_backend.add_argument("--voice-audio-name", default="")
    append_backend.add_argument("--quote-text", default="")
    append_backend.add_argument("--quote-message-id", default="")
    append_backend.add_argument("--quote-sender-name", default="")
    append_backend.add_argument("--quote-received-at", default="")
    append_backend.add_argument("--history-json", default="")
    append_backend.add_argument("--history-file", default="")
    append_backend.add_argument("--raw-id", default="")
    append_backend.add_argument("--conversation-key", default="")

    import_hook = sub.add_parser("import-hook-events")
    import_hook.add_argument("--hook-event-file", required=True)
    import_hook.add_argument("--backend-event-file", default=None)
    import_hook.add_argument("--state-file", default=None)

    pull_hook = sub.add_parser("pull-hook-messages")
    pull_hook.add_argument("--hook-event-file", required=True)
    pull_hook.add_argument("--backend-event-file", default=None)
    pull_hook.add_argument("--state-file", default=None)
    pull_hook.add_argument("--mode", default=None)
    pull_hook.add_argument("--loops", type=int, default=1)
    pull_hook.add_argument("--forever", action="store_true")
    pull_hook.add_argument("--interval", type=float, default=1.0)
    pull_hook.add_argument("--verbose", action="store_true")
    pull_hook.add_argument("--extra-root", action="append", default=[])
    pull_hook.add_argument("--allow-concurrent-consumer", action="store_true", help="skip the single-instance lock (unsafe: two consumers race the import offset)")

    append_hook_source = sub.add_parser("append-hook-source-event")
    append_hook_source.add_argument("--hook-event-file", default=None)
    append_hook_source.add_argument("--source", choices=["raw", "weflow-push", "weflow-message"], default="raw")
    append_hook_source.add_argument("--payload-json", default="")
    append_hook_source.add_argument("--payload-file", default="")

    weflow_health = sub.add_parser("weflow-health")
    weflow_health.add_argument("--base-url", default="http://127.0.0.1:5031")
    weflow_health.add_argument("--token", default="")
    weflow_health.add_argument("--token-env", default="")
    weflow_health.add_argument("--allow-non-local", action="store_true")

    pull_weflow = sub.add_parser("pull-weflow-messages")
    pull_weflow.add_argument("--base-url", default="http://127.0.0.1:5031")
    pull_weflow.add_argument("--token", default="")
    pull_weflow.add_argument("--token-env", default="")
    pull_weflow.add_argument("--hook-event-file", default=None)
    pull_weflow.add_argument("--backend-event-file", default=None)
    pull_weflow.add_argument("--state-file", default=None)
    pull_weflow.add_argument("--weflow-state-file", default=None)
    pull_weflow.add_argument("--talker", action="append", default=[])
    pull_weflow.add_argument("--session-limit", type=int, default=100)
    pull_weflow.add_argument("--message-limit", type=int, default=100)
    pull_weflow.add_argument("--max-pages", type=int, default=1)
    pull_weflow.add_argument("--max-messages", type=int, default=0)
    pull_weflow.add_argument("--since", type=int, default=None)
    pull_weflow.add_argument("--lookback-seconds", type=int, default=300)
    pull_weflow.add_argument("--workers", type=int, default=1)
    pull_weflow.add_argument("--no-media", action="store_true")
    pull_weflow.add_argument("--context-only", action="store_true")
    pull_weflow.add_argument("--mode", default=None)
    pull_weflow.add_argument("--loops", type=int, default=1)
    pull_weflow.add_argument("--forever", action="store_true")
    pull_weflow.add_argument("--interval", type=float, default=1.0)
    pull_weflow.add_argument("--verbose", action="store_true")
    pull_weflow.add_argument("--extra-root", action="append", default=[])
    pull_weflow.add_argument("--allow-concurrent-consumer", action="store_true", help="skip the single-instance lock (unsafe: two consumers race the import offset)")
    pull_weflow.add_argument("--allow-non-local", action="store_true")

    backfill_weflow = sub.add_parser(
        "backfill-weflow-history",
        help="pull a conversation's full history as context-only (no replies) to initialize its ledger",
    )
    backfill_weflow.add_argument("--base-url", default="http://127.0.0.1:5031")
    backfill_weflow.add_argument("--token", default="")
    backfill_weflow.add_argument("--token-env", default="")
    backfill_weflow.add_argument("--talker", action="append", default=[], required=True, help="talker id(s) to backfill; repeatable")
    backfill_weflow.add_argument("--message-limit", type=int, default=100)
    backfill_weflow.add_argument("--max-pages", type=int, default=0, help="0 = walk all pages")
    backfill_weflow.add_argument("--max-messages", type=int, default=0, help="0 = no cap")
    backfill_weflow.add_argument("--workers", type=int, default=1)
    backfill_weflow.add_argument("--no-media", action="store_true")
    backfill_weflow.add_argument("--verbose", action="store_true")
    backfill_weflow.add_argument("--allow-non-local", action="store_true")

    listen_weflow = sub.add_parser("listen-weflow-sse")
    listen_weflow.add_argument("--base-url", default="http://127.0.0.1:5031")
    listen_weflow.add_argument("--token", default="")
    listen_weflow.add_argument("--token-env", default="")
    listen_weflow.add_argument("--hook-event-file", default=None)
    listen_weflow.add_argument("--weflow-state-file", default=None)
    listen_weflow.add_argument("--max-events", type=int, default=1)
    listen_weflow.add_argument("--max-seconds", type=float, default=None)
    listen_weflow.add_argument("--forever", action="store_true")
    listen_weflow.add_argument("--allow-non-local", action="store_true")

    migrate_conv = sub.add_parser("migrate-conversations")
    migrate_conv.add_argument("--map", required=True, help="path to migration map JSON")
    migrate_conv.add_argument("--apply", action="store_true", help="apply changes (default is dry-run preview)")
    migrate_conv.add_argument("--backup", action="store_true", help="copy data-dir to a timestamped backup before applying")
    migrate_conv.add_argument("--verbose", action="store_true")

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

    wechat_snapshot = sub.add_parser("wechat-snapshot")
    wechat_snapshot.add_argument("--title-keyword", action="append", default=None)
    wechat_snapshot.add_argument("--max-nodes", type=int, default=500)
    wechat_snapshot.add_argument("--max-depth", type=int, default=8)
    wechat_snapshot.add_argument("--output", default=None)
    wechat_snapshot.add_argument("--probe-handles", action="store_true")

    voice_cache = sub.add_parser("wechat-voice-cache-probe")
    voice_cache.add_argument("--root", action="append", default=[])
    voice_cache.add_argument("--include-default-roots", action="store_true")
    voice_cache.add_argument("--chat-title", default="")
    voice_cache.add_argument("--observed-at", default="")
    voice_cache.add_argument("--audio-name", default="")
    voice_cache.add_argument("--message-id", default="")
    voice_cache.add_argument("--window-seconds", type=int, default=600)
    voice_cache.add_argument("--max-scan-files", type=int, default=2000)

    delete_channel = sub.add_parser("delete-channel")
    delete_channel.add_argument("conversation_id")

    sub.add_parser("cleanup-hidden-channels")

    capture = sub.add_parser("wechat-capture")
    capture.add_argument("--hwnd", type=int, default=None)
    capture.add_argument("--output", default="data/wechat_window.bmp")
    capture.add_argument("--mode", choices=["window", "screen", "auto"], default="auto")

    ocr_image = sub.add_parser("ocr-image")
    ocr_image.add_argument("image")

    local_asr = sub.add_parser("local-asr")
    local_asr.add_argument("audio")
    local_asr.add_argument("--model", default="base")
    local_asr.add_argument("--language", default="auto")

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
            if args.hook_event_file:
                poller = PollingRunner(
                    runtime,
                    _backend_event_driver(config, runtime, event_file),
                    poll_interval_seconds=0,
                )
                runners.append(
                    (
                        "hook-messages",
                        HookMessagePullRunner(
                            HookEventJsonlImporter(
                                args.hook_event_file,
                                event_file,
                                state_path=args.hook_state_file or Path(args.data_dir) / "hook_events_state.json",
                            ),
                            poller,
                            hook_event_file=args.hook_event_file,
                            backend_event_file=event_file,
                        ),
                    )
                )
            else:
                runners.append(
                    (
                        "backend-events",
                        PollingRunner(
                            runtime,
                            _backend_event_driver(config, runtime, event_file),
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
        result = probe_send_controls(args.data_dir, driver=args.driver)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "diagnostics-export":
        result = sidebar_diagnostics_export(
            args.data_dir,
            {"limit": args.limit, "persist": not bool(args.no_persist)},
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "native-migration-probe":
        payload = {
            "persist": not bool(args.no_persist),
            "force_scan": bool(args.force_scan),
            "include_cleanup_sizes": not bool(args.no_cleanup_sizes),
        }
        if args.timeout_seconds is not None:
            payload["timeout_seconds"] = args.timeout_seconds
        if args.max_depth is not None:
            payload["max_depth"] = args.max_depth
        if args.max_entries is not None:
            payload["max_entries"] = args.max_entries
        if args.limit is not None:
            payload["limit"] = args.limit
        result = sidebar_native_migration_probe(args.data_dir, payload)
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
            backend=args.backend,
            weflow_base_url=args.weflow_base_url,
            weflow_token_env=args.weflow_token_env,
            weflow_send_text_path=args.weflow_send_text_path,
            weflow_send_file_path=args.weflow_send_file_path,
            weflow_send_timeout_seconds=args.weflow_send_timeout_seconds,
            wechat_native_base_url=args.wechat_native_base_url,
            wechat_native_send_text_path=args.wechat_native_send_text_path,
            wechat_native_send_image_path=args.wechat_native_send_image_path,
            wechat_native_send_file_path=args.wechat_native_send_file_path,
            wechat_native_status_path=args.wechat_native_status_path,
            wechat_native_timeout_seconds=args.wechat_native_timeout_seconds,
            wechat_native_verify_timeout_seconds=args.wechat_native_verify_timeout_seconds,
            wechat_native_file_verify_timeout_seconds=args.wechat_native_file_verify_timeout_seconds,
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
    if args.command == "send-bridge-retry":
        result = retry_bridge_item(
            args.data_dir,
            args.bridge_id,
            reviewer=args.reviewer,
            note=args.note,
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
    if args.command == "storage-status":
        result = build_storage_migration_status(
            args.data_dir,
            include_sizes=not bool(args.no_sizes),
            max_entries_per_component=args.max_entries_per_component,
        )
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
        result = runner.run_forever(max_loops=None if args.forever else args.loops)
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
            raw_id=args.raw_id,
            source_payload=_source_payload_args(args),
        )
        print(json.dumps({"status": "ok", "event_file": event_file, "raw_id": raw_id, "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "import-hook-events":
        event_file = args.backend_event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        importer = HookEventJsonlImporter(
            args.hook_event_file,
            event_file,
            state_path=args.state_file or Path(args.data_dir) / "hook_events_state.json",
        )
        result = importer.import_new()
        print(json.dumps({**result.__dict__, "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "append-hook-source-event":
        hook_event_file = args.hook_event_file or str(Path(args.data_dir) / "hook_events.jsonl")
        payload = _json_payload_arg(args.payload_json, args.payload_file)
        result = append_hook_source_event(hook_event_file, payload, source=args.source)
        print(json.dumps({**result.__dict__, "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "weflow-health":
        result = weflow_health_status(
            args.base_url,
            token=_token_arg(args.token, args.token_env),
            allow_non_local=args.allow_non_local,
            require_token=False,
            require_fork=False,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "pull-weflow-messages":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        hook_event_file = args.hook_event_file or str(Path(args.data_dir) / "hook_events.jsonl")
        backend_event_file = args.backend_event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        state_file = args.state_file or Path(args.data_dir) / "hook_events_state.json"
        token = _token_arg(args.token, args.token_env)
        weflow_ready = require_weflow_ready(args.base_url, token=token, allow_non_local=args.allow_non_local)
        bridge = WeFlowHttpBridge(
            args.base_url,
            token=token,
            hook_event_file=hook_event_file,
            state_path=args.weflow_state_file,
            allow_non_local=args.allow_non_local,
        )
        weflow_extra_roots = [*args.extra_root, *_weflow_media_roots(weflow_ready)]
        driver = _backend_event_driver(config, runtime, backend_event_file, extra_roots=weflow_extra_roots)
        runner = HookMessagePullRunner(
            HookEventJsonlImporter(hook_event_file, backend_event_file, state_path=state_file),
            PollingRunner(runtime, driver, poll_interval_seconds=args.interval),
            hook_event_file=hook_event_file,
            backend_event_file=backend_event_file,
            consume_lock_enabled=not args.allow_concurrent_consumer,
        )
        with runner.single_instance(enabled=not args.allow_concurrent_consumer, label="cli:pull-weflow-messages"):
            result = _run_weflow_pull_loop(
                bridge,
                runner,
                talkers=args.talker,
                session_limit=args.session_limit,
                message_limit=args.message_limit,
                max_pages=args.max_pages,
                max_messages=args.max_messages,
                since=args.since,
                lookback_seconds=args.lookback_seconds,
                workers=args.workers,
                media=not args.no_media,
                context_only=args.context_only or (args.since is not None and args.since <= 0),
                max_loops=None if args.forever else args.loops,
                interval=args.interval,
            )
        if not args.verbose:
            result.pop("processed", None)
        result["weflow_ready"] = weflow_ready
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "backfill-weflow-history":
        result = run_weflow_backfill_sync(
            args.data_dir,
            {
                "base_url": args.base_url,
                "token": args.token,
                "token_env": args.token_env,
                "talkers": args.talker,
                "message_limit": args.message_limit,
                "max_pages": args.max_pages,
                "max_messages": args.max_messages,
                "workers": args.workers,
                "no_media": args.no_media,
                "allow_non_local": args.allow_non_local,
            },
        )
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "listen-weflow-sse":
        hook_event_file = args.hook_event_file or str(Path(args.data_dir) / "hook_events.jsonl")
        token = _token_arg(args.token, args.token_env)
        weflow_ready = require_weflow_ready(args.base_url, token=token, allow_non_local=args.allow_non_local)
        bridge = WeFlowHttpBridge(
            args.base_url,
            token=token,
            hook_event_file=hook_event_file,
            state_path=args.weflow_state_file,
            allow_non_local=args.allow_non_local,
        )
        result = bridge.listen_sse(
            max_events=None if args.forever else args.max_events,
            max_seconds=args.max_seconds,
        )
        print(json.dumps({**result.__dict__, "weflow_ready": weflow_ready, "send_enabled": False}, ensure_ascii=False, indent=2))
        return
    if args.command == "migrate-conversations":
        result = _run_migrate_conversations(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "pull-hook-messages":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        hook_event_file = args.hook_event_file
        backend_event_file = args.backend_event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        state_file = args.state_file or Path(args.data_dir) / "hook_events_state.json"
        driver = _backend_event_driver(config, runtime, backend_event_file, extra_roots=args.extra_root)
        runner = HookMessagePullRunner(
            HookEventJsonlImporter(hook_event_file, backend_event_file, state_path=state_file),
            PollingRunner(runtime, driver, poll_interval_seconds=args.interval),
            hook_event_file=hook_event_file,
            backend_event_file=backend_event_file,
            consume_lock_enabled=not args.allow_concurrent_consumer,
        )
        with runner.single_instance(enabled=not args.allow_concurrent_consumer, label="cli:pull-hook-messages"):
            result = runner.run_forever(max_loops=None if args.forever else args.loops)
        if not args.verbose:
            result.pop("processed", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "poll-backend-events":
        config = load_config(args.data_dir)
        if args.mode:
            config.mode = args.mode
        runtime = build_runtime(config)
        event_file = args.event_file or str(Path(args.data_dir) / "backend_events.jsonl")
        driver = _backend_event_driver(config, runtime, event_file, extra_roots=args.extra_root)
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
        status = "ok" if windows else ("process_only" if processes else "not_found")
        result = {
            "status": status,
            "windows": [item.__dict__ for item in windows],
            "processes": processes,
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
    if args.command == "wechat-voice-cache-probe":
        config = load_config(args.data_dir)
        roots = resolve_allowed_roots(config.data_dir, config.wechat_voice_roots + args.root)
        if args.include_default_roots:
            roots = [*roots, *default_wechat_voice_roots()]
        resolver = WeChatVoiceCacheResolver(
            roots,
            allowed_extensions=config.file_allowed_extensions,
            max_bytes=config.file_max_bytes,
            time_window_seconds=args.window_seconds,
            max_scan_files=args.max_scan_files,
        )
        voice = {
            "audio_name": args.audio_name,
            "message_id": args.message_id,
        }
        result = resolver.resolve(voice, chat_title=args.chat_title, observed_at=args.observed_at)
        payload = {
            "status": result.status,
            "capability": voice_cache_capability(roots, config.file_allowed_extensions),
            "result": result.to_dict(),
            "send_enabled": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
        config = _load_config_or_default(args.data_dir)
        ocr = build_default_ocr_engine(mode=config.ocr_mode).health()
        office = LibreOfficeRuntime().health()
        asr = LocalAsrSubprocessEngine(mode=config.asr_mode).health()
        voice_roots = resolve_allowed_roots(config.data_dir, config.wechat_voice_roots)
        result = {
            "ocr": ocr.__dict__,
            "libreoffice": office.__dict__,
            "asr": asr.__dict__,
            "wechat_voice_cache": voice_cache_capability(voice_roots, config.file_allowed_extensions),
            "send_enabled": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "ocr-image":
        engine = build_default_ocr_engine(mode=config.ocr_mode)
        try:
            text = engine.read_text(args.image)
            result = {"status": "ok", "text": text}
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "local-asr":
        engine = LocalAsrSubprocessEngine(model=args.model, language=args.language, mode=config.asr_mode)
        transcript = engine.transcribe(args.audio)
        result = {
            "status": transcript.status,
            "transcript": transcript.__dict__,
            "send_enabled": False,
        }
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
    audio_path = str(getattr(args, "voice_audio", "")).strip()
    pending = bool(getattr(args, "voice_pending", False))
    if not text and not pending and not audio_path:
        return None
    duration = str(getattr(args, "voice_duration", "")).strip()
    voice = {
        "status": "transcribed" if text else "pending",
        "source": "manual_voice_transcript" if text else "voice_audio_pending",
        "text": text,
        "duration": duration,
        "audio_path": audio_path,
        "audio_name": str(getattr(args, "voice_audio_name", "")).strip(),
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


def _source_payload_args(args: argparse.Namespace) -> dict[str, Any] | None:
    conversation_key = str(getattr(args, "conversation_key", "") or "").strip()
    if not conversation_key:
        return None
    return {
        "source": "append_backend_event_cli",
        "conversation_key": conversation_key,
    }


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


def _json_payload_arg(payload_json: str, payload_file: str) -> dict[str, Any]:
    if payload_json and payload_file:
        raise SystemExit("--payload-json and --payload-file cannot both be used")
    if payload_file:
        payload = json.loads(Path(payload_file).read_text(encoding="utf-8"))
    elif payload_json:
        payload = json.loads(payload_json)
    else:
        payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")
    return payload


def _token_arg(token: str, token_env: str) -> str:
    if token:
        return token
    if token_env:
        return os.environ.get(token_env, "")
    return os.environ.get("WEFLOW_API_TOKEN", "")


def _weflow_media_roots(weflow_ready: dict[str, Any]) -> list[str]:
    health = weflow_ready.get("health") if isinstance(weflow_ready.get("health"), dict) else {}
    media_path = str(health.get("mediaExportPath") or health.get("media_export_path") or "").strip()
    return [media_path] if media_path else []


def _run_weflow_pull_loop(
    bridge: WeFlowHttpBridge,
    runner: HookMessagePullRunner,
    *,
    talkers: list[str],
    session_limit: int,
    message_limit: int,
    max_pages: int,
    max_messages: int,
    since: int | None,
    lookback_seconds: int,
    workers: int,
    media: bool,
    context_only: bool,
    max_loops: int | None,
    interval: float,
) -> dict[str, Any]:
    loops = 0
    source_scanned = 0
    source_appended = 0
    imported_count = 0
    processed_count = 0
    processed: list[dict[str, Any]] = []
    last_source: dict[str, Any] = {}
    last_pull: dict[str, Any] = {}
    while max_loops is None or loops < max_loops:
        source = bridge.pull_once(
            talkers=talkers,
            session_limit=session_limit,
            message_limit=message_limit,
            max_pages=max_pages,
            max_messages=max_messages,
            since=since,
            lookback_seconds=lookback_seconds,
            workers=workers,
            media=media,
            context_only=context_only,
        )
        last_source = _jsonable_dataclass(source)
        source_scanned += source.scanned_count
        source_appended += source.appended_count
        pull = runner.run_once()
        last_pull = pull
        loops += 1
        imported_count += int(pull.get("import", {}).get("appended_count", 0) or 0)
        processed_count += int(pull.get("processed_count", 0) or 0)
        processed.extend([item for item in pull.get("processed", []) if isinstance(item, dict)])
        if max_loops is None or loops < max_loops:
            time.sleep(interval)
    status = "stopped"
    if last_source.get("status") not in {None, "", "ok"}:
        status = "stopped_with_source_errors"
    return {
        "status": status,
        "loops": loops,
        "base_url": bridge.base_url,
        "workers": max(1, int(workers or 1)),
        "hook_event_file": str(bridge.writer.path),
        "backend_event_file": str(runner.backend_event_file),
        "source_scanned_count": source_scanned,
        "source_appended_count": source_appended,
        "imported_count": imported_count,
        "processed_count": processed_count,
        "last_source": last_source,
        "queue": last_pull.get("queue", {}) if last_pull else runner.queue_status(None),
        "last_import": last_pull.get("import", {}) if last_pull else {},
        "last_poll": last_pull.get("poll", {}) if last_pull else {},
        "processed": processed,
        "send_enabled": False,
    }


def _jsonable_dataclass(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _run_migrate_conversations(args) -> dict[str, Any]:
    from app.personal_wechat_bot.normalizer.normalizer import conversation_id_for

    data_dir = Path(args.data_dir)
    migrations = _resolve_migrations(args.map, conversation_id_for)
    backup_path = ""
    if args.apply and args.backup:
        backup_path = _backup_data_dir(data_dir)
    report = migrate_conversations(data_dir, migrations, dry_run=not args.apply)
    payload = report.to_dict()
    payload["applied"] = bool(args.apply)
    payload["backup_path"] = backup_path
    if not args.apply:
        payload["hint"] = "dry-run only; re-run with --apply (and --backup) to execute"
    if not args.verbose:
        for item in payload.get("items", []):
            item.pop("moved_dirs", None)
    return payload


def _resolve_migrations(map_path: str, conversation_id_for) -> list[ConversationMigration]:
    """Load a migration map, deriving new_id from talker_id when omitted.

    Map entries may provide new_id directly, or provide talker_id (+ optional
    conversation_type, default 'private'/'group' inferred from '@chatroom') so
    the new conversation_id is computed the same way the pipeline would.
    """

    raw = json.loads(Path(map_path).read_text(encoding="utf-8"))
    items = raw.get("migrations") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit("migration map must contain a 'migrations' list")
    resolved: list[ConversationMigration] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        old_id = str(item.get("old_id") or "").strip()
        if not old_id:
            continue
        new_id = str(item.get("new_id") or "").strip()
        talker_id = str(item.get("talker_id") or "").strip()
        chat_title = str(item.get("chat_title") or "").strip()
        if not new_id and talker_id:
            ctype = str(item.get("conversation_type") or "").strip()
            if not ctype:
                ctype = "group" if talker_id.endswith("@chatroom") else "private"
            new_id = conversation_id_for(ctype, talker_id)
        if not new_id:
            raise SystemExit(f"migration entry for old_id={old_id} needs new_id or talker_id")
        resolved.append(
            ConversationMigration(old_id=old_id, new_id=new_id, chat_title=chat_title, talker_id=talker_id)
        )
    if not resolved:
        raise SystemExit("no usable migration entries found")
    return resolved


def _backup_data_dir(data_dir: Path) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    backup = data_dir.parent / f"{data_dir.name}_backup_{stamp}"
    shutil.copytree(data_dir, backup)
    return str(backup)


def _load_config_or_default(data_dir: str) -> BotConfig:
    try:
        return load_config(data_dir)
    except ConfigError:
        return BotConfig(data_dir=data_dir)


def _voice_cache_resolver(config, *, extra_roots: list[str] | None = None) -> WeChatVoiceCacheResolver | None:
    roots = config.wechat_voice_roots + list(extra_roots or [])
    if not roots:
        return None
    return WeChatVoiceCacheResolver(
        resolve_allowed_roots(config.data_dir, roots),
        allowed_extensions=config.file_allowed_extensions,
        max_bytes=config.file_max_bytes,
    )


def _backend_event_driver(
    config: BotConfig,
    runtime,
    event_file: str | Path,
    *,
    extra_roots: list[str] | None = None,
) -> BackendEventJsonlDriver:
    roots = config.file_read_roots + config.wechat_voice_roots + list(extra_roots or [])
    return BackendEventJsonlDriver(
        event_file,
        runtime.file_index,
        allowed_input_roots=resolve_allowed_roots(config.data_dir, roots),
        allowed_extensions=config.file_allowed_extensions,
        max_input_bytes=config.file_max_bytes,
        attachment_parser=BackendAttachmentParser(
            build_default_ocr_engine(mode=config.ocr_mode),
            LocalAsrSubprocessEngine(mode=config.asr_mode),
        ),
        file_workspace=runtime.file_workspace,
        session_store=runtime.session_store,
        voice_cache_resolver=_voice_cache_resolver(config, extra_roots=extra_roots),
    )


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


if __name__ == "__main__":
    main()
