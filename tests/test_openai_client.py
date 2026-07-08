from __future__ import annotations

import json
import unittest

from app.personal_wechat_bot.config.schema import ProviderConfig
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool
from app.personal_wechat_bot.llm.openai_client import (
    RelayOpenAIClient,
    normalize_openai_base_url,
    validate_endpoint_url,
)


class OpenAIClientCompatibilityTest(unittest.TestCase):
    def test_normalize_base_url_adds_v1_when_missing(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://relay.example.com"),
            "https://relay.example.com/v1",
        )

    def test_normalize_base_url_keeps_existing_v1(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://relay.example.com/v1/"),
            "https://relay.example.com/v1",
        )

    def test_normalize_base_url_keeps_deepseek_official_root(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.deepseek.com", provider="deepseek"),
            "https://api.deepseek.com",
        )

    def test_normalize_base_url_strips_deepseek_v1_suffix(self) -> None:
        self.assertEqual(
            normalize_openai_base_url("https://api.deepseek.com/v1/", provider="deepseek"),
            "https://api.deepseek.com",
        )


class EndpointUrlValidationTest(unittest.TestCase):
    def test_accepts_http_and_https_with_host(self) -> None:
        self.assertEqual(validate_endpoint_url("https://relay.example.com/v1/chat/completions"), "")
        self.assertEqual(validate_endpoint_url("http://127.0.0.1:8000/v1/chat/completions"), "")

    def test_rejects_non_http_scheme(self) -> None:
        self.assertIn("unsupported_url_scheme", validate_endpoint_url("file:///etc/passwd"))
        self.assertIn("unsupported_url_scheme", validate_endpoint_url("ftp://host/x"))

    def test_rejects_missing_host(self) -> None:
        self.assertEqual(validate_endpoint_url("https:///v1"), "missing_url_host")

    def test_chat_completion_rejects_bad_base_url_before_key_use(self) -> None:
        # A non-http(s) base_url must fail fast with a validation error, never
        # reaching the network with the API key attached.
        config = ProviderConfig(
            provider="relay",
            model="test-model",
            base_url="file:///etc/passwd",
            api_key_env="TEST_KEY",
        )

        class _StubPool:
            def default_key(self) -> str:
                raise AssertionError("key must not be resolved for an invalid base_url")

        client = RelayOpenAIClient(config, key_pool=_StubPool())
        with self.assertRaises(RuntimeError) as ctx:
            client._chat_completion([{"role": "user", "content": "hi"}])
        self.assertIn("invalid base_url", str(ctx.exception))


class KeyFailoverTest(unittest.TestCase):
    def _client_with_two_keys(self, tmp: str) -> tuple[object, list[str]]:
        from pathlib import Path

        key_file = Path(tmp) / "keys.md"
        key_file.write_text("K1 = bad-key\nK2 = good-key\n", encoding="utf-8")
        provider = ProviderConfig(
            provider="relay",
            model="m",
            base_url="https://relay.example.com",
            api_key_env="",
            api_key_file="keys.md",
        )
        pool = ApiKeyPool(provider, tmp)
        return RelayOpenAIClient(provider, key_pool=pool), []

    def test_fails_over_to_next_key_on_401_and_retires_bad_key(self) -> None:
        import tempfile
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._client_with_two_keys(tmp)
            used_keys: list[str] = []

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

            def fake_urlopen(req, timeout=None):
                auth = req.headers["Authorization"]
                used_keys.append(auth)
                if auth.endswith("bad-key"):
                    raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
                return _FakeResp()

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                data = client._chat_completion([{"role": "user", "content": "hi"}])
            finally:
                urllib.request.urlopen = orig

            self.assertEqual(client._extract_content(data), "ok")
            # Tried the bad key, then failed over to the good one.
            self.assertTrue(any("bad-key" in k for k in used_keys))
            self.assertTrue(any("good-key" in k for k in used_keys))

    def test_all_keys_rejected_raises_after_bounded_attempts(self) -> None:
        import tempfile
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._client_with_two_keys(tmp)
            calls: list[str] = []

            def fake_urlopen(req, timeout=None):
                calls.append(req.headers["Authorization"])
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    client._chat_completion([{"role": "user", "content": "hi"}])
            finally:
                urllib.request.urlopen = orig

            self.assertIn("exhausted", str(ctx.exception))
            # Bounded: each of the 2 keys tried exactly once, no infinite loop.
            self.assertEqual(len(calls), 2)

    def test_non_auth_error_propagates_without_failover(self) -> None:
        import tempfile
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._client_with_two_keys(tmp)
            calls: list[str] = []

            def fake_urlopen(req, timeout=None):
                calls.append(req.headers["Authorization"])
                raise urllib.error.HTTPError(req.full_url, 500, "server error", {}, None)

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                with self.assertRaises(urllib.error.HTTPError):
                    client._chat_completion([{"role": "user", "content": "hi"}])
            finally:
                urllib.request.urlopen = orig

            # A 5xx is not a key problem: fail fast on the first key, no failover.
            self.assertEqual(len(calls), 1)

    def test_retired_key_is_removed_from_candidates_until_cooldown_expires(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client, _ = self._client_with_two_keys(tmp)
            refs = client.key_pool.available_refs()
            client._retire_key(refs[0])

            candidates = client._candidate_refs("conversation-1")

            self.assertNotIn(refs[0], candidates)
            self.assertIn(refs[1], candidates)

    def test_each_key_uses_its_own_provider_config(self) -> None:
        import tempfile
        import urllib.request
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "keys.md"
            key_file.write_text("K1 = key-one\nK2 = key-two\n", encoding="utf-8")
            provider = ProviderConfig(
                provider="relay",
                model="fallback-model",
                base_url="https://fallback.example/v1",
                api_key_env="",
                api_key_file="keys.md",
            )
            pool = ApiKeyPool(provider, tmp)
            refs = pool.available_refs()
            pool.set_key_model_config(refs[0], provider="relay", model="model-one", base_url="https://one.example/v1")
            pool.set_key_model_config(refs[1], provider="deepseek", model="model-two", base_url="https://api.deepseek.com")
            client = RelayOpenAIClient(provider, key_pool=pool)
            seen: list[tuple[str, str, str]] = []

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

            def fake_urlopen(req, timeout=None):
                body = json.loads(req.data.decode("utf-8"))
                seen.append((req.full_url, body["model"], req.headers["Authorization"]))
                return _FakeResp()

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                client._chat_completion([{"role": "user", "content": "hi"}], conversation_id="conversation-a")
                client._retire_key(refs[0])
                client._chat_completion([{"role": "user", "content": "hi"}], conversation_id="conversation-a")
            finally:
                urllib.request.urlopen = orig

            self.assertEqual(seen[0][0], "https://one.example/v1/chat/completions")
            self.assertEqual(seen[0][1], "model-one")
            self.assertEqual(seen[1][0], "https://api.deepseek.com/chat/completions")
            self.assertEqual(seen[1][1], "model-two")

    def test_single_cooling_key_fails_without_network_retry(self) -> None:
        import tempfile
        import urllib.request
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "keys.md"
            key_file.write_text("K1 = only-key\n", encoding="utf-8")
            provider = ProviderConfig(
                provider="relay",
                model="m",
                base_url="https://relay.example.com",
                api_key_env="",
                api_key_file="keys.md",
            )
            client = RelayOpenAIClient(provider, key_pool=ApiKeyPool(provider, tmp))
            ref = client.key_pool.available_refs()[0]
            client._retire_key(ref)
            calls = {"n": 0}

            def fake_urlopen(req, timeout=None):
                calls["n"] += 1
                raise AssertionError("cooling key must not be retried")

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    client._chat_completion([{"role": "user", "content": "hi"}])
            finally:
                urllib.request.urlopen = orig

            self.assertIn("cooling down", str(ctx.exception))
            self.assertEqual(calls["n"], 0)


class LlmResourceGateTest(unittest.TestCase):
    def test_chat_completion_uses_workload_schedule_for_llm_gate(self) -> None:
        import tempfile
        import urllib.request
        from pathlib import Path
        from unittest import mock

        from app.personal_wechat_bot.runtime.resource_scheduler import ResourceSchedule

        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "keys.md"
            key_file.write_text("K1 = key-one\n", encoding="utf-8")
            provider = ProviderConfig(
                provider="relay",
                model="m",
                base_url="https://relay.example.com",
                api_key_env="",
                api_key_file="keys.md",
            )
            scheduler = _RecordingScheduler()
            client = RelayOpenAIClient(provider, key_pool=ApiKeyPool(provider, tmp), resource_scheduler=scheduler)

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

            def fake_urlopen(req, timeout=None):
                return _FakeResp()

            leases: list[tuple[str, int, int, str]] = []

            class _Lease:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def fake_acquire_llm(
                *,
                workload,
                max_parallel=None,
                total_max_parallel=None,
                reason="",
                root=None,
                timeout_seconds=None,
            ):
                leases.append((workload, int(max_parallel or 0), int(total_max_parallel or 0), reason))
                return _Lease()

            orig = urllib.request.urlopen
            urllib.request.urlopen = fake_urlopen
            try:
                with mock.patch("app.personal_wechat_bot.llm.openai_client.acquire_llm", side_effect=fake_acquire_llm):
                    client._chat_completion([{"role": "user", "content": "hi"}], workload="background", conversation_id="c1")
            finally:
                urllib.request.urlopen = orig

            self.assertEqual(scheduler.workloads, ["background"])
            self.assertEqual(leases, [("background", 3, 10, "llm:background:c1")])


class _RecordingScheduler:
    def __init__(self):
        self.workloads: list[str] = []

    def conversation_parallelism(self, workload: str) -> object:
        from app.personal_wechat_bot.runtime.resource_scheduler import ResourceSchedule

        self.workloads.append(workload)
        return ResourceSchedule(
            workload="background" if workload == "background" else "interactive",
            max_parallel_conversations=3 if workload == "background" else 7,
            llm_total=10,
            llm_interactive=7,
            llm_background=3,
            media_cpu=2,
            file_io=1,
            gpu_media=1,
        )


if __name__ == "__main__":
    unittest.main()
