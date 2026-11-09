import json
import os
import unittest
from unittest.mock import patch

import httpx

from app import web_research


class FakeTavilyResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TavilyResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_searches_are_capped_deduplicated_and_return_traceable_sources(self):
        calls = []

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, *, headers, json):
                calls.append({"url": url, "headers": headers, "body": json})
                result_url = (
                    "https://example.org/research-methods"
                    if len(calls) < 3
                    else "https://example.org/independent-source"
                )
                return FakeTavilyResponse({
                    "results": [{
                        "title": "Research source " + str(len(calls)),
                        "url": result_url,
                        "content": "A short source snippet with relevant detail.",
                        "score": 0.81,
                    }],
                    "usage": {"credits": 1},
                })

        env = {
            "TAVILY_API_KEY": "tavily-secret",
            "TAVILY_MAX_QUERIES_PER_TASK": "4",
            "TAVILY_MAX_RESULTS_PER_QUERY": "5",
        }
        with patch.dict(os.environ, env), patch("app.web_research.httpx.AsyncClient", FakeClient):
            result = await web_research.collect_research(
                "Research reliable methods for evaluating AI agent teams.",
                ["compare evaluation methods", "identify limitations"],
            )

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["url"] == "https://api.tavily.com/search" for call in calls))
        self.assertTrue(all(call["headers"]["Authorization"] == "Bearer tavily-secret" for call in calls))
        self.assertTrue(all(call["body"]["search_depth"] == "basic" for call in calls))
        self.assertTrue(all(call["body"]["include_raw_content"] is False for call in calls))
        self.assertTrue(all(call["body"]["max_results"] == 5 for call in calls))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["credits"], 3)
        self.assertEqual([source.source_id for source in result["sources"]], ["S1", "S2"])
        self.assertEqual(len(result["sources"]), 2)
        safe_result = {**result, "sources": [source.model_dump() for source in result["sources"]]}
        self.assertNotIn("tavily-secret", json.dumps(safe_result))
        self.assertEqual(result["sources"][0].url, "https://example.org/research-methods")

    async def test_search_query_count_is_capped_at_four(self):
        with patch.dict(os.environ, {"TAVILY_MAX_QUERIES_PER_TASK": "12"}):
            queries = web_research.build_research_queries(
                "Find current research about multi-agent AI workforces.",
                [f"workstream {index}" for index in range(10)],
            )
        self.assertEqual(len(queries), 4)

    async def test_missing_api_key_returns_explicit_not_configured_state(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            result = await web_research.collect_research(
                "Research AI agent evaluation methods.",
                ["evaluation metrics"],
            )
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["sources"], [])
        self.assertIn("no live web search was performed", result["notice"])


if __name__ == "__main__":
    unittest.main()
