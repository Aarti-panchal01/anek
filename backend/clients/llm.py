"""OpenAI-compatible JSON chat client with a deterministic test double."""

import json
from typing import Any, Protocol

import httpx

from backend.config import Settings

class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class MockLLMClient:
    def __init__(self, responses: list[Any] | None = None):
        self.responses = list(responses or [])

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"ok": True, "mock": True, "messages": len(messages)}


class LiveLLMClient:
    def __init__(self, settings: Settings, timeout: float = 60.0):
        if not settings.llm_base_url or not settings.llm_model or not settings.llm_api_key:
            raise ValueError("LLM_BASE_URL, LLM_MODEL, and LLM_API_KEY are required")
        self.url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.timeout = timeout

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        response = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        if response.is_error:
            # Include the provider's validation detail without echoing request
            # headers, which contain credentials.
            detail = response.text[:1200]
            raise RuntimeError(f"LLM provider returned HTTP {response.status_code}: {detail}")
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("LLM response must be a JSON object")
        return result


def create_llm_client(mock: bool = False) -> LLMClient:
    if mock:
        return MockLLMClient()
    from backend.config import get_settings

    return LiveLLMClient(get_settings())
