"""OpenAI-compatible LLM client boundary with deterministic test behavior."""

from typing import Any, Protocol


class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class MockLLMClient:
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"ok": True, "mock": True, "messages": len(messages)}


def create_llm_client(mock: bool = False) -> LLMClient:
    if mock:
        return MockLLMClient()
    raise NotImplementedError("Live LLM methods are implemented in the Brain phase.")
