from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.channel_store import CHANNEL_POLICY
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.vision.ocr import build_default_ocr_engine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import voice_cache_capability
from app.personal_wechat_bot.wechat_driver.send_driver_factory import (
    implemented_send_drivers,
    is_real_send_driver_implemented,
    registered_send_drivers,
)


def build_preflight_report(
    config: BotConfig,
    show_accepted: bool = False,
    *,
    include_tool_health: bool = True,
) -> dict[str, Any]:
    chat_provider = config.providers["chat"]
    key_pool = ApiKeyPool(chat_provider, config.data_dir)
    key_refs = key_pool.refs()
    key_descriptions = key_pool.describe()
    api_key_present = any(item.available for item in key_refs)
    file_roots = resolve_allowed_roots(config.data_dir, config.file_read_roots)
    voice_roots = resolve_allowed_roots(config.data_dir, config.wechat_voice_roots)
    warnings = _warnings(config, api_key_present)
    if include_tool_health:
        ocr_health = build_default_ocr_engine(mode=config.ocr_mode).health()
        asr_health = LocalAsrSubprocessEngine(mode=config.asr_mode).health()
        office_health = LibreOfficeRuntime().health()
    else:
        ocr_health, asr_health, office_health = _skipped_tool_health(config)
    real_send_implemented = is_real_send_driver_implemented(config.send_driver)
    write_access_configured = config.send_enabled and real_send_implemented
    return {
        "status": "warn" if warnings else "ok",
        "data_dir": str(Path(config.data_dir).resolve()),
        "mode": config.mode,
        "send_policy": {
            "dry_run": config.mode == "dry_run",
            "confirm_required": config.mode == "confirm",
            "auto_requested": config.mode == "auto",
            "real_send_implemented": real_send_implemented,
            "send_enabled": config.send_enabled,
            "send_backend": str(getattr(config, "send_backend", "dry_run") or "dry_run"),
            "send_confirm_required": config.send_confirm_required,
            "send_max_chars": config.send_max_chars,
            "send_min_interval_seconds": config.send_min_interval_seconds,
        },
        "wechat_access": {
            "read_only": not write_access_configured,
            "write_access_configured": write_access_configured,
            "primary_inputs": ["poll-backend-events"],
            "context_only_inputs": ["poll-snapshot"],
            "fallback_inputs": [],
            "debug_inputs": ["wechat-snapshot", "wechat-capture", "poll-fake"],
            "removed_inputs": ["poll-clipboard"],
            "available_inputs": [
                "poll-backend-events",
                "wechat-snapshot",
                "poll-snapshot",
                "poll-fake",
                "wechat-capture",
            ],
            "page_ocr_ingestion": "disabled",
            "send_driver": config.send_driver,
            "implemented_send_drivers": implemented_send_drivers(),
            "registered_send_drivers": registered_send_drivers(),
        },
        "model": {
            "provider_id": chat_provider.provider_id,
            "provider": chat_provider.provider,
            "model": chat_provider.model,
            "base_url_configured": bool(chat_provider.base_url),
            "api_key_env": chat_provider.api_key_env,
            "api_key_env_pool_count": len(chat_provider.api_key_env_pool),
            "api_key_file_configured": bool(chat_provider.api_key_file),
            "api_key_present": api_key_present,
            "key_pool_refs": [
                {
                    "ref": item.ref,
                    "source": item.source,
                    "available": item.available,
                    "model_config": key_descriptions[index].get("model_config", {}) if index < len(key_descriptions) else {},
                }
                for index, item in enumerate(key_refs)
            ],
            "key_model_configs": [
                item.get("model_config", {})
                for item in key_descriptions
            ],
            "key_pool_available_count": key_pool.available_count(),
            "max_concurrency": chat_provider.max_concurrency,
        },
        "accepted_conversations": {
            "mode": "channel_admission_guarded",
            "contacts_count": len(config.accepted_contacts),
            "groups_count": len(config.accepted_groups),
            "contacts": sorted(config.accepted_contacts) if show_accepted else None,
            "groups": sorted(config.accepted_groups) if show_accepted else None,
        },
        "conversation_channels": {
            "policy": CHANNEL_POLICY,
            "auto_register_private": "identified_or_accepted_only",
            "auto_register_groups": True,
            "blocks_unknown_private": True,
            "private_key_slots": 1,
            "group_key_slots": 2,
            "storage": str(Path(config.data_dir).resolve() / "conversation_channels"),
            "isolation": {
                "context": "conversation_ledgers/<stable_segment> plus conversation_sessions/<stable_segment>/current_session_id",
                "files": "file_workspace/<stable_segment>/session_id",
                "backend": "conversation_channels/<stable_segment>/backend",
            },
        },
        "files": {
            "read_roots": [str(path) for path in file_roots],
            "wechat_voice_roots": [str(path) for path in voice_roots],
            "allowed_extensions": list(config.file_allowed_extensions),
            "max_bytes": config.file_max_bytes,
            "multimedia_parse": {
                "images": "file_layer_ocr_to_workspace_artifacts",
                "voice_messages": "backend_event_pending_voice_supported; readable voice cache/local ASR fallback when configured",
                "voice_main_path": "local_asr_over_readable_voice_cache",
                "voice_cache_fallback": "readable_file_cache_only; WeChat DB decryption is not supported by design",
                "audio_files": "preserved_and_indexed; local_asr_fallback_when_available",
                "docx_embedded_images": "extracted_and_ocr_if_ocr_engine_available",
                "docx_embedded_audio": "extracted_and_local_asr_if_available",
                "pdf_embedded_media": "image_extraction_plus_optional_page_render_ocr",
            },
        },
        "runtime_guards": {
            "persistent_dedup": True,
            "group_cooldown_seconds": config.group_cooldown_seconds,
            "conversation_scheduler": "single_conversation_serial_global_limited_parallel",
            "confirm_queue": str(Path(config.data_dir).resolve() / "confirm_queue.jsonl"),
            "confirm_queue_authority": str(Path(config.data_dir).resolve() / "confirm_queue.sqlite"),
            "send_audit": str(Path(config.data_dir).resolve() / "send_audit.jsonl"),
            "send_audit_authority": str(Path(config.data_dir).resolve() / "send_audit.sqlite"),
            "state_storage_policy": {
                "conversation_ledger": "sqlite_authority_jsonl_markdown_projection",
                "confirm_queue": "sqlite_authority_jsonl_projection",
                "send_audit": "sqlite_authority_jsonl_forensic_projection",
                "sidebar_state": "sqlite_authority_json_projection",
                "send_bridge": "jsonl_evidence_preserved_on_history_clear",
            },
        },
        "tools": {
            "ocr": {
                "name": "vision.ocr",
                "scope": "tool_layer_file_workspace_only",
                "backend": ocr_health.backend,
                "available": ocr_health.available,
                "gpu_available": ocr_health.gpu_available,
                "gpu_required": ocr_health.gpu_required,
                "gpu_used": ocr_health.gpu_used,
                "mode": ocr_health.mode,
                "detail": ocr_health.detail,
            },
            "web_fetch": {
                "name": "web.fetch",
                "scope": "http_text_only",
                "available": True,
            },
            "web_search": {
                "name": "web.search",
                "scope": "http_search_scored_filtered_concurrent_fetch",
                "available": True,
                "levels": ["light", "standard", "deep"],
                "uses_browser": False,
            },
            "asr": {
                "name": "voice.local_asr",
                "scope": "tool_layer_file_workspace_only",
                "backend": asr_health.backend,
                "available": asr_health.available,
                "detail": asr_health.detail,
                "install": asr_health.install,
            },
            "wechat_voice_cache": voice_cache_capability(voice_roots, config.file_allowed_extensions),
        },
        "ocr": {
            "backend": ocr_health.backend,
            "available": ocr_health.available,
            "gpu_available": ocr_health.gpu_available,
            "gpu_required": ocr_health.gpu_required,
            "gpu_used": ocr_health.gpu_used,
            "mode": ocr_health.mode,
            "detail": ocr_health.detail,
        },
        "libreoffice": {
            "available": office_health.available,
            "executable": office_health.executable,
            "version": office_health.version,
        },
        "asr": {
            "backend": asr_health.backend,
            "available": asr_health.available,
            "model": asr_health.model,
            "detail": asr_health.detail,
            "install": asr_health.install,
        },
        "wechat_voice_cache": voice_cache_capability(voice_roots, config.file_allowed_extensions),
        "warnings": warnings,
    }


def _skipped_tool_health(config: BotConfig) -> tuple[Any, Any, Any]:
    reason = "skipped_for_fast_readiness"
    ocr = SimpleNamespace(
        backend="not_checked",
        available=False,
        gpu_available=False,
        gpu_required=config.ocr_mode == "gpu",
        gpu_used=False,
        mode=config.ocr_mode,
        detail=reason,
    )
    asr = SimpleNamespace(
        backend="not_checked",
        available=False,
        model="",
        detail=reason,
        install="",
    )
    office = SimpleNamespace(
        available=False,
        executable="",
        version="",
    )
    return ocr, asr, office


def _warnings(config: BotConfig, api_key_present: bool) -> list[str]:
    warnings: list[str] = []
    if config.mode == "auto":
        warnings.append("auto mode requested; use confirm mode first before real sending")
    if config.send_enabled and not is_real_send_driver_implemented(config.send_driver):
        warnings.append("send_enabled is true but send_driver is not implemented")
    if config.send_enabled and config.mode != "confirm" and config.send_confirm_required:
        warnings.append("send is enabled but mode is not confirm while send_confirm_required is true")
    if not config.accepted_contacts and not config.accepted_groups:
        warnings.append("no accepted contacts or groups recorded yet; unknown private WeChat conversations will be blocked")
    chat_provider = config.providers["chat"]
    if chat_provider.base_url and not api_key_present:
        warnings.append(f"chat provider base_url is configured but {chat_provider.api_key_env} is missing")
    if config.file_max_bytes <= 0:
        warnings.append("file_max_bytes must be positive")
    if not config.file_read_roots:
        warnings.append("file_read_roots is empty; document tools cannot read input files")
    return warnings
