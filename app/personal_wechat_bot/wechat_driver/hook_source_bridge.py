from __future__ import annotations

import json
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cmp_to_key
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request, urlopen

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.wechat_driver.jsonl_bus import append_jsonl


WEFLOW_LOCAL_BUILD_FLAVOR = "chatbot-win-local-fork"


@dataclass(frozen=True)
class HookSourceAppendResult:
    status: str
    hook_event_file: str
    appended_count: int
    errors: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class WeFlowPullResult:
    status: str
    base_url: str
    hook_event_file: str
    session_count: int
    scanned_count: int
    appended_count: int
    errors: tuple[dict[str, str], ...] = ()
    state_path: str = ""
    media_export_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class _WeFlowSessionPullResult:
    session_id: str
    scanned_count: int
    appended_count: int
    errors: tuple[dict[str, str], ...] = ()
    media_export_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeFlowSseResult:
    status: str
    base_url: str
    hook_event_file: str
    scanned_count: int
    appended_count: int
    skipped_count: int = 0
    last_event_id: str = ""
    errors: tuple[dict[str, str], ...] = ()
    state_path: str = ""


@dataclass(frozen=True)
class WcfCallbackServerConfig:
    hook_event_file: str | Path
    host: str = "127.0.0.1"
    port: int = 8791
    path: str = "/callback"


def weflow_health_status(
    base_url: str = "http://127.0.0.1:5031",
    *,
    token: str = "",
    timeout_seconds: float = 5.0,
    allow_non_local: bool = False,
    require_token: bool = False,
    require_fork: bool = False,
) -> dict[str, Any]:
    """Check WeFlow without creating local hook files."""

    api_base = _api_base_url(base_url)
    try:
        if not allow_non_local:
            _require_local_http_url(api_base)
        token = token.strip()
        if require_token and not token:
            raise ValueError("WEFLOW_HTTP_TOKEN is required for formal WeFlow pull")
        health = _weflow_json(api_base, "/health", token=token, timeout_seconds=timeout_seconds)
        fork_ok = _weflow_fork_marker_ok(health)
        if require_fork and not fork_ok:
            flavor = _first_text(health, "buildFlavor", "build_flavor", "fork.buildFlavor", "fork.build_flavor")
            raise ValueError(f"WeFlow health missing required local fork marker: {flavor or 'unknown'}")
        return {
            "status": "ok",
            "base_url": api_base,
            "local_only": _url_is_local_http(api_base),
            "token_required": bool(require_token),
            "token_present": bool(token),
            "required_build_flavor": WEFLOW_LOCAL_BUILD_FLAVOR if require_fork else "",
            "fork_ok": fork_ok,
            "health": health,
            "send_enabled": False,
        }
    except Exception as exc:
        return {
            "status": "error",
            "base_url": api_base,
            "type": type(exc).__name__,
            "message": str(exc),
            "token_required": bool(require_token),
            "token_present": bool(token.strip()),
            "required_build_flavor": WEFLOW_LOCAL_BUILD_FLAVOR if require_fork else "",
            "send_enabled": False,
        }


def require_weflow_ready(
    base_url: str = "http://127.0.0.1:5031",
    *,
    token: str,
    timeout_seconds: float = 5.0,
    allow_non_local: bool = False,
) -> dict[str, Any]:
    result = weflow_health_status(
        base_url,
        token=token,
        timeout_seconds=timeout_seconds,
        allow_non_local=allow_non_local,
        require_token=True,
        require_fork=True,
    )
    if result.get("status") != "ok":
        raise ValueError(str(result.get("message") or "WeFlow is not ready"))
    return result


class HookEventJsonlWriter:
    """Append normalized external WeChat source events to the hook JSONL bus."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        append_jsonl(self.path, _sorted_payload(payload))


class WeFlowHttpBridge:
    """Consume WeFlow local HTTP API/SSE and write project hook JSONL events."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5031",
        *,
        token: str = "",
        hook_event_file: str | Path,
        state_path: str | Path | None = None,
        timeout_seconds: float = 10.0,
        allow_non_local: bool = False,
    ):
        self.base_url = _api_base_url(base_url)
        self.token = token.strip()
        self.writer = HookEventJsonlWriter(hook_event_file)
        self.state_path = Path(state_path) if state_path else self.writer.path.parent / "weflow_bridge_state.json"
        self.timeout_seconds = timeout_seconds
        if not allow_non_local:
            _require_local_http_url(self.base_url)

    def pull_once(
        self,
        *,
        talkers: list[str] | None = None,
        session_limit: int = 100,
        message_limit: int = 100,
        max_pages: int = 1,
        max_messages: int = 0,
        since: int | None = None,
        lookback_seconds: int = 300,
        media: bool = True,
        context_only: bool | None = None,
        workers: int = 1,
    ) -> WeFlowPullResult:
        sessions: list[dict[str, Any]]
        errors: list[dict[str, str]] = []
        media_export_paths: set[str] = set()
        if talkers:
            sessions = [{"id": item, "name": item, "type": "group" if item.endswith("@chatroom") else "private"} for item in talkers]
        else:
            try:
                sessions = self.list_sessions(limit=session_limit)
            except Exception as exc:
                return WeFlowPullResult(
                    status="error",
                    base_url=self.base_url,
                    hook_event_file=str(self.writer.path),
                    session_count=0,
                    scanned_count=0,
                    appended_count=0,
                    errors=({"type": type(exc).__name__, "message": str(exc)},),
                    state_path=str(self.state_path),
                    media_export_paths=(),
                )

        worker_count = max(1, int(workers or 1))
        results: list[_WeFlowSessionPullResult] = []
        if worker_count <= 1 or len(sessions) <= 1:
            for session in sessions:
                results.append(
                    self._pull_session_once(
                        session,
                        since=since,
                        lookback_seconds=lookback_seconds,
                        message_limit=message_limit,
                        max_pages=max_pages,
                        max_messages=max_messages,
                        media=media,
                        context_only=context_only,
                    )
                )
        else:
            max_workers = min(worker_count, len(sessions))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self._pull_session_once,
                        session,
                        since=since,
                        lookback_seconds=lookback_seconds,
                        message_limit=message_limit,
                        max_pages=max_pages,
                        max_messages=max_messages,
                        media=media,
                        context_only=context_only,
                    ): session
                    for session in sessions
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        session_id = _session_id_from_meta(futures[future])
                        errors.append({"session": session_id, "type": type(exc).__name__, "message": str(exc)})

        scanned = sum(item.scanned_count for item in results)
        appended = sum(item.appended_count for item in results)
        for item in results:
            errors.extend(item.errors)
            media_export_paths.update(item.media_export_paths)
        return WeFlowPullResult(
            status="ok" if not errors else "partial_error",
            base_url=self.base_url,
            hook_event_file=str(self.writer.path),
            session_count=len(sessions),
            scanned_count=scanned,
            appended_count=appended,
            errors=tuple(errors[:20]),
            state_path=str(self.state_path),
            media_export_paths=tuple(sorted(media_export_paths)),
        )

    def _pull_session_once(
        self,
        session: dict[str, Any],
        *,
        since: int | None,
        lookback_seconds: int,
        message_limit: int,
        max_pages: int,
        max_messages: int,
        media: bool,
        context_only: bool | None,
    ) -> _WeFlowSessionPullResult:
        session_id = _session_id_from_meta(session)
        if not session_id:
            return _WeFlowSessionPullResult("", 0, 0)
        errors: list[dict[str, str]] = []
        appended_raw_ids: list[str] = []
        media_export_paths: set[str] = set()
        scanned = 0
        appended = 0
        now_since = int(time.time()) - max(0, lookback_seconds)
        history_context_only = context_only if context_only is not None else (since is not None and since <= 0)
        with _path_lock(_talker_lock_path(self.state_path, session_id), timeout_seconds=self.timeout_seconds):
            state_snapshot = _read_weflow_state_locked(self.state_path, timeout_seconds=self.timeout_seconds)
            seen = set(_string_list(state_snapshot.get("seen_raw_ids")))
            sessions_state = state_snapshot.get("sessions")
            if not isinstance(sessions_state, dict):
                sessions_state = {}
            session_state = sessions_state.get(session_id, {}) if isinstance(sessions_state.get(session_id), dict) else {}
            session_since = since
            if session_since is None:
                session_since = _safe_int(session_state.get("since"), now_since)
            start_time = max(0, session_since - 2)
            try:
                payloads = self.raw_message_pages(
                    session_id,
                    start=start_time,
                    limit=message_limit,
                    max_pages=max_pages,
                    max_messages=max_messages,
                    media=media,
                )
            except Exception as exc:
                errors.append({"session": session_id, "type": type(exc).__name__, "message": str(exc)})
                return _WeFlowSessionPullResult(session_id, 0, 0, tuple(errors))

            messages: list[dict[str, Any]] = []
            merged_api_meta: dict[str, Any] = {}
            for payload in payloads:
                page_messages = payload.get("messages")
                if isinstance(page_messages, list):
                    messages.extend([item for item in page_messages if isinstance(item, dict)])
                media_meta = payload.get("media") if isinstance(payload.get("media"), dict) else {}
                export_path = _first_text(media_meta, "exportPath", "export_path")
                if export_path:
                    media_export_paths.add(export_path)
                merged_api_meta = {
                    **merged_api_meta,
                    "api_talker": _first_text(payload, "talker") or session_id,
                    "api_count": payload.get("count"),
                    "api_has_more": payload.get("hasMore"),
                    "media": media_meta,
                }

            messages = _weflow_sort_messages(messages)
            meta = {**session, **merged_api_meta}
            max_timestamp = session_since
            for message in messages:
                scanned += 1
                normalized = normalize_weflow_message(
                    message,
                    session_id=session_id,
                    session_meta=meta,
                    context_only=bool(history_context_only),
                )
                raw_id = str(normalized.get("raw_id") or "").strip()
                if raw_id in seen:
                    continue
                self.writer.append(normalized)
                appended += 1
                if raw_id:
                    seen.add(raw_id)
                    appended_raw_ids.append(raw_id)
                max_timestamp = max(max_timestamp, _epoch_seconds(message.get("timestamp") or message.get("createTime"), max_timestamp))
            _merge_weflow_state(
                self.state_path,
                session_id=session_id,
                since=max_timestamp,
                seen_raw_ids=appended_raw_ids,
                timeout_seconds=self.timeout_seconds,
            )
        return _WeFlowSessionPullResult(
            session_id,
            scanned,
            appended,
            tuple(errors),
            tuple(sorted(media_export_paths)),
        )

    def listen_sse(
        self,
        *,
        max_events: int | None = None,
        max_seconds: float | None = None,
    ) -> WeFlowSseResult:
        state = _read_json_object(self.state_path)
        last_event_id = str(state.get("weflow_sse_last_event_id") or "").strip()
        seen = set(_string_list(state.get("weflow_sse_seen")))
        started = time.monotonic()
        scanned = 0
        appended = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        event_name = ""
        event_id = ""
        data_lines: list[str] = []

        def remember_state() -> None:
            state["weflow_sse_last_event_id"] = last_event_id
            state["weflow_sse_seen"] = sorted(seen)[-5000:]
            _write_json_object(self.state_path, state)

        def flush_event() -> None:
            nonlocal scanned, appended, skipped, last_event_id
            if not data_lines:
                return
            scanned += 1
            if event_id:
                last_event_id = event_id
            try:
                payload = json.loads("\n".join(data_lines))
                if not isinstance(payload, dict):
                    skipped += 1
                    remember_state()
                    return
                normalized_event = str(payload.get("event") or event_name or "").strip()
                if not _weflow_push_has_session(payload) or normalized_event == "ready":
                    skipped += 1
                    remember_state()
                    return
                dedupe_key = _weflow_push_dedupe_key(payload, normalized_event, event_id)
                if dedupe_key in seen:
                    skipped += 1
                else:
                    normalized = normalize_weflow_push_event({**payload, "event": normalized_event or "message.new"})
                    if event_id:
                        normalized["event_id"] = event_id
                    self.writer.append(normalized)
                    seen.add(dedupe_key)
                    appended += 1
                remember_state()
            except Exception as exc:
                skipped += 1
                errors.append({"type": type(exc).__name__, "message": str(exc)})
                remember_state()

        try:
            params = {"lastEventId": last_event_id} if last_event_id else None
            headers = {"Last-Event-ID": last_event_id} if last_event_id else None
            with self._open("/push/messages", params=params, method="GET", stream=True, headers=headers) as response:
                while True:
                    if max_seconds is not None and time.monotonic() - started >= max_seconds:
                        break
                    raw_line = response.readline()
                    if raw_line == b"":
                        flush_event()
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        flush_event()
                        event_name = ""
                        event_id = ""
                        data_lines = []
                        if max_events is not None and appended >= max_events:
                            break
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("id:"):
                        event_id = line[3:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
        except Exception as exc:
            errors.append({"type": type(exc).__name__, "message": str(exc)})
            return WeFlowSseResult(
                status="error" if scanned == 0 else "partial_error",
                base_url=self.base_url,
                hook_event_file=str(self.writer.path),
                scanned_count=scanned,
                appended_count=appended,
                skipped_count=skipped,
                last_event_id=last_event_id,
                errors=tuple(errors[:20]),
                state_path=str(self.state_path),
            )
        return WeFlowSseResult(
            status="ok" if not errors else "partial_error",
            base_url=self.base_url,
            hook_event_file=str(self.writer.path),
            scanned_count=scanned,
            appended_count=appended,
            skipped_count=skipped,
            last_event_id=last_event_id,
            errors=tuple(errors[:20]),
            state_path=str(self.state_path),
        )

    def health(self) -> dict[str, Any]:
        return self._json("/health")

    def list_sessions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._json("/sessions", params={"format": "chatlab", "limit": limit})
        sessions = payload.get("sessions")
        if isinstance(sessions, list):
            return [item for item in sessions if isinstance(item, dict)]
        return []

    def session_messages(self, session_id: str, *, since: int, limit: int, media: bool) -> dict[str, Any]:
        params = {"since": since, "limit": limit, "media": "1" if media else "0"}
        return self._json(f"/sessions/{quote(session_id, safe='')}/messages", params=params)

    def raw_messages(
        self,
        talker: str,
        *,
        start: int = 0,
        end: int = 0,
        offset: int = 0,
        limit: int = 100,
        media: bool = True,
    ) -> dict[str, Any]:
        params = {
            "talker": talker,
            "format": "json",
            "start": start or None,
            "end": end or None,
            "offset": max(0, offset),
            "limit": max(1, limit),
            "media": "1" if media else "0",
            "image": "1" if media else "0",
            "voice": "1" if media else "0",
            "video": "1" if media else "0",
            "emoji": "1" if media else "0",
            "file": "1" if media else "0",
        }
        payload = self._json("/messages", params=params)
        if payload.get("success") is False:
            raise ValueError(str(payload.get("error") or "WeFlow raw messages request failed"))
        return payload

    def raw_message_pages(
        self,
        talker: str,
        *,
        start: int = 0,
        end: int = 0,
        limit: int = 100,
        max_pages: int = 1,
        max_messages: int = 0,
        media: bool = True,
    ) -> list[dict[str, Any]]:
        page_limit = max(1, min(10000, int(limit or 100)))
        pages: list[dict[str, Any]] = []
        offset = 0
        page_count = 0
        total_messages = 0
        unlimited_pages = max_pages <= 0
        while unlimited_pages or page_count < max_pages:
            remaining = max_messages - total_messages if max_messages > 0 else page_limit
            if max_messages > 0 and remaining <= 0:
                break
            payload = self.raw_messages(
                talker,
                start=start,
                end=end,
                offset=offset,
                limit=min(page_limit, remaining) if max_messages > 0 else page_limit,
                media=media,
            )
            pages.append(payload)
            page_count += 1
            messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
            count = len(messages)
            total_messages += count
            if count <= 0 or payload.get("hasMore") is not True:
                break
            offset += count
        return pages

    def _json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._open(path, params=params, method="GET") as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise ValueError("WeFlow API response must be a JSON object")
        return payload

    def _open(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        stream: bool = False,
        headers: dict[str, str] | None = None,
    ):
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            url = f"{url}?{urlencode({key: value for key, value in params.items() if value is not None})}"
        request_headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if headers:
            request_headers.update(headers)
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, headers=request_headers, method=method)
        return urlopen(request, timeout=self.timeout_seconds)


def append_hook_source_event(
    hook_event_file: str | Path,
    payload: dict[str, Any],
    *,
    source: str,
) -> HookSourceAppendResult:
    writer = HookEventJsonlWriter(hook_event_file)
    try:
        if source == "weflow-push":
            writer.append(normalize_weflow_push_event(payload))
        elif source == "weflow-message":
            session_id = str(payload.get("sessionId") or payload.get("talker") or payload.get("session_id") or "").strip()
            writer.append(normalize_weflow_message(payload, session_id=session_id, session_meta=payload))
        elif source == "wcf-callback":
            writer.append(normalize_wcf_callback(payload))
        else:
            writer.append(payload)
    except Exception as exc:
        return HookSourceAppendResult(
            status="error",
            hook_event_file=str(writer.path),
            appended_count=0,
            errors=({"type": type(exc).__name__, "message": str(exc)},),
        )
    return HookSourceAppendResult(status="ok", hook_event_file=str(writer.path), appended_count=1)


def normalize_weflow_push_event(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("WeFlow push payload must be a JSON object")
    session_id = _first_text(payload, "sessionId", "session_id", "talker", "talkerId")
    if not session_id:
        raise ValueError("WeFlow push payload missing sessionId")
    event_name = _first_text(payload, "event", "type") or "message.new"
    rawid = _first_text(payload, "rawid", "rawId", "platformMessageId", "serverId")
    timestamp = _epoch_seconds(payload.get("timestamp"), int(time.time()))
    is_recall = event_name in {"message.revoke", "message.recall", "revoke", "recall"}
    recall_message_id = _weflow_recall_message_id(payload, fallback=rawid) if is_recall else ""
    raw_id_suffix = rawid or recall_message_id or str(timestamp)
    sender_name = _first_text(payload, "sourceName", "senderName", "accountName", "sender") or ("system" if is_recall else "unknown")
    chat_title = _first_text(payload, "groupName", "sessionName", "talkerName", "displayName") or session_id
    result: dict[str, Any] = {
        "source": "weflow_push",
        "event_type": "recall" if is_recall else "message",
        "talker": session_id,
        "talker_name": chat_title,
        "sender_name": sender_name,
        "sender_id": _first_text(payload, "sender", "senderUsername", "senderId"),
        "msgid": rawid,
        "server_id": rawid,
        "raw_id": f"weflow:{'recall' if is_recall else 'message'}:{session_id}:{raw_id_suffix}",
        "text": _first_text(payload, "content", "text"),
        "timestamp": timestamp,
        "is_group": _session_type_is_group(payload, session_id),
        "raw": payload,
    }
    avatar = _first_text(payload, "avatarUrl", "avatar")
    if avatar:
        result["avatar_url"] = avatar
    if is_recall:
        result["recall"] = {
            "target_raw_id": _weflow_message_raw_id(session_id, recall_message_id),
            "target_message_id": recall_message_id,
            "reason": _first_text(payload, "content") or "wechat_recall",
        }
    return {key: value for key, value in result.items() if value not in ("", None)}


def normalize_weflow_message(
    message: dict[str, Any],
    *,
    session_id: str,
    session_meta: dict[str, Any] | None = None,
    context_only: bool = False,
) -> dict[str, Any]:
    if not isinstance(message, dict):
        raise ValueError("WeFlow message payload must be a JSON object")
    meta = session_meta if isinstance(session_meta, dict) else {}
    talker = session_id or _first_text(message, "talker", "sessionId", "chatroomId")
    if not talker:
        raise ValueError("WeFlow message missing session id")
    server_id = _server_id_text(_first_text(message, "platformMessageId", "serverId", "server_id"))
    local_id = _first_text(message, "localId", "local_id")
    message_key = _first_text(message, "messageKey", "message_key")
    raw_id = f"weflow:message:{talker}:{server_id or message_key or local_id or _safe_int(message.get('timestamp'), 0)}"
    text = _first_text(message, "parsedContent", "content", "rawContent", "text")
    sender_id = _first_text(message, "sender", "senderUsername", "sender_id")
    sender_name = _first_text(message, "accountName", "groupNickname", "senderName") or sender_id or "unknown"
    chat_title = _first_text(meta, "name", "displayName", "groupName", "username", "api_talker") or talker
    timestamp = _epoch_seconds(message.get("timestamp") or message.get("createTime"), int(time.time()))
    attachments = _weflow_attachments(message)
    media_meta = meta.get("media") if isinstance(meta.get("media"), dict) else {}
    media_export_path = _first_text(media_meta, "exportPath", "export_path")
    local_type = _first_text(message, "localType", "local_type")
    media_type = _first_text(message, "mediaType")
    message_type = media_type or _weflow_message_type(local_type, message)
    result: dict[str, Any] = {
        "source": "weflow_http_raw",
        "event_type": "message",
        "talker": talker,
        "talker_name": chat_title,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "raw_id": raw_id,
        "msgid": server_id or local_id,
        "server_id": server_id,
        "local_id": local_id,
        "message_key": message_key,
        "sort_key": _first_text(message, "sortSeq", "sort_seq"),
        "create_time": _first_text(message, "createTime", "create_time"),
        "local_type": local_type,
        "message_type": message_type,
        "text": text,
        "timestamp": timestamp,
        "is_self": _truthy(message.get("isSend")),
        "is_group": _session_type_is_group(meta, talker),
        "attachments": attachments,
        "media_export_path": media_export_path,
        "file_name": _first_text(message, "fileName", "file_name"),
        "file_size": _safe_int(message.get("fileSize") or message.get("file_size"), 0),
        "file_ext": _first_text(message, "fileExt", "file_ext"),
        "file_md5": _first_text(message, "fileMd5", "file_md5"),
        "app_msg_kind": _first_text(message, "appMsgKind", "app_msg_kind"),
        "app_msg_type": _first_text(message, "xmlType", "xml_type"),
        "context_only": bool(context_only),
        "capture_phase": "history_backfill" if context_only else "incremental",
        "raw": {"message": message, "session": meta},
    }
    voice_payload = _weflow_voice(message, attachments)
    if voice_payload:
        result["voice"] = voice_payload
    quote_payload = _weflow_quote(message)
    if quote_payload:
        result["quote"] = quote_payload
    return {key: value for key, value in result.items() if value not in ("", None, [])}


def normalize_wcf_callback(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("WCF callback payload must be a JSON object")
    is_group = _truthy(payload.get("is_group") or payload.get("isGroup"))
    roomid = _first_text(payload, "roomid", "roomId")
    sender = _first_text(payload, "sender", "senderWxid", "sender_id")
    talker = roomid if is_group and roomid else sender
    if not talker:
        raise ValueError("WCF callback missing sender/roomid")
    msg_id = _first_text(payload, "id", "msgid", "msgId")
    message_type = _first_text(payload, "type", "msgType", "messageType")
    result: dict[str, Any] = {
        "source": "wechatferry_callback",
        "event_type": "message",
        "talker": talker,
        "talker_name": roomid or sender or talker,
        "sender_id": sender,
        "sender_name": sender or "unknown",
        "msgid": msg_id,
        "raw_id": f"wcf:message:{talker}:{msg_id or int(time.time() * 1000)}",
        "message_type": message_type,
        "text": _first_text(payload, "content", "text"),
        "is_self": _truthy(payload.get("is_self") or payload.get("isSelf")),
        "is_group": is_group,
        "observed_at": utc_now_iso(),
        "raw": payload,
    }
    attachments = []
    for key, kind in (("thumb", "image"), ("extra", _wcf_media_kind(message_type))):
        value = _first_text(payload, key)
        if value and _looks_like_path(value):
            attachments.append({"path": value, "kind": kind or "file"})
    if attachments:
        result["attachments"] = attachments
    return {key: value for key, value in result.items() if value not in ("", None, [])}


def run_wcf_callback_server(config: WcfCallbackServerConfig) -> None:
    _require_local_host(config.host)
    writer = HookEventJsonlWriter(config.hook_event_file)
    path = config.path if config.path.startswith("/") else f"/{config.path}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/health":
                self._send_json(404, {"status": "not_found"})
                return
            self._send_json(200, {"status": "ok", "hook_event_file": str(writer.path), "send_enabled": False})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != path:
                self._send_json(404, {"status": "not_found"})
                return
            length = _safe_int(self.headers.get("Content-Length"), 0)
            try:
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(raw or "{}")
                writer.append(normalize_wcf_callback(payload))
            except Exception as exc:
                self._send_json(400, {"status": "error", "type": type(exc).__name__, "message": str(exc)})
                return
            self._send_json(200, {"status": "ok", "appended_count": 1, "send_enabled": False})

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _weflow_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    path = _first_text(message, "mediaLocalPath", "mediaPath", "filePath", "localPath")
    kind = _attachment_kind(_first_text(message, "mediaType") or _weflow_message_type(_first_text(message, "localType"), message))
    media_name = _first_text(message, "mediaFileName", "fileName", "file_name")
    if path:
        attachments.append(
            {
                key: value
                for key, value in {
                    "path": path,
                    "name": media_name or Path(path).name,
                    "kind": kind or "file",
                    "url": _first_text(message, "mediaUrl", "media_url"),
                    "size": _safe_int(message.get("fileSize") or message.get("file_size"), 0),
                    "md5": _first_text(message, "fileMd5", "file_md5"),
                    "media_type": _first_text(message, "mediaType"),
                }.items()
                if value not in ("", None, 0)
            }
        )
    elif _weflow_message_type(_first_text(message, "localType"), message) == "file":
        file_name = _first_text(message, "fileName", "file_name", "mediaFileName")
        if file_name:
            attachments.append(
                {
                    key: value
                    for key, value in {
                        "path": "",
                        "name": file_name,
                        "kind": "file",
                        "size": _safe_int(message.get("fileSize") or message.get("file_size"), 0),
                        "md5": _first_text(message, "fileMd5", "file_md5"),
                        "file_ext": _first_text(message, "fileExt", "file_ext"),
                        "status": "metadata_only",
                    }.items()
                    if value not in ("", None, 0)
                }
            )
    return attachments


def _weflow_voice(message: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    local_type = _first_text(message, "localType", "local_type")
    media_type = _first_text(message, "mediaType")
    looks_like_voice = local_type == "34" or media_type == "voice" or media_type == "audio"
    if not looks_like_voice:
        return {}
    audio = next((item for item in attachments if str(item.get("kind") or "") in {"voice", "audio"}), {})
    text = _first_text(message, "voiceText", "voice_text", "transcript")
    audio_path = str(audio.get("path") or _first_text(message, "mediaLocalPath", "voiceAudioPath", "voice_audio_path")).strip()
    audio_name = str(audio.get("name") or _first_text(message, "mediaFileName", "voiceAudioName", "voice_audio_name")).strip()
    duration = _first_text(message, "voiceDuration", "voice_duration", "voiceDurationSeconds", "voice_duration_seconds")
    return {
        key: value
        for key, value in {
            "status": "transcribed" if text else "pending",
            "source": "weflow_http_raw",
            "text": text,
            "duration": duration,
            "audio_path": audio_path,
            "audio_name": audio_name,
        }.items()
        if value
    }


def _weflow_quote(message: dict[str, Any]) -> dict[str, str]:
    quote_payload = message.get("quote")
    if not isinstance(quote_payload, dict):
        quote_payload = {}
    result = {
        "message_id": _first_text(message, "replyToMessageId") or _first_text(quote_payload, "platformMessageId"),
        "sender_name": _first_text(quote_payload, "accountName", "sender"),
        "text": _first_text(quote_payload, "content", "text"),
        "type": _first_text(quote_payload, "type"),
        "source": "weflow_http_raw",
    }
    return {key: value for key, value in result.items() if value}


def _weflow_message_type(local_type: str, message: dict[str, Any]) -> str:
    explicit = _first_text(message, "mediaType", "type")
    if explicit and explicit not in {"0", "1"}:
        return _attachment_kind(explicit) or explicit
    if local_type == "1":
        return "text"
    if local_type == "3":
        return "image"
    if local_type == "34":
        return "audio"
    if local_type == "43":
        return "video"
    if local_type == "47":
        return "emoji"
    if local_type == "49":
        app_kind = _first_text(message, "appMsgKind", "app_msg_kind")
        app_type = _first_text(message, "xmlType", "xml_type")
        if app_kind == "file" or app_type == "6" or _first_text(message, "fileName", "file_name"):
            return "file"
        if app_kind:
            return app_kind
        return "app"
    return local_type or "message"


def _attachment_kind(value: str) -> str:
    text = str(value or "").strip().lower()
    return {"voice": "audio", "audio": "audio", "image": "image", "video": "video", "emoji": "emoji", "file": "file"}.get(text, "")


def _server_id_text(value: str) -> str:
    text = str(value or "").strip()
    if text in {"0", "0.0"}:
        return ""
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _weflow_sort_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(messages) < 2:
        return messages
    indexed = list(enumerate(messages))
    indexed.sort(key=cmp_to_key(_compare_indexed_weflow_messages))
    return [message for _, message in indexed]


def _compare_indexed_weflow_messages(left: tuple[int, dict[str, Any]], right: tuple[int, dict[str, Any]]) -> int:
    left_index, left_message = left
    right_index, right_message = right
    diff = _compare_weflow_messages(left_message, right_message)
    if diff:
        return diff
    return left_index - right_index


def _compare_weflow_messages(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_sort_seq = max(0, _safe_int(left.get("sortSeq") or left.get("sort_seq"), 0))
    right_sort_seq = max(0, _safe_int(right.get("sortSeq") or right.get("sort_seq"), 0))
    left_create_time = max(0, _epoch_seconds(left.get("createTime") or left.get("create_time"), 0))
    right_create_time = max(0, _epoch_seconds(right.get("createTime") or right.get("create_time"), 0))
    left_local_id = max(0, _safe_int(left.get("localId") or left.get("local_id"), 0))
    right_local_id = max(0, _safe_int(right.get("localId") or right.get("local_id"), 0))
    left_server_id = max(0, _safe_int(left.get("serverId") or left.get("server_id"), 0))
    right_server_id = max(0, _safe_int(right.get("serverId") or right.get("server_id"), 0))
    if left_sort_seq > 0 and right_sort_seq > 0 and left_sort_seq != right_sort_seq:
        return left_sort_seq - right_sort_seq
    if left_create_time != right_create_time:
        return left_create_time - right_create_time
    if left_sort_seq != right_sort_seq:
        return left_sort_seq - right_sort_seq
    if left_local_id != right_local_id:
        return left_local_id - right_local_id
    if left_server_id != right_server_id:
        return left_server_id - right_server_id
    left_key = _first_text(left, "messageKey", "message_key")
    right_key = _first_text(right, "messageKey", "message_key")
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def _session_type_is_group(payload: dict[str, Any], session_id: str) -> bool:
    session_type = _first_text(payload, "sessionType", "type", "meta.type")
    if session_type in {"group", "2"}:
        return True
    if session_type in {"private", "friend", "other", "1"}:
        return False
    return session_id.endswith("@chatroom")


def _session_id_from_meta(session: dict[str, Any]) -> str:
    return str(session.get("id") or session.get("username") or session.get("talker") or session.get("sessionId") or "").strip()


def _weflow_push_has_session(payload: dict[str, Any]) -> bool:
    return bool(_first_text(payload, "sessionId", "session_id", "talker", "talkerId"))


def _weflow_push_dedupe_key(payload: dict[str, Any], event_name: str, event_id: str) -> str:
    session_id = _first_text(payload, "sessionId", "session_id", "talker", "talkerId")
    rawid = _first_text(payload, "rawid", "rawId", "platformMessageId", "serverId")
    target = _weflow_recall_message_id(payload, fallback="") if event_name in {"message.revoke", "message.recall", "revoke", "recall"} else ""
    if session_id or rawid or target or event_id:
        return "|".join([event_name or "message.new", session_id, rawid, target, event_id])
    seed = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _weflow_recall_message_id(payload: dict[str, Any], *, fallback: str = "") -> str:
    explicit = _first_text(
        payload,
        "target_message_id",
        "targetMessageId",
        "recalled_message_id",
        "recalledMessageId",
        "target_raw_id",
        "targetRawId",
        "recalled_raw_id",
        "recalledRawId",
        "old_msg_id",
        "oldMsgId",
    )
    if explicit:
        return _message_id_from_raw_id(explicit)
    text = _first_text(payload, "content", "text")
    for tag in ("newmsgid", "msgid", "rawid", "rawId", "serverId", "messageId"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(1).strip()
            if value.startswith("<![CDATA[") and value.endswith("]]>"):
                value = value[9:-3].strip()
            if value:
                return _message_id_from_raw_id(value)
    match = re.search(r"(?:rawid|rawId|msgid|newmsgid|messageId|serverId)[^A-Za-z0-9_-]+([A-Za-z0-9_-]{4,})", text)
    if match:
        return _message_id_from_raw_id(match.group(1))
    return _message_id_from_raw_id(fallback)


def _message_id_from_raw_id(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("weflow:message:"):
        return text.rsplit(":", 1)[-1]
    return text


def _weflow_message_raw_id(session_id: str, message_id: str) -> str:
    if message_id.startswith(("weflow:", "hook:", "wcf:")):
        return message_id
    return f"weflow:message:{session_id}:{message_id}" if message_id else ""


def _wcf_media_kind(message_type: str) -> str:
    return {"3": "image", "34": "audio", "43": "video", "49": "file"}.get(str(message_type), "file")


def _sorted_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted_payload(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_payload(item) for item in value]
    return value


def _api_base_url(base_url: str) -> str:
    text = base_url.strip().rstrip("/")
    if not text:
        text = "http://127.0.0.1:5031"
    return text if text.endswith("/api/v1") else f"{text}/api/v1"


def _weflow_json(api_base_url: str, path: str, *, token: str = "", timeout_seconds: float = 5.0) -> dict[str, Any]:
    url = api_base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("WeFlow health response must be a JSON object")
    return payload


def _url_is_local_http(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _weflow_fork_marker_ok(health: dict[str, Any]) -> bool:
    flavor = _first_text(health, "buildFlavor", "build_flavor", "fork.buildFlavor", "fork.build_flavor")
    return flavor == WEFLOW_LOCAL_BUILD_FLAVOR


def _require_local_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http":
        raise ValueError("message source bridge only allows http by default")
    _require_local_host(parsed.hostname or "")


def _require_local_host(host: str) -> None:
    if host.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("message source bridge must bind/connect to localhost unless explicitly overridden")


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if "." in key:
            current: Any = payload
            for part in key.split("."):
                current = current.get(part) if isinstance(current, dict) else None
            value = current
        else:
            value = payload.get(key)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _epoch_seconds(value: Any, default: int = 0) -> int:
    number = _safe_int(value, default)
    if number > 10_000_000_000:
        return number // 1000
    return number


def _looks_like_path(value: str) -> bool:
    text = value.strip()
    return bool(text and (":\\" in text or ":/" in text or "/" in text or "\\" in text))


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _read_weflow_state_locked(path: Path, *, timeout_seconds: float) -> dict[str, Any]:
    with _path_lock(path.with_suffix(path.suffix + ".lock"), timeout_seconds=timeout_seconds):
        return _read_json_object(path)


def _merge_weflow_state(
    path: Path,
    *,
    session_id: str,
    since: int,
    seen_raw_ids: list[str],
    timeout_seconds: float,
) -> None:
    with _path_lock(path.with_suffix(path.suffix + ".lock"), timeout_seconds=timeout_seconds):
        state = _read_json_object(path)
        sessions = state.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
            state["sessions"] = sessions
        previous = sessions.get(session_id) if isinstance(sessions.get(session_id), dict) else {}
        previous_since = _safe_int(previous.get("since"), 0) if isinstance(previous, dict) else 0
        sessions[session_id] = {"since": max(previous_since, since)}
        seen = set(_string_list(state.get("seen_raw_ids")))
        seen.update(item for item in seen_raw_ids if str(item).strip())
        state["seen_raw_ids"] = sorted(seen)[-50000:]
        _write_json_object(path, state)


def _talker_lock_path(state_path: Path, talker: str) -> Path:
    digest = hashlib.sha256(str(talker or "unknown").encode("utf-8")).hexdigest()[:16]
    return state_path.with_suffix(state_path.suffix + f".talker-{digest}.lock")


@contextmanager
def _path_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _stale_lock(path):
                try:
                    path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for WeFlow state lock: {path}")
            time.sleep(0.025)
    try:
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _stale_lock(path: Path, *, max_age_seconds: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > max_age_seconds
    except OSError:
        return False
