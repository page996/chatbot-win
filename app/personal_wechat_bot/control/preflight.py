from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.conversation.channel_store import CHANNEL_POLICY
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.tools.document.libreoffice import LibreOfficeRuntime
from app.personal_wechat_bot.tools.permissions import resolve_allowed_roots
from app.personal_wechat_bot.vision.ocr import RapidOcrSubprocessEngine
from app.personal_wechat_bot.voice.asr import LocalAsrSubprocessEngine
from app.personal_wechat_bot.wechat_driver.voice_cache_resolver import voice_cache_capability
from app.personal_wechat_bot.wechat_driver.voice_transcription import WeChatVoiceTranscriptionBridge
from app.personal_wechat_bot.wechat_driver.send_driver_factory import (
    implemented_send_drivers,
    is_real_send_driver_implemented,
    registered_send_drivers,
)
from app.personal_wechat_bot.wechat_driver.bridge_send import manual_bridge_bindings


def build_preflight_report(
    config: BotConfig,
    show_accepted: bool = False,
    *,
    show_whitelist: bool | None = None,
) -> dict[str, Any]:
    if show_whitelist is not None:
        show_accepted = show_accepted or show_whitelist
    chat_provider = config.providers.get("chat", config.llm)
    key_pool = ApiKeyPool(chat_provider, config.data_dir)
    key_refs = key_pool.refs()
    api_key_present = any(item.available for item in key_refs)
    file_roots = resolve_allowed_roots(config.data_dir, config.file_read_roots)
    voice_roots = resolve_allowed_roots(config.data_dir, config.wechat_voice_roots)
    manual_bound_channels = manual_bridge_bindings(config.data_dir)
    warnings = _warnings(config, api_key_present, manual_bound_count=len(manual_bound_channels))
    ocr_health = RapidOcrSubprocessEngine().health()
    asr_health = LocalAsrSubprocessEngine().health()
    wechat_voice = WeChatVoiceTranscriptionBridge(config.data_dir).health()
    office_health = LibreOfficeRuntime().health()
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
            "send_confirm_required": config.send_confirm_required,
            "send_max_chars": config.send_max_chars,
            "send_min_interval_seconds": config.send_min_interval_seconds,
        },
        "wechat_access": {
            "read_only": not write_access_configured,
            "write_access_configured": write_access_configured,
            "primary_inputs": ["poll-backend-events", "poll-snapshot", "poll-fake"],
            "fallback_inputs": ["wechat-snapshot", "poll-clipboard"],
            "debug_inputs": ["wechat-capture"],
            "deprecated_inputs": ["ocr-snapshot", "poll-ocr-window", "ocr-window-diagnose"],
            "available_inputs": [
                "poll-backend-events",
                "wechat-snapshot",
                "poll-clipboard",
                "poll-snapshot",
                "poll-fake",
                "wechat-capture",
            ],
            "page_ocr_ingestion": "disabled",
            "send_driver": config.send_driver,
            "implemented_send_drivers": implemented_send_drivers(),
            "registered_send_drivers": registered_send_drivers(),
            "manual_bound_channels": {
                "count": len(manual_bound_channels),
                "policy": "bridge_outbox_manual_captured_channels_only",
                "items": [
                    {
                        "conversation_id": item.conversation_id,
                        "conversation_type": item.conversation_type,
                        "chat_title": item.chat_title,
                        "status": item.status,
                        "last_seen_at": item.last_seen_at,
                    }
                    for item in manual_bound_channels
                ],
            },
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
                {"ref": item.ref, "source": item.source, "available": item.available}
                for item in key_refs
            ],
            "key_pool_available_count": key_pool.available_count(),
            "max_concurrency": chat_provider.max_concurrency,
        },
        "accepted_conversations": {
            "mode": "channel_auto_accept",
            "contacts_count": len(config.accepted_contacts),
            "groups_count": len(config.accepted_groups),
            "contacts": sorted(config.accepted_contacts) if show_accepted else None,
            "groups": sorted(config.accepted_groups) if show_accepted else None,
            "legacy_files_synced": ["contacts_whitelist.json", "groups_whitelist.json"],
        },
        "legacy_whitelist": {
            "mode": "compatibility_alias_not_used_for_routing",
            "contacts_count": len(config.accepted_contacts),
            "groups_count": len(config.accepted_groups),
            "contacts": sorted(config.accepted_contacts) if show_accepted else None,
            "groups": sorted(config.accepted_groups) if show_accepted else None,
        },
        "conversation_channels": {
            "policy": CHANNEL_POLICY,
            "auto_register_private": True,
            "auto_register_groups": True,
            "private_key_slots": 1,
            "group_key_slots": 2,
            "storage": str(Path(config.data_dir).resolve() / "conversation_channels"),
            "isolation": {
                "context": "conversation_ledgers/conversation_id plus conversation_sessions/current_session_id",
                "files": "file_workspace/conversation_id/session_id",
                "backend": "conversation_channels/conversation_id/backend",
            },
        },
        "files": {
            "read_roots": [str(path) for path in file_roots],
            "wechat_voice_roots": [str(path) for path in voice_roots],
            "allowed_extensions": list(config.file_allowed_extensions),
            "max_bytes": config.file_max_bytes,
            "multimedia_parse": {
                "images": "file_layer_ocr_to_workspace_artifacts",
                "voice_messages": "backend_event_pending_voice_supported; WeChat built-in transcript first, readable voice cache/local ASR fallback when configured",
                "voice_main_path": "wechat_builtin_voice_to_text_bridge_for_manually_bound_windows",
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
        },
        "tools": {
            "ocr": {
                "name": "vision.ocr",
                "scope": "tool_layer_file_workspace_only",
                "backend": ocr_health.backend,
                "available": ocr_health.available,
                "gpu_available": ocr_health.gpu_available,
                "detail": ocr_health.detail,
            },
            "web_fetch": {
                "name": "web.fetch",
                "scope": "http_text_only",
                "available": True,
            },
            "asr": {
                "name": "voice.local_asr",
                "scope": "tool_layer_file_workspace_only",
                "backend": asr_health.backend,
                "available": asr_health.available,
                "detail": asr_health.detail,
                "install": asr_health.install,
            },
            "wechat_voice_to_text": wechat_voice,
            "wechat_voice_cache": voice_cache_capability(voice_roots, config.file_allowed_extensions),
        },
        "ocr": {
            "backend": ocr_health.backend,
            "available": ocr_health.available,
            "gpu_available": ocr_health.gpu_available,
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


def _warnings(config: BotConfig, api_key_present: bool, *, manual_bound_count: int = 0) -> list[str]:
    warnings: list[str] = []
    if config.mode == "auto":
        warnings.append("auto mode requested; use confirm mode first before real sending")
    if config.send_enabled and not is_real_send_driver_implemented(config.send_driver):
        warnings.append("send_enabled is true but send_driver is not implemented")
    if config.send_enabled and config.mode != "confirm" and config.send_confirm_required:
        warnings.append("send is enabled but mode is not confirm while send_confirm_required is true")
    if config.send_enabled and config.send_driver == "bridge_outbox" and manual_bound_count <= 0:
        warnings.append("bridge_outbox requires at least one manually captured WeChat channel binding")
    if not config.accepted_contacts and not config.accepted_groups:
        warnings.append("no accepted contacts or groups recorded yet; new WeChat conversations will auto-register channels")
    chat_provider = config.providers.get("chat", config.llm)
    if chat_provider.base_url and not api_key_present:
        warnings.append(f"chat provider base_url is configured but {chat_provider.api_key_env} is missing")
    if config.file_max_bytes <= 0:
        warnings.append("file_max_bytes must be positive")
    if not config.file_read_roots:
        warnings.append("file_read_roots is empty; document tools cannot read input files")
    return warnings
