from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config, persistent_config_dir
from app.personal_wechat_bot.control.preflight import build_preflight_report
from app.personal_wechat_bot.domain.models import utc_now_iso


PLAN_FILENAME = "\u6253\u9020\u8ba1\u5212.md"

_SQLITE_AUTHORITY_CONTRACTS = (
    {
        "component_id": "conversation_channels",
        "filename": "conversation_channels.sqlite",
        "meta_table": "channel_registry_meta",
        "expected_tables": ("channel_registry_meta", "conversation_channels"),
    },
    {
        "component_id": "conversation_ledgers",
        "filename": "conversation_ledger.sqlite",
        "meta_table": "ledger_meta",
        "expected_tables": ("ledger_meta", "ledger_conversations", "ledger_entries"),
    },
    {
        "component_id": "conversation_sessions",
        "filename": "conversation_sessions.sqlite",
        "meta_table": "session_meta",
        "expected_tables": ("session_meta", "conversation_session_states", "conversation_session_events"),
    },
    {
        "component_id": "confirm_queue",
        "filename": "confirm_queue.sqlite",
        "meta_table": "confirm_queue_meta",
        "expected_tables": ("confirm_queue_meta", "confirm_queue_items"),
    },
    {
        "component_id": "send_audit",
        "filename": "send_audit.sqlite",
        "meta_table": "send_audit_meta",
        "expected_tables": ("send_audit_meta", "send_audit_events"),
    },
    {
        "component_id": "task_manager",
        "filename": "scheduler.sqlite",
        "meta_table": "scheduler_meta",
        "expected_tables": ("scheduler_meta", "tasks", "task_events"),
    },
    {
        "component_id": "sidebar_state",
        "filename": "sidebar_state.sqlite",
        "meta_table": "sidebar_state_meta",
        "expected_tables": ("sidebar_state_meta", "sidebar_state_values", "weflow_operation_history"),
    },
    {
        "component_id": "channel_state",
        "filename": "channel_state.sqlite",
        "meta_table": "channel_state_meta",
        "expected_tables": ("channel_state_meta", "channel_states"),
    },
)


@dataclass(frozen=True)
class PlanResidualRule:
    residual_id: str
    needle: str
    status: str
    current_truth: str
    action: str


_PLAN_RESIDUAL_RULES = [
    PlanResidualRule(
        "manual_snapshot_bridge",
        "\u624b\u5de5\u590d\u5236\u4e00\u6bb5\u4f60\u6574\u7406\u6210\u4e0a\u8ff0\u5feb\u7167\u683c\u5f0f\u7684\u6587\u672c",
        "superseded_historical_note",
        "backend events and snapshot polling are implemented; WeChat page OCR ingestion is disabled",
        "keep as history, rely on backend events, pure capture, or snapshot sources for current input",
    ),
    PlanResidualRule(
        "manual_reply_before_send_module",
        "\u5728\u53d1\u9001\u6a21\u5757\u5b9e\u73b0\u524d\uff0c\u9700\u8981\u4eba\u5de5\u590d\u5236\u5019\u9009\u56de\u590d\u5230\u5fae\u4fe1",
        "superseded_historical_note",
        "bridge_outbox real send driver is implemented; current runtime truth is reported by preflight/send-readiness",
        "keep as history; use confirm queue and the local non-foreground send bridge for real-send rollout",
    ),
    PlanResidualRule(
        "uia_empty_window_bridge",
        "UIA \u6587\u672c\u4ecd\u4e3a\u7a7a",
        "fallback_historical_note",
        "backend-first ingestion is primary; OCR is no longer used to read WeChat pages",
        "keep as history, continue backend-side development and pure window capture first",
    ),
    PlanResidualRule(
        "ocr_backend_missing",
        "OCR Python \u540e\u7aef\u5c1a\u672a\u5b89\u88c5",
        "superseded_historical_note",
        "project-local RapidOCR subprocess runtime is now wired and checked by capabilities/preflight",
        "keep as history, verify current OCR health in audit output",
    ),
    PlanResidualRule(
        "office_missing",
        "\u672a\u53d1\u73b0 `soffice` / `libreoffice` / `tesseract`",
        "superseded_historical_note",
        "project-local LibreOffice is now preferred from vendor/libreoffice/program/soffice.exe",
        "keep as history, verify LibreOffice health in audit output",
    ),
    PlanResidualRule(
        "office_path_install_advice",
        "\u5b89\u88c5 LibreOffice\uff0c\u5e76\u786e\u4fdd `soffice` \u5728 PATH \u4e2d",
        "superseded_historical_note",
        "LibreOffice is injected into the project vendor tree; PATH pollution is no longer required",
        "keep as history, prefer the vendor runtime",
    ),
    PlanResidualRule(
        "pdf_pending_extraction",
        "`.pdf` \u5f53\u524d\u53ea\u767b\u8bb0\u5e76\u8fd4\u56de\u201c\u5f85\u63a5\u5165 PDF \u6587\u672c\u62bd\u53d6\u201d",
        "superseded_historical_note",
        "BackendAttachmentParser now supports PDF extraction through the attachment worker",
        "keep as history, current parser is the source of truth",
    ),
    PlanResidualRule(
        "libreoffice_reserved_only",
        "\u5df2\u9884\u7559 `outputs/libreoffice/` \u8def\u5f84",
        "superseded_historical_note",
        "FileWorkspace now exposes libreoffice_convert_to_pdf and project LibreOffice is available",
        "keep as history, use workspace outputs for conversions",
    ),
]


_DISPOSABLE_ARTIFACTS = {
    "wechat_window.bmp": "window-capture smoke image; reproducible fallback evidence",
    "wechat_uia_snapshot.txt": "empty/temporary UIA snapshot smoke output",
    "wechat_upload_smoke.jsonl": "PAGE upload smoke event stream",
    "wechat_combo_content_test.jsonl": "mixed-content smoke event stream",
    "inbox/backend_parse_smoke.txt": "backend parser smoke input",
    "inbox/page_snapshot.txt": "manual PAGE snapshot smoke input",
}


_DISPOSABLE_DIRECTORIES = {
    "runtime/sidebar_browser_profile": "Chromium sidebar profile/cache; regenerated by the sidebar launcher",
}


_RETAINED_ARTIFACTS = {
    "config.json": "runtime configuration",
    "accepted_contacts.json": "accepted private-channel state",
    "accepted_groups.json": "accepted group-channel state",
    "topic_rules.json": "conversation policy state",
    "search_blocklist.json": "search safety policy",
    "backend_events.jsonl": "default backend event stream; may contain replay evidence",
    "backend_events.jsonl.raw_ids.json": "backend event raw_id append deduplication index",
    "logs.jsonl": "event log and audit trail",
    "send_audit.jsonl": "manual confirmation and send-attempt audit trail",
    "send_audit.sqlite": "SQLite authority for send audit projections",
    "send_audit.sqlite-shm": "SQLite shared-memory companion for send audit",
    "send_audit.sqlite-wal": "SQLite WAL companion for send audit",
    "backend_file_watcher.sqlite": "backend watcher deduplication state",
    "processed_messages.sqlite": "message deduplication state",
    "conversation_cooldowns.sqlite": "cooldown state",
    "file_index.sqlite": "registered file index",
    "confirm_queue.jsonl": "pending confirmation queue when present",
    "confirm_queue.sqlite": "SQLite authority for pending confirmation queue",
    "confirm_queue.sqlite-shm": "SQLite shared-memory companion for confirmation queue",
    "confirm_queue.sqlite-wal": "SQLite WAL companion for confirmation queue",
}


_PLAN_AUDIT_META_MARKERS = (
    "\u6b8b\u7559\u5ba1\u8ba1",
    "dated history",
    "\u8fc7\u671f\u4e8b\u5b9e",
)


_STORAGE_COMPONENTS = [
    {
        "component_id": "persistent_config",
        "kind": "preserved_config",
        "authority": "file_with_sidecar_mirror",
        "paths": [
            "config.json",
            "accepted_contacts.json",
            "accepted_groups.json",
            "topic_rules.json",
            "search_blocklist.json",
            "api_key_models.local.json",
            "api_keys.local.md",
        ],
        "clear_history": "preserve",
        "migration_action": "retain small portable config files; the mirrored sidecar survives history reset",
    },
    {
        "component_id": "runtime_cards",
        "kind": "preserved_config",
        "authority": "file_with_persistent_config_root",
        "paths": ["runtime_cards"],
        "clear_history": "preserve",
        "migration_action": "persistent_config_dir is authoritative for runtime cards; keep channel overrides there",
    },
    {
        "component_id": "conversation_ledgers",
        "kind": "conversation_truth",
        "authority": "sqlite_authority_with_jsonl_markdown_projection",
        "paths": ["conversation_ledger.sqlite", "conversation_ledgers"],
        "clear_history": "reset",
        "migration_action": "database-backed ordered entries; keep messages.jsonl and conversation.md as lossless readable projections",
    },
    {
        "component_id": "conversation_sessions",
        "kind": "session_state",
        "authority": "sqlite_authority_with_json_jsonl_projection",
        "paths": ["conversation_sessions.sqlite", "conversation_sessions"],
        "clear_history": "reset",
        "migration_action": "database-backed current-session pointers and reset events; keep readable state/event projections",
    },
    {
        "component_id": "conversation_channels",
        "kind": "channel_registry",
        "authority": "sqlite_authority_with_readable_file_projection",
        "paths": ["conversation_channels.sqlite", "conversation_channels"],
        "clear_history": "reset",
        "migration_action": "database-backed; keep channel.json/index.json as readable path-resolution projections",
    },
    {
        "component_id": "file_workspace",
        "kind": "file_artifact_store",
        "authority": "content_addressed_blob_store_with_manifest_projection",
        "paths": ["file_workspace"],
        "clear_history": "reset",
        "migration_action": "deduplicate originals by SHA-256 blob and hardlink; keep derived artifacts and manifests beside each conversation",
    },
    {
        "component_id": "confirm_queue",
        "kind": "send_review_state",
        "authority": "sqlite_authority_with_jsonl_projection",
        "paths": ["confirm_queue.sqlite", "confirm_queue.jsonl"],
        "clear_history": "reset",
        "migration_action": "SQLite is authoritative; JSONL is a regenerated readable projection and is never imported",
    },
    {
        "component_id": "send_audit",
        "kind": "send_review_evidence",
        "authority": "sqlite_authority_with_jsonl_forensic_projection",
        "paths": ["send_audit.jsonl", "send_audit.sqlite"],
        "clear_history": "reset",
        "migration_action": "SQLite is the operational/read authority; JSONL is an append-only forensic projection and is never imported",
    },
    {
        "component_id": "send_bridge",
        "kind": "delivery_evidence",
        "authority": "jsonl_evidence_chain",
        "paths": [
            "send_bridge/outbox.jsonl",
            "send_bridge/acks.jsonl",
            "send_bridge/synced_acks.json",
            "send_bridge/accepted_reverify.json",
            "send_bridge/.bridge_worker.lock",
        ],
        "clear_history": "preserve",
        "migration_action": "never delete during history reset; preserve as an intact evidence chain and never replay it as new work",
    },
    {
        "component_id": "task_manager",
        "kind": "task_runtime_state",
        "authority": "scheduler_sqlite_authority_with_json_projection",
        "paths": ["scheduler.sqlite", "task_manager/tasks.json"],
        "clear_history": "reset",
        "migration_action": "SQLite is authoritative; JSON is a regenerated readable projection and is never imported",
    },
    {
        "component_id": "sidebar_state",
        "kind": "console_runtime_state",
        "authority": "sqlite_authority_with_json_projection",
        "paths": ["sidebar_state.sqlite", "weflow_sidebar_state.json"],
        "clear_history": "reset",
        "migration_action": "SQLite is authoritative for WeFlow console state; JSON is a regenerated readable projection",
    },
    {
        "component_id": "channel_state",
        "kind": "channel_projection",
        "authority": "sqlite_projection_from_channel_registry",
        "paths": ["channel_state.sqlite"],
        "clear_history": "reset",
        "migration_action": "operational projection only; source is the SQLite conversation channel registry",
    },
    {
        "component_id": "dedupe_and_indexes",
        "kind": "runtime_indexes",
        "authority": "sqlite_runtime_indexes",
        "paths": [
            "processed_messages.sqlite",
            "backend_file_watcher.sqlite",
            "file_index.sqlite",
            "conversation_cooldowns.sqlite",
        ],
        "clear_history": "reset",
        "migration_action": "runtime indexes are SQLite-backed and safe to rebuild after context reset",
    },
    {
        "component_id": "backend_and_hook_events",
        "kind": "ingress_event_stream",
        "authority": "jsonl_ingress_stream_with_dedup_indexes",
        "paths": [
            "backend_events.jsonl",
            "backend_events.jsonl.raw_ids.json",
            "hook_events.jsonl",
            "hook_events.jsonl.raw_ids.json",
            "hook_events_state.json",
        ],
        "clear_history": "reset",
        "migration_action": "raw ingress history is resettable before launch; preserve only via diagnostics export when needed",
    },
    {
        "component_id": "diagnostics_and_native_probe",
        "kind": "rebuildable_diagnostics",
        "authority": "file_reports",
        "paths": ["diagnostics", "native_diagnostics"],
        "clear_history": "operator_cleanup_only",
        "migration_action": "keep latest native migration probe; old diagnostics are dry-run cleanup candidates",
    },
]

_SQLITE_AUTHORITY_MARKERS = ("sqlite_authority", "scheduler_sqlite", "sqlite_runtime")
_FILE_TRUTH_KINDS = {
    "conversation_truth",
    "session_state",
    "channel_registry",
    "file_artifact_store",
    "ingress_event_stream",
}


def build_plan_audit(data_dir: str | Path = "data", plan_path: str | Path | None = None) -> dict[str, Any]:
    data_root = Path(data_dir)
    resolved_plan = Path(plan_path) if plan_path is not None else _project_root() / PLAN_FILENAME
    warnings: list[str] = []
    preflight: dict[str, Any] | None = None

    try:
        config = load_config(data_root)
        preflight = build_preflight_report(config)
    except Exception as exc:
        warnings.append(f"preflight unavailable: {type(exc).__name__}: {exc}")

    plan_text = ""
    if resolved_plan.exists():
        plan_text = resolved_plan.read_text(encoding="utf-8", errors="replace")
    else:
        warnings.append(f"plan file not found: {resolved_plan}")

    cleanup_dry_run = build_artifact_cleanup_report(data_root, apply=False)
    plan_residuals = _find_plan_residuals(plan_text)
    current_truth = _current_truth(preflight, data_root)
    cleanup_order = _cleanup_order(plan_residuals, cleanup_dry_run, current_truth)

    status = "warn" if warnings else "ok"
    return {
        "status": status,
        "plan_path": str(resolved_plan.resolve()) if resolved_plan.exists() else str(resolved_plan),
        "data_dir": str(data_root.resolve()),
        "current_truth": current_truth,
        "plan_residuals": plan_residuals,
        "plan_residual_count": len(plan_residuals),
        "cleanup_order": cleanup_order,
        "artifact_cleanup": cleanup_dry_run,
        "warnings": warnings,
    }


def build_artifact_cleanup_report(data_dir: str | Path = "data", apply: bool = False) -> dict[str, Any]:
    data_root = Path(data_dir).resolve()
    candidates: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    retained: list[dict[str, Any]] = []
    total_reclaimable = 0
    deleted_count = 0

    for relative, reason in sorted(_DISPOSABLE_ARTIFACTS.items()):
        path = _safe_data_child(data_root, relative)
        if not path.exists():
            missing.append({"relative_path": relative, "reason": reason})
            continue
        item = _artifact_item(data_root, path, reason)
        total_reclaimable += int(item["bytes"])
        if apply:
            deleted, error = _delete_candidate(path)
            if deleted:
                item["action"] = "deleted"
                item["deleted"] = True
                deleted_count += 1
            else:
                item["action"] = "delete_failed"
                item["deleted"] = False
                item["error"] = error
        else:
            item["action"] = "would_delete"
            item["deleted"] = False
        candidates.append(item)

    for relative, reason in sorted(_DISPOSABLE_DIRECTORIES.items()):
        path = _safe_data_child(data_root, relative)
        if not path.exists():
            missing.append({"relative_path": relative, "reason": reason})
            continue
        item = _artifact_item(data_root, path, reason)
        total_reclaimable += int(item["bytes"])
        if apply:
            deleted, error = _delete_candidate(path)
            if deleted:
                item["action"] = "deleted"
                item["deleted"] = True
                deleted_count += 1
            else:
                item["action"] = "delete_failed"
                item["deleted"] = False
                item["error"] = error
        else:
            item["action"] = "would_delete"
            item["deleted"] = False
        candidates.append(item)

    native_cleanup = _native_diagnostics_cleanup(data_root)
    for path, reason in native_cleanup["candidates"]:
        item = _artifact_item(data_root, path, reason)
        total_reclaimable += int(item["bytes"])
        if apply:
            deleted, error = _delete_candidate(path)
            if deleted:
                item["action"] = "deleted"
                item["deleted"] = True
                deleted_count += 1
            else:
                item["action"] = "delete_failed"
                item["deleted"] = False
                item["error"] = error
        else:
            item["action"] = "would_delete"
            item["deleted"] = False
        candidates.append(item)

    for relative, reason in sorted(_RETAINED_ARTIFACTS.items()):
        path = _safe_data_child(data_root, relative)
        if path.exists():
            item = _artifact_item(data_root, path, reason)
            item["action"] = "retain"
            retained.append(item)

    for path, reason in native_cleanup["retained"]:
        if path.exists():
            item = _artifact_item(data_root, path, reason)
            item["action"] = "retain"
            retained.append(item)

    file_workspace = data_root / "file_workspace"
    if file_workspace.exists():
        retained.append(
            {
                "relative_path": "file_workspace",
                "path": str(file_workspace),
                "bytes": _tree_size(file_workspace),
                "reason": "isolated per-conversation/session middle layer; do not clean automatically",
                "action": "retain",
            }
        )

    return {
        "status": "applied" if apply else "dry_run",
        "data_dir": str(data_root),
        "apply": apply,
        "safe_default": not apply,
        "candidate_count": len(candidates),
        "deleted_count": deleted_count,
        "total_reclaimable_bytes": total_reclaimable,
        "candidates": candidates,
        "missing_candidates": missing,
        "retained": retained,
    }


def build_storage_migration_status(
    data_dir: str | Path = "data",
    *,
    include_sizes: bool = True,
    max_entries_per_component: int = 5000,
) -> dict[str, Any]:
    """Report storage ownership and fresh-start boundaries without mutating data.

    Conversation channels, ledgers, and sessions use SQLite authorities with
    readable file projections. Binary artifacts and send-bridge evidence remain
    file-based by design. A deployment may clear conversation history and let the
    three databases initialize empty; this report inspects that contract without
    creating a missing database or importing legacy projections.
    """

    data_root = Path(data_dir).resolve()
    sidecar_root = persistent_config_dir(data_root).resolve()
    items = [
        _storage_component_item(
            data_root,
            component,
            include_sizes=include_sizes,
            max_entries=max(1, int(max_entries_per_component)),
        )
        for component in _STORAGE_COMPONENTS
    ]
    for item in items:
        if item["component_id"] in {"persistent_config", "runtime_cards"}:
            sidecar_report = _path_report(
                sidecar_root if item["component_id"] == "persistent_config" else sidecar_root / "runtime_cards",
                data_root=None,
                include_sizes=include_sizes,
                max_entries=max(1, int(max_entries_per_component)),
            )
            item["sidecar_root"] = sidecar_report
            if sidecar_report.get("exists"):
                item["exists"] = True
                item["existing_path_count"] = int(item.get("existing_path_count", 0) or 0) + 1
                item["total_bytes"] = int(item.get("total_bytes", 0) or 0) + int(sidecar_report.get("bytes", 0) or 0)
                item["truncated"] = bool(item.get("truncated")) or bool(sidecar_report.get("truncated"))

    summary = _storage_summary(items)
    database_contracts = [_sqlite_authority_contract(data_root, spec) for spec in _SQLITE_AUTHORITY_CONTRACTS]
    return {
        "schema": "storage_migration_status_v1",
        "status": "ok",
        "created_at": utc_now_iso(),
        "data_dir": str(data_root),
        "persistent_config_dir": str(sidecar_root),
        "safe_default": "report_only_no_mutation",
        "summary": summary,
        "items": items,
        "database_contracts": database_contracts,
        "database_contract_summary": {
            "configured_count": len(database_contracts),
            "existing_count": sum(1 for item in database_contracts if item.get("exists")),
            "valid_count": sum(1 for item in database_contracts if item.get("valid")),
            "error_count": sum(1 for item in database_contracts if item.get("status") == "error"),
        },
        "migration_boundaries": [
            {
                "component_id": "file_workspace",
                "boundary": "binary_artifact_store",
                "reason": "large originals and derived artifacts should remain content-addressed files instead of SQLite BLOBs",
                "allowed_next_step": "run bounded workspace cleanup; unreferenced content blobs are reclaimed automatically",
            },
            {
                "component_id": "send_bridge",
                "boundary": "delivery_evidence_chain",
                "reason": "outbox and ack files are the non-foreground send evidence trail and survive history reset",
                "allowed_next_step": "preserve the evidence chain intact; never delete or replay it via general cleanup",
            },
        ],
        "recommended_sequence": [
            "stop history-writing workers and use the guarded history-clear path",
            "preserve configuration, runtime cards, and the complete send-bridge evidence chain",
            "start conversation channels, ledgers, and sessions from empty SQLite authorities; never import old projections",
            "verify integrity, schema version, tables, indexes, row counts, and regenerated readable projections",
            "continue using cleanup-artifacts as dry-run first for disposable diagnostics/cache",
        ],
    }


def _sqlite_authority_contract(data_root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    path = (data_root / str(spec.get("filename") or "")).resolve()
    expected_tables = [str(item) for item in spec.get("expected_tables", ()) if str(item)]
    report: dict[str, Any] = {
        "component_id": str(spec.get("component_id") or ""),
        "path": str(path),
        "relative_path": path.name,
        "exists": path.is_file(),
        "status": "missing",
        "valid": False,
        "schema_version": "",
        "integrity_check": "not_run",
        "expected_tables": expected_tables,
        "missing_tables": expected_tables,
        "tables": [],
        "error": "",
    }
    if not path.is_file():
        return report
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        db.row_factory = sqlite3.Row
        try:
            integrity_row = db.execute("PRAGMA integrity_check(1)").fetchone()
            integrity = str(integrity_row[0] if integrity_row is not None else "")
            table_names = [
                str(row["name"] or "")
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
                if str(row["name"] or "")
            ]
            tables: list[dict[str, Any]] = []
            for table_name in table_names:
                quoted = table_name.replace('"', '""')
                columns = [
                    {
                        "name": str(row["name"] or ""),
                        "type": str(row["type"] or ""),
                        "not_null": bool(row["notnull"]),
                        "primary_key_position": int(row["pk"] or 0),
                    }
                    for row in db.execute(f'PRAGMA table_info("{quoted}")').fetchall()
                ]
                indexes = [
                    {
                        "name": str(row["name"] or ""),
                        "unique": bool(row["unique"]),
                        "origin": str(row["origin"] or ""),
                    }
                    for row in db.execute(f'PRAGMA index_list("{quoted}")').fetchall()
                ]
                row_count = int(db.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
                tables.append(
                    {
                        "name": table_name,
                        "row_count": row_count,
                        "columns": columns,
                        "indexes": indexes,
                    }
                )
            meta_table = str(spec.get("meta_table") or "")
            schema_version = ""
            if meta_table and meta_table in table_names:
                quoted_meta = meta_table.replace('"', '""')
                row = db.execute(
                    f'SELECT value FROM "{quoted_meta}" WHERE key = ?',
                    ("schema_version",),
                ).fetchone()
                schema_version = str(row[0] or "") if row is not None else ""
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        report.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
        return report
    missing_tables = [name for name in expected_tables if name not in table_names]
    valid = integrity.lower() == "ok" and not missing_tables and bool(schema_version)
    report.update(
        {
            "status": "ok" if valid else "invalid",
            "valid": valid,
            "schema_version": schema_version,
            "integrity_check": integrity,
            "missing_tables": missing_tables,
            "tables": tables,
        }
    )
    return report


def _storage_component_item(
    data_root: Path,
    component: dict[str, Any],
    *,
    include_sizes: bool,
    max_entries: int,
) -> dict[str, Any]:
    path_reports = [
        _path_report(
            _safe_data_child(data_root, relative),
            data_root=data_root,
            include_sizes=include_sizes,
            max_entries=max_entries,
        )
        for relative in component.get("paths", [])
    ]
    existing_paths = [item for item in path_reports if item.get("exists")]
    authority = str(component.get("authority") or "")
    kind = str(component.get("kind") or "")
    storage_state = _storage_state(authority, kind)
    return {
        "component_id": str(component.get("component_id") or ""),
        "kind": kind,
        "authority": authority,
        "storage_state": storage_state,
        "clear_history": str(component.get("clear_history") or ""),
        "migration_action": str(component.get("migration_action") or ""),
        "exists": bool(existing_paths),
        "existing_path_count": len(existing_paths),
        "total_bytes": sum(int(item.get("bytes", 0) or 0) for item in path_reports),
        "truncated": any(bool(item.get("truncated")) for item in path_reports),
        "paths": path_reports,
    }


def _storage_state(authority: str, kind: str) -> str:
    text = authority.lower()
    if any(marker in text for marker in _SQLITE_AUTHORITY_MARKERS):
        return "database_backed"
    if kind in _FILE_TRUTH_KINDS:
        return "file_truth_not_migrated"
    if "projection" in text:
        return "projection"
    return "file_backed"


def _path_report(
    path: Path,
    *,
    data_root: Path | None,
    include_sizes: bool,
    max_entries: int,
) -> dict[str, Any]:
    exists = path.exists()
    relative_path = ""
    if data_root is not None:
        try:
            relative_path = path.relative_to(data_root).as_posix()
        except ValueError:
            relative_path = str(path)
    else:
        relative_path = str(path)
    report: dict[str, Any] = {
        "relative_path": relative_path,
        "path": str(path),
        "exists": exists,
        "type": "missing",
        "bytes": 0,
        "entry_count": 0,
        "truncated": False,
    }
    if not exists:
        return report
    if path.is_file():
        report["type"] = "file"
        if include_sizes:
            try:
                report["bytes"] = path.stat().st_size
            except OSError:
                report["bytes"] = 0
        report["entry_count"] = 1
        return report
    if path.is_dir():
        report["type"] = "directory"
        if include_sizes:
            size, count, truncated = _tree_size_limited(path, max_entries=max_entries)
            report["bytes"] = size
            report["entry_count"] = count
            report["truncated"] = truncated
        return report
    report["type"] = "other"
    return report


def _storage_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts_by_state: dict[str, int] = {}
    bytes_by_kind: dict[str, int] = {}
    clear_history: dict[str, int] = {}
    existing = 0
    truncated = 0
    for item in items:
        state = str(item.get("storage_state") or "unknown")
        kind = str(item.get("kind") or "unknown")
        clear = str(item.get("clear_history") or "unknown")
        counts_by_state[state] = counts_by_state.get(state, 0) + 1
        bytes_by_kind[kind] = bytes_by_kind.get(kind, 0) + int(item.get("total_bytes", 0) or 0)
        clear_history[clear] = clear_history.get(clear, 0) + 1
        if item.get("exists"):
            existing += 1
        if item.get("truncated"):
            truncated += 1
    return {
        "component_count": len(items),
        "existing_component_count": existing,
        "truncated_component_count": truncated,
        "counts_by_storage_state": counts_by_state,
        "bytes_by_kind": bytes_by_kind,
        "clear_history_policy_counts": clear_history,
        "database_backed_count": counts_by_state.get("database_backed", 0),
        "file_truth_not_migrated_count": counts_by_state.get("file_truth_not_migrated", 0),
        "preserved_component_count": clear_history.get("preserve", 0),
        "reset_component_count": clear_history.get("reset", 0),
    }


def _find_plan_residuals(plan_text: str) -> list[dict[str, Any]]:
    residuals: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line_no, line in enumerate(plan_text.splitlines(), start=1):
        if any(marker in line for marker in _PLAN_AUDIT_META_MARKERS):
            continue
        for rule in _PLAN_RESIDUAL_RULES:
            if rule.needle not in line:
                continue
            key = (rule.residual_id, line_no)
            if key in seen:
                continue
            seen.add(key)
            residuals.append(
                {
                    "id": rule.residual_id,
                    "line": line_no,
                    "status": rule.status,
                    "text": line.strip(),
                    "current_truth": rule.current_truth,
                    "recommended_action": rule.action,
                }
            )
    return residuals


def _current_truth(preflight: dict[str, Any] | None, data_root: Path) -> dict[str, Any]:
    if preflight is None:
        return {
            "preflight_available": False,
            "send_enabled": False,
            "real_send_implemented": False,
            "wechat_read_only": True,
            "file_workspace_path": str((data_root / "file_workspace").resolve()),
        }
    send_policy = preflight.get("send_policy", {})
    wechat_access = preflight.get("wechat_access", {})
    return {
        "preflight_available": True,
        "mode": preflight.get("mode"),
        "send_enabled": bool(send_policy.get("send_enabled")),
        "real_send_implemented": bool(send_policy.get("real_send_implemented")),
        "wechat_read_only": bool(wechat_access.get("read_only")),
        "primary_inputs": list(wechat_access.get("primary_inputs", [])),
        "context_only_inputs": list(wechat_access.get("context_only_inputs", [])),
        "fallback_inputs": list(wechat_access.get("fallback_inputs", [])),
        "ocr": dict(preflight.get("ocr", {})),
        "libreoffice": dict(preflight.get("libreoffice", {})),
        "file_workspace_path": str((data_root / "file_workspace").resolve()),
    }


def _cleanup_order(
    plan_residuals: list[dict[str, Any]],
    cleanup_report: dict[str, Any],
    current_truth: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "priority": 1,
            "item": "plan-current-state-audit",
            "status": "implemented",
            "action": "run audit-plan after major changes and keep the residual count at zero",
            "evidence": {"plan_residual_count": len(plan_residuals)},
        },
        {
            "priority": 2,
            "item": "disposable-smoke-artifacts",
            "status": "ready_dry_run_first",
            "action": "run cleanup-artifacts, then cleanup-artifacts --apply only after reviewing candidates",
            "evidence": {"candidate_count": cleanup_report.get("candidate_count", 0)},
        },
        {
            "priority": 3,
            "item": "runtime-ledgers",
            "status": "retain",
            "action": "keep sqlite/jsonl ledgers until an explicit rotation/export policy exists",
            "evidence": {"retained_count": len(cleanup_report.get("retained", []))},
        },
        {
            "priority": 4,
            "item": "page-ocr-ingestion",
            "status": "disabled",
            "action": "keep OCR only in the file/tool layer; use backend events, snapshot text, or pure wechat-capture for page acquisition",
            "evidence": {"page_ocr_ingestion": "disabled"},
        },
        {
            "priority": 5,
            "item": "real-wechat-send",
            "status": _real_send_status(current_truth),
            "action": _real_send_action(current_truth),
            "evidence": {
                "send_enabled": bool(current_truth.get("send_enabled")),
                "real_send_implemented": bool(current_truth.get("real_send_implemented")),
                "wechat_read_only": bool(current_truth.get("wechat_read_only")),
            },
        },
    ]


def _real_send_status(current_truth: dict[str, Any]) -> str:
    if current_truth.get("send_enabled") and current_truth.get("real_send_implemented") and not current_truth.get("wechat_read_only"):
        return "guarded_confirm_rollout_ready"
    if current_truth.get("real_send_implemented"):
        return "implemented_but_not_ready"
    return "not_implemented"


def _real_send_action(current_truth: dict[str, Any]) -> str:
    if current_truth.get("send_enabled") and current_truth.get("real_send_implemented") and not current_truth.get("wechat_read_only"):
        return "continue guarded confirm-mode tests; keep auto mode disabled until send audit logs are clean"
    if current_truth.get("real_send_implemented"):
        return "enable only after confirm-mode controls and the local non-foreground bridge are ready"
    return "configure the bridge_outbox send driver and its local native bridge before real-send rollout"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _artifact_item(data_root: Path, path: Path, reason: str) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(data_root).as_posix(),
        "path": str(path),
        "bytes": path.stat().st_size if path.is_file() else _tree_size(path),
        "reason": reason,
    }


def _safe_data_child(data_root: Path, relative: str) -> Path:
    candidate = (data_root / relative).resolve()
    if candidate != data_root and data_root not in candidate.parents:
        raise PermissionError(f"path outside data directory: {candidate}")
    return candidate


def _native_diagnostics_cleanup(data_root: Path) -> dict[str, list[tuple[Path, str]]]:
    native_dir = data_root / "native_diagnostics"
    if not native_dir.exists():
        return {"candidates": [], "retained": []}
    keep: set[Path] = set()
    latest = native_dir / "native-migration-latest.json"
    if latest.exists():
        keep.add(latest.resolve())
    snapshots = sorted(
        (path for path in native_dir.glob("native-migration-*.json") if path.name != "native-migration-latest.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if snapshots:
        keep.add(snapshots[0].resolve())
    candidates: list[tuple[Path, str]] = []
    retained: list[tuple[Path, str]] = []
    for path in sorted((item for item in native_dir.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        resolved = path.resolve()
        if resolved in keep:
            retained.append((path, "latest native migration probe evidence"))
            continue
        candidates.append((path, "historical native hook diagnostic/capture artifact; reproducible from current probe scripts"))
    return {"candidates": candidates, "retained": retained}


def _delete_candidate(path: Path) -> tuple[bool, str]:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            return False, "not_file_or_directory"
        return not path.exists(), ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def _tree_size_limited(path: Path, *, max_entries: int) -> tuple[int, int, bool]:
    total = 0
    count = 0
    try:
        for child in path.rglob("*"):
            count += 1
            if count > max_entries:
                return total, count - 1, True
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total, count, False
    return total, count, False
