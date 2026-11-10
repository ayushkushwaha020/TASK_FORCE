import asyncio
import json
import os
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
        providers._provider_cooldown_info.clear()
        providers._provider_next_request_at.clear()
        providers._provider_locks.clear()
        self.pacing_env = patch.dict(os.environ, {
            "AI_PROVIDER_MIN_INTERVAL_SECONDS": "0",
            "AI_PROVIDER_MAX_REQUESTS_PER_MINUTE": "120",
        })
        self.pacing_env.start()
        self.addCleanup(self.pacing_env.stop)

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


    async def test_429_retry_after_header_is_respected(self):
        request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
        response = httpx.Response(429, headers={"Retry-After": "37"}, request=request)
        self.assertEqual(providers._cooldown_seconds_from_response(response), 37)

    async def test_gemini_daily_quota_message_gets_longer_cooldown(self):
        request = httpx.Request("POST", "https://provider.example/generateContent")
        response = httpx.Response(
            429,
            json={"error": {"message": "Quota exceeded: requests per day limit has been reached."}},
            request=request,
        )
        with patch.dict(os.environ, {"AI_PROVIDER_DAILY_QUOTA_COOLDOWN_SECONDS": "1800"}):
            self.assertEqual(providers._cooldown_seconds_from_response(response), 1800)

    async def test_gemini_quota_reset_metadata_is_classified_as_daily_quota(self):
        request = httpx.Request("POST", "https://provider.example/generateContent")
        response = httpx.Response(429, json={
            "error": {
                "code": 429,
                "message": "Quota exhausted.",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    }]},
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED", "metadata": {
                        "quotaResetDelay": "51897s",
                    }},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
                ],
            }
        }, request=request)
        info = providers._provider_429_diagnostics(response)
        self.assertEqual(info["kind"], "daily_quota")
        self.assertEqual(info["label"], "Daily quota exhausted")
        self.assertEqual(info["quota_id"], "GenerateRequestsPerDayPerModel-FreeTier".replace("PerDayPerModel", "PerDayPerProjectPerModel"))
        self.assertEqual(info["retry_after_seconds"], 51897)
        self.assertEqual(providers._cooldown_seconds_from_response(response), 51897)

    async def test_request_completion_raises_structured_daily_quota_error(self):
        provider = {
            "name": "gemini-main", "protocol": "gemini_generate_content", "base_url": "https://gemini.example/v1beta",
            "api_key": "secret", "model": "gemini-test", "max_requests_per_hour": None,
            "max_requests_per_minute": 120, "min_request_interval_seconds": 0,
        }
        request = httpx.Request("POST", "https://gemini.example/v1beta/models/gemini-test:generateContent")
        response = httpx.Response(429, json={"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            }]},
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED", "metadata": {"quotaResetDelay": "51897s"}},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
        ]}}, request=request)
        error = httpx.HTTPStatusError("quota exhausted", request=request, response=response)
        with patch("app.providers._provider_registry", return_value=[provider]), patch(
            "app.providers._call_model", new=AsyncMock(side_effect=error)
        ):
            with self.assertRaises(providers.ProviderUnavailableError) as raised:
                await providers.request_completion([{"role": "user", "content": "hello"}])
            status = providers.provider_status()
        self.assertEqual(raised.exception.reason, "daily_quota")
        self.assertEqual(raised.exception.retry_after_seconds, 51897)
        self.assertEqual(status["providers"][0]["cooldown_info"]["label"], "Daily quota exhausted")

    async def test_daily_quota_ignores_misleading_one_second_retryinfo_without_reset_metadata(self):
        request = httpx.Request("POST", "https://provider.example/generateContent")
        response = httpx.Response(429, json={
            "error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                }]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"},
            ]}
        }, request=request)
        info = providers._provider_429_diagnostics(response)
        self.assertEqual(info["kind"], "daily_quota")
        self.assertGreater(info["retry_after_seconds"], 300)

    async def test_single_provider_calls_are_serialized(self):
        provider = {
            "name": "single", "protocol": "openai_compatible", "base_url": "https://single.example/v1",
            "api_key": "secret", "model": "model", "max_requests_per_hour": None,
            "max_requests_per_minute": 120, "min_request_interval_seconds": 0,
        }
        active = 0
        peak = 0
        async def fake_call(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "ok"
        with patch("app.providers._provider_registry", return_value=[provider]), patch(
            "app.providers._call_model", new=AsyncMock(side_effect=fake_call)
        ):
            results = await asyncio.gather(*[
                providers.request_completion([{"role": "user", "content": str(i)}]) for i in range(4)
            ])
        self.assertEqual(results, ["ok"] * 4)
        self.assertEqual(peak, 1)

    async def test_cooled_primary_is_skipped_and_secondary_is_used(self):
        calls = []
        async def fake_call(provider, messages, *, temperature, json_mode, model):
            calls.append(provider["name"])
            return "secondary response"
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": ""}), \
             patch("app.providers._provider_cooldowns", {"primary": time.monotonic() + 90}), \
             patch("app.providers._call_model", new=AsyncMock(side_effect=fake_call)):
            result = await providers.request_completion([{"role": "user", "content": "hello"}])
        self.assertEqual(result, "secondary response")
        self.assertEqual(calls, ["secondary"])

    async def test_provider_status_reports_non_secret_pacing_and_cooldown(self):
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": provider_config(), "AI_API_KEY": "", "AI_MODEL": "", "AI_PROVIDER_MAX_REQUESTS_PER_MINUTE": "4"}):
            providers._provider_cooldowns["primary"] = time.monotonic() + 25
            status = providers.provider_status()
        primary = next(item for item in status["providers"] if item["name"] == "primary")
        self.assertTrue(primary["cooling_down"])
        self.assertGreaterEqual(primary["cooldown_seconds"], 1)
        self.assertEqual(primary["max_requests_per_minute"], 4)
        self.assertEqual(status["available_provider_count"], 1)
        self.assertTrue(status["fallback_configured"])

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
