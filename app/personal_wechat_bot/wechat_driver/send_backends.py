"""Pluggable, non-foreground send backends for the outbox bridge.

The bridge worker consumes ``send_bridge/outbox.jsonl`` and delivers each record
to WeChat through one of these backends. None of them touch the foreground
window, the mouse, or the clipboard: delivery is by wxid/roomid, not by focus.

Two backends ship today:

* :class:`DryRunSendBackend` — the safe default. It never contacts WeChat; it
  records and logs deliveries and reports success. This keeps the whole
  outbox -> bridge -> ack -> ledger chain runnable and testable without a live
  WeChat / WeChatFerry install.
* :class:`WcfSendBackend` — real delivery via WeChatFerry (``wcferry``) over its
  pynng RPC socket (DLL injection, no foreground interaction). ``wcferry`` is
  imported lazily so environments without it can still import this module and
  run the dry-run backend.

WeChatFerry's ``send_*`` calls are documented as not thread-safe, so every
backend serializes its own sends behind a lock. The bridge worker additionally
drives a single sender thread; the lock here is defence in depth.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Extensions WeChatFerry should deliver as an image rather than a generic file.
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


@dataclass(frozen=True)
class SendOutcome:
    """Result of a single backend delivery attempt."""

    ok: bool
    reason: str
    external_message_id: str = ""

    @classmethod
    def success(cls, reason: str = "sent", external_message_id: str = "") -> "SendOutcome":
        return cls(True, reason, external_message_id)

    @classmethod
    def failure(cls, reason: str) -> "SendOutcome":
        return cls(False, reason, "")


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
        return SendOutcome.success("dry_run_text")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        with self._lock:
            self.sent_files.append((receiver, path, caption))
        logger.info("[dry_run] send_file -> %s: %s (caption=%r)", receiver, path, caption)
        return SendOutcome.success("dry_run_file")

    def close(self) -> None:
        return None


class WcfSendBackend:
    """Real delivery via WeChatFerry over its pynng RPC socket.

    ``wcferry`` is imported and the client is connected lazily, on first use, so
    this module imports cleanly in environments without WeChatFerry installed.
    """

    name = "wcf"

    def __init__(self, *, host: str = "127.0.0.1", port: int = 10086) -> None:
        self.host = host or "127.0.0.1"
        self.port = int(port or 10086)
        self._lock = threading.Lock()
        self._client: Any | None = None
        self._connect_error = ""

    def _ensure_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            from wcferry import Wcf  # type: ignore import-not-found
        except Exception as exc:  # pragma: no cover - depends on optional dep
            self._connect_error = f"wcferry_import_failed:{type(exc).__name__}:{exc}"
            logger.error("WcfSendBackend: %s", self._connect_error)
            return None
        try:
            # host is non-None -> connect to an already-running wcf RPC server
            # rather than launching a local wcf.exe.
            self._client = Wcf(host=self.host, port=self.port)
        except Exception as exc:  # pragma: no cover - depends on live WeChat
            self._connect_error = f"wcf_connect_failed:{type(exc).__name__}:{exc}"
            logger.error("WcfSendBackend: %s", self._connect_error)
            self._client = None
        return self._client

    def health_check(self) -> bool:
        with self._lock:
            client = self._ensure_client()
            if client is None:
                return False
            try:
                return bool(client.is_login())
            except Exception as exc:  # pragma: no cover - depends on live WeChat
                logger.error("WcfSendBackend health_check failed: %s", exc)
                return False

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        with self._lock:
            client = self._ensure_client()
            if client is None:
                return SendOutcome.failure(self._connect_error or "wcf_unavailable")
            try:
                ret = client.send_text(text, receiver)
            except Exception as exc:  # pragma: no cover - depends on live WeChat
                return SendOutcome.failure(f"wcf_send_text_error:{type(exc).__name__}:{exc}")
            return self._outcome_from_ret(ret, "wcf_send_text")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        resolved = str(path)
        suffix = Path(resolved).suffix.lower()
        with self._lock:
            client = self._ensure_client()
            if client is None:
                return SendOutcome.failure(self._connect_error or "wcf_unavailable")
            try:
                if suffix in _IMAGE_EXTENSIONS:
                    ret = client.send_image(resolved, receiver)
                else:
                    ret = client.send_file(resolved, receiver)
            except Exception as exc:  # pragma: no cover - depends on live WeChat
                return SendOutcome.failure(f"wcf_send_file_error:{type(exc).__name__}:{exc}")
            return self._outcome_from_ret(ret, "wcf_send_file")

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            if client is None:
                return
            try:
                client.cleanup()
            except Exception:  # pragma: no cover - best effort
                pass

    @staticmethod
    def _outcome_from_ret(ret: Any, reason: str) -> SendOutcome:
        # WeChatFerry returns 0 on success, non-zero on failure.
        try:
            code = int(ret)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            return SendOutcome.success(reason)
        return SendOutcome.failure(f"{reason}_failed:code={code}")


def build_send_backend(config: Any) -> SendBackend:
    """Construct the send backend named by ``config.send_backend`` (default dry_run)."""
    name = str(getattr(config, "send_backend", "") or "dry_run").strip().lower()
    if name in {"", "dry_run", "dryrun", "mock"}:
        return DryRunSendBackend()
    if name == "wcf":
        return WcfSendBackend(
            host=str(getattr(config, "wcf_host", "") or "127.0.0.1"),
            port=int(getattr(config, "wcf_port", 0) or 10086),
        )
    raise ValueError(f"unknown send_backend: {name!r}")
