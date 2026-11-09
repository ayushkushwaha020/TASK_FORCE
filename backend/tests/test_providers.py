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

    async def test_legacy_single_endpoint_configuration_remains_supported(self):
        with patch.dict("os.environ", {"AI_PROVIDERS_JSON": "", "AI_API_KEY": "legacy-secret", "AI_API_BASE_URL": "https://legacy.example/v1", "AI_MODEL": "legacy-model"}):
            status = providers.provider_status()
            registry = providers._provider_registry()
        self.assertTrue(status["provider_configured"])
        self.assertEqual(registry[0]["name"], "default-compatible")
        self.assertNotIn("legacy-secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
