import json
import os
from typing import Any

import httpx


def provider_is_configured() -> bool:
    return bool(os.getenv("AI_API_KEY") and os.getenv("AI_MODEL"))


async def request_json_plan(instruction: str) -> dict[str, Any]:
    """Request a structured plan from a configured OpenAI-compatible endpoint.

    This only creates a plan. It does not execute tools, run code, browse websites,
    or verify deliverables.
    """
    api_key = os.getenv("AI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()
    base_url = os.getenv("AI_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    timeout = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "45"))
    if not api_key or not model:
        raise RuntimeError("No AI provider is configured.")

    system_prompt = """You are a task-planning engine for a multi-agent workforce.
Return only valid JSON with keys: title, category, agents, subtasks.
agents must contain 3 to 20 objects with keys name, role, personality, task, emoji.
Always include one independent QA / Verification agent.
Use distinct specialist roles appropriate to the task. Do not claim tasks are executed.
subtasks must contain 3 to 12 objects with keys title and owner.
Keep plans actionable, concise, and grounded in the user's instruction."""

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": instruction},
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed.get("agents"), list) or not isinstance(parsed.get("subtasks"), list):
        raise ValueError("Provider returned an invalid task plan.")
    return parsed
