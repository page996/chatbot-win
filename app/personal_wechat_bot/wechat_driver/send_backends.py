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

import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Extensions WeChatFerry should deliver as an image rather than a generic file.
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
_DEFAULT_WCF_SEND_TIMEOUT_SECONDS = 15.0
_WCF_CHILD_SCRIPT = r"""
import json
import sys

payload = json.loads(sys.stdin.read() or "{}")
try:
    from wcferry import Wcf

    client = Wcf(host=str(payload.get("host") or "127.0.0.1"), port=int(payload.get("port") or 10086))
    op = str(payload.get("op") or "")
    if op == "health":
        result = {"ok": bool(client.is_login()), "reason": "wcf_is_login"}
    elif op == "send_text":
        ret = client.send_text(str(payload.get("text") or ""), str(payload.get("receiver") or ""))
        result = {"ok": int(ret) == 0, "ret": ret, "reason": "wcf_send_text"}
    elif op == "send_image":
        ret = client.send_image(str(payload.get("path") or ""), str(payload.get("receiver") or ""))
        result = {"ok": int(ret) == 0, "ret": ret, "reason": "wcf_send_file"}
    elif op == "send_file":
        ret = client.send_file(str(payload.get("path") or ""), str(payload.get("receiver") or ""))
        result = {"ok": int(ret) == 0, "ret": ret, "reason": "wcf_send_file"}
    else:
        result = {"ok": False, "reason": f"unknown_op:{op}"}
    try:
        client.cleanup()
    except Exception:
        pass
except Exception as exc:
    result = {"ok": False, "reason": f"{type(exc).__name__}:{exc}"}
print(json.dumps(result, ensure_ascii=False))
"""

ChildRunner = Callable[[dict[str, Any], float], dict[str, Any]]


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

    Each wire send is executed in a short-lived child Python process. wcferry's
    Python client currently sets fixed pynng socket timeouts internally, but a
    genuinely wedged RPC/C call can still block the calling thread. A subprocess
    gives the bridge worker a hard kill boundary. A timeout has unknown delivery
    state, so the worker keeps running but quarantines that outbox item instead
    of blindly retrying and risking a duplicate send.
    """

    name = "wcf"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 10086,
        timeout_seconds: float = _DEFAULT_WCF_SEND_TIMEOUT_SECONDS,
        child_runner: ChildRunner | None = None,
    ) -> None:
        self.host = host or "127.0.0.1"
        self.port = int(port or 10086)
        self.timeout_seconds = max(1.0, float(timeout_seconds or _DEFAULT_WCF_SEND_TIMEOUT_SECONDS))
        self._lock = threading.Lock()
        self._child_runner = child_runner or _run_wcf_child

    def health_check(self) -> bool:
        with self._lock:
            result = self._call_child({"op": "health"})
            return bool(result.get("ok"))

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        with self._lock:
            result = self._call_child({"op": "send_text", "receiver": receiver, "text": text})
            return self._outcome_from_child(result, "wcf_send_text")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        resolved = str(path)
        suffix = Path(resolved).suffix.lower()
        with self._lock:
            op = "send_image" if suffix in _IMAGE_EXTENSIONS else "send_file"
            result = self._call_child({"op": op, "receiver": receiver, "path": resolved, "caption": caption})
            return self._outcome_from_child(result, "wcf_send_file")

    def close(self) -> None:
        return None

    def _call_child(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = {
            "host": self.host,
            "port": self.port,
            **payload,
        }
        try:
            return self._child_runner(request, self.timeout_seconds)
        except TimeoutError:
            return {"ok": False, "reason": f"wcf_rpc_timeout:{self.timeout_seconds:g}s"}
        except Exception as exc:
            return {"ok": False, "reason": f"wcf_child_error:{type(exc).__name__}:{exc}"}

    @staticmethod
    def _outcome_from_child(result: dict[str, Any], reason: str) -> SendOutcome:
        if bool(result.get("ok")):
            return SendOutcome.success(reason)
        if "ret" in result:
            return SendOutcome.failure(f"{reason}_failed:code={result.get('ret')}")
        detail = str(result.get("reason") or "unknown")
        return SendOutcome.failure(f"{reason}_error:{detail}")


def build_send_backend(config: Any) -> SendBackend:
    """Construct the send backend named by ``config.send_backend`` (default dry_run)."""
    name = str(getattr(config, "send_backend", "") or "dry_run").strip().lower()
    if name in {"", "dry_run", "dryrun", "mock"}:
        return DryRunSendBackend()
    if name == "wcf":
        return WcfSendBackend(
            host=str(getattr(config, "wcf_host", "") or "127.0.0.1"),
            port=int(getattr(config, "wcf_port", 0) or 10086),
            timeout_seconds=float(getattr(config, "wcf_send_timeout_seconds", 0) or _DEFAULT_WCF_SEND_TIMEOUT_SECONDS),
        )
    raise ValueError(f"unknown send_backend: {name!r}")


def _run_wcf_child(payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _WCF_CHILD_SCRIPT],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("WcfSendBackend child timed out after %.3fs", timeout_seconds)
        raise TimeoutError(str(exc)) from exc
    stdout = (completed.stdout or "").strip()
    if stdout:
        try:
            parsed = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            if completed.returncode != 0 and parsed.get("ok") is not False:
                return {
                    "ok": False,
                    "reason": f"child_exit:{completed.returncode}:{(completed.stderr or '').strip()[-500:]}",
                }
            return parsed
    return {
        "ok": False,
        "reason": f"child_exit:{completed.returncode}:{(completed.stderr or '').strip()[-500:]}",
    }
