import json

import pytest

from backend.clients.llm import (
    BadInputError,
    ProviderError,
    RateLimitedError,
    SarvamTranslationClient,
    SharedSarvamLimiter,
    UnsupportedLanguageError,
    _provider_call,
)


@pytest.fixture(autouse=True)
def reset_provider_breaker():
    SharedSarvamLimiter._unavailable_until = 0.0
    SharedSarvamLimiter._starts.clear()
    yield
    SharedSarvamLimiter._unavailable_until = 0.0


def _client():
    client = SarvamTranslationClient.__new__(SarvamTranslationClient)
    client.translation_model = "mayura:v1"
    client.rewrite_model = "sarvam-105b-conversations"
    client.model = client.translation_model
    client.models_used = []
    return client


def test_sarvam_translation_uses_mayura_and_returns_three_candidate_sets(monkeypatch):
    calls = []

    class Text:
        def translate(self, **kwargs):
            calls.append(kwargs)
            return type("Translation", (), {"translated_text": "ನಮಸ್ಕಾರ"})()

    client = _client()
    client.client = type("Sarvam", (), {"text": Text()})()
    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation: operation()))
    response = client.chat_json([
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({
            "source_language": "en-IN", "target_language": "kn-IN",
            "timed_source_spans": [{"source_text": "Hello"}, {"source_text": "World"}],
        })},
    ])
    assert len(calls) == 2
    assert calls[0]["model"] == "mayura:v1"
    assert calls[0]["target_language_code"] == "kn-IN"
    assert len(response["candidates"]) == 3
    assert all(len(candidate["translations"]) == 2 for candidate in response["candidates"])


def test_sarvam_rejects_unsupported_language_before_request():
    client = _client()
    client.client = type("Sarvam", (), {})()
    messages = [
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({"target_language": "xx-XX", "timed_source_spans": []})},
    ]
    with pytest.raises(UnsupportedLanguageError):
        client.chat_json(messages)


def test_shared_provider_retry_classifies_rate_limit_and_recovers(monkeypatch):
    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation: operation()))
    monkeypatch.setattr("backend.clients.llm.time.sleep", lambda _: None)
    monkeypatch.setattr("backend.clients.llm.random.uniform", lambda *_: 0)

    class TooManyRequests(Exception):
        status_code = 429

    attempts = 0

    def request():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TooManyRequests()
        return "ok"

    assert _provider_call(request) == "ok"
    assert attempts == 3


@pytest.mark.parametrize(("status", "error"), [(429, RateLimitedError), (503, ProviderError), (400, BadInputError)])
def test_provider_errors_have_clear_classes_and_no_secret_text(monkeypatch, status, error):
    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation: operation()))
    monkeypatch.setattr("backend.clients.llm.time.sleep", lambda _: None)

    class HttpError(Exception):
        status_code = status

    with pytest.raises(error):
        _provider_call(lambda: (_ for _ in ()).throw(HttpError("private provider response")))


def test_mocked_failing_provider_opens_five_minute_circuit(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("backend.clients.llm.time.monotonic", lambda: now[0])
    calls = []

    class ProviderUnavailable(Exception):
        status_code = 503

    def failing_provider():
        calls.append(1)
        raise ProviderUnavailable()

    with pytest.raises(ProviderError):
        _provider_call(failing_provider, attempts=1)
    with pytest.raises(ProviderError, match="circuit is open"):
        _provider_call(failing_provider, attempts=1)
    assert calls == [1]
    now[0] += 301
    with pytest.raises(ProviderError):
        _provider_call(failing_provider, attempts=1)
    assert calls == [1, 1]
