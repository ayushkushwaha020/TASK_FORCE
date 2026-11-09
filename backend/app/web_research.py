import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from .schemas import ResearchSource


def web_research_is_configured() -> bool:
    return bool(os.getenv("TAVILY_API_KEY", "").strip())


def research_settings() -> dict[str, int]:
    try:
        max_queries = int(os.getenv("TAVILY_MAX_QUERIES_PER_TASK", "4"))
        max_results = int(os.getenv("TAVILY_MAX_RESULTS_PER_QUERY", "4"))
    except ValueError as exc:
        raise ValueError("Tavily query and result limits must be integers.") from exc
    return {
        "max_queries": min(max(max_queries, 1), 4),
        "max_results": min(max(max_results, 1), 5),
    }


def build_research_queries(instruction: str, subtask_titles: list[str]) -> list[str]:
    """Create a small deterministic query set; the search provider never chooses scope."""
    base = " ".join(instruction.split())[:360]
    queries = [base]
    for title in subtask_titles:
        suffix = " ".join(title.split())[:140]
        if suffix:
            queries.append(f"{base[:200]} {suffix}")
    unique: list[str] = []
    seen: set[str] = set()
    max_queries = research_settings()["max_queries"]
    for query in queries:
        key = query.casefold().strip()
        if key and key not in seen:
            unique.append(query[:500])
            seen.add(key)
        if len(unique) >= max_queries:
            break
    return unique


async def _search_one(client: httpx.AsyncClient, query: str, max_results: int) -> dict[str, Any]:
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    url = os.getenv("TAVILY_SEARCH_URL", "https://api.tavily.com/search").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("TAVILY_SEARCH_URL must be an HTTP or HTTPS URL.")
    response = await client.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "query": query,
            "search_depth": "basic",
            "chunks_per_source": 2,
            "max_results": max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_usage": True,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Web search returned an unexpected response.")
    raw_results = payload.get("results", [])
    if not isinstance(raw_results, list):
        raise ValueError("Web search response did not contain a results array.")
    results = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()[:300]
        result_url = str(item.get("url", "")).strip()[:2000]
        snippet = str(item.get("content", "")).strip()[:1800]
        parsed_result = urlparse(result_url)
        if not title or not snippet or parsed_result.scheme not in ("https", "http") or not parsed_result.netloc:
            continue
        score_value = item.get("score")
        try:
            score = float(score_value) if score_value is not None else None
        except (TypeError, ValueError):
            score = None
        published = item.get("published_date") or item.get("publishedDate")
        results.append({
            "title": title,
            "url": result_url,
            "snippet": snippet,
            "score": score,
            "published_date": str(published)[:80] if published else None,
        })
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    credits = usage.get("credits", 0)
    try:
        credits = max(0, int(credits or 0))
    except (TypeError, ValueError):
        credits = 0
    return {"query": query, "results": results, "credits": credits}


async def collect_research(instruction: str, subtask_titles: list[str]) -> dict[str, Any]:
    """Search a bounded number of queries and return source snippets with traceable URLs.

    Search snippets are evidence leads, not full-page extraction or independent verification.
    """
    if not web_research_is_configured():
        return {
            "status": "not_configured",
            "notice": "TAVILY_API_KEY is not configured; no live web search was performed.",
            "sources": [],
            "credits": 0,
            "errors": [],
        }
    queries = build_research_queries(instruction, subtask_titles)
    settings = research_settings()
    timeout = float(os.getenv("TAVILY_TIMEOUT_SECONDS", "25"))
    semaphore = asyncio.Semaphore(2)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def bounded(query: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await _search_one(client, query, settings["max_results"])
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    return {"query": query, "results": [], "credits": 0, "error": f"HTTP {status}"}
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    return {"query": query, "results": [], "credits": 0, "error": type(exc).__name__}
                except Exception as exc:
                    return {"query": query, "results": [], "credits": 0, "error": type(exc).__name__}
        batches = await asyncio.gather(*(bounded(query) for query in queries))

    seen_urls: set[str] = set()
    sources: list[ResearchSource] = []
    credits = 0
    errors: list[str] = []
    for batch in batches:
        credits += batch.get("credits", 0)
        if batch.get("error"):
            errors.append(f"{batch['query']}: {batch['error']}")
        for item in batch.get("results", []):
            url_key = item["url"].rstrip("/").casefold()
            if url_key in seen_urls:
                continue
            seen_urls.add(url_key)
            sources.append(ResearchSource(
                source_id=f"S{len(sources) + 1}",
                query=batch["query"],
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                score=item.get("score"),
                published_date=item.get("published_date"),
            ))
    if errors and sources:
        status = "partial"
        notice = f"Live web search returned {len(sources)} source snippets with {len(errors)} query error(s). Snippets are not full-page verification."
    elif errors:
        status = "failed"
        notice = "Live web search did not return sources. " + "; ".join(errors[:3])
    else:
        status = "completed"
        notice = f"Collected {len(sources)} source snippets from {len(queries)} bounded search queries. Snippets are leads, not full-page verification."
    return {"status": status, "notice": notice, "sources": sources, "credits": credits, "errors": errors, "queries": queries}
