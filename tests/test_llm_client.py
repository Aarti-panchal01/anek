import logging
import uuid
from pathlib import Path

import httpx
import pytest

from backend.clients.llm import FallbackLLMClient, LiveLLMClient, create_translation_client, create_workflow_client
from backend.config import Settings


def _settings(cache_path, keys="key-one,key-two"):
    return Settings(
        llm_base_url="https://example.invalid/v1",
        llm_model="test-model",
        groq_api_keys=keys,
        llm_2_api_key="",
        llm_2_api_keys="",
        llm_cache_path=str(cache_path),
    )


def _response(status, body, headers=None):
    return httpx.Response(
        status,
        json=body,
        headers=headers,
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"),
    )


@pytest.fixture
def cache_path():
    directory = Path("data")
    directory.mkdir(exist_ok=True)
    path = directory / f"test-llm-cache-{uuid.uuid4().hex}.sqlite3"
    yield path
    path.unlink(missing_ok=True)


def test_quota_response_rotates_key_and_success_is_cached(cache_path, monkeypatch, caplog):
    client = LiveLLMClient(_settings(cache_path))
    calls = []

    def post(url, headers, json, timeout):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return _response(
                429,
                {"error": {"type": "tokens", "code": "rate_limit_exceeded", "message": "quota"}},
                {"Retry-After": "0"},
            )
        return _response(200, {"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    monkeypatch.setattr("backend.clients.llm.httpx.post", post)
    caplog.set_level(logging.INFO, logger="backend.clients.llm")
    messages = [{"role": "user", "content": "same request"}]

    assert client.chat_json(messages) == {"answer": "ok"}
    assert calls == ["Bearer key-one", "Bearer key-two"]
    assert "key index 2 answered" in caplog.text
    assert "key-one" not in caplog.text and "key-two" not in caplog.text

    second_client = LiveLLMClient(_settings(cache_path))
    assert second_client.chat_json(messages) == {"answer": "ok"}
    assert len(calls) == 2


def test_translation_falls_back_to_groq_after_all_gemini_keys_are_limited(
    cache_path, monkeypatch, caplog
):
    settings = Settings(
        llm_base_url="https://groq.example/v1",
        llm_model="groq-model",
        groq_api_keys="groq-key",
        llm_2_base_url="https://gemini.example/v1",
        llm_2_model="gemini-model",
        llm_2_api_keys="gemini-key-1,gemini-key-2",
        llm_2_fallback_models="gemini-backup",
        llm_cache_path=str(cache_path),
    )
    gemini_calls = []

    def post(url, headers, json, timeout):
        gemini_calls.append((url, headers["Authorization"]))
        return _response(
            429,
            {"error": {"code": "rate_limit_exceeded", "message": "quota exceeded"}},
            {"Retry-After": "60"},
        )

    class Groq:
        model = "groq-model"

        def chat_json(self, messages):
            return {"provider": "groq"}

    monkeypatch.setattr("backend.clients.llm.httpx.post", post)
    caplog.set_level(logging.WARNING, logger="backend.clients.llm")
    client = create_translation_client(Groq(), settings)

    assert client.chat_json([{"role": "user", "content": "translate this"}]) == {"provider": "groq"}
    assert [auth for _, auth in gemini_calls] == ["Bearer gemini-key-1", "Bearer gemini-key-2"]
    assert all(url.startswith("https://gemini.example/") for url, _ in gemini_calls)
    assert "QuotaPoolExhausted" in caplog.text
    assert "gemini-key-1" not in caplog.text and "gemini-key-2" not in caplog.text


def test_all_six_cooling_down_waits_for_the_shortest(cache_path, monkeypatch):
    client = LiveLLMClient(_settings(cache_path, ",".join(f"key-{i}" for i in range(6))))
    now = [10.0]
    monkeypatch.setattr("backend.clients.llm.time.monotonic", lambda: now[0])
    monkeypatch.setattr("backend.clients.llm.time.sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    client.cooldowns = {index: 30.0 + index for index in range(6)}

    assert client._available_key_index() == 0
    assert now[0] == 30.0


def test_invalid_key_is_skipped_without_logging_secret(monkeypatch, caplog):
    cache_path = Path("data") / f"test-llm-cache-{uuid.uuid4().hex}.sqlite3"
    client = LiveLLMClient(_settings(cache_path))
    calls = []

    def post(url, headers, json, timeout):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return _response(401, {"error": {"code": "invalid_api_key", "message": "Invalid API Key"}})
        return _response(200, {"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    monkeypatch.setattr("backend.clients.llm.httpx.post", post)
    caplog.set_level(logging.ERROR, logger="backend.clients.llm")
    assert client.chat_json([{"role": "user", "content": "invalid key test"}]) == {"answer": "ok"}
    assert calls == ["Bearer key-one", "Bearer key-two"]
    assert "key index 1" in caplog.text
    assert "key-one" not in caplog.text and "key-two" not in caplog.text
    client.cache_path.unlink(missing_ok=True)


def test_transient_service_error_rotates_to_next_key(cache_path, monkeypatch, caplog):
    client = LiveLLMClient(_settings(cache_path))
    calls = []

    def post(url, headers, json, timeout):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return _response(503, {"error": {"message": "temporarily unavailable"}})
        return _response(200, {"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    monkeypatch.setattr("backend.clients.llm.httpx.post", post)
    monkeypatch.setattr("backend.clients.llm.time.sleep", lambda delay: None)
    caplog.set_level(logging.WARNING, logger="backend.clients.llm")

    assert client.chat_json([{"role": "user", "content": "transient retry"}]) == {"answer": "ok"}
    assert calls == ["Bearer key-one", "Bearer key-two"]
    assert "key index 2" in caplog.text
    assert "key-one" not in caplog.text and "key-two" not in caplog.text


def test_retry_delay_parses_groq_retry_time_and_quota_error():
    response = _response(
        429,
        {"error": {"type": "tokens", "code": "rate_limit_exceeded", "message": "Please try again in 3m13.5s."}},
    )
    assert LiveLLMClient._retry_delay(response) == 193.5
    quota = _response(402, {"error": {"code": "insufficient_quota", "message": "quota exceeded"}})
    assert LiveLLMClient._is_quota_error(quota)


def test_provider_fallback_order_for_translation_and_other_workflow_calls(monkeypatch):
    settings = Settings(
        llm_base_url="https://groq.example/v1",
        llm_model="groq-model",
        groq_api_keys="groq-key",
        llm_2_base_url="https://gemini.example/v1",
        llm_2_model="gemini-model",
        llm_2_api_keys="gemini-key-1,gemini-key-2",
        llm_2_fallback_models="gemini-backup-a,gemini-backup-b",
        llm_cache_path=str(Path("data") / f"provider-fallback-{uuid.uuid4().hex}.sqlite3"),
    )

    class Provider:
        def __init__(self, provider_settings, **kwargs):
            self.model = provider_settings.llm_model
            self.keys = provider_settings.groq_api_keys
            self.fallback_on_quota = kwargs.get("fallback_on_quota", False)

        def chat_json(self, messages):
            if self.model == "groq-model":
                raise RuntimeError("organization restricted")
            return {"provider": self.model}

    monkeypatch.setattr("backend.clients.llm.LiveLLMClient", Provider)
    groq = Provider(settings)

    translation = create_translation_client(groq, settings)
    assert isinstance(translation, FallbackLLMClient)
    assert isinstance(translation.primary, FallbackLLMClient)
    assert translation.primary.primary.model == "gemini-model"
    assert translation.primary.primary.fallback_on_quota is True
    assert translation.primary.secondary.primary.model == "gemini-backup-a"
    assert translation.primary.secondary.secondary.model == "gemini-backup-b"
    assert translation.secondary is groq

    workflow = create_workflow_client(groq, settings)
    assert isinstance(workflow, FallbackLLMClient)
    assert workflow.primary is groq
    assert isinstance(workflow.secondary, FallbackLLMClient)
    assert workflow.secondary.primary.model == "gemini-model"
    assert workflow.chat_json([{"role": "user", "content": "check"}]) == {"provider": "gemini-model"}


def test_model_fallback_records_model_that_actually_answered(cache_path, monkeypatch):
    settings = _settings(cache_path, keys="groq-key")
    settings.llm_2_api_key = "gemini-key"
    settings.llm_2_api_keys = ""
    settings.llm_2_model = "gemini-primary"
    settings.llm_2_fallback_models = "gemini-backup"

    class Provider:
        def __init__(self, provider_settings, **kwargs):
            self.model = provider_settings.llm_model

        def chat_json(self, messages):
            if self.model == "gemini-primary":
                raise RuntimeError("temporary service error")
            return {"provider": self.model}

    monkeypatch.setattr("backend.clients.llm.LiveLLMClient", Provider)
    from backend.clients.llm import create_translation_client

    client = create_translation_client(Provider(settings), settings)
    assert client.chat_json([{"role": "user", "content": "translation"}]) == {"provider": "gemini-backup"}
    assert client.models_used == ["gemini-backup"]
