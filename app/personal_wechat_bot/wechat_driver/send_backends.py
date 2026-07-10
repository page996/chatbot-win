"""Pluggable, non-foreground send backends for the outbox bridge.

The bridge worker consumes ``send_bridge/outbox.jsonl`` and delivers each record
to WeChat through one of these backends. None of them touch the foreground
window, the mouse, or the clipboard: delivery is by wxid/roomid, not by focus.

Three backends ship today:

* :class:`DryRunSendBackend` — the safe default. It never contacts WeChat; it
  records and logs deliveries and reports success. This keeps the whole
  outbox -> bridge -> ack -> ledger chain runnable and testable without a live
  WeChat native bridge install.
* :class:`WeFlowHttpSendBackend` — optional HTTP adapter for a local WeFlow fork
  when it exposes send capabilities.
* :class:`WeChatNativeHttpSendBackend` — the project-owned local native bridge
  contract for PC WeChat 4.x delivery by wxid/roomid.

Every backend serializes its own sends behind a lock. The bridge worker
additionally drives a single sender thread; the lock here is defence in depth.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
_DEFAULT_WEFLOW_SEND_TIMEOUT_SECONDS = 35.0
_DEFAULT_WECHAT_NATIVE_SEND_TIMEOUT_SECONDS = 15.0
_DEFAULT_WECHAT_NATIVE_VERIFY_TIMEOUT_SECONDS = 10.0
_DEFAULT_WECHAT_NATIVE_FILE_VERIFY_TIMEOUT_SECONDS = 45.0
_DEFAULT_WECHAT_NATIVE_VERIFY_POLL_SECONDS = 0.75
_MAX_EVIDENCE_STRING_LENGTH = 500
_MAX_EVIDENCE_LIST_ITEMS = 20
_MAX_EVIDENCE_DICT_ITEMS = 50
_SYNTHETIC_PRIVATE_WECHAT_SUFFIXES = frozenset(
    {
        "a",
        "b",
        "c",
        "alice",
        "bob",
        "carol",
        "dave",
        "friend",
        "stranger",
        "unknown",
        "unidentified",
        "test",
        "tester",
        "abc",
        "abc123",
    }
)


@dataclass(frozen=True)
class SendOutcome:
    """Result of a single backend delivery attempt."""

    ok: bool
    reason: str
    external_message_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    delivery_verified: bool = True

    @classmethod
    def success(
        cls,
        reason: str = "sent",
        external_message_id: str = "",
        payload: dict[str, Any] | None = None,
        *,
        delivery_verified: bool = True,
    ) -> "SendOutcome":
        return cls(True, reason, external_message_id, payload or {}, delivery_verified)

    @classmethod
    def failure(cls, reason: str, payload: dict[str, Any] | None = None) -> "SendOutcome":
        return cls(False, reason, "", payload or {}, False)

    @classmethod
    def accepted_unverified(
        cls,
        reason: str,
        *,
        external_message_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> "SendOutcome":
        return cls(True, reason, external_message_id, payload or {}, False)


def weflow_http_status(
    base_url: str = "http://127.0.0.1:5031",
    *,
    token_env: str = "WEFLOW_API_TOKEN",
    token: str = "",
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Check the local WeFlow HTTP bridge used for 4.x Weixin integration."""

    resolved_token = token or _token_from_env(token_env)
    api_base = _weflow_api_base_url(base_url)
    try:
        _require_local_http_url(api_base)
        health = _http_json(api_base.rstrip("/") + "/health", method="GET", token=resolved_token, timeout_seconds=timeout_seconds)
        capabilities = _weflow_send_capabilities(health)
        return {
            "status": "available",
            "available": True,
            "base_url": base_url,
            "api_base_url": api_base,
            "token_env": token_env,
            "token_present": bool(resolved_token),
            "health": health,
            "send_capabilities": capabilities,
            "reason": "",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "available": False,
            "base_url": base_url,
            "api_base_url": api_base,
            "token_env": token_env,
            "token_present": bool(resolved_token),
            "send_capabilities": _weflow_send_capabilities({}),
            "reason": f"{type(exc).__name__}:{exc}",
        }


def _weflow_send_capabilities(health: dict[str, Any]) -> dict[str, Any]:
    raw = health.get("capabilities") if isinstance(health.get("capabilities"), dict) else {}

    def _support(key: str) -> bool | None:
        if key not in raw:
            return None
        return bool(raw.get(key))

    return {
        "text": {
            "supports": _support("sendText"),
            "status": _capability_status(_support("sendText")),
        },
        "file": {
            "supports": _support("sendFile"),
            "status": _capability_status(_support("sendFile")),
        },
        "backend": str(raw.get("sendBackend") or ""),
    }


def _capability_status(value: bool | None) -> str:
    if value is True:
        return "supported"
    if value is False:
        return "unsupported"
    return "unknown"


def _token_from_env(token_env: str) -> str:
    name = str(token_env or "").strip() or "WEFLOW_API_TOKEN"
    return os.environ.get(name, "") or os.environ.get("WEFLOW_API_TOKEN", "")


def _weflow_api_base_url(base_url: str) -> str:
    text = str(base_url or "").strip().rstrip("/") or "http://127.0.0.1:5031"
    return text if text.endswith("/api/v1") else f"{text}/api/v1"


def _weflow_endpoint_url(base_url: str, endpoint_path: str) -> str:
    path = str(endpoint_path or "").strip() or "/send/text"
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return _weflow_api_base_url(base_url).rstrip("/") + "/" + path.lstrip("/")


def _local_endpoint_url(base_url: str, endpoint_path: str, *, default_base_url: str, default_path: str) -> str:
    base = str(base_url or "").strip().rstrip("/") or default_base_url
    path = str(endpoint_path or "").strip() or default_path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def _require_local_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise ValueError("local send backend only allows http endpoints")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local send backend only allows localhost endpoints")


def _http_json(
    url: str,
    *,
    method: str,
    token: str = "",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float,
) -> dict[str, Any]:
    _require_local_http_url(url)
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=max(0.2, float(timeout_seconds))) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        finally:
            exc.close()
        raise ValueError(f"http_{exc.code}:{detail}") from exc
    except URLError as exc:
        raise ValueError(f"url_error:{exc.reason}") from exc
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("response_not_json_object")
    return parsed


def _weflow_payload_success(payload: dict[str, Any]) -> bool:
    if not payload:
        return True
    if payload.get("success") is False or payload.get("ok") is False:
        return False
    if payload.get("success") is True or payload.get("ok") is True:
        return True
    if payload.get("code") in (0, "0"):
        return True
    status = str(payload.get("status") or "").strip().lower()
    if status in {"ok", "success", "sent"}:
        return True
    if status in {"error", "failed", "fail"}:
        return False
    return True


def _weflow_failure_detail(payload: dict[str, Any]) -> str:
    for key in ("error", "message", "reason", "detail"):
        value = payload.get(key)
        if value:
            return str(value)
    if "code" in payload:
        return f"code={payload.get('code')}"
    return "send_failed"


def _weflow_external_message_id(payload: dict[str, Any]) -> str:
    for key in ("message_id", "messageId", "msgid", "msgId", "id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _compact_evidence(value: Any, *, depth: int = 0) -> Any:
    """Keep backend response evidence JSON-serializable and bounded."""

    if depth > 4:
        return str(value)[:_MAX_EVIDENCE_STRING_LENGTH]
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_EVIDENCE_DICT_ITEMS:
                compact["_truncated"] = True
                break
            compact[str(key)] = _compact_evidence(item, depth=depth + 1)
        return compact
    if isinstance(value, list):
        items = [_compact_evidence(item, depth=depth + 1) for item in value[:_MAX_EVIDENCE_LIST_ITEMS]]
        if len(value) > _MAX_EVIDENCE_LIST_ITEMS:
            items.append({"_truncated": True, "omitted": len(value) - _MAX_EVIDENCE_LIST_ITEMS})
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_EVIDENCE_STRING_LENGTH:
            return value[:_MAX_EVIDENCE_STRING_LENGTH] + "...[truncated]"
        return value
    return str(value)[:_MAX_EVIDENCE_STRING_LENGTH]


def _backend_evidence(
    *,
    backend: str,
    response: dict[str, Any] | None = None,
    endpoint_path: str = "",
    operation: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"backend": backend}
    if operation:
        payload["operation"] = operation
    if endpoint_path:
        payload["endpoint_path"] = endpoint_path
    if response is not None:
        payload["response"] = _compact_evidence(response)
    return payload


def _synthetic_private_wechat_receiver_blocker(receiver: str, *, backend: str) -> str:
    """Block fixture wxids from ever reaching a real send endpoint.

    Channel admission is the primary authorization layer. This backend-level
    guard is the last stop for direct probes and tests accidentally pointed at a
    live WeChat hook, where placeholders like ``wxid_a`` can open a stranger
    chat before the native client reports failure.
    """

    text = str(receiver or "").strip()
    if not text.startswith("wxid_"):
        return ""
    suffix = text[5:].strip().lower()
    if not suffix:
        return f"{backend}_blocked_synthetic_private_receiver:empty_suffix"
    if len(suffix) <= 4 or suffix in _SYNTHETIC_PRIVATE_WECHAT_SUFFIXES:
        return f"{backend}_blocked_synthetic_private_receiver:{text}"
    if re.fullmatch(r"(?:alice|bob|carol|dave|friend|stranger|unknown|unidentified|test|tester|abc)\d*", suffix):
        return f"{backend}_blocked_synthetic_private_receiver:{text}"
    return ""


def wechat_native_http_status(
    base_url: str = "http://127.0.0.1:30001",
    *,
    text_path: str = "/SendTextMsg",
    image_path: str = "/SendImgMsg",
    file_path: str = "/send_file_msg",
    status_path: str = "/QueryDB/status",
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Check a local PC WeChat hook HTTP service without touching the UI."""

    url = _local_endpoint_url(
        base_url,
        status_path,
        default_base_url="http://127.0.0.1:30001",
        default_path="/QueryDB/status",
    )
    try:
        status_payload = _http_json(url, method="GET", timeout_seconds=timeout_seconds)
        logged_in = _wechat_native_is_logged_in(status_payload)
        return {
            "status": "available" if logged_in else "not_login",
            "available": logged_in,
            "base_url": base_url,
            "status_url": url,
            "health": status_payload,
            "send_capabilities": wechat_native_send_capabilities(
                text_path=text_path,
                image_path=image_path,
                file_path=file_path,
            ),
            "reason": "" if logged_in else "wechat_native_not_login",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "available": False,
            "base_url": base_url,
            "status_url": url,
            "health": {},
            "send_capabilities": wechat_native_send_capabilities(
                text_path=text_path,
                image_path=image_path,
                file_path=file_path,
            ),
            "reason": f"{type(exc).__name__}:{exc}",
        }


def wechat_native_send_capabilities(
    *,
    text_path: str = "/SendTextMsg",
    image_path: str = "/SendImgMsg",
    file_path: str = "/send_file_msg",
) -> dict[str, Any]:
    """Describe configured hook send endpoints without performing a send."""

    text_path = str(text_path or "").strip()
    image_path = str(image_path or "").strip()
    file_path = str(file_path or "").strip()
    image_default = image_path.lower() == "/sendimgmsg"
    file_default = file_path.lower() == "/send_file_msg"
    return {
        "text": {
            "status": "configured" if text_path else "not_configured",
            "path": text_path,
            "supports": bool(text_path),
            "verification": "native_response_or_weflow_readback",
            "note": (
                "the bundled 4.1.10.53 text hook is verified by WeFlow readback when available; "
                "without readback it is reported as accepted but unverified"
            ),
        },
        "image": {
            "status": (
                "default_route_unsupported_in_text_hook_build"
                if image_default
                else ("custom_endpoint_unverified" if image_path else "not_configured")
            ),
            "path": image_path,
            "supports": False if image_default else None,
            "note": (
                "the bundled wechat-hook-411053-text artifact returns unsupported_on_411053_text_only for /SendImgMsg"
                if image_default
                else "custom endpoint; verify by bridge ack payload"
            ),
            "verification": "bridge_ack_payload_response",
        },
        "file": {
            "status": "default_route_accepts_unverified_native_file" if file_default else ("custom_endpoint_unverified" if file_path else "not_configured"),
            "path": file_path,
            "supports": True if file_default else None,
            "note": (
                "the project-owned /send_file_msg contract reaches the 4.1.10.53 native file upload path; delivery remains accepted-but-unverified until readback confirms the WeChat message"
                if file_default
                else "custom endpoint; verify by bridge ack payload"
            ),
            "verification": "bridge_ack_payload_response",
            "delivery_verified": False if file_default else None,
            "async_completion": True if file_default else None,
        },
    }


def wechat_native_file_send_blocker(
    path: str,
    *,
    image_path: str = "/SendImgMsg",
    file_path: str = "/send_file_msg",
) -> str:
    """Return a deterministic blocker for known text-only hook media routes.

    The current 4.1.10.53 hook artifact used by this project exposes text
    sending and a native ordinary-file path through ``/send_file_msg``. The
    default image route is still intentionally blocked: image/GIF delivery is a
    separate media path and should not be faked as an ordinary file send.
    Custom endpoints are left unblocked so a replacement hook can be dropped in
    without changing the bridge/outbox contract.
    """

    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS and str(image_path or "").strip().lower() == "/sendimgmsg":
        return "wechat_native_http_send_image_unsupported:unsupported_on_411053_text_only"
    return ""


def _wechat_native_is_logged_in(payload: dict[str, Any]) -> bool:
    if payload.get("IsLogin") in (1, "1", True):
        return True
    data = payload.get("data")
    if isinstance(data, dict) and data.get("IsLogin") in (1, "1", True):
        return True
    return False


def _wechat_native_payload_success(payload: dict[str, Any]) -> bool:
    if payload.get("ret") in (0, "0"):
        return True
    if "ret" in payload:
        return False
    if payload.get("success") is True or payload.get("ok") is True:
        return True
    if payload.get("success") is False or payload.get("ok") is False:
        return False
    if payload.get("code") in (0, "0"):
        return True
    if "code" in payload:
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"ok", "success", "sent"}:
        return True
    if status in {"error", "failed", "fail"}:
        return False
    retmsg = str(payload.get("retmsg") or payload.get("message") or "").strip().lower()
    return retmsg in {"ok", "success", "sent"}


def _wechat_native_payload_delivery_verified(payload: dict[str, Any]) -> bool:
    """True only when a replacement hook explicitly verifies WeChat delivery.

    The bundled 4.1.10.53 text hook returns ``ret=0`` after the HTTP handler
    runs, even though the handler does not expose a native send return value or
    message id. Treat that as accepted-but-unverified unless a custom hook adds
    an explicit verification flag.
    """

    sources = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        sources.append(data)
    delivery = payload.get("delivery")
    if isinstance(delivery, dict):
        sources.append(delivery)
    for source in sources:
        for key in ("delivery_verified", "verified_delivery", "wechat_delivery_verified"):
            if source.get(key) is True:
                return True
        if source.get("verified") is True and source.get("delivery") is True:
            return True
    return False


def _wechat_native_failure_detail(payload: dict[str, Any]) -> str:
    for key in ("retmsg", "msg", "error", "message", "reason", "detail", "desc"):
        value = payload.get(key)
        if value:
            return str(value)
    if "ret" in payload:
        return f"ret={payload.get('ret')}"
    if "code" in payload:
        return f"code={payload.get('code')}"
    if "status" in payload:
        return f"status={payload.get('status')}"
    return "send_failed"


def _wechat_native_external_message_id(payload: dict[str, Any]) -> str:
    sources = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        sources.append(data)
    for source in sources:
        for key in ("message_id", "messageId", "msgid", "msgId", "id", "newMsgId", "clientMsgId"):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _wechat_native_delivery_watermark(messages: list[dict[str, Any]]) -> dict[str, Any]:
    max_sort_seq = 0
    max_create_time = 0
    max_local_id = 0
    message_keys: list[str] = []
    for message in messages:
        sort_seq = _message_int(message, "sortSeq", "sort_seq")
        create_time = _message_timestamp_seconds(message)
        local_id = _message_int(message, "localId", "local_id", "msgid", "msgId")
        max_sort_seq = max(max_sort_seq, sort_seq)
        max_create_time = max(max_create_time, create_time)
        max_local_id = max(max_local_id, local_id)
        key = _weflow_message_external_id(message)
        if key:
            message_keys.append(key)
    return {
        "count": len(messages),
        "max_sort_seq": max_sort_seq,
        "max_create_time": max_create_time,
        "max_local_id": max_local_id,
        "message_keys": message_keys[:50],
    }


def _find_verified_wechat_text_message(
    messages: list[dict[str, Any]],
    *,
    text: str,
    before: dict[str, Any],
    send_started_at: float,
) -> dict[str, Any]:
    for message in messages:
        if not _weflow_message_is_outgoing(message):
            continue
        if _normalize_wechat_text(_weflow_message_text(message)) != _normalize_wechat_text(text):
            continue
        if _weflow_message_is_newer_than_probe(message, before=before, send_started_at=send_started_at):
            return message
    return {}


def _find_verified_wechat_file_message(
    messages: list[dict[str, Any]],
    *,
    file_name: str,
    file_size: int,
    before: dict[str, Any],
    send_started_at: float,
) -> dict[str, Any]:
    expected_name = _normalize_wechat_file_name(file_name)
    if not expected_name:
        return {}
    matches: list[dict[str, Any]] = []
    for message in messages:
        if not _weflow_message_is_outgoing(message):
            continue
        actual_name = _normalize_wechat_file_name(_weflow_message_file_name(message))
        if actual_name:
            if actual_name != expected_name:
                continue
        elif expected_name not in _normalize_wechat_file_name(_weflow_message_text(message)):
            continue
        actual_size = _weflow_message_file_size(message)
        if file_size > 0 and actual_size > 0 and actual_size != file_size:
            continue
        if _weflow_message_is_newer_than_probe(message, before=before, send_started_at=send_started_at):
            matches.append(message)
    return matches[0] if len(matches) == 1 else {}


def _weflow_message_is_outgoing(message: dict[str, Any]) -> bool:
    return message.get("isSend") in (1, "1", True)


def _weflow_message_text(message: dict[str, Any]) -> str:
    for key in ("content", "rawContent", "parsedContent", "text"):
        value = message.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _normalize_wechat_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _weflow_message_is_newer_than_probe(
    message: dict[str, Any],
    *,
    before: dict[str, Any],
    send_started_at: float,
) -> bool:
    before_keys = {str(item) for item in before.get("message_keys", []) if item}
    message_key = _weflow_message_external_id(message)
    if message_key and message_key in before_keys:
        return False
    if _message_int(message, "localId", "local_id", "msgid", "msgId") > int(before.get("max_local_id") or 0):
        return True
    if _message_int(message, "sortSeq", "sort_seq") > int(before.get("max_sort_seq") or 0):
        return True
    timestamp = _message_timestamp_seconds(message)
    if timestamp and timestamp >= int(send_started_at) - 5:
        return True
    return bool(message_key and not before_keys and timestamp == 0)


def _message_timestamp_seconds(message: dict[str, Any]) -> int:
    value = _message_int(message, "createTime", "create_time", "timestamp")
    if value:
        return value // 1000 if value > 10_000_000_000 else value
    sort_seq = _message_int(message, "sortSeq", "sort_seq")
    return sort_seq // 1000 if sort_seq > 10_000_000_000 else sort_seq


def _message_int(message: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = message.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _weflow_message_external_id(message: dict[str, Any]) -> str:
    for key in ("serverId", "server_id", "messageKey", "message_key", "msgId", "msgid", "localId", "local_id"):
        value = message.get(key)
        if value not in (None, "", "0", 0):
            return str(value)
    return ""


def _weflow_message_file_name(message: dict[str, Any]) -> str:
    for key in ("fileName", "file_name", "name", "title"):
        value = message.get(key)
        if value not in (None, ""):
            return Path(str(value)).name
    content = _weflow_message_text(message).strip()
    if not content:
        return ""
    try:
        root = ET.fromstring(content)
        title = root.findtext("appmsg/title") if root.tag == "msg" else root.findtext("title")
        if title:
            return Path(str(title)).name
    except ET.ParseError:
        pass
    title = _xml_fragment_text(content, "title")
    return Path(title).name if title else ""


def _weflow_message_file_size(message: dict[str, Any]) -> int:
    for key in ("fileSize", "file_size", "size", "totalLen", "totallen"):
        size = _safe_int(message.get(key))
        if size > 0:
            return size
    content = _weflow_message_text(message).strip()
    if not content:
        return 0
    try:
        root = ET.fromstring(content)
        text = root.findtext("appmsg/appattach/totallen") if root.tag == "msg" else root.findtext("appattach/totallen")
        size = _safe_int(text)
        if size > 0:
            return size
    except ET.ParseError:
        pass
    return _safe_int(_xml_fragment_text(content, "totallen"))


def _xml_fragment_text(text: str, tag: str) -> str:
    start = f"<{tag}>"
    end = f"</{tag}>"
    raw = str(text or "")
    start_index = raw.find(start)
    if start_index < 0:
        return ""
    start_index += len(start)
    end_index = raw.find(end, start_index)
    if end_index < 0:
        return ""
    return raw[start_index:end_index]


def _normalize_wechat_file_name(name: str) -> str:
    return Path(str(name or "").strip()).name.casefold()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso_epoch_seconds(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


class SendBackend(Protocol):
    name: str

    def health_check(self) -> bool: ...

    def send_text(self, receiver: str, text: str) -> SendOutcome: ...

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome: ...

    def close(self) -> None: ...


class DryRunSendBackend:
    """Default backend: records deliveries, contacts nothing, always succeeds.

    Lets the full send chain be exercised end to end without a live WeChat.
    """

    name = "dry_run"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sent_texts: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, str, str]] = []

    def health_check(self) -> bool:
        return True

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        with self._lock:
            self.sent_texts.append((receiver, text))
        logger.info("[dry_run] send_text -> %s: %r", receiver, text)
        return SendOutcome.success(
            "dry_run_not_delivered:text",
            payload=_backend_evidence(backend=self.name, operation="send_text"),
        )

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        with self._lock:
            self.sent_files.append((receiver, path, caption))
        logger.info("[dry_run] send_file -> %s: %s (caption=%r)", receiver, path, caption)
        return SendOutcome.success(
            "dry_run_not_delivered:file",
            payload=_backend_evidence(backend=self.name, operation="send_file"),
        )

    def close(self) -> None:
        return None


class WeFlowHttpSendBackend:
    """Real delivery through a local WeFlow HTTP send endpoint.

    The project-side bridge stays unchanged: it delivers by receiver wxid/roomid
    and records truthful acks. The WeFlow fork is responsible for implementing
    the actual 4.x Weixin send primitive behind these HTTP endpoints.
    """

    name = "weflow_http"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:5031",
        token_env: str = "WEFLOW_API_TOKEN",
        token: str = "",
        text_path: str = "/send/text",
        file_path: str = "/send/file",
        timeout_seconds: float = _DEFAULT_WEFLOW_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url or "").strip() or "http://127.0.0.1:5031"
        self.token_env = str(token_env or "").strip() or "WEFLOW_API_TOKEN"
        self.token = token
        self.text_path = str(text_path or "").strip() or "/send/text"
        self.file_path = str(file_path or "").strip() or "/send/file"
        self.timeout_seconds = max(1.0, float(timeout_seconds or _DEFAULT_WEFLOW_SEND_TIMEOUT_SECONDS))
        self._lock = threading.Lock()

    def health_check(self) -> bool:
        return bool(self.health_report().get("ok"))

    def health_report(self) -> dict[str, Any]:
        status = weflow_http_status(
            self.base_url,
            token_env=self.token_env,
            token=self._token(),
            timeout_seconds=min(self.timeout_seconds, 3.0),
        )
        return {"ok": bool(status.get("available")) and bool(status.get("token_present")), **status}

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        receiver = str(receiver or "").strip()
        if not receiver:
            return SendOutcome.failure("weflow_http_missing_receiver")
        if not str(text or "").strip():
            return SendOutcome.failure("weflow_http_empty_text")
        receiver_blocker = _synthetic_private_wechat_receiver_blocker(receiver, backend=self.name)
        if receiver_blocker:
            return SendOutcome.failure(receiver_blocker)
        payload = {
            "receiver": receiver,
            "talker": receiver,
            "talkerId": receiver,
            "sessionId": receiver,
            "text": str(text),
            "content": str(text),
            "type": "text",
            "timeoutSeconds": self.timeout_seconds,
        }
        return self._post(self.text_path, payload, "weflow_http_send_text")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        receiver = str(receiver or "").strip()
        resolved = str(path or "").strip()
        if not receiver:
            return SendOutcome.failure("weflow_http_missing_receiver")
        if not resolved:
            return SendOutcome.failure("weflow_http_empty_file_path")
        receiver_blocker = _synthetic_private_wechat_receiver_blocker(receiver, backend=self.name)
        if receiver_blocker:
            return SendOutcome.failure(receiver_blocker)
        payload = {
            "receiver": receiver,
            "talker": receiver,
            "talkerId": receiver,
            "sessionId": receiver,
            "path": resolved,
            "filePath": resolved,
            "caption": str(caption or ""),
            "type": "file",
            "timeoutSeconds": self.timeout_seconds,
        }
        return self._post(self.file_path, payload, "weflow_http_send_file")

    def close(self) -> None:
        return None

    def _token(self) -> str:
        return self.token or _token_from_env(self.token_env)

    def _post(self, endpoint_path: str, payload: dict[str, Any], reason: str) -> SendOutcome:
        token = self._token()
        if not token:
            return SendOutcome.failure(f"{reason}_error:weflow_token_missing")
        url = _weflow_endpoint_url(self.base_url, endpoint_path)
        with self._lock:
            try:
                response = _http_json(
                    url,
                    method="POST",
                    token=token,
                    payload=payload,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                return SendOutcome.failure(
                    f"{reason}_error:{type(exc).__name__}:{exc}",
                    payload=_backend_evidence(
                        backend=self.name,
                        operation=reason,
                        endpoint_path=endpoint_path,
                    ),
                )
        if _weflow_payload_success(response):
            return SendOutcome.success(
                reason,
                external_message_id=_weflow_external_message_id(response),
                payload=_backend_evidence(
                    backend=self.name,
                    operation=reason,
                    endpoint_path=endpoint_path,
                    response=response,
                ),
            )
        return SendOutcome.failure(
            f"{reason}_failed:{_weflow_failure_detail(response)}",
            payload=_backend_evidence(
                backend=self.name,
                operation=reason,
                endpoint_path=endpoint_path,
                response=response,
            ),
        )


class WeChatNativeHttpSendBackend:
    """Real delivery through the project-owned local PC WeChat native HTTP port.

    This backend never automates the foreground window. It posts wxid/roomid
    deliveries to the native 4.x bridge process injected into WeChat.
    """

    name = "wechat_native_http"

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:30001",
        text_path: str = "/SendTextMsg",
        image_path: str = "/SendImgMsg",
        file_path: str = "/send_file_msg",
        status_path: str = "/QueryDB/status",
        timeout_seconds: float = _DEFAULT_WECHAT_NATIVE_SEND_TIMEOUT_SECONDS,
        verify_base_url: str = "http://127.0.0.1:5031",
        verify_token_env: str = "WEFLOW_API_TOKEN",
        verify_timeout_seconds: float = _DEFAULT_WECHAT_NATIVE_VERIFY_TIMEOUT_SECONDS,
        file_verify_timeout_seconds: float = _DEFAULT_WECHAT_NATIVE_FILE_VERIFY_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = str(base_url or "").strip() or "http://127.0.0.1:30001"
        self.text_path = str(text_path or "").strip() or "/SendTextMsg"
        self.image_path = str(image_path or "").strip() or "/SendImgMsg"
        self.file_path = str(file_path or "").strip() or "/send_file_msg"
        self.status_path = str(status_path or "").strip() or "/QueryDB/status"
        self.timeout_seconds = max(1.0, float(timeout_seconds or _DEFAULT_WECHAT_NATIVE_SEND_TIMEOUT_SECONDS))
        self.verify_base_url = str(verify_base_url or "").strip() or "http://127.0.0.1:5031"
        self.verify_token_env = str(verify_token_env or "").strip() or "WEFLOW_API_TOKEN"
        self.verify_timeout_seconds = max(0.0, float(verify_timeout_seconds or 0.0))
        self.file_verify_timeout_seconds = max(0.0, float(file_verify_timeout_seconds or 0.0))
        self._lock = threading.Lock()

    def health_check(self) -> bool:
        return bool(self.health_report().get("ok"))

    def health_report(self) -> dict[str, Any]:
        status = wechat_native_http_status(
            self.base_url,
            text_path=self.text_path,
            image_path=self.image_path,
            file_path=self.file_path,
            status_path=self.status_path,
            timeout_seconds=min(self.timeout_seconds, 3.0),
        )
        return {"ok": bool(status.get("available")), **status}

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        receiver = str(receiver or "").strip()
        text = str(text or "")
        if not receiver:
            return SendOutcome.failure("wechat_native_http_missing_receiver")
        if not text.strip():
            return SendOutcome.failure("wechat_native_http_empty_text")
        receiver_blocker = _synthetic_private_wechat_receiver_blocker(receiver, backend=self.name)
        if receiver_blocker:
            return SendOutcome.failure(receiver_blocker)
        verification = self._text_delivery_probe(receiver, text)
        payload = {"wxidorgid": receiver, "msg": text}
        return self._post(
            self.text_path,
            payload,
            "wechat_native_http_send_text",
            verification=verification,
            verification_kind="text",
        )

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        receiver = str(receiver or "").strip()
        resolved = str(path or "").strip()
        if not receiver:
            return SendOutcome.failure("wechat_native_http_missing_receiver")
        if not resolved:
            return SendOutcome.failure("wechat_native_http_empty_file_path")
        receiver_blocker = _synthetic_private_wechat_receiver_blocker(receiver, backend=self.name)
        if receiver_blocker:
            return SendOutcome.failure(receiver_blocker)
        resolved_path = Path(resolved).expanduser()
        if resolved_path.exists():
            resolved = str(resolved_path.resolve())
        suffix = Path(resolved).suffix.lower()
        if suffix in _IMAGE_EXTENSIONS:
            blocker = wechat_native_file_send_blocker(
                resolved,
                image_path=self.image_path,
                file_path=self.file_path,
            )
            if blocker:
                return SendOutcome.failure(
                    blocker,
                    payload=_backend_evidence(
                        backend=self.name,
                        operation="wechat_native_http_send_image",
                        endpoint_path=self.image_path,
                    ),
                )
            payload = {"wxidorgid": receiver, "path": resolved}
            return self._post(self.image_path, payload, "wechat_native_http_send_image")
        blocker = wechat_native_file_send_blocker(
            resolved,
            image_path=self.image_path,
            file_path=self.file_path,
        )
        if blocker:
            return SendOutcome.failure(
                blocker,
                payload=_backend_evidence(
                    backend=self.name,
                    operation="wechat_native_http_send_file",
                    endpoint_path=self.file_path,
                ),
            )
        verification = self._file_delivery_probe(str(receiver), resolved)
        payload = {"wxid": receiver, "filepath": resolved, "stage": "send"}
        return self._post(
            self.file_path,
            payload,
            "wechat_native_http_send_file",
            verification=verification,
            verification_kind="file",
        )

    def close(self) -> None:
        return None

    def _endpoint_url(self, endpoint_path: str, default_path: str) -> str:
        return _local_endpoint_url(
            self.base_url,
            endpoint_path,
            default_base_url="http://127.0.0.1:30001",
            default_path=default_path,
        )

    def _text_delivery_probe(self, receiver: str, text: str) -> dict[str, Any]:
        probe: dict[str, Any] = {
            "receiver": receiver,
            "text": text,
            "send_started_at": time.time(),
            "token_present": bool(_token_from_env(self.verify_token_env)),
            "verify_base_url": self.verify_base_url,
            "verify_token_env": self.verify_token_env,
            "before": {},
        }
        if self.file_verify_timeout_seconds <= 0:
            probe["disabled"] = True
            return probe
        token = _token_from_env(self.verify_token_env)
        if not token:
            probe["disabled"] = True
            probe["reason"] = "weflow_token_missing"
            return probe
        try:
            before = self._read_weflow_messages(receiver, limit=10, timeout_seconds=min(2.0, self.verify_timeout_seconds))
            probe["before"] = _wechat_native_delivery_watermark(before)
        except Exception as exc:
            probe["before_error"] = f"{type(exc).__name__}:{exc}"
        return probe

    def _file_delivery_probe(self, receiver: str, path: str) -> dict[str, Any]:
        file_path = Path(str(path or ""))
        probe: dict[str, Any] = {
            "receiver": receiver,
            "path": str(path or ""),
            "file_name": file_path.name,
            "file_size": file_path.stat().st_size if file_path.exists() else 0,
            "send_started_at": time.time(),
            "token_present": bool(_token_from_env(self.verify_token_env)),
            "verify_base_url": self.verify_base_url,
            "verify_token_env": self.verify_token_env,
            "before": {},
        }
        if self.verify_timeout_seconds <= 0:
            probe["disabled"] = True
            return probe
        token = _token_from_env(self.verify_token_env)
        if not token:
            probe["disabled"] = True
            probe["reason"] = "weflow_token_missing"
            return probe
        try:
            before = self._read_weflow_messages(receiver, limit=20, timeout_seconds=min(2.0, self.file_verify_timeout_seconds))
            probe["before"] = _wechat_native_delivery_watermark(before)
        except Exception as exc:
            probe["before_error"] = f"{type(exc).__name__}:{exc}"
        return probe

    def verify_accepted_bridge_record(self, record: dict[str, Any], ack: dict[str, Any]) -> SendOutcome | None:
        """Re-check an accepted native record without sending it again.

        Native file sends may finish their async upload after the initial HTTP
        request returns. The bridge worker uses this hook to promote an
        accepted/unverified record to sent once WeFlow readback observes the
        message, while preserving the no-duplicate-send contract.
        """

        if str(record.get("kind", "text")) != "file":
            return None
        if str(ack.get("status", "")) != "accepted":
            return None
        payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
        if str(payload.get("backend") or "").strip().lower() != self.name:
            return None
        if not payload.get("accepted_unverified") and payload.get("delivery_verified") is not False:
            return None
        verification = self._verify_existing_file_record(record, ack)
        if not verification.get("verified"):
            return None
        evidence = {
            **payload,
            "delivery_verified": True,
            "accepted_unverified": False,
            "delivery_verification": verification,
            "late_delivery_verification": True,
        }
        return SendOutcome.success(
            "wechat_native_http_send_file_verified_late",
            external_message_id=str(verification.get("external_message_id") or ack.get("external_message_id") or ""),
            payload=evidence,
        )

    def _read_weflow_messages(self, receiver: str, *, limit: int, timeout_seconds: float) -> list[dict[str, Any]]:
        token = _token_from_env(self.verify_token_env)
        if not token:
            raise ValueError("weflow_token_missing")
        url = (
            _weflow_api_base_url(self.verify_base_url).rstrip("/")
            + f"/messages?talker={quote(receiver, safe='')}&format=json&limit={max(1, int(limit))}"
        )
        payload = _http_json(url, method="GET", token=token, timeout_seconds=timeout_seconds)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

    def _verify_text_delivery(self, probe: dict[str, Any]) -> dict[str, Any]:
        receiver = str(probe.get("receiver") or "")
        text = str(probe.get("text") or "")
        started_at = float(probe.get("send_started_at") or time.time())
        deadline = time.time() + self.verify_timeout_seconds
        result: dict[str, Any] = {
            "verified": False,
            "receiver": receiver,
            "method": "weflow_readback",
            "timeout_seconds": self.verify_timeout_seconds,
            "before": probe.get("before") if isinstance(probe.get("before"), dict) else {},
            "token_present": bool(probe.get("token_present")),
        }
        if probe.get("disabled"):
            result["reason"] = str(probe.get("reason") or "disabled")
            return result

        last_error = ""
        attempts = 0
        while time.time() <= deadline:
            attempts += 1
            try:
                messages = self._read_weflow_messages(receiver, limit=10, timeout_seconds=min(2.0, self.verify_timeout_seconds))
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                time.sleep(min(_DEFAULT_WECHAT_NATIVE_VERIFY_POLL_SECONDS, max(0.1, deadline - time.time())))
                continue
            match = _find_verified_wechat_text_message(
                messages,
                text=text,
                before=result["before"],
                send_started_at=started_at,
            )
            result["attempts"] = attempts
            result["last_count"] = len(messages)
            result["last_watermark"] = _wechat_native_delivery_watermark(messages)
            if match:
                result["verified"] = True
                result["reason"] = "matched_weflow_outgoing_text"
                result["external_message_id"] = _weflow_message_external_id(match)
                result["message"] = _compact_evidence(match)
                return result
            time.sleep(min(_DEFAULT_WECHAT_NATIVE_VERIFY_POLL_SECONDS, max(0.1, deadline - time.time())))

        result["attempts"] = attempts
        result["reason"] = "not_observed_before_timeout"
        if last_error:
            result["last_error"] = last_error
        return result

    def _verify_file_delivery(self, probe: dict[str, Any]) -> dict[str, Any]:
        receiver = str(probe.get("receiver") or "")
        file_name = str(probe.get("file_name") or "")
        file_size = _safe_int(probe.get("file_size"))
        started_at = float(probe.get("send_started_at") or time.time())
        timeout_seconds = self.file_verify_timeout_seconds
        deadline = time.time() + timeout_seconds
        result: dict[str, Any] = {
            "verified": False,
            "receiver": receiver,
            "file_name": file_name,
            "file_size": file_size,
            "method": "weflow_readback",
            "timeout_seconds": timeout_seconds,
            "before": probe.get("before") if isinstance(probe.get("before"), dict) else {},
            "token_present": bool(probe.get("token_present")),
        }
        if probe.get("disabled"):
            result["reason"] = str(probe.get("reason") or "disabled")
            return result

        last_error = ""
        attempts = 0
        while time.time() <= deadline:
            attempts += 1
            try:
                messages = self._read_weflow_messages(receiver, limit=50, timeout_seconds=min(2.0, timeout_seconds))
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                time.sleep(min(_DEFAULT_WECHAT_NATIVE_VERIFY_POLL_SECONDS, max(0.1, deadline - time.time())))
                continue
            match = _find_verified_wechat_file_message(
                messages,
                file_name=file_name,
                file_size=file_size,
                before=result["before"],
                send_started_at=started_at,
            )
            result["attempts"] = attempts
            result["last_count"] = len(messages)
            result["last_watermark"] = _wechat_native_delivery_watermark(messages)
            if match:
                result["verified"] = True
                result["reason"] = "matched_weflow_outgoing_file"
                result["external_message_id"] = _weflow_message_external_id(match)
                result["message"] = _compact_evidence(match)
                result["matched_file_name"] = _weflow_message_file_name(match)
                result["matched_file_size"] = _weflow_message_file_size(match)
                return result
            time.sleep(min(_DEFAULT_WECHAT_NATIVE_VERIFY_POLL_SECONDS, max(0.1, deadline - time.time())))
        result["attempts"] = attempts
        result["reason"] = "not_observed_before_timeout"
        if last_error:
            result["last_error"] = last_error
        return result

    def _verify_existing_file_record(self, record: dict[str, Any], ack: dict[str, Any]) -> dict[str, Any]:
        payload = ack.get("payload") if isinstance(ack.get("payload"), dict) else {}
        prior = payload.get("delivery_verification") if isinstance(payload.get("delivery_verification"), dict) else {}
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        receiver = str(record.get("receiver") or response.get("wxid") or prior.get("receiver") or "").strip()
        path = Path(str(record.get("path") or ""))
        file_name = str(record.get("name") or prior.get("file_name") or path.name).strip()
        file_size = _safe_int(prior.get("file_size"))
        if file_size <= 0:
            try:
                file_size = path.stat().st_size if path.exists() else 0
            except OSError:
                file_size = 0
        before = prior.get("before") if isinstance(prior.get("before"), dict) else {}
        started_at = _iso_epoch_seconds(str(ack.get("created_at") or "")) or _iso_epoch_seconds(
            str(record.get("created_at") or "")
        ) or time.time()
        result: dict[str, Any] = {
            "verified": False,
            "receiver": receiver,
            "file_name": file_name,
            "file_size": file_size,
            "method": "weflow_readback_late",
            "timeout_seconds": 0.0,
            "before": before,
            "token_present": bool(_token_from_env(self.verify_token_env)),
        }
        if not receiver:
            result["reason"] = "missing_receiver"
            return result
        if not file_name:
            result["reason"] = "missing_file_name"
            return result
        token = _token_from_env(self.verify_token_env)
        if not token:
            result["reason"] = "weflow_token_missing"
            return result
        try:
            messages = self._read_weflow_messages(
                receiver,
                limit=50,
                timeout_seconds=min(3.0, max(0.5, self.file_verify_timeout_seconds or 3.0)),
            )
        except Exception as exc:
            result["reason"] = "readback_error"
            result["last_error"] = f"{type(exc).__name__}:{exc}"
            return result
        result["attempts"] = 1
        result["last_count"] = len(messages)
        result["last_watermark"] = _wechat_native_delivery_watermark(messages)
        match = _find_verified_wechat_file_message(
            messages,
            file_name=file_name,
            file_size=file_size,
            before=before,
            send_started_at=started_at,
        )
        if not match:
            result["reason"] = "not_observed_on_late_recheck"
            return result
        result["verified"] = True
        result["reason"] = "matched_weflow_outgoing_file_late"
        result["external_message_id"] = _weflow_message_external_id(match)
        result["message"] = _compact_evidence(match)
        result["matched_file_name"] = _weflow_message_file_name(match)
        result["matched_file_size"] = _weflow_message_file_size(match)
        return result

    def _post(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
        reason: str,
        *,
        verification: dict[str, Any] | None = None,
        verification_kind: str = "text",
    ) -> SendOutcome:
        url = self._endpoint_url(endpoint_path, endpoint_path)
        with self._lock:
            try:
                response = _http_json(
                    url,
                    method="POST",
                    payload=payload,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                return SendOutcome.failure(
                    f"{reason}_error:{type(exc).__name__}:{exc}",
                    payload=_backend_evidence(
                        backend=self.name,
                        operation=reason,
                        endpoint_path=endpoint_path,
                    ),
                )
        if _wechat_native_payload_success(response):
            evidence = _backend_evidence(
                backend=self.name,
                operation=reason,
                endpoint_path=endpoint_path,
                response=response,
            )
            delivery_verified = _wechat_native_payload_delivery_verified(response)
            evidence["delivery_verified"] = delivery_verified
            external_message_id = _wechat_native_external_message_id(response)
            if not delivery_verified and verification:
                verified = (
                    self._verify_file_delivery(verification)
                    if str(verification_kind or "").lower() == "file"
                    else self._verify_text_delivery(verification)
                )
                evidence["delivery_verification"] = verified
                if verified.get("verified"):
                    delivery_verified = True
                    evidence["delivery_verified"] = True
                    external_message_id = str(verified.get("external_message_id") or external_message_id)
                    return SendOutcome.success(
                        f"{reason}_verified",
                        external_message_id=external_message_id,
                        payload=evidence,
                    )
            if not delivery_verified:
                evidence["accepted_unverified"] = True
                return SendOutcome.accepted_unverified(
                    f"{reason}_accepted_unverified",
                    external_message_id=external_message_id,
                    payload=evidence,
                )
            return SendOutcome.success(
                reason,
                external_message_id=external_message_id,
                payload=evidence,
            )
        return SendOutcome.failure(
            f"{reason}_failed:{_wechat_native_failure_detail(response)}",
            payload=_backend_evidence(
                backend=self.name,
                operation=reason,
                endpoint_path=endpoint_path,
                response=response,
            ),
        )


def build_send_backend(config: Any) -> SendBackend:
    """Construct the send backend named by ``config.send_backend`` (default dry_run)."""
    name = str(getattr(config, "send_backend", "") or "dry_run").strip().lower()
    if name in {"", "dry_run", "dryrun", "mock"}:
        return DryRunSendBackend()
    if name == "weflow_http":
        return WeFlowHttpSendBackend(
            base_url=str(getattr(config, "weflow_base_url", "") or "http://127.0.0.1:5031"),
            token_env=str(getattr(config, "weflow_token_env", "") or "WEFLOW_API_TOKEN"),
            text_path=str(getattr(config, "weflow_send_text_path", "") or "/send/text"),
            file_path=str(getattr(config, "weflow_send_file_path", "") or "/send/file"),
            timeout_seconds=float(getattr(config, "weflow_send_timeout_seconds", 0) or _DEFAULT_WEFLOW_SEND_TIMEOUT_SECONDS),
        )
    if name == "wechat_native_http":
        verify_timeout = getattr(config, "wechat_native_verify_timeout_seconds", _DEFAULT_WECHAT_NATIVE_VERIFY_TIMEOUT_SECONDS)
        file_verify_timeout = getattr(
            config,
            "wechat_native_file_verify_timeout_seconds",
            _DEFAULT_WECHAT_NATIVE_FILE_VERIFY_TIMEOUT_SECONDS,
        )
        return WeChatNativeHttpSendBackend(
            base_url=str(getattr(config, "wechat_native_base_url", "") or "http://127.0.0.1:30001"),
            text_path=str(getattr(config, "wechat_native_send_text_path", "") or "/SendTextMsg"),
            image_path=str(getattr(config, "wechat_native_send_image_path", "") or "/SendImgMsg"),
            file_path=str(getattr(config, "wechat_native_send_file_path", "") or "/send_file_msg"),
            status_path=str(getattr(config, "wechat_native_status_path", "") or "/QueryDB/status"),
            timeout_seconds=float(
                getattr(config, "wechat_native_timeout_seconds", 0) or _DEFAULT_WECHAT_NATIVE_SEND_TIMEOUT_SECONDS
            ),
            verify_base_url=str(getattr(config, "weflow_base_url", "") or "http://127.0.0.1:5031"),
            verify_token_env=str(getattr(config, "weflow_token_env", "") or "WEFLOW_API_TOKEN"),
            verify_timeout_seconds=float(verify_timeout if verify_timeout is not None else _DEFAULT_WECHAT_NATIVE_VERIFY_TIMEOUT_SECONDS),
            file_verify_timeout_seconds=float(
                file_verify_timeout
                if file_verify_timeout is not None
                else _DEFAULT_WECHAT_NATIVE_FILE_VERIFY_TIMEOUT_SECONDS
            ),
        )
    raise ValueError(f"unknown send_backend: {name!r}")
