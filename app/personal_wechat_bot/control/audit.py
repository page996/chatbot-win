from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.loader import load_config
from app.personal_wechat_bot.control.preflight import build_preflight_report


PLAN_FILENAME = "\u6253\u9020\u8ba1\u5212.md"


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
        "keep as history; use confirm queue and the WeChatFerry send bridge for real-send rollout",
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


_RETAINED_ARTIFACTS = {
    "config.json": "runtime configuration",
    "accepted_contacts.json": "accepted private-channel compatibility state",
    "accepted_groups.json": "accepted group-channel compatibility state",
    "contacts_whitelist.json": "legacy accepted-contact compatibility state",
    "groups_whitelist.json": "legacy accepted-group compatibility state",
    "topic_rules.json": "conversation policy state",
    "search_blocklist.json": "search safety policy",
        "backend_events.jsonl": "default backend event stream; may contain replay evidence",
        "backend_events.jsonl.raw_ids.json": "backend event raw_id append deduplication index",
    "logs.jsonl": "event log and audit trail",
    "send_audit.jsonl": "manual confirmation and send-attempt audit trail",
    "backend_file_watcher.sqlite": "backend watcher deduplication state",
    "processed_messages.sqlite": "message deduplication state",
    "conversation_cooldowns.sqlite": "cooldown state",
    "file_index.sqlite": "registered file index",
    "confirm_queue.jsonl": "pending confirmation queue when present",
}


_PLAN_AUDIT_META_MARKERS = (
    "\u6b8b\u7559\u5ba1\u8ba1",
    "dated history",
    "\u8fc7\u671f\u4e8b\u5b9e",
)


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
            if path.is_file():
                path.unlink()
                item["action"] = "deleted"
                item["deleted"] = True
                deleted_count += 1
            else:
                item["action"] = "skipped_non_file"
                item["deleted"] = False
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
            "action": "run audit-plan after major changes; historical stale lines stay as dated notes",
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
        return "enable only after confirm-mode controls and the WeChatFerry send bridge are ready"
    return "configure the bridge_outbox send driver and its WeChatFerry bridge before real-send rollout"


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


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total
