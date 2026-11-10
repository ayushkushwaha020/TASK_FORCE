import asyncio
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


_provider_usage: dict[str, deque[float]] = defaultdict(deque)
_provider_cooldowns: dict[str, float] = {}
_provider_next_request_at: dict[str, float] = {}
_provider_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_usage_window_seconds = 3600
_minute_window_seconds = 60


class ProviderUnavailableError(RuntimeError):
    """All configured providers are cooling down or have exhausted local budgets."""

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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
            protocol = str(item.get("protocol", "openai_compatible")).strip().lower()
            if protocol not in ("openai_compatible", "anthropic_messages", "gemini_generate_content"):
                raise ValueError(f"Provider {name} uses an unsupported protocol: {protocol}.")
            if not name or not api_key or not base_url or not model:
                raise ValueError(f"Provider entry {index + 1} needs name, api_key, base_url, and model.")
            cap = item.get("max_requests_per_hour")
            rpm = item.get("max_requests_per_minute", os.getenv("AI_PROVIDER_MAX_REQUESTS_PER_MINUTE", "4"))
            interval = item.get("min_request_interval_seconds", os.getenv("AI_PROVIDER_MIN_INTERVAL_SECONDS", "15"))
            try:
                rpm = int(rpm)
                interval = float(interval)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Provider {name} has invalid request pacing settings.") from exc
            if rpm < 1 or rpm > 120:
                raise ValueError(f"Provider {name} max_requests_per_minute must be between 1 and 120.")
            if interval < 0 or interval > 120:
                raise ValueError(f"Provider {name} min_request_interval_seconds must be between 0 and 120.")
            max_output_tokens = item.get("max_output_tokens", 4096)
            max_output_field = str(item.get("max_output_field", "max_tokens"))
            if not isinstance(max_output_tokens, int) or max_output_tokens < 256 or max_output_tokens > 65536:
                raise ValueError(f"Provider {name} max_output_tokens must be between 256 and 65536.")
            if max_output_field not in ("max_tokens", "max_completion_tokens"):
                raise ValueError(f"Provider {name} max_output_field must be max_tokens or max_completion_tokens.")
            if cap is not None and (not isinstance(cap, int) or cap < 1 or cap > 10000):
                raise ValueError(f"Provider {name} max_requests_per_hour must be between 1 and 10000.")
            if not base_url.startswith(("https://", "http://")):
                raise ValueError(f"Provider {name} base_url must use HTTP or HTTPS.")
            if any(existing["name"] == name for existing in providers):
                raise ValueError(f"Provider names must be unique; duplicate name: {name}.")
            providers.append({
                "name": name, "api_key": api_key, "base_url": base_url, "model": model,
                "protocol": protocol,
                "api_version": str(item.get("api_version", "2026-10-21")),
                "max_output_tokens": max_output_tokens,
                "max_output_field": max_output_field,
                "fallback_model": str(item.get("fallback_model", "")).strip(),
                "priority": int(item.get("priority", 100)),
                "max_requests_per_hour": cap,
                "max_requests_per_minute": rpm,
                "min_request_interval_seconds": interval,
            })
    if os.getenv("AI_API_KEY", "").strip() and os.getenv("AI_MODEL", "").strip():
        protocol = os.getenv("AI_PROVIDER_PROTOCOL", "openai_compatible").strip().lower()
        if protocol not in ("openai_compatible", "anthropic_messages", "gemini_generate_content"):
            raise ValueError(f"AI_PROVIDER_PROTOCOL is unsupported: {protocol}.")
        default_bases = {
            "openai_compatible": "https://api.openai.com/v1",
            "anthropic_messages": "https://api.anthropic.com",
            "gemini_generate_content": "https://generativelanguage.googleapis.com/v1beta",
        }
        name = os.getenv("AI_PROVIDER_NAME", "default-provider").strip() or "default-provider"
        if any(existing["name"] == name for existing in providers):
            raise ValueError(f"Provider names must be unique; duplicate name: {name}.")
        providers.append({
            "name": name,
            "api_key": os.getenv("AI_API_KEY", "").strip(),
            "base_url": os.getenv("AI_API_BASE_URL", default_bases[protocol]).rstrip("/"),
            "model": os.getenv("AI_MODEL", "").strip(),
            "protocol": protocol,
            "api_version": os.getenv("AI_API_VERSION", "2026-10-21"),
            "max_output_tokens": int(os.getenv("AI_MAX_OUTPUT_TOKENS", "4096")),
            "max_output_field": os.getenv("AI_MAX_OUTPUT_FIELD", "max_tokens"),
            "fallback_model": os.getenv("AI_FALLBACK_MODEL", "").strip(),
            "priority": int(os.getenv("AI_PROVIDER_PRIORITY", "10")),
            "max_requests_per_hour": None,
            "max_requests_per_minute": int(os.getenv("AI_PROVIDER_MAX_REQUESTS_PER_MINUTE", "4")),
            "min_request_interval_seconds": float(os.getenv("AI_PROVIDER_MIN_INTERVAL_SECONDS", "15")),
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
        ready_count = 0
        retry_delays: list[int] = []
        for provider in providers:
            bucket = _provider_usage[provider["name"]]
            while bucket and now - bucket[0] > _usage_window_seconds:
                bucket.popleft()
            minute_count = sum(1 for stamp in bucket if now - stamp <= _minute_window_seconds)
            cooldown_seconds = max(0, int(_provider_cooldowns.get(provider["name"], 0) - now + 0.999))
            hourly_limited = provider.get("max_requests_per_hour") is not None and len(bucket) >= provider["max_requests_per_hour"]
            minute_limited = minute_count >= provider.get("max_requests_per_minute", 4)
            ready = cooldown_seconds == 0 and not hourly_limited and not minute_limited
            if ready:
                ready_count += 1
            elif cooldown_seconds:
                retry_delays.append(cooldown_seconds)
            elif minute_limited and bucket:
                retry_delays.append(max(1, int(bucket[-provider.get("max_requests_per_minute", 4)] + _minute_window_seconds - now + 0.999)))
            usage.append({
                "name": provider["name"],
                "model": provider["model"],
                "protocol": provider["protocol"],
                "max_requests_per_hour": provider["max_requests_per_hour"],
                "requests_this_hour": len(bucket),
                "requests_this_minute": minute_count,
                "max_requests_per_minute": provider.get("max_requests_per_minute", 4),
                "min_request_interval_seconds": provider.get("min_request_interval_seconds", 15),
                "cooling_down": cooldown_seconds > 0,
                "cooldown_seconds": cooldown_seconds,
                "ready": ready,
            })
        return {
            "provider_configured": bool(providers),
            "provider_type": "multi-protocol-router" if providers else None,
            "provider_count": len(providers),
            "providers": usage,
            "available_provider_count": ready_count,
            "retry_after_seconds": min(retry_delays) if retry_delays else None,
            "fallback_configured": len(providers) > 1 or any(p.get("fallback_model") for p in providers),
            "credentials_exposed": False,
        }
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "provider_configured": False, "provider_type": None, "provider_count": 0,
            "providers": [], "available_provider_count": 0, "retry_after_seconds": None,
            "fallback_configured": False, "credentials_exposed": False,
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
    minute_cap = provider.get("max_requests_per_minute", int(os.getenv("AI_PROVIDER_MAX_REQUESTS_PER_MINUTE", "4")))
    if sum(1 for stamp in bucket if now - stamp <= _minute_window_seconds) >= minute_cap:
        return False
    bucket.append(now)
    return True


def _cooldown_seconds_from_response(response: httpx.Response) -> int:
    """Prefer explicit provider retry timing; fall back to a conservative cooldown."""
    fallback = max(30, min(900, int(os.getenv("AI_PROVIDER_429_COOLDOWN_SECONDS", "60"))))
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(1, min(86400, int(float(retry_after))))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(1, min(86400, int((retry_at - datetime.now(timezone.utc)).total_seconds())))
            except (TypeError, ValueError, OverflowError):
                pass

    body: Any = None
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = None

    delays: list[int] = []
    fragments: list[str] = []
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in {"retrydelay", "retry_after", "retryafter"} and isinstance(item, (str, int, float)):
                    match = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds|m|min|minutes|h|hours)?", str(item), re.I)
                    if match:
                        amount = float(match.group(1))
                        unit = (match.group(2) or "s").casefold()
                        factor = 0.001 if unit == "ms" else 60 if unit in {"m", "min", "minutes"} else 3600 if unit in {"h", "hours"} else 1
                        delays.append(max(1, min(86400, int(amount * factor + 0.999))))
                elif isinstance(item, (dict, list)):
                    walk(item)
                elif isinstance(item, str) and key.casefold() in {"message", "status", "reason"}:
                    fragments.append(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(body)
    message = " ".join(fragments)
    if message:
        match = re.search(r"retry(?:\s+in|\s+after)\s+(\d+(?:\.\d+)?)\s*(ms|seconds?|secs?|s|minutes?|mins?|m|hours?|h)?", message, re.I)
        if match:
            amount = float(match.group(1))
            unit = (match.group(2) or "s").casefold()
            factor = 0.001 if unit == "ms" else 60 if unit.startswith("m") else 3600 if unit.startswith("h") else 1
            delays.append(max(1, min(86400, int(amount * factor + 0.999))))
    if delays:
        return max(fallback, max(delays))
    if re.search(r"per[- ]day|daily quota|requests?[_ ]per[_ ]day|generate_content_free_tier_requests_per_day|quota (?:is )?exhausted", message, re.I):
        return max(60, min(86400, int(os.getenv("AI_PROVIDER_DAILY_QUOTA_COOLDOWN_SECONDS", "3600"))))
    return fallback


async def _wait_for_provider_slot(provider: dict[str, Any]) -> bool:
    """Serialize requests per endpoint and pace them below low-tier RPM limits."""
    name = provider["name"]
    now = time.monotonic()
    if _provider_cooldowns.get(name, 0) > now:
        return False
    interval = float(provider.get("min_request_interval_seconds", os.getenv("AI_PROVIDER_MIN_INTERVAL_SECONDS", "15")))
    next_at = _provider_next_request_at.get(name, 0)
    if next_at > now:
        await asyncio.sleep(next_at - now)
    now = time.monotonic()
    if _provider_cooldowns.get(name, 0) > now:
        return False

    # A minute-window cap is queued, not failed. This avoids a burst of requests
    # from parallel specialists when a single low-quota provider is configured.
    bucket = _provider_usage[name]
    minute_cap = int(provider.get("max_requests_per_minute", os.getenv("AI_PROVIDER_MAX_REQUESTS_PER_MINUTE", "4")))
    while sum(1 for stamp in bucket if time.monotonic() - stamp <= _minute_window_seconds) >= minute_cap:
        current = time.monotonic()
        minute_stamps = [stamp for stamp in bucket if current - stamp <= _minute_window_seconds]
        if not minute_stamps:
            break
        await asyncio.sleep(max(0.1, minute_stamps[0] + _minute_window_seconds - current + 0.05))
        if _provider_cooldowns.get(name, 0) > time.monotonic():
            return False

    if not _reserve_provider(provider):
        return False
    _provider_next_request_at[name] = time.monotonic() + interval
    return True


def provider_availability() -> dict[str, Any]:
    """Small non-secret readiness view used by retry controls and diagnostics."""
    status = provider_status()
    return {
        "configured": status.get("provider_configured", False),
        "available_provider_count": status.get("available_provider_count", 0),
        "retry_after_seconds": status.get("retry_after_seconds"),
        "fallback_configured": status.get("fallback_configured", False),
    }


async def _call_model(
    provider: dict[str, Any], messages: list[dict[str, str]], *, temperature: float, json_mode: bool, model: str
) -> str:
    timeout = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "60"))
    protocol = provider.get("protocol", "openai_compatible")
    api_key = provider["api_key"]
    base_url = provider["base_url"].rstrip("/")

    if protocol == "openai_compatible":
        body: dict[str, Any] = {"model": model, "temperature": temperature, "messages": messages}
        body[provider.get("max_output_field", "max_tokens")] = provider.get("max_output_tokens", 4096)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = base_url + "/chat/completions"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            if json_mode and response.status_code in (400, 422):
                # Count this second underlying HTTP request against the same provider budget.
                if not _reserve_provider(provider):
                    response.raise_for_status()
                body.pop("response_format", None)
                response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("OpenAI-compatible provider returned an unexpected response.") from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Provider returned an empty response.")
        return text.strip()

    if protocol == "anthropic_messages":
        system_text = "\n\n".join(m["content"] for m in messages if m.get("role") in ("system", "developer"))
        chat_messages = [
            {"role": m.get("role") if m.get("role") in ("user", "assistant") else "user", "content": m.get("content", "")}
            for m in messages if m.get("role") not in ("system", "developer")
        ]
        body = {
            "model": model,
            "max_tokens": provider.get("max_output_tokens", 4096),
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_text:
            body["system"] = system_text
        headers = {
            "x-api-key": api_key,
            "anthropic-version": provider.get("api_version", "2026-10-21"),
            "Content-Type": "application/json",
        }
        url = base_url + ("/messages" if base_url.endswith("/v1") else "/v1/messages")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        try:
            text = "\n".join(block["text"] for block in payload["content"] if block.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise ValueError("Anthropic Messages API returned an unexpected response.") from exc
        if not text.strip():
            raise ValueError("Anthropic returned no text content.")
        return text.strip()

    if protocol == "gemini_generate_content":
        system_text = "\n\n".join(m["content"] for m in messages if m.get("role") in ("system", "developer"))
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            }
            for m in messages if m.get("role") not in ("system", "developer")
        ]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": provider.get("max_output_tokens", 4096),
            },
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        model_path = model if model.startswith("models/") else "models/" + model
        url = base_url + "/" + model_path + ":generateContent"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
        try:
            candidates = payload.get("candidates", [])
            text = "\n".join(
                part["text"]
                for candidate in candidates
                for part in candidate.get("content", {}).get("parts", [])
                if isinstance(part.get("text"), str)
            )
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("Gemini generateContent returned an unexpected response.") from exc
        if not text.strip():
            feedback = payload.get("promptFeedback", {}).get("blockReason", "")
            raise ValueError("Gemini returned no text candidate." + (f" Block reason: {feedback}" if feedback else ""))
        return text.strip()

    raise ValueError(f"Unsupported provider protocol: {protocol}")


async def test_provider_connections() -> dict[str, Any]:
    """Run one short, user-triggered request per configured provider."""
    from datetime import datetime, timezone

    try:
        configured = _provider_registry()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider configuration is invalid ({type(exc).__name__}).") from exc

    async def test_one(provider: dict[str, Any]) -> dict[str, Any]:
        name = provider["name"]
        protocol = provider.get("protocol", "openai_compatible")
        model = provider["model"]
        limited = {**provider, "max_output_tokens": 32}
        started = time.perf_counter()
        try:
            async with _provider_locks[name]:
                if not await _wait_for_provider_slot(provider):
                    remaining = max(0, int(_provider_cooldowns.get(name, 0) - time.monotonic() + 0.999))
                    note = f"Provider cooling down for about {remaining}s." if remaining else "Local request budget reached."
                    return {"name": name, "protocol": protocol, "model": model, "status": "skipped", "latency_ms": None, "message": note}
                async with asyncio.timeout(float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "60")) + 2):
                    await _call_model(
                        limited,
                        [
                            {"role": "system", "content": "You are checking provider connectivity. Reply with the exact word OK."},
                            {"role": "user", "content": "Reply with OK only."},
                        ],
                        temperature=0.0, json_mode=False, model=model,
                    )
            elapsed = round((time.perf_counter() - started) * 1000)
            return {"name": name, "protocol": protocol, "model": model, "status": "connected", "latency_ms": elapsed, "message": "Provider returned a response."}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                cooldown = _cooldown_seconds_from_response(exc.response)
                _provider_cooldowns[name] = time.monotonic() + cooldown
            label = "rate limited" if status == 429 else "request rejected"
            return {"name": name, "protocol": protocol, "model": model, "status": "failed", "latency_ms": round((time.perf_counter() - started) * 1000), "message": f"HTTP {status}: {label}. Check the provider key, model ID, and account quota."}
        except (httpx.TimeoutException, TimeoutError):
            return {"name": name, "protocol": protocol, "model": model, "status": "failed", "latency_ms": None, "message": "Provider request timed out."}
        except Exception as exc:
            return {"name": name, "protocol": protocol, "model": model, "status": "failed", "latency_ms": None, "message": f"Connection test failed ({type(exc).__name__})."}

    semaphore = asyncio.Semaphore(4)
    async def bounded(provider: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await test_one(provider)

    results = await asyncio.gather(*(bounded(provider) for provider in configured))
    return {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "provider_count": len(configured),
        "results": results,
        "credentials_exposed": False,
    }

async def request_completion(
    messages: list[dict[str, str]], *, temperature: float = 0.2, json_mode: bool = False
) -> str:
    """Route through paced providers, serializing calls per endpoint and honoring 429 cooldowns."""
    try:
        providers = _provider_registry()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider configuration is invalid ({type(exc).__name__}).") from exc
    if not providers:
        raise RuntimeError("Configure AI_PROVIDERS_JSON or AI_API_KEY and AI_MODEL on the backend.")

    errors: list[str] = []
    for provider in providers:
        name = provider["name"]
        models = [provider["model"]]
        fallback_model = provider.get("fallback_model", "")
        if fallback_model and fallback_model != provider["model"]:
            models.append(fallback_model)
        provider_failed = False
        # One in-flight request per provider. Multiple agents may still run
        # concurrently when multiple independent providers are configured.
        async with _provider_locks[name]:
            for model_index, model in enumerate(models):
                for attempt in range(2):
                    if not await _wait_for_provider_slot(provider):
                        now = time.monotonic()
                        cooldown = max(0, int(_provider_cooldowns.get(name, 0) - now + 0.999))
                        errors.append(name + (f": cooling down for about {cooldown}s" if cooldown else ": local request budget reached"))
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
                            cooldown = _cooldown_seconds_from_response(exc.response)
                            _provider_cooldowns[name] = time.monotonic() + cooldown
                            errors.append(name + f": provider rate limit response; cooling down for about {cooldown}s")
                            provider_failed = True
                            break
                        errors.append(name + f": HTTP {status}")
                        if status >= 500 and attempt == 0:
                            await asyncio.sleep(0.8)
                            continue
                        if model_index + 1 < len(models):
                            break
                        provider_failed = True
                        break
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        if attempt == 0:
                            await asyncio.sleep(0.8)
                            continue
                        errors.append(name + f": {type(exc).__name__}")
                        provider_failed = True
                        break
                    except Exception as exc:
                        errors.append(name + f": {type(exc).__name__}: {str(exc)[:180]}")
                        provider_failed = True
                        break
                if provider_failed:
                    break

    detail = "; ".join(errors[-8:]) or "No configured provider is currently available."
    readiness = provider_availability()
    retry_after = readiness.get("retry_after_seconds")
    retry_note = f" Retry after about {retry_after} seconds." if retry_after else ""
    raise ProviderUnavailableError(
        "All configured providers failed or reached their configured limits. " + detail + retry_note,
        retry_after_seconds=retry_after,
    )



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
