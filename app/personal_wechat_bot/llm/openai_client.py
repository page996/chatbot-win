from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urljoin, urlparse

from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.domain.models import NormalizedMessage, SpeakDecision
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.runtime.resource_gate import acquire_llm
from app.personal_wechat_bot.runtime.resource_scheduler import ResourceScheduler
from app.personal_wechat_bot.tools.web.http_safety import read_response_with_deadline


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"

# HTTP statuses that mean "this key is bad/exhausted right now": don't keep
# using it — fail over to another key and put this one on a cooldown so it isn't
# reselected immediately.
_KEY_FAILOVER_STATUSES = {401, 403, 429}
# How long a key stays retired after an auth/rate failure before it may be
# retried (a 429 is often transient; a 401 usually is not, but a bounded
# cooldown lets a rotated/re-enabled key recover without a restart).
_BAD_KEY_COOLDOWN_SECONDS = 300.0
_DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
_MAX_OPENAI_RESPONSE_BYTES = 16 * 1024 * 1024
_OPENAI_OPEN_SLOTS = threading.BoundedSemaphore(8)


class UnsafeRelayRedirectError(RuntimeError):
    """Raised before a relay redirect can carry credentials to a new authority."""


class _RelayRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target_url = urljoin(req.full_url, str(newurl or ""))
        source = urlparse(req.full_url)
        target = urlparse(target_url)
        has_authorization = bool(req.get_header("Authorization"))
        blocked_reason = ""
        if source.scheme == "https" and target.scheme != "https":
            blocked_reason = "https_downgrade"
        elif has_authorization and _url_authority(source) != _url_authority(target):
            blocked_reason = "cross_authority"
        if blocked_reason:
            if fp is not None:
                fp.close()
            raise UnsafeRelayRedirectError(f"unsafe relay redirect blocked:{blocked_reason}")
        return super().redirect_request(req, fp, code, msg, headers, target_url)


def _url_authority(parsed) -> tuple[str, int]:
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "", 0
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return host, port


def _open_relay_request(request: urllib.request.Request, timeout=None):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RelayRedirectHandler(),
    )
    timeout_seconds = max(0.2, float(timeout or _DEFAULT_OPENAI_TIMEOUT_SECONDS))
    deadline = time.monotonic() + timeout_seconds
    if not _OPENAI_OPEN_SLOTS.acquire(timeout=timeout_seconds):
        raise TimeoutError("openai_request_deadline_exceeded")

    completed = threading.Event()
    cancelled = threading.Event()
    outcome_guard = threading.Lock()
    outcome: dict[str, object] = {}

    def open_request() -> None:
        try:
            response = opener.open(request, timeout=timeout_seconds)
            with outcome_guard:
                if cancelled.is_set():
                    response.close()
                else:
                    outcome["response"] = response
        except BaseException as exc:
            with outcome_guard:
                if not cancelled.is_set():
                    outcome["error"] = exc
        finally:
            _OPENAI_OPEN_SLOTS.release()
            completed.set()

    worker = threading.Thread(target=open_request, name="openai-http-open", daemon=True)
    try:
        worker.start()
    except Exception:
        _OPENAI_OPEN_SLOTS.release()
        raise

    remaining = deadline - time.monotonic()
    if remaining <= 0 or not completed.wait(remaining):
        cancelled.set()
        with outcome_guard:
            response = outcome.pop("response", None)
            if response is not None:
                response.close()  # type: ignore[attr-defined]
        raise TimeoutError("openai_request_deadline_exceeded")
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    response = outcome.get("response")
    if response is None:
        raise RuntimeError("openai_request_missing_response")
    return response


def _relay_request_open(request: urllib.request.Request, *, timeout_seconds: float):
    return _open_relay_request(request, timeout=timeout_seconds)


def _provider_timeout_seconds(config: ProviderConfig) -> float:
    try:
        value = float(config.max_wait_seconds or _DEFAULT_OPENAI_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        value = _DEFAULT_OPENAI_TIMEOUT_SECONDS
    return max(0.2, value)


def normalize_openai_base_url(base_url: str, provider: str = "relay") -> str:
    base = base_url.rstrip("/")
    if not base:
        return ""
    if provider == "deepseek" or "api.deepseek.com" in base:
        return base.removesuffix("/v1")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def validate_endpoint_url(url: str) -> str:
    """Return an error string if ``url`` is unsafe to attach an API key to.

    Only http/https with a real host is allowed. Used on both the model-probe
    path and the live chat send path so the API key is never egressed to a
    non-http(s) scheme (file://, ftp://, ...) or a schemeless/hostless string.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid_base_url"
    if parsed.scheme not in {"http", "https"}:
        return f"unsupported_url_scheme:{parsed.scheme or 'none'}"
    if not parsed.hostname:
        return "missing_url_host"
    if parsed.username is not None or parsed.password is not None:
        return "url_credentials_not_allowed"
    return ""


class RelayOpenAIClient:
    """Small OpenAI-compatible relay client skeleton.

    The minimum closed loop uses FakeLLMClient. This class is intentionally
    narrow and is not exercised unless explicitly configured later.
    """

    def __init__(
        self,
        config: ProviderConfig,
        key_pool: ApiKeyPool | None = None,
        channel_store: ConversationChannelStore | None = None,
        resource_scheduler: ResourceScheduler | None = None,
    ):
        self.config = config
        self.model = config.model
        self.key_pool = key_pool or ApiKeyPool(config)
        self.channel_store = channel_store
        self.resource_scheduler = resource_scheduler
        # ref -> monotonic timestamp until which the key is retired after an
        # auth/rate failure. Kept in-memory (per client); a restart clears it.
        self._bad_keys: dict[str, float] = {}
        self._bad_key_lock = threading.Lock()
        self._key_semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._key_semaphore_limits: dict[str, int] = {}
        self._key_semaphore_lock = threading.Lock()

    def generate_reply(self, prompt: str, *, workload: str = "interactive") -> str:
        data = self._chat_completion(
            [{"role": "user", "content": prompt}],
            conversation_id=_conversation_id_from_prompt(prompt),
            workload=workload,
        )
        return self._extract_content(data)

    def classify_topic(
        self,
        recent_messages: list[NormalizedMessage],
        topics: list[str],
        avoid_topics: list[str],
    ) -> SpeakDecision:
        if not recent_messages:
            raise RuntimeError("missing recent messages for topic classification")
        latest = recent_messages[-1]
        prompt = {
            "conversation_id": latest.conversation_id,
            "conversation_type": latest.conversation_type,
            "topics": topics,
            "avoid_topics": avoid_topics,
            "recent_messages": [
                {
                    "sender": item.sender_name,
                    "text": item.text,
                    "conversation_type": item.conversation_type,
                }
                for item in recent_messages[-10:]
            ],
            "output_schema": {
                "decision": "speak|silent|wait",
                "reason": "short explanation",
                "topic": "selected topic or null",
                "confidence": 0.0,
                "style_context": "short style hint",
            },
        }
        data = self._chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a WeChat group topic classifier. Allowed topics are semantic labels, not keywords. "
                        "Choose speak when the latest group message meaningfully relates to an allowed topic, asks a question, "
                        "or invites discussion the bot can naturally join. Choose silent for unrelated chatter, avoided topics, "
                        "spam, or very low confidence. Choose wait when the context is incomplete. Return JSON only."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            temperature=0,
            conversation_id=latest.conversation_id,
            workload="interactive",
        )
        parsed = self._parse_json(self._extract_content(data))
        decision = str(parsed.get("decision", "silent"))
        if decision not in {"speak", "silent", "wait"}:
            decision = "silent"
        reason = str(parsed.get("reason", "relay_topic_classifier"))
        topic = parsed.get("topic")
        topic_value = str(topic) if topic not in {None, ""} else None
        confidence = self._coerce_float(parsed.get("confidence", 0.0))
        style_context = str(parsed.get("style_context", "自然朋友聊天"))
        return SpeakDecision(
            conversation_id=latest.conversation_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            topic=topic_value,
            confidence=confidence,
            style_context=style_context,
        )

    def _chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        conversation_id: str = "",
        workload: str = "interactive",
    ) -> dict[str, object]:
        if not self.config.base_url and not any(
            bool(getattr(self.key_pool.config_for_ref(ref), "base_url", ""))
            for ref in _pool_available_refs(self.key_pool)
        ):
            raise RuntimeError("missing relay base_url")
        if self.config.base_url:
            default_endpoint = normalize_openai_base_url(self.config.base_url, self.config.provider) + "/chat/completions"
            url_error = validate_endpoint_url(default_endpoint)
            if url_error:
                raise RuntimeError(f"invalid base_url: {url_error}")

        # Try each candidate key in turn. On a 401/403/429 the key is retired
        # (cooldown) and we fail over to the next; a bounded candidate list means
        # we never loop forever. Non-auth errors (network, 5xx) propagate on the
        # first key — those are not a key-selection problem.
        candidates = self._candidate_refs(conversation_id)
        if not candidates:
            if _pool_available_refs(self.key_pool):
                raise RuntimeError("all candidate API keys are cooling down")
            raise RuntimeError(f"missing API key env: {self.config.api_key_env}")
        last_auth_error: Exception | None = None
        for ref in candidates:
            provider_config = _provider_for_ref(self.key_pool, ref, self.config)
            endpoint = normalize_openai_base_url(provider_config.base_url, provider_config.provider) + "/chat/completions"
            # Validate the endpoint BEFORE attaching the API key so a malformed or
            # non-http(s) base_url can never egress the key to an unexpected target.
            url_error = validate_endpoint_url(endpoint)
            if url_error:
                raise RuntimeError(f"invalid base_url for key {ref}: {url_error}")
            payload: dict[str, object] = {"model": provider_config.model, "messages": messages}
            if temperature is not None:
                payload["temperature"] = temperature
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            api_key = self.key_pool.key_for_ref(ref)
            if not api_key:
                continue
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                method="POST",
            )
            try:
                with self._llm_slot(workload, conversation_id):
                    with self._key_slot(ref, provider_config.max_concurrency):
                        timeout_seconds = _provider_timeout_seconds(provider_config)
                        deadline = time.monotonic() + timeout_seconds
                        with _relay_request_open(req, timeout_seconds=timeout_seconds) as resp:
                            raw = read_response_with_deadline(
                                resp,
                                max_bytes=_MAX_OPENAI_RESPONSE_BYTES,
                                deadline=deadline,
                            )
                            return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code in _KEY_FAILOVER_STATUSES:
                        self._retire_key(ref)
                        last_auth_error = RuntimeError(f"key {ref} rejected: HTTP {exc.code}")
                        continue
                    raise
                finally:
                    exc.close()
        raise RuntimeError(
            f"all candidate API keys exhausted (last auth error: {last_auth_error})"
        )

    def _candidate_refs(self, conversation_id: str) -> list[str]:
        """Ordered list of key refs to try for this request.

        Starts with the conversation's sticky/rotated pick (when a channel store
        is present), then appends every other currently-available ref as
        failover. Refs on cooldown are excluded until their cooldown elapses; a
        cooling key must not be retried immediately after an auth/rate failure.
        """
        ordered: list[str] = []
        seen: set[str] = set()

        def _add(ref: str | None) -> None:
            if ref and ref not in seen:
                seen.add(ref)
                ordered.append(ref)

        if conversation_id and self.channel_store is not None:
            _add(self.channel_store.ref_for_request(conversation_id))
        for ref in _pool_available_refs(self.key_pool):
            _add(ref)

        return [ref for ref in ordered if not self._is_on_cooldown(ref)]

    def _is_on_cooldown(self, ref: str) -> bool:
        with self._bad_key_lock:
            until = self._bad_keys.get(ref)
        if until is None:
            return False
        if time.monotonic() >= until:
            # Cooldown elapsed: give the key another chance.
            with self._bad_key_lock:
                self._bad_keys.pop(ref, None)
            return False
        return True

    def _retire_key(self, ref: str) -> None:
        with self._bad_key_lock:
            self._bad_keys[ref] = time.monotonic() + _BAD_KEY_COOLDOWN_SECONDS

    @contextmanager
    def _key_slot(self, ref: str, max_concurrency: int | None) -> Iterator[None]:
        limit = max(1, int(max_concurrency or 1))
        semaphore = self._semaphore_for_ref(ref, limit)
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    @contextmanager
    def _llm_slot(self, workload: str, conversation_id: str = "") -> Iterator[None]:
        schedule = self._resource_schedule(workload)
        reason = f"llm:{schedule.workload}:{conversation_id or 'global'}"
        with acquire_llm(
            workload=schedule.workload,
            reason=reason,
            max_parallel=schedule.max_parallel_conversations,
            total_max_parallel=schedule.llm_total,
            root=self._resource_gate_root(),
        ):
            yield

    def _resource_schedule(self, workload: str):
        scheduler = self.resource_scheduler
        if scheduler is not None:
            try:
                return scheduler.conversation_parallelism(workload)
            except Exception:
                pass
        fallback = ResourceScheduler(
            self._resource_gate_data_dir(),
            key_pool=self.key_pool,
            provider_max_concurrency=self.config.max_concurrency,
        )
        return fallback.conversation_parallelism(workload)

    def _resource_gate_data_dir(self):
        return getattr(self.key_pool, "data_dir", None) or "data"

    def _resource_gate_root(self):
        from pathlib import Path

        return Path(self._resource_gate_data_dir()) / "runtime_locks"

    def _semaphore_for_ref(self, ref: str, limit: int) -> threading.BoundedSemaphore:
        with self._key_semaphore_lock:
            current = self._key_semaphores.get(ref)
            current_limit = self._key_semaphore_limits.get(ref)
            if current is None or current_limit != limit:
                current = threading.BoundedSemaphore(limit)
                self._key_semaphores[ref] = current
                self._key_semaphore_limits[ref] = limit
            return current

    def _api_key_for_conversation(self, conversation_id: str) -> str | None:
        if conversation_id and self.channel_store is not None:
            key = self.channel_store.api_key_for_request(conversation_id)
            if key:
                return key
        return self.key_pool.default_key()

    def _extract_content(self, data: dict[str, object]) -> str:
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("missing choices in relay response")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError("invalid choice in relay response")
        message = choice.get("message", {})
        if not isinstance(message, dict):
            raise RuntimeError("invalid message in relay response")
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item) for item in content)
        return str(content)

    def _parse_json(self, content: str) -> dict[str, object]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _coerce_float(self, value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


def _conversation_id_from_prompt(prompt: str) -> str:
    match = re.search(r"conversation_id=([A-Za-z0-9_.-]+)", prompt)
    return match.group(1) if match else ""


def _pool_available_refs(pool: ApiKeyPool) -> list[str]:
    return pool.available_refs() if hasattr(pool, "available_refs") else []


def _provider_for_ref(pool: ApiKeyPool, ref: str, fallback: ProviderConfig) -> ProviderConfig:
    provider_for_ref = getattr(pool, "provider_for_ref", None)
    if callable(provider_for_ref):
        return provider_for_ref(ref)
    return fallback
