from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.personal_wechat_bot.domain.models import NormalizedMessage, utc_now_iso
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool


CHANNEL_POLICY = "auto_accept_wechat_contacts_and_groups"
TRUSTED_CHANNEL_SOURCES = frozenset({"backend_events_jsonl", "backend_file_watcher", "manual_backend_event"})


@dataclass(frozen=True)
class ConversationChannel:
    conversation_id: str
    conversation_type: str
    chat_title: str
    status: str
    key_slots: int
    api_key_refs: list[str]
    session_scope: str
    backend_dir: str
    context_dir: str
    file_workspace_dir: str
    sender_names: list[str]
    sender_wechat_ids: list[str]
    source_names: list[str]
    trusted_channel_source: bool
    created_at: str
    updated_at: str
    next_key_index: int = 0


class ConversationChannelStore:
    """Persistent per-conversation channel registry.

    A channel is created the first time a private chat or group appears. It
    owns the stable API-key refs and records where backend/context/file data for
    that conversation should live. Actual file copies remain under FileWorkspace
    using conversation_id/session_id.
    """

    def __init__(
        self,
        data_dir: str | Path,
        key_pool: ApiKeyPool,
        *,
        file_workspace_root: str | Path,
        context_root: str | Path,
    ):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "conversation_channels"
        self.key_pool = key_pool
        self.file_workspace_root = Path(file_workspace_root)
        self.context_root = Path(context_root)
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure_channel(self, message: NormalizedMessage) -> ConversationChannel:
        with self._lock:
            path = self._channel_path(message.conversation_id)
            existing = self._read_channel_payload(path)
            now = utc_now_iso()
            key_slots = _key_slots_for(message.conversation_type)
            api_key_refs = _merge_key_refs(
                list(existing.get("api_key_refs", [])) if existing else [],
                self._assign_key_refs(message.conversation_id, key_slots),
                key_slots,
            )
            sender_names = _append_unique(list(existing.get("sender_names", [])) if existing else [], message.sender_name)
            sender_wechat_ids = _append_unique(
                list(existing.get("sender_wechat_ids", [])) if existing else [],
                message.sender_wechat_id or "",
            )
            source_name = str(message.metadata.get("source", "")).strip()
            source_names = _append_unique(list(existing.get("source_names", [])) if existing else [], source_name)
            trusted_channel_source = bool(existing.get("trusted_channel_source", False)) if existing else False
            if source_name in TRUSTED_CHANNEL_SOURCES or message.metadata.get("trusted_channel_source") is True:
                trusted_channel_source = True
            channel_dir = self._channel_dir(message.conversation_id)
            backend_dir = channel_dir / "backend"
            backend_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "conversation_id": message.conversation_id,
                "conversation_type": message.conversation_type,
                "chat_title": message.chat_title,
                "status": "active",
                "key_slots": key_slots,
                "api_key_refs": api_key_refs,
                "session_scope": "per_conversation_current_session",
                "backend_dir": str(backend_dir),
                "context_dir": str(self.context_root / message.conversation_id),
                "file_workspace_dir": str(self.file_workspace_root / _safe_segment(message.conversation_id)),
                "sender_names": sender_names,
                "sender_wechat_ids": sender_wechat_ids,
                "source_names": source_names,
                "trusted_channel_source": trusted_channel_source,
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
                "next_key_index": int(existing.get("next_key_index", 0)) if existing else 0,
            }
            self._write_json(path, payload)
            self._update_index(payload)
            return _channel_from_payload(payload)

    def get_channel(self, conversation_id: str) -> ConversationChannel | None:
        payload = self._read_channel_payload(self._channel_path(conversation_id))
        return _channel_from_payload(payload) if payload else None

    def api_key_for_request(self, conversation_id: str) -> str | None:
        with self._lock:
            path = self._channel_path(conversation_id)
            payload = self._read_channel_payload(path)
            if not payload:
                return self.key_pool.default_key()
            refs = [str(item) for item in payload.get("api_key_refs", []) if item]
            available_refs = [ref for ref in refs if self.key_pool.key_for_ref(ref)]
            if not available_refs:
                return self.key_pool.default_key()
            next_index = int(payload.get("next_key_index", 0))
            ref = available_refs[next_index % len(available_refs)]
            payload["next_key_index"] = (next_index + 1) % len(available_refs)
            payload["updated_at"] = utc_now_iso()
            self._write_json(path, payload)
            self._update_index(payload)
            return self.key_pool.key_for_ref(ref)

    def list_channels(self) -> list[ConversationChannel]:
        channels: list[ConversationChannel] = []
        for path in sorted(self.root.glob("*/channel.json")):
            payload = self._read_channel_payload(path)
            if payload:
                channels.append(_channel_from_payload(payload))
        return channels

    def delete_channel(self, conversation_id: str) -> bool:
        return self.delete_channel_with_cleanup(conversation_id)["deleted"]

    def delete_channel_with_cleanup(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._channel_path(conversation_id)
            payload = self._read_channel_payload(path)
            if not path.exists() and not payload:
                return {
                    "deleted": False,
                    "cleanup_policy": "missing",
                    "removed": [],
                    "retained": [],
                }
            cleanup_policy = _cleanup_policy_for(payload)
            removed: list[str] = []
            retained: list[str] = []
            if cleanup_policy == "non_wechat_purge":
                for target in self._associated_paths(conversation_id):
                    if _remove_path(target):
                        removed.append(str(target))
            else:
                retained.extend(str(target) for target in self._associated_paths(conversation_id))
            channel_dir = path.parent
            if _remove_path(channel_dir):
                removed.append(str(channel_dir))
            self._remove_from_index(conversation_id)
            return {
                "deleted": True,
                "cleanup_policy": cleanup_policy,
                "removed": removed,
                "retained": retained,
            }

    def _assign_key_refs(self, conversation_id: str, slots: int) -> list[str]:
        refs = self.key_pool.refs()
        available = [item.ref for item in refs if item.available]
        candidates = available or [item.ref for item in refs]
        if not candidates:
            return []
        size = max(1, min(slots, len(candidates)))
        start = _stable_index(conversation_id, len(candidates))
        return [candidates[(start + offset) % len(candidates)] for offset in range(size)]

    def _channel_dir(self, conversation_id: str) -> Path:
        return self.root / _safe_segment(conversation_id)

    def _channel_path(self, conversation_id: str) -> Path:
        return self._channel_dir(conversation_id) / "channel.json"

    def _associated_paths(self, conversation_id: str) -> list[Path]:
        segment = _safe_segment(conversation_id)
        return [
            self.context_root / segment,
            self.file_workspace_root / segment,
            self.data_dir / "conversation_sessions" / segment,
        ]

    def _read_channel_payload(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _update_index(self, payload: dict[str, Any]) -> None:
        index_path = self.root / "index.json"
        index = self._read_channel_payload(index_path)
        channels = index.get("channels", []) if isinstance(index.get("channels"), list) else []
        kept = [
            item
            for item in channels
            if isinstance(item, dict) and item.get("conversation_id") != payload.get("conversation_id")
        ]
        kept.append(
            {
                "conversation_id": payload.get("conversation_id", ""),
                "conversation_type": payload.get("conversation_type", ""),
                "chat_title": payload.get("chat_title", ""),
                "status": payload.get("status", ""),
                "key_slots": payload.get("key_slots", 0),
                "api_key_refs": payload.get("api_key_refs", []),
                "source_names": payload.get("source_names", []),
                "trusted_channel_source": payload.get("trusted_channel_source", False),
                "updated_at": payload.get("updated_at", ""),
            }
        )
        self._write_json(
            index_path,
            {
                "policy": CHANNEL_POLICY,
                "channels": sorted(kept, key=lambda item: str(item.get("updated_at", ""))),
                "updated_at": utc_now_iso(),
            },
        )

    def _remove_from_index(self, conversation_id: str) -> None:
        index_path = self.root / "index.json"
        index = self._read_channel_payload(index_path)
        channels = index.get("channels", []) if isinstance(index.get("channels"), list) else []
        kept = [
            item
            for item in channels
            if isinstance(item, dict) and item.get("conversation_id") != conversation_id
        ]
        self._write_json(
            index_path,
            {
                "policy": CHANNEL_POLICY,
                "channels": sorted(kept, key=lambda item: str(item.get("updated_at", ""))),
                "updated_at": utc_now_iso(),
            },
        )


def _channel_from_payload(payload: dict[str, Any]) -> ConversationChannel:
    return ConversationChannel(
        conversation_id=str(payload.get("conversation_id", "")),
        conversation_type=str(payload.get("conversation_type", "")),
        chat_title=str(payload.get("chat_title", "")),
        status=str(payload.get("status", "active")),
        key_slots=int(payload.get("key_slots", 1)),
        api_key_refs=[str(item) for item in payload.get("api_key_refs", [])],
        session_scope=str(payload.get("session_scope", "per_conversation_current_session")),
        backend_dir=str(payload.get("backend_dir", "")),
        context_dir=str(payload.get("context_dir", "")),
        file_workspace_dir=str(payload.get("file_workspace_dir", "")),
        sender_names=[str(item) for item in payload.get("sender_names", [])],
        sender_wechat_ids=[str(item) for item in payload.get("sender_wechat_ids", [])],
        source_names=[str(item) for item in payload.get("source_names", [])],
        trusted_channel_source=bool(payload.get("trusted_channel_source", False)),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
        next_key_index=int(payload.get("next_key_index", 0)),
    )


def _key_slots_for(conversation_type: str) -> int:
    return 2 if conversation_type == "group" else 1


def _merge_key_refs(existing: list[str], assigned: list[str], slots: int) -> list[str]:
    merged = [item for item in existing if item]
    for item in assigned:
        if item and item not in merged:
            merged.append(item)
    return merged[: max(1, slots)]


def _append_unique(values: list[str], value: str) -> list[str]:
    cleaned = [str(item) for item in values if str(item).strip()]
    if value and value not in cleaned:
        cleaned.append(value)
    return cleaned[-20:]


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "default"


def _stable_index(value: str, modulo: int) -> int:
    if modulo <= 0:
        return 0
    import hashlib

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def _cleanup_policy_for(payload: dict[str, Any]) -> str:
    if not payload:
        return "non_wechat_purge"
    if bool(payload.get("trusted_channel_source", False)):
        return "wechat_preserve"
    source_names = {str(item).strip() for item in payload.get("source_names", []) if str(item).strip()}
    if source_names.intersection(TRUSTED_CHANNEL_SOURCES):
        return "wechat_preserve"
    sender_ids = {str(item).strip() for item in payload.get("sender_wechat_ids", []) if str(item).strip()}
    if sender_ids:
        return "wechat_preserve"
    return "non_wechat_purge"


def _remove_path(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True
