import asyncio
import json
import os
import time
from collections import defaultdict, deque
from typing import Any

import httpx


_provider_usage: dict[str, deque[float]] = defaultdict(deque)
_provider_cooldowns: dict[str, float] = {}
_usage_window_seconds = 3600


def _provider_registry() -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    raw = os.getenv("AI_PROVIDERS_JSON", "").strip()
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("AI_PROVIDERS_JSON must be a JSON array of provider objects.")
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"AI_PROVIDERS_JSON entry {index + 1} must be an object.")
            name = str(item.get("name", f"provider-{index + 1}")).strip()
            api_key = str(item.get("api_key", "")).strip()
            base_url = str(item.get("base_url", "")).strip().rstrip("/")
            model = str(item.get("model", "")).strip()
            if not name or not api_key or not base_url or not model:
                raise ValueError(f"Provider entry {index + 1} needs name, api_key, base_url, and model.")
            cap = item.get("max_requests_per_hour")
            if cap is not None and (not isinstance(cap, int) or cap < 1 or cap > 10000):
                raise ValueError(f"Provider {name} max_requests_per_hour must be between 1 and 10000.")
            providers.append({
                "name": name, "api_key": api_key, "base_url": base_url, "model": model,
                "fallback_model": str(item.get("fallback_model", "")).strip(),
                "priority": int(item.get("priority", 100)),
                "max_requests_per_hour": cap,
            })
    if os.getenv("AI_API_KEY", "").strip() and os.getenv("AI_MODEL", "").strip():
        providers.append({
            "name": os.getenv("AI_PROVIDER_NAME", "default-compatible").strip() or "default-compatible",
            "api_key": os.getenv("AI_API_KEY", "").strip(),
            "base_url": os.getenv("AI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            "model": os.getenv("AI_MODEL", "").strip(),
            "fallback_model": os.getenv("AI_FALLBACK_MODEL", "").strip(),
            "priority": int(os.getenv("AI_PROVIDER_PRIORITY", "10")),
            "max_requests_per_hour": None,
        })
    return sorted(providers, key=lambda item: item["priority"])


def provider_is_configured() -> bool:
    try:
        return bool(_provider_registry())
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def provider_status() -> dict[str, Any]:
    try:
        providers = _provider_registry()
        usage = []
        now = time.monotonic()
        for provider in providers:
            bucket = _provider_usage[provider["name"]]
            while bucket and now - bucket[0] > _usage_window_seconds:
                bucket.popleft()
            usage.append({
                "name": provider["name"],
                "model": provider["model"],
                "max_requests_per_hour": provider["max_requests_per_hour"],
                "requests_this_hour": len(bucket),
                "cooling_down": _provider_cooldowns.get(provider["name"], 0) > now,
            })
        return {
            "provider_configured": bool(providers),
            "provider_type": "openai-compatible-router" if providers else None,
            "provider_count": len(providers),
            "providers": usage,
            "fallback_configured": any(p.get("fallback_model") for p in providers),
            "credentials_exposed": False,
        }
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "provider_configured": False, "provider_type": None, "provider_count": 0,
            "providers": [], "fallback_configured": False, "credentials_exposed": False,
            "configuration_error": str(exc)[:240],
        }


def _reserve_provider(provider: dict[str, Any]) -> bool:
    now = time.monotonic()
    name = provider["name"]
    if _provider_cooldowns.get(name, 0) > now:
        return False
    bucket = _provider_usage[name]
    while bucket and now - bucket[0] > _usage_window_seconds:
        bucket.popleft()
    cap = provider.get("max_requests_per_hour")
    if cap is not None and len(bucket) >= cap:
        return False
    bucket.append(now)
    return True


async def _call_model(
    provider: dict[str, Any], messages: list[dict[str, str]], *, temperature: float, json_mode: bool, model: str
) -> str:
    timeout = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "60"))
    body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    url = provider["base_url"] + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=body)
        # Some OpenAI-compatible endpoints do not implement response_format.
        if json_mode and response.status_code in (400, 422):
            body.pop("response_format", None)
            response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Provider returned an unexpected chat-completion response.") from exc
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Provider returned an empty response.")
    return text.strip()


async def request_completion(
    messages: list[dict[str, str]], *, temperature: float = 0.2, json_mode: bool = False
) -> str:
    """Route to configured endpoints in priority order, budgeting each actual HTTP attempt."""
    try:
        providers = _provider_registry()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider configuration is invalid ({type(exc).__name__}).") from exc
    if not providers:
        raise RuntimeError("Configure AI_PROVIDERS_JSON or AI_API_KEY and AI_MODEL on the backend.")
    errors: list[str] = []
    for provider in providers:
        models = [provider["model"]]
        fallback_model = provider.get("fallback_model", "")
        if fallback_model and fallback_model != provider["model"]:
            models.append(fallback_model)
        provider_failed = False
        for model_index, model in enumerate(models):
            for attempt in range(2):
                # Count every underlying provider request, including retries and fallback models.
                if not _reserve_provider(provider):
                    errors.append(provider["name"] + ": cooling down or local request budget reached")
                    provider_failed = True
                    break
                try:
                    return await _call_model(
                        provider, messages, temperature=temperature,
                        json_mode=json_mode, model=model,
                    )
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status == 429:
                        # Respect the endpoint's rate limit and move to another configured provider.
                        _provider_cooldowns[provider["name"]] = time.monotonic() + 60
                        errors.append(provider["name"] + ": provider rate limit response")
                        provider_failed = True
                        break
                    errors.append(provider["name"] + f": HTTP {status}")
                    if status >= 500 and attempt == 0:
                        await asyncio.sleep(0.6)
                        continue
                    if model_index + 1 < len(models):
                        break
                    provider_failed = True
                    break
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.6)
                        continue
                    errors.append(provider["name"] + f": {type(exc).__name__}")
                    provider_failed = True
                    break
                except Exception as exc:
                    errors.append(provider["name"] + f": {type(exc).__name__}")
                    provider_failed = True
                    break
            if provider_failed:
                break
    detail = "; ".join(errors[-6:]) or "No configured provider is currently available."
    raise RuntimeError("All configured providers failed or reached their configured limits. " + detail)


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
    content = await request_completion(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": instruction}],
        temperature=0.2, json_mode=True,
    )
    parsed = parse_json_object(content)
    if not isinstance(parsed.get("agents"), list) or not isinstance(parsed.get("subtasks"), list):
        raise ValueError("Provider returned a task plan without agent and subtask arrays.")
    return parsed
