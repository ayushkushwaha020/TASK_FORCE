import asyncio
import json
import os
from typing import Any

import httpx


def provider_is_configured() -> bool:
    return bool(os.getenv("AI_API_KEY", "").strip() and os.getenv("AI_MODEL", "").strip())


def provider_status() -> dict[str, Any]:
    return {
        "provider_configured": provider_is_configured(),
        "provider_type": "openai-compatible" if provider_is_configured() else None,
        "fallback_configured": bool(os.getenv("AI_FALLBACK_MODEL", "").strip()),
        "credentials_exposed": False,
    }


async def _call_model(model: str, messages: list[dict[str, str]], *, temperature: float, json_mode: bool) -> str:
    api_key = os.getenv("AI_API_KEY", "").strip()
    base_url = os.getenv("AI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    timeout = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "60"))
    if not api_key:
        raise RuntimeError("AI_API_KEY is not configured.")
    body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(f"{base_url}/chat/completions", headers=headers, json=body)
        if json_mode and response.status_code in (400, 422):
            body.pop("response_format", None)
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("AI provider returned an unexpected chat-completion response.") from exc
    if not isinstance(text, str) or not text.strip():
        raise ValueError("AI provider returned an empty response.")
    return text.strip()


async def request_completion(messages: list[dict[str, str]], *, temperature: float = 0.2, json_mode: bool = False) -> str:
    """Call the configured model, with a configured fallback model when available."""
    primary = os.getenv("AI_MODEL", "").strip()
    fallback = os.getenv("AI_FALLBACK_MODEL", "").strip()
    if not provider_is_configured():
        raise RuntimeError("AI_API_KEY and AI_MODEL must be configured on the backend.")
    last_error: Exception | None = None
    models = [primary] + ([fallback] if fallback and fallback != primary else [])
    for model in models:
        for attempt in range(2):
            try:
                return await _call_model(model, messages, temperature=temperature, json_mode=json_mode)
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status is not None and status < 500 and status != 429:
                    break
                if attempt == 0:
                    await asyncio.sleep(0.7)
            except Exception as exc:
                last_error = exc
                break
    raise RuntimeError(f"AI provider request failed after bounded retry/fallback ({type(last_error).__name__}).") from last_error


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain a JSON object.")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response JSON must be an object.")
    return parsed


async def request_json_plan(instruction: str) -> dict[str, Any]:
    system_prompt = """You are the planning director for a multi-agent AI workforce.
Return valid JSON with keys: title, category, agents, subtasks.
agents must contain 3 to 20 objects with keys name, role, personality, task, emoji.
Always include exactly one independent QA / Verification specialist.
Use distinct roles appropriate to the task, including a director/manager and relevant specialists.
subtasks must contain 3 to 12 objects with keys title and owner.
Assign each subtask owner to an agent name from the team.
Do not claim tasks are executed, researched, tested, or verified.
Keep the plan actionable and grounded in the user's instruction."""
    content = await request_completion([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": instruction},
    ], temperature=0.2, json_mode=True)
    parsed = parse_json_object(content)
    if not isinstance(parsed.get("agents"), list) or not isinstance(parsed.get("subtasks"), list):
        raise ValueError("Provider returned a task plan without agent and subtask arrays.")
    return parsed
