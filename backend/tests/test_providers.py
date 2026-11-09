import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import providers


def provider_config():
    return json.dumps([
        {"name": "secondary", "base_url": "https://secondary.example/v1", "api_key": "secondary-secret", "model": "model-b", "priority": 20, "max_requests_per_hour": 4},
        {"name": "primary", "base_url": "https://primary.example/v1", "api_key": "primary-secret", "model": "model-a", "priority": 10, "max_requests_per_hour": 2},
    ])


class ProviderRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        providers._provider_usage.clear()
        providers._provider_cooldowns.clear()

    async def test_registry_orders_providers_and_status_never_exposes_keys(self):
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": ""}):
            registry = providers._provider_registry()
            status = providers.provider_status()
        self.assertEqual([item["name"] for item in registry], ["primary", "secondary"])
        self.assertEqual(status["provider_count"], 2)
        self.assertEqual([item["name"] for item in status["providers"]], ["primary", "secondary"])
        status_text = json.dumps(status)
        self.assertNotIn("primary-secret", status_text)
        self.assertNotIn("secondary-secret", status_text)

    async def test_local_hourly_budget_is_enforced(self):
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": ""}):
            primary = providers._provider_registry()[0]
        self.assertTrue(providers._reserve_provider(primary))
        self.assertTrue(providers._reserve_provider(primary))
        self.assertFalse(providers._reserve_provider(primary))

    async def test_rate_limited_provider_fails_over_to_next_configured_endpoint(self):
        calls = []

        async def fake_call(provider, messages, *, temperature, json_mode, model):
            calls.append(provider["name"])
            if provider["name"] == "primary":
                request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "secondary provider response"

        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": ""}), patch("app.providers._call_model", new=AsyncMock(side_effect=fake_call)):
            result = await providers.request_completion([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "secondary provider response")
        self.assertEqual(calls, ["primary", "secondary"])
        self.assertGreater(providers._provider_cooldowns["primary"], time.monotonic())


    async def test_retry_attempts_consume_the_same_provider_budget(self):
        calls = []

        async def fake_call(provider, messages, *, temperature, json_mode, model):
            calls.append(provider["name"])
            if provider["name"] == "primary":
                request = httpx.Request("POST", "https://primary.example/v1/chat/completions")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError("upstream unavailable", request=request, response=response)
            return "secondary provider response"

        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": ""}), patch("app.providers._call_model", new=AsyncMock(side_effect=fake_call)):
            result = await providers.request_completion([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "secondary provider response")
        # Primary's local cap is two in the fixture, so the retry fits within its budget.
        self.assertEqual(calls, ["primary", "primary", "secondary"])
        self.assertEqual(len(providers._provider_usage["primary"]), 2)


    async def test_gemini_native_generate_content_adapter_maps_system_and_user_messages(self):
        captured = []
        payload = {"candidates": [{"content": {"parts": [{"text": "Gemini result"}]}}]}

        class FakeClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def post(self, url, *, headers, json):
                captured.append({"url": url, "headers": headers, "json": json})
                return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

        provider = {
            "name": "gemini", "protocol": "gemini_generate_content",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "gemini-secret", "model": "gemini-test", "max_output_tokens": 1000,
        }
        with patch("app.providers.httpx.AsyncClient", FakeClient):
            result = await providers._call_model(
                provider,
                [{"role": "system", "content": "You are a planner."}, {"role": "user", "content": "Make a plan."}],
                temperature=0.2, json_mode=True, model="gemini-test",
            )
        self.assertEqual(result, "Gemini result")
        self.assertEqual(captured[0]["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent")
        self.assertEqual(captured[0]["headers"]["x-goog-api-key"], "gemini-secret")
        self.assertNotIn("Authorization", captured[0]["headers"])
        self.assertEqual(captured[0]["json"]["systemInstruction"]["parts"][0]["text"], "You are a planner.")
        self.assertEqual(captured[0]["json"]["generationConfig"]["responseMimeType"], "application/json")

    async def test_anthropic_messages_adapter_maps_system_and_parses_text_blocks(self):
        captured = []
        payload = {"content": [{"type": "text", "text": "Claude result"}], "stop_reason": "end_turn"}

        class FakeClient:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def post(self, url, *, headers, json):
                captured.append({"url": url, "headers": headers, "json": json})
                return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

        provider = {
            "name": "anthropic", "protocol": "anthropic_messages",
            "base_url": "https://api.anthropic.com", "api_key": "anthropic-secret",
            "model": "claude-test", "api_version": "2026-10-21", "max_output_tokens": 1500,
        }
        with patch("app.providers.httpx.AsyncClient", FakeClient):
            result = await providers._call_model(
                provider,
                [{"role": "system", "content": "You are a reviewer."}, {"role": "user", "content": "Review this draft."}],
                temperature=0.1, json_mode=False, model="claude-test",
            )
        self.assertEqual(result, "Claude result")
        self.assertEqual(captured[0]["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured[0]["headers"]["x-api-key"], "anthropic-secret")
        self.assertEqual(captured[0]["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(captured[0]["json"]["system"], "You are a reviewer.")
        self.assertEqual(captured[0]["json"]["max_tokens"], 1500)


    async def test_explicit_connection_test_uses_small_budgeted_calls_without_leaking_keys(self):
        provider = {
            "name": "test-provider", "protocol": "openai_compatible",
            "base_url": "https://provider.example/v1", "api_key": "never-expose-this",
            "model": "test-model", "max_output_tokens": 4096, "max_requests_per_hour": 5,
        }
        with patch("app.providers._provider_registry", return_value=[provider]), patch(
            "app.providers._call_model", new=AsyncMock(return_value="OK")
        ) as call:
            result = await providers.test_provider_connections()
        self.assertEqual(result["provider_count"], 1)
        self.assertEqual(result["results"][0]["status"], "connected")
        self.assertEqual(call.await_args.kwargs["json_mode"], False)
        self.assertEqual(call.await_args.args[0]["max_output_tokens"], 32)
        self.assertNotIn("never-expose-this", json.dumps(result))
        self.assertEqual(len(providers._provider_usage["test-provider"]), 1)

    async def test_explicit_connection_test_skips_provider_over_local_budget(self):
        provider = {
            "name": "budgeted-provider", "protocol": "openai_compatible",
            "base_url": "https://provider.example/v1", "api_key": "secret",
            "model": "test-model", "max_requests_per_hour": 1,
        }
        providers._provider_usage["budgeted-provider"].append(time.monotonic())
        with patch("app.providers._provider_registry", return_value=[provider]), patch(
            "app.providers._call_model", new=AsyncMock(return_value="OK")
        ) as call:
            result = await providers.test_provider_connections()
        self.assertEqual(result["results"][0]["status"], "skipped")
        call.assert_not_awaited()

    async def test_legacy_single_endpoint_configuration_remains_supported(self):
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": "", "AI_API_KEY": "legacy-secret", "AI_API_BASE_URL": "https://legacy.example/v1", "AI_MODEL": "legacy-model"}):
            status = providers.provider_status()
            registry = providers._provider_registry()
        self.assertTrue(status["provider_configured"])
        self.assertEqual(registry[0]["name"], "default-provider")
        self.assertNotIn("legacy-secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
