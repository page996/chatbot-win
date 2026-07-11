from __future__ import annotations

import json
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request

from app.personal_wechat_bot.domain.models import utc_now_iso
from app.personal_wechat_bot.runtime.history_fence import (
    history_writer_fence_if_owned,
    history_writer_leases_if_owned,
)
from app.personal_wechat_bot.runtime.process_lock import short_process_lock
from app.personal_wechat_bot.tools.web.http_safety import (
    guarded_local_urlopen,
    guarded_same_authority_urlopen,
    read_response_with_deadline,
    validate_http_url,
    validate_local_http_url,
)
from app.personal_wechat_bot.wechat_driver.jsonl_bus import append_jsonl_once
from app.personal_wechat_bot.wechat_driver.system_accounts import is_system_account


WEFLOW_LOCAL_BUILD_FLAVOR = "chatbot-win-local-fork"
WEFLOW_JSON_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
ProgressCallback = Callable[[dict[str, Any]], None]
WEFLOW_FRIEND_TRUE_KEYS = (
    "is_friend",
    "isFriend",
    "friend",
    "is_contact",
    "isContact",
    "contact",
    "in_contacts",
    "inContacts",
    "in_address_book",
    "inAddressBook",
)
WEFLOW_NON_FRIEND_TRUE_KEYS = (
    "is_stranger",
    "isStranger",
    "stranger",
    "non_friend",
    "nonFriend",
    "is_non_friend",
    "isNonFriend",
    "temporary",
    "is_temporary",
    "isTemporary",
)
WEFLOW_RELATION_KEYS = (
    "relationship",
    "relation",
    "contact_status",
    "contactStatus",
    "friend_status",
    "friendStatus",
    "verifyFlag",
    "verify_flag",
)
WEFLOW_NON_FRIEND_VALUES = frozenset(
    {
        "unknown",
        "stranger",
        "non_friend",
        "nonfriend",
        "not_friend",
        "temporary",
        "temp",
        "stranger_from_group",
        "group_only",
    }
)
WEFLOW_FRIEND_VALUES = frozenset({"friend", "contact", "contacts", "accepted", "verified", "known"})
WEFLOW_PLACEHOLDER_NAMES = frozenset(
    {
        "unknown",
        "unknown contact",
        "unknown user",
        "unknown friend",
        "wechat user",
        "weixin user",
        "未知",
        "未知联系人",
        "未知用户",
        "微信用户",
        "微信联系人",
        "system",
        "none",
        "null",
    }
)
WEFLOW_NON_FRIEND_TEXT_KEYS = (
    "banner",
    "notice",
    "tip",
    "hint",
    "subtitle",
    "description",
    "relation_text",
    "relationText",
    "relationship_text",
    "relationshipText",
    "friend_tip",
    "friendTip",
    "verify_content",
    "verifyContent",
    "status_text",
    "statusText",
)
WEFLOW_NON_FRIEND_TEXT_MARKERS = (
    "对方还不是你的朋友",
    "还不是你的朋友",
    "不是你的朋友",
    "not your friend",
    "not a friend",
)


class WeFlowPullCancelled(RuntimeError):
    pass


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
        if allow_non_local:
            validate_http_url(api_base)
        else:
            _require_local_http_url(api_base)
        token = token.strip()
        if require_token and not token:
            raise ValueError("WEFLOW_HTTP_TOKEN is required for formal WeFlow pull")
        health = _weflow_json(
            api_base,
            "/health",
            token=token,
            timeout_seconds=timeout_seconds,
            allow_non_local=allow_non_local,
        )
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
        with history_writer_fence_if_owned(
            self.path.parent,
            label="hook_event_writer_init",
        ):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)

    def append(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        with history_writer_fence_if_owned(
            self.path.parent,
            label="hook_event_writer_append",
        ):
            return append_jsonl_once(
                self.path,
                _sorted_payload(payload),
                key_field="raw_id",
                index_path=self.path.with_suffix(self.path.suffix + ".raw_ids.json"),
            )


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
        self.allow_non_local = bool(allow_non_local)
        if self.allow_non_local:
            validate_http_url(self.base_url)
        else:
            _require_local_http_url(self.base_url)

    def _history_paths(self) -> tuple[Path, ...]:
        return (self.writer.path.parent, self.state_path.parent)

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
        ignore_seen: bool = False,
        workers: int = 1,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> WeFlowPullResult:
        with history_writer_leases_if_owned(
            self._history_paths(),
            label="weflow_pull_once",
        ):
            return self._pull_once_leased(
                talkers=talkers,
                session_limit=session_limit,
                message_limit=message_limit,
                max_pages=max_pages,
                max_messages=max_messages,
                since=since,
                lookback_seconds=lookback_seconds,
                media=media,
                context_only=context_only,
                ignore_seen=ignore_seen,
                workers=workers,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )

    def _pull_once_leased(
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
        ignore_seen: bool = False,
        workers: int = 1,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> WeFlowPullResult:
        sessions: list[dict[str, Any]]
        errors: list[dict[str, str]] = []
        media_export_paths: set[str] = set()
        _raise_if_cancelled(cancel_event)
        explicit_talkers = bool(talkers)
        if explicit_talkers:
            sessions = _weflow_sessions_from_talkers(talkers or [])
            try:
                discovered_sessions = self.list_sessions(limit=min(5000, max(session_limit, len(sessions), 1000)))
            except Exception:
                # Older or test WeFlow bridges may expose raw message pulls
                # without a sessions endpoint. Keep the explicit pull working;
                # unidentified private channels are still blocked downstream.
                discovered_sessions = []
            else:
                sessions = _merge_explicit_weflow_sessions(sessions, discovered_sessions)
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

        # Drop WeChat system/service accounts (filehelper, official accounts,
        # etc.): they are not real conversations, so we never pull, backfill, or
        # reply to them.
        sessions = [
            session
            for session in sessions
            if not is_system_account(_session_id_from_meta(session))
        ]
        if not explicit_talkers:
            sessions = [session for session in sessions if _weflow_session_pull_admitted(session)]
        sessions = _dedupe_weflow_sessions(sessions)
        _emit_progress(progress_callback, event="sessions", session_count=len(sessions), scanned_count=0, appended_count=0)

        worker_count = max(1, int(workers or 1))
        results: list[_WeFlowSessionPullResult] = []
        if worker_count <= 1 or len(sessions) <= 1:
            for session in sessions:
                _raise_if_cancelled(cancel_event)
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
                        ignore_seen=ignore_seen,
                        cancel_event=cancel_event,
                        progress_callback=progress_callback,
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
                        ignore_seen=ignore_seen,
                        cancel_event=cancel_event,
                        progress_callback=progress_callback,
                    ): session
                    for session in sessions
                }
                for future in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set():
                        for pending in futures:
                            pending.cancel()
                    try:
                        results.append(future.result())
                    except WeFlowPullCancelled:
                        session_id = _session_id_from_meta(futures[future])
                        errors.append({"session": session_id, "type": "cancelled", "message": "WeFlow pull cancelled"})
                    except Exception as exc:
                        session_id = _session_id_from_meta(futures[future])
                        errors.append({"session": session_id, "type": type(exc).__name__, "message": str(exc)})

        scanned = sum(item.scanned_count for item in results)
        appended = sum(item.appended_count for item in results)
        for item in results:
            errors.extend(item.errors)
            media_export_paths.update(item.media_export_paths)
        cancelled = bool(cancel_event is not None and cancel_event.is_set())
        return WeFlowPullResult(
            status="cancelled" if cancelled else ("ok" if not errors else "partial_error"),
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
        ignore_seen: bool = False,
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> _WeFlowSessionPullResult:
        session_id = _session_id_from_meta(session)
        if not session_id:
            return _WeFlowSessionPullResult("", 0, 0)
        errors: list[dict[str, str]] = []
        appended_raw_ids: list[str] = []
        media_export_paths: set[str] = set()
        current_recent_raw_ids: dict[str, int] = {}
        scanned = 0
        appended = 0
        now_since = int(time.time()) - max(0, lookback_seconds)
        history_context_only = context_only if context_only is not None else (since is not None and since <= 0)
        cursor_recovery_context_only = False
        with _path_lock(_talker_lock_path(self.state_path, session_id), timeout_seconds=self.timeout_seconds):
            _raise_if_cancelled(cancel_event)
            state_snapshot = _read_weflow_state_locked(self.state_path, timeout_seconds=self.timeout_seconds)
            history_reset_epoch = _safe_int(state_snapshot.get("history_reset_epoch"), 0)
            seen = set() if ignore_seen else set(_string_list(state_snapshot.get("seen_raw_ids")))
            sessions_state = state_snapshot.get("sessions")
            if not isinstance(sessions_state, dict):
                sessions_state = {}
            session_state = sessions_state.get(session_id, {}) if isinstance(sessions_state.get(session_id), dict) else {}
            previous_recent_raw_ids = _recent_raw_id_map(session_state.get("recent_raw_ids"))
            session_since = since
            if session_since is None:
                session_since = _safe_int(session_state.get("since"), now_since)
            reset_cursor_bootstrap = bool(
                since is None
                and history_reset_epoch > 0
                and _safe_int(session_state.get("since"), 0) <= 0
            )
            if reset_cursor_bootstrap:
                session_since = history_reset_epoch
            last_message_at = _session_last_message_at(session)
            if (
                since is None
                and not reset_cursor_bootstrap
                and last_message_at > 0
                and session_since > last_message_at + 2
                and not seen
            ):
                session_since = max(0, last_message_at - max(2, min(max(0, lookback_seconds), 3600)))
                cursor_recovery_context_only = True
            start_time = max(0, session_since - 2)
            try:
                payloads = self.raw_message_pages(
                    session_id,
                    start=start_time,
                    limit=message_limit,
                    max_pages=max_pages,
                    max_messages=max_messages,
                    media=media,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            except WeFlowPullCancelled:
                errors.append({"session": session_id, "type": "cancelled", "message": "WeFlow pull cancelled"})
                return _WeFlowSessionPullResult(session_id, scanned, appended, tuple(errors))
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
            complete_window = bool(messages) and not any(payload.get("hasMore") is True for payload in payloads)
            for message in messages:
                _raise_if_cancelled(cancel_event)
                scanned += 1
                message_timestamp = _epoch_seconds(
                    message.get("timestamp") or message.get("createTime"),
                    0 if history_reset_epoch > 0 else max_timestamp,
                )
                reset_context_only = bool(
                    history_reset_epoch > 0
                    and (message_timestamp <= 0 or message_timestamp <= history_reset_epoch)
                )
                normalized = normalize_weflow_message(
                    message,
                    session_id=session_id,
                    session_meta=meta,
                    context_only=bool(
                        history_context_only
                        or cursor_recovery_context_only
                        or reset_context_only
                    ),
                )
                raw_id = str(normalized.get("raw_id") or "").strip()
                max_timestamp = max(max_timestamp, message_timestamp)
                if raw_id:
                    current_recent_raw_ids[raw_id] = message_timestamp
                if raw_id in seen:
                    continue
                was_appended = self.writer.append(normalized)
                if was_appended:
                    appended += 1
                if raw_id:
                    seen.add(raw_id)
                    appended_raw_ids.append(raw_id)
                _emit_progress(
                    progress_callback,
                    event="message",
                    session_id=session_id,
                    scanned_count=scanned,
                    appended_count=appended,
                    last_raw_id=raw_id,
                )
            if complete_window and not ignore_seen and not bool(history_context_only or cursor_recovery_context_only):
                for recall_payload in _synthetic_recalls_for_missing_recent_messages(
                    session_id=session_id,
                    session_meta=meta,
                    previous_recent_raw_ids=previous_recent_raw_ids,
                    current_recent_raw_ids=current_recent_raw_ids,
                    start_time=start_time,
                ):
                    raw_id = str(recall_payload.get("raw_id") or "").strip()
                    if self.writer.append(recall_payload):
                        appended += 1
                        if raw_id:
                            appended_raw_ids.append(raw_id)
                    _emit_progress(
                        progress_callback,
                        event="delete",
                        session_id=session_id,
                        scanned_count=scanned,
                        appended_count=appended,
                        last_raw_id=raw_id,
                    )
            _merge_weflow_state(
                self.state_path,
                session_id=session_id,
                since=max_timestamp,
                seen_raw_ids=appended_raw_ids,
                recent_raw_ids=current_recent_raw_ids,
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
        with history_writer_leases_if_owned(
            self._history_paths(),
            label="weflow_sse_listener",
        ):
            return self._listen_sse_leased(
                max_events=max_events,
                max_seconds=max_seconds,
            )

    def _listen_sse_leased(
        self,
        *,
        max_events: int | None = None,
        max_seconds: float | None = None,
    ) -> WeFlowSseResult:
        state = _read_json_object(self.state_path)
        history_reset_epoch = _safe_int(state.get("history_reset_epoch"), 0)
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
                    event_timestamp = _epoch_seconds(payload.get("timestamp"), 0)
                    reset_context_only = bool(
                        history_reset_epoch > 0
                        and (event_timestamp <= 0 or event_timestamp <= history_reset_epoch)
                    )
                    normalized = normalize_weflow_push_event(
                        {
                            **payload,
                            "event": normalized_event or "message.new",
                            "context_only": bool(
                                payload.get("context_only")
                                or payload.get("contextOnly")
                                or reset_context_only
                            ),
                        }
                    )
                    if event_id:
                        normalized["event_id"] = event_id
                    seen.add(dedupe_key)
                    if self.writer.append(normalized):
                        appended += 1
                    else:
                        skipped += 1
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
        cancel_event: Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        page_limit = max(1, min(10000, int(limit or 100)))
        pages: list[dict[str, Any]] = []
        offset = 0
        page_count = 0
        total_messages = 0
        unlimited_pages = max_pages <= 0
        while unlimited_pages or page_count < max_pages:
            _raise_if_cancelled(cancel_event)
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
            _emit_progress(
                progress_callback,
                event="page",
                session_id=talker,
                page_count=page_count,
                page_messages=count,
                total_messages=total_messages,
                has_more=bool(payload.get("hasMore") is True),
            )
            if count <= 0 or payload.get("hasMore") is not True:
                break
            offset += count
        return pages

    def _json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.2, float(self.timeout_seconds))
        with self._open(path, params=params, method="GET", deadline=deadline) as response:
            raw = _read_bounded_response(
                response,
                max_bytes=WEFLOW_JSON_RESPONSE_MAX_BYTES,
                timeout_seconds=self.timeout_seconds,
                deadline=deadline,
            ).decode("utf-8")
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
        deadline: float | None = None,
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
        try:
            guarded_open = guarded_same_authority_urlopen if self.allow_non_local else guarded_local_urlopen
            return guarded_open(
                request,
                timeout_seconds=self.timeout_seconds,
                deadline=deadline,
            )
        except HTTPError as exc:
            exc.close()
            raise


def append_hook_source_event(
    hook_event_file: str | Path,
    payload: dict[str, Any],
    *,
    source: str,
) -> HookSourceAppendResult:
    event_path = Path(hook_event_file)
    with history_writer_fence_if_owned(
        event_path.parent,
        label="append_hook_source_event",
    ):
        return _append_hook_source_event_fenced(event_path, payload, source=source)


def _append_hook_source_event_fenced(
    hook_event_file: str | Path,
    payload: dict[str, Any],
    *,
    source: str,
) -> HookSourceAppendResult:
    writer = HookEventJsonlWriter(hook_event_file)
    try:
        if source == "weflow-push":
            appended = writer.append(normalize_weflow_push_event(payload))
        elif source == "weflow-message":
            session_id = str(payload.get("sessionId") or payload.get("talker") or payload.get("session_id") or "").strip()
            appended = writer.append(normalize_weflow_message(payload, session_id=session_id, session_meta=payload))
        else:
            appended = writer.append(payload)
    except Exception as exc:
        return HookSourceAppendResult(
            status="error",
            hook_event_file=str(writer.path),
            appended_count=0,
            errors=({"type": type(exc).__name__, "message": str(exc)},),
        )
    return HookSourceAppendResult(status="ok", hook_event_file=str(writer.path), appended_count=1 if appended else 0)


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
    context_only = bool(payload.get("context_only") or payload.get("contextOnly"))
    recall_message_id = _weflow_recall_message_id(payload, fallback=rawid) if is_recall else ""
    raw_id_suffix = rawid or recall_message_id or str(timestamp)
    sender_name = _first_text(payload, "sourceName", "senderName", "accountName", "sender") or ("system" if is_recall else "unknown")
    chat_title = _preferred_display_name(
        _first_text(payload, "groupName"),
        _first_text(payload, "sessionName"),
        _first_text(payload, "displayName"),
        _first_text(payload, "remark"),
        _first_text(payload, "nickName"),
        _first_text(payload, "talkerName"),
        session_id,
    )
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
        "is_self": _weflow_is_self(payload),
        "is_group": _session_type_is_group(payload, session_id),
        "context_only": context_only,
        "capture_phase": "history_backfill" if context_only else "incremental",
        "source_payload": _weflow_source_payload(
            "weflow_push",
            conversation_key=session_id,
            session_meta=payload,
            message=payload,
            context_only=context_only,
            message_type=_first_text(payload, "messageType", "message_type", "type"),
            server_id=rawid,
            msg_id=rawid,
        ),
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
    raw_id = f"weflow:message:{talker}:{_weflow_stable_message_identity(message, server_id=server_id, local_id=local_id, message_key=message_key)}"
    text = _first_text(message, "parsedContent", "content", "rawContent", "text")
    sender_id = _first_text(message, "sender", "senderUsername", "sender_id")
    sender_name = _first_text(message, "accountName", "groupNickname", "senderName") or sender_id or "unknown"
    chat_title = _preferred_display_name(
        _first_text(meta, "remark"),
        _first_text(meta, "displayName"),
        _first_text(meta, "display_name"),
        _first_text(meta, "nickName"),
        _first_text(meta, "nickname"),
        _first_text(meta, "groupName"),
        _first_text(meta, "group_name"),
        _first_text(meta, "name"),
        _first_text(meta, "username"),
        _first_text(meta, "api_talker"),
        talker,
    )
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
        "is_self": _weflow_is_self(message),
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
        "source_payload": _weflow_source_payload(
            "weflow_http_raw",
            conversation_key=talker,
            session_meta=meta,
            message=message,
            context_only=bool(context_only),
            message_type=message_type,
            local_type=local_type,
            media_export_path=media_export_path,
            sort_key=_first_text(message, "sortSeq", "sort_seq"),
            server_id=server_id,
            local_id=local_id,
            message_key=message_key,
            msg_id=server_id or local_id,
        ),
        "raw": {"message": message, "session": meta},
    }
    voice_payload = _weflow_voice(message, attachments)
    if voice_payload:
        result["voice"] = voice_payload
    quote_payload = _weflow_quote(message)
    if quote_payload:
        result["quote"] = quote_payload
    return {key: value for key, value in result.items() if value not in ("", None, [])}

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
    audio_path = str(audio.get("path") or _first_text(message, "mediaLocalPath", "voiceAudioPath", "voice_audio_path")).strip()
    audio_name = str(audio.get("name") or _first_text(message, "mediaFileName", "voiceAudioName", "voice_audio_name")).strip()
    duration = _first_text(message, "voiceDuration", "voice_duration", "voiceDurationSeconds", "voice_duration_seconds")
    return {
        key: value
        for key, value in {
            "status": "pending",
            "source": "weflow_http_raw",
            "duration": duration,
            "audio_path": audio_path,
            "audio_name": audio_name,
        }.items()
        if value
    }


def _weflow_is_self(message: dict[str, Any]) -> bool:
    if _truthy(
        message.get("isSend")
        or message.get("is_send")
        or message.get("isSelf")
        or message.get("fromMe")
        or message.get("from_me")
        or message.get("self")
    ):
        return True
    sender_id = _first_text(
        message,
        "sender",
        "senderUsername",
        "senderId",
        "fromUser",
        "fromUserName",
        "account",
        "accountId",
    )
    return bool(sender_id and sender_id in _weflow_owner_wxids(message))


def _weflow_owner_wxids(message: dict[str, Any]) -> set[str]:
    try:
        raw = json.dumps(message, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(message)
    owner_ids: set[str] = set()
    for match in re.finditer(r"(?:xwechat_files|WeChat Files)(?:\\\\|[\\/])(wxid_[A-Za-z0-9]+)(?:_[A-Za-z0-9]+)?", raw, re.IGNORECASE):
        owner_ids.add(match.group(1))
    return owner_ids


def _weflow_quote(message: dict[str, Any]) -> dict[str, str]:
    quote_payload = message.get("quote")
    if not isinstance(quote_payload, dict):
        quote_payload = {}
    aliases = [
        value
        for value in (
            _first_text(message, "replyToMessageId"),
            _first_text(quote_payload, "platformMessageId"),
            _first_text(quote_payload, "serverId", "server_id"),
            _first_text(quote_payload, "messageId", "message_id", "msgid", "msgId"),
            _first_text(quote_payload, "localId", "local_id"),
            _first_text(quote_payload, "messageKey", "message_key"),
        )
        if value
    ]
    result = {
        "message_id": aliases[0] if aliases else "",
        "message_ids": aliases,
        "sender_name": _first_text(quote_payload, "accountName", "sender"),
        "text": _first_text(quote_payload, "content", "text"),
        "type": _first_text(quote_payload, "type"),
        "source": "weflow_http_raw",
    }
    return {key: value for key, value in result.items() if value}


def _weflow_stable_message_identity(
    message: dict[str, Any],
    *,
    server_id: str,
    local_id: str,
    message_key: str,
) -> str:
    if server_id:
        return server_id
    if message_key and not _looks_like_path(message_key):
        return message_key
    create_time = _first_text(message, "createTime", "create_time")
    sort_key = _first_text(message, "sortSeq", "sort_seq")
    if local_id:
        return ":".join(item for item in ("local", local_id, create_time, sort_key) if item)
    sender = _first_text(message, "sender", "senderUsername", "sender_id")
    text = _first_text(message, "parsedContent", "content", "rawContent", "text")
    seed = json.dumps(
        {
            "create_time": create_time or _safe_int(message.get("timestamp"), 0),
            "sort_key": sort_key,
            "sender": sender,
            "text": text,
            "type": _first_text(message, "localType", "local_type", "mediaType", "type"),
            "file": _first_text(message, "fileName", "file_name", "mediaFileName"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "fingerprint:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


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


def _weflow_sessions_from_talkers(talkers: list[str]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for item in talkers:
        session_id = str(item or "").strip()
        if not session_id:
            continue
        sessions.append({"id": session_id, "name": session_id, "type": "group" if session_id.endswith("@chatroom") else "private"})
    return sessions


def _merge_explicit_weflow_sessions(
    explicit_sessions: list[dict[str, Any]],
    discovered_sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    discovered_by_id: dict[str, dict[str, Any]] = {}
    for session in discovered_sessions:
        if not isinstance(session, dict):
            continue
        for session_id in _weflow_session_identity_candidates(session):
            discovered_by_id.setdefault(session_id, session)

    merged_sessions: list[dict[str, Any]] = []
    for explicit in explicit_sessions:
        session_id = _session_id_from_meta(explicit)
        discovered = discovered_by_id.get(session_id, {})
        if discovered:
            merged_sessions.append({**discovered, **explicit, "id": session_id})
        else:
            merged_sessions.append(explicit)
    return merged_sessions


def _weflow_session_identity_candidates(session: dict[str, Any]) -> list[str]:
    values = [
        session.get("id"),
        session.get("username"),
        session.get("talker"),
        session.get("sessionId"),
        session.get("session_id"),
    ]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _weflow_session_pull_admitted(session: dict[str, Any]) -> bool:
    session_id = _session_id_from_meta(session)
    if not session_id:
        return False
    if _session_type_is_group(session, session_id):
        return True
    if _weflow_explicit_non_friend(session):
        return False
    if _weflow_explicit_friend(session):
        return True
    if _looks_like_private_wechat_receiver(session_id):
        return False
    for value in (
        _first_text(session, "remark", "remarkName"),
        _first_text(session, "displayName", "display_name"),
        _first_text(session, "nickName", "nickname"),
        _first_text(session, "name"),
    ):
        text = str(value or "").strip()
        if _weflow_session_title_identifies_contact(text, session_id):
            return True
    return False


def _weflow_session_title_identifies_contact(value: str, session_id: str) -> bool:
    text = str(value or "").strip()
    if not text or text == session_id:
        return False
    if text.lower() in WEFLOW_PLACEHOLDER_NAMES:
        return False
    if _looks_like_wechat_receiver(text):
        return False
    return any(ch.isalnum() for ch in text)


def _weflow_source_payload(
    source: str,
    *,
    conversation_key: str,
    session_meta: dict[str, Any],
    message: dict[str, Any],
    context_only: bool,
    message_type: str = "",
    local_type: str = "",
    media_export_path: str = "",
    sort_key: str = "",
    server_id: str = "",
    local_id: str = "",
    message_key: str = "",
    msg_id: str = "",
) -> dict[str, Any]:
    session_type = _first_text(session_meta, "sessionType", "type", "session_type")
    payload: dict[str, Any] = {
        "source": source,
        "adapter": source,
        "conversation_key": conversation_key,
        "talker_id": conversation_key,
        "talker": conversation_key,
        "session_id": conversation_key,
        "session_type": session_type,
        "is_group": _session_type_is_group(session_meta, conversation_key),
        "message_type": message_type,
        "sort_key": sort_key,
        "server_id": server_id,
        "local_id": local_id,
        "message_key": message_key,
        "local_type": local_type,
        "msg_id": msg_id,
        "media_export_path": media_export_path,
        "context_only": bool(context_only),
    }
    for key in (
        "remark",
        "remarkName",
        "displayName",
        "display_name",
        "nickName",
        "nickname",
        "name",
        "username",
        "sessionName",
        "talkerName",
        *WEFLOW_FRIEND_TRUE_KEYS,
        *WEFLOW_NON_FRIEND_TRUE_KEYS,
        *WEFLOW_RELATION_KEYS,
    ):
        value = _first_present_value(session_meta, message, key=key)
        if value is not None:
            payload[key] = value
    session_identity = _compact_weflow_session_identity(session_meta)
    if session_identity:
        payload["session"] = session_identity
    return {key: value for key, value in payload.items() if value not in ("", None, [], {})}


def _compact_weflow_session_identity(session: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(session, dict):
        return {}
    keys = (
        "id",
        "username",
        "talker",
        "sessionId",
        "session_id",
        "type",
        "sessionType",
        "session_type",
        "remark",
        "remarkName",
        "displayName",
        "display_name",
        "nickName",
        "nickname",
        "name",
        *WEFLOW_FRIEND_TRUE_KEYS,
        *WEFLOW_NON_FRIEND_TRUE_KEYS,
        *WEFLOW_RELATION_KEYS,
    )
    result: dict[str, Any] = {}
    for key in keys:
        if key in session and session.get(key) not in ("", None, [], {}):
            result[key] = session.get(key)
    return result


def _first_present_value(*payloads: dict[str, Any], key: str) -> Any:
    for payload in payloads:
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    return None


def _weflow_explicit_friend(payload: dict[str, Any]) -> bool:
    for key in WEFLOW_FRIEND_TRUE_KEYS:
        if key in payload and _truthy(payload.get(key)):
            return True
    for key in WEFLOW_RELATION_KEYS:
        value = str(payload.get(key) or "").strip().lower()
        if value in WEFLOW_FRIEND_VALUES:
            return True
    return False


def _weflow_explicit_non_friend(payload: dict[str, Any]) -> bool:
    for key in WEFLOW_NON_FRIEND_TRUE_KEYS:
        if key in payload and _truthy(payload.get(key)):
            return True
    for key in WEFLOW_FRIEND_TRUE_KEYS:
        if key in payload and payload.get(key) is False:
            return True
    for key in WEFLOW_RELATION_KEYS:
        value = str(payload.get(key) or "").strip().lower()
        if value in WEFLOW_NON_FRIEND_VALUES:
            return True
    for key in WEFLOW_NON_FRIEND_TEXT_KEYS:
        value = str(payload.get(key) or "").strip().lower()
        if value and any(marker in value for marker in WEFLOW_NON_FRIEND_TEXT_MARKERS):
            return True
    return False


def _dedupe_weflow_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one worker lane per talker while preserving discovery order."""

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for session in sessions:
        session_id = _session_id_from_meta(session)
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        deduped.append(session)
    return deduped


def _session_last_message_at(session: dict[str, Any]) -> int:
    if not isinstance(session, dict):
        return 0
    return _epoch_seconds(
        session.get("lastMessageAt")
        or session.get("last_message_at")
        or session.get("last_message_time")
        or session.get("lastMessageTime")
        or session.get("lastActiveTime")
        or session.get("updateTime"),
        0,
    )


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
    if message_id.startswith(("weflow:", "hook:")):
        return message_id
    return f"weflow:message:{session_id}:{message_id}" if message_id else ""


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise WeFlowPullCancelled("WeFlow pull cancelled")


def _emit_progress(callback: ProgressCallback | None, **payload: Any) -> None:
    if callback is None:
        return
    callback({key: value for key, value in payload.items() if value not in ("", None)})


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


def _weflow_json(
    api_base_url: str,
    path: str,
    *,
    token: str = "",
    timeout_seconds: float = 5.0,
    allow_non_local: bool = False,
) -> dict[str, Any]:
    url = api_base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    deadline = time.monotonic() + max(0.2, float(timeout_seconds))
    try:
        guarded_open = guarded_same_authority_urlopen if allow_non_local else guarded_local_urlopen
        with guarded_open(request, timeout_seconds=timeout_seconds, deadline=deadline) as response:
            raw = _read_bounded_response(
                response,
                max_bytes=WEFLOW_JSON_RESPONSE_MAX_BYTES,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            ).decode("utf-8")
    except HTTPError as exc:
        exc.close()
        raise
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("WeFlow health response must be a JSON object")
    return payload


def _read_bounded_response(
    response: Any,
    *,
    max_bytes: int,
    timeout_seconds: float,
    deadline: float | None = None,
) -> bytes:
    try:
        return read_response_with_deadline(
            response,
            max_bytes=max_bytes,
            deadline=deadline if deadline is not None else time.monotonic() + max(0.2, float(timeout_seconds)),
        )
    except TimeoutError as exc:
        raise TimeoutError("weflow_response_deadline_exceeded") from exc
    except ValueError as exc:
        if "http_response_too_large" in str(exc):
            raise ValueError(str(exc).replace("http_response_too_large", "weflow_response_too_large", 1)) from exc
        raise


def _url_is_local_http(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _weflow_fork_marker_ok(health: dict[str, Any]) -> bool:
    flavor = _first_text(health, "buildFlavor", "build_flavor", "fork.buildFlavor", "fork.build_flavor")
    return flavor == WEFLOW_LOCAL_BUILD_FLAVOR


def _require_local_http_url(url: str) -> None:
    validate_local_http_url(url)


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


def _preferred_display_name(*values: Any) -> str:
    fallback = ""
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if not fallback:
            fallback = text
        if not _looks_like_wechat_receiver(text):
            return text
    return fallback


def _looks_like_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith(("wxid_", "gh_")) or text.endswith("@chatroom"))


def _looks_like_private_wechat_receiver(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith(("wxid_", "gh_")))


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
    lowered = text.lower()
    return bool(
        text
        and (
            ":\\" in text
            or ":/" in text
            or "/" in text
            or "\\" in text
            or "%5c" in lowered
            or "%2f" in lowered
            or "%3a" in lowered
        )
    )


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


def _recent_raw_id_map(value: Any) -> dict[str, int]:
    if isinstance(value, dict):
        result: dict[str, int] = {}
        for key, raw_timestamp in value.items():
            raw_id = str(key or "").strip()
            if raw_id:
                result[raw_id] = _safe_int(raw_timestamp, 0)
        return result
    if isinstance(value, list):
        return {str(item).strip(): 0 for item in value if str(item).strip()}
    return {}


def _synthetic_recalls_for_missing_recent_messages(
    *,
    session_id: str,
    session_meta: dict[str, Any],
    previous_recent_raw_ids: dict[str, int],
    current_recent_raw_ids: dict[str, int],
    start_time: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    current = set(current_recent_raw_ids)
    missing = [
        raw_id
        for raw_id, timestamp in sorted(previous_recent_raw_ids.items(), key=lambda item: item[1])
        if raw_id and raw_id not in current and timestamp >= start_time
    ]
    if not missing:
        return []
    timestamp = int(time.time())
    talker_name = _preferred_display_name(
        _first_text(session_meta, "remark"),
        _first_text(session_meta, "displayName"),
        _first_text(session_meta, "display_name"),
        _first_text(session_meta, "nickName"),
        _first_text(session_meta, "nickname"),
        _first_text(session_meta, "groupName"),
        _first_text(session_meta, "group_name"),
        _first_text(session_meta, "name"),
        _first_text(session_meta, "username"),
        session_id,
    )
    events: list[dict[str, Any]] = []
    for raw_id in missing[: max(1, limit)]:
        target_message_id = _message_id_from_raw_id(raw_id)
        delete_id = hashlib.sha256(f"{session_id}:{raw_id}:local-delete".encode("utf-8")).hexdigest()[:16]
        events.append(
            {
                "source": "weflow_http_raw",
                "event_type": "recall",
                "talker": session_id,
                "talker_name": talker_name,
                "sender_id": "",
                "sender_name": "system",
                "raw_id": f"weflow:delete:{session_id}:{delete_id}",
                "msgid": f"delete:{target_message_id}",
                "message_type": "recall",
                "text": "",
                "timestamp": timestamp,
                "is_self": False,
                "is_group": _session_type_is_group(session_meta, session_id),
                "recall": {
                    "target_raw_id": raw_id,
                    "target_message_id": target_message_id,
                    "reason": "weflow_local_missing_from_recent_window",
                },
                "raw": {
                    "source": "recent_window_diff",
                    "target_raw_id": raw_id,
                    "session": session_meta,
                },
            }
        )
    return events


def _merge_weflow_state(
    path: Path,
    *,
    session_id: str,
    since: int,
    seen_raw_ids: list[str],
    recent_raw_ids: dict[str, int] | None = None,
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
        session_payload = dict(previous) if isinstance(previous, dict) else {}
        session_payload["since"] = max(previous_since, since)
        if recent_raw_ids is not None:
            session_payload["recent_raw_ids"] = {
                key: value
                for key, value in sorted(recent_raw_ids.items(), key=lambda item: item[1])[-500:]
                if key
            }
        sessions[session_id] = session_payload
        seen = set(_string_list(state.get("seen_raw_ids")))
        seen.update(item for item in seen_raw_ids if str(item).strip())
        state["seen_raw_ids"] = sorted(seen)[-50000:]
        _write_json_object(path, state)


def _talker_lock_path(state_path: Path, talker: str) -> Path:
    digest = hashlib.sha256(str(talker or "unknown").encode("utf-8")).hexdigest()[:16]
    return state_path.with_suffix(state_path.suffix + f".talker-{digest}.lock")


@contextmanager
def _path_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    with short_process_lock(
        path,
        timeout_seconds=timeout_seconds,
        stale_after_seconds=60.0,
        timeout_label="WeFlow state lock",
    ):
        yield
