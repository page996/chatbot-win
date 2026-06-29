from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_CHAT_MODEL = "gpt-5.5"
DEFAULT_IMAGE_MODELS = ""
DEFAULT_BASE_ENV = "BOT_RELAY_BASE_URL"
DEFAULT_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_CHAT_BASE_ENV = "BOT_RELAY_CHAT_BASE_URL"
DEFAULT_IMAGE_BASE_ENV = "BOT_RELAY_IMAGE_BASE_URL"
DEFAULT_IMAGE_KEY_ENV = "OPENAI_IMAGE_API_KEY"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"


@dataclass(frozen=True)
class EndpointResult:
    ok: bool
    model: str
    endpoint: str
    elapsed_seconds: float
    status: int | None
    summary: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test OpenAI-compatible relay connectivity without storing secrets."
    )
    parser.add_argument("--base-url", default=os.environ.get(DEFAULT_BASE_ENV), help=f"Shared relay base URL, or {DEFAULT_BASE_ENV}.")
    parser.add_argument("--chat-base-url", default=os.environ.get(DEFAULT_CHAT_BASE_ENV), help=f"Chat relay base URL, or {DEFAULT_CHAT_BASE_ENV}.")
    parser.add_argument("--image-base-url", default=os.environ.get(DEFAULT_IMAGE_BASE_ENV), help=f"Image relay base URL, or {DEFAULT_IMAGE_BASE_ENV}.")
    parser.add_argument("--api-key-env", default=DEFAULT_KEY_ENV, help="Fallback environment variable containing the API key.")
    parser.add_argument("--chat-api-key-env", default=DEFAULT_KEY_ENV, help="Environment variable containing the chat API key.")
    parser.add_argument("--image-api-key-env", default=DEFAULT_IMAGE_KEY_ENV, help="Environment variable containing the image API key.")
    parser.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL)
    parser.add_argument("--image-models", default=DEFAULT_IMAGE_MODELS, help="Comma-separated image model candidates. Leave empty to skip image checks.")
    parser.add_argument("--timeout", type=int, default=300, help="Per-request timeout in seconds.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="Request User-Agent. Some relays block Python's default UA.")
    parser.add_argument("--skip-chat", action="store_true", help="Skip chat connectivity checks.")
    parser.add_argument("--skip-image", action="store_true", help="Skip image connectivity checks.")
    return parser.parse_args()


def endpoint_bases(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return [base, base[:-3].rstrip("/")]
    return [f"{base}/v1", base]


def post_json(endpoint: str, api_key: str, payload: dict[str, Any], timeout: int, user_agent: str) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(data) if data else {}


def summarize_error(exc: BaseException) -> tuple[int | None, str]:
    if isinstance(exc, urllib.error.HTTPError):
        raw = exc.read().decode("utf-8", errors="replace")
        snippet = raw.replace("\r", " ").replace("\n", " ")[:900]
        return exc.code, f"HTTPError: {snippet}"
    if isinstance(exc, urllib.error.URLError):
        return None, f"URLError: {exc.reason}"
    return None, f"{exc.__class__.__name__}: {exc}"


def preview_text(value: Any, limit: int = 180) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def run_chat(base: str, api_key: str, model: str, timeout: int, user_agent: str) -> EndpointResult:
    endpoint = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a connectivity test assistant. Reply briefly.",
            },
            {
                "role": "user",
                "content": "Reply with PONG and one short sentence saying the model is available.",
            },
        ],
    }
    started = time.monotonic()
    try:
        status, data = post_json(endpoint, api_key, payload, timeout, user_agent)
        elapsed = time.monotonic() - started
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        keys = ",".join(sorted(data.keys()))
        return EndpointResult(
            ok=True,
            model=model,
            endpoint=endpoint,
            elapsed_seconds=elapsed,
            status=status,
            summary=f"keys=[{keys}], content={preview_text(content)}",
        )
    except BaseException as exc:
        elapsed = time.monotonic() - started
        status, summary = summarize_error(exc)
        return EndpointResult(False, model, endpoint, elapsed, status, summary)


def summarize_image_response(data: dict[str, Any]) -> str:
    keys = ",".join(sorted(data.keys()))
    items = data.get("data")
    if not isinstance(items, list) or not items:
        return f"keys=[{keys}], no image data array"
    first = items[0] if isinstance(items[0], dict) else {}
    item_keys = ",".join(sorted(first.keys()))
    has_url = bool(first.get("url"))
    b64_len = len(first.get("b64_json") or "")
    revised_prompt = preview_text(first.get("revised_prompt", ""), 120)
    return f"keys=[{keys}], item_keys=[{item_keys}], has_url={has_url}, b64_len={b64_len}, revised_prompt={revised_prompt}"


def run_image(base: str, api_key: str, model: str, timeout: int, user_agent: str) -> EndpointResult:
    endpoint = f"{base}/images/generations"
    payload = {
        "model": model,
        "prompt": "A simple blue square centered on a white background. Connectivity test image.",
        "size": "1024x1024",
        "n": 1,
    }
    started = time.monotonic()
    try:
        status, data = post_json(endpoint, api_key, payload, timeout, user_agent)
        elapsed = time.monotonic() - started
        return EndpointResult(
            ok=True,
            model=model,
            endpoint=endpoint,
            elapsed_seconds=elapsed,
            status=status,
            summary=summarize_image_response(data),
        )
    except BaseException as exc:
        elapsed = time.monotonic() - started
        status, summary = summarize_error(exc)
        return EndpointResult(False, model, endpoint, elapsed, status, summary)


def print_result(kind: str, result: EndpointResult) -> None:
    state = "PASS" if result.ok else "FAIL"
    status = result.status if result.status is not None else "n/a"
    print(f"[{state}] {kind} model={result.model!r} status={status} elapsed={result.elapsed_seconds:.1f}s")
    print(f"       endpoint={result.endpoint}")
    print(f"       {result.summary}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    chat_base_url = args.chat_base_url or args.base_url
    image_base_url = args.image_base_url or args.base_url
    if not args.skip_chat and not chat_base_url:
        print(f"Missing chat relay base URL. Set {DEFAULT_CHAT_BASE_ENV}, {DEFAULT_BASE_ENV}, or pass --chat-base-url.", file=sys.stderr)
        return 2
    image_models = [item.strip() for item in args.image_models.split(",") if item.strip()]
    image_requested = bool(image_models) and not args.skip_image
    if image_requested and not image_base_url:
        print(f"Missing image relay base URL. Set {DEFAULT_IMAGE_BASE_ENV}, {DEFAULT_BASE_ENV}, or pass --image-base-url.", file=sys.stderr)
        return 2
    chat_api_key = os.environ.get(args.chat_api_key_env) or os.environ.get(args.api_key_env)
    image_api_key = os.environ.get(args.image_api_key_env) or os.environ.get(args.api_key_env)
    if not args.skip_chat and not chat_api_key:
        print(f"Missing chat API key. Set {args.chat_api_key_env} or {args.api_key_env}.", file=sys.stderr)
        return 2
    if image_requested and not image_api_key:
        print(f"Missing image API key. Set {args.image_api_key_env} or {args.api_key_env}.", file=sys.stderr)
        return 2

    chat_bases = endpoint_bases(chat_base_url) if chat_base_url else []
    image_bases = endpoint_bases(image_base_url) if image_base_url else []
    results: list[EndpointResult] = []

    chat_passed = args.skip_chat
    if not args.skip_chat:
        print("== Chat connectivity ==")
        chat_passed = False
        for base in chat_bases:
            result = run_chat(base, chat_api_key, args.chat_model, args.timeout, args.user_agent)
            results.append(result)
            print_result("chat", result)
            if result.ok:
                chat_passed = True
                break

    image_passed = not image_requested
    if image_requested:
        print("== Image connectivity ==")
        image_passed = False
        for model in image_models:
            for base in image_bases:
                result = run_image(base, image_api_key, model, args.timeout, args.user_agent)
                results.append(result)
                print_result("image", result)
                if result.ok:
                    image_passed = True
                    break
            if image_passed:
                break

    if chat_passed and image_passed:
        print("Overall: PASS")
        return 0
    print("Overall: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
