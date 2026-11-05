"""OpenAI-compatible JSON chat client with key rotation and a local response cache."""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from backend.config import Settings

LOGGER = logging.getLogger(__name__)
_DEFAULT_COOLDOWN_SECONDS = 60.0
_MODEL_CIRCUIT_SECONDS = 300.0


class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class QuotaPoolExhausted(RuntimeError):
    """Raised after every key for a provider has been quota-limited."""


class ModelCircuitOpen(RuntimeError):
    """Raised without a request while a failed model is cooling down."""


class CircuitBreakerLLMClient:
    """Keep a failing provider model out of subsequent clip requests for five minutes."""

    _open_until: dict[str, float] = {}
    _lock = threading.Lock()

    def __init__(self, client: LLMClient):
        self.client = client
        self.model = str(getattr(client, "model", type(client).__name__))
        self.circuit_key = str(getattr(client, "circuit_key", self.model))

    @property
    def models_used(self):
        return getattr(self.client, "models_used", [])

    @property
    def fallback_on_quota(self):
        return getattr(self.client, "fallback_on_quota", False)

    @classmethod
    def is_open(cls, key: str) -> bool:
        with cls._lock:
            until = cls._open_until.get(key, 0.0)
            if until <= time.monotonic():
                cls._open_until.pop(key, None)
                return False
            return True

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self.is_open(self.circuit_key):
            raise ModelCircuitOpen(f"LLM model {self.model} is cooling down")
        try:
            result = self.client.chat_json(messages)
        except Exception as exc:
            with self._lock:
                self._open_until[self.circuit_key] = time.monotonic() + _MODEL_CIRCUIT_SECONDS
            LOGGER.warning("LLM model %s failed (%s); circuit opened for 300 seconds", self.model, type(exc).__name__)
            raise
        with self._lock:
            self._open_until.pop(self.circuit_key, None)
        return result


def _with_circuit_breaker(client: LLMClient) -> CircuitBreakerLLMClient:
    return client if isinstance(client, CircuitBreakerLLMClient) else CircuitBreakerLLMClient(client)


def clear_response_cache(settings: Settings) -> None:
    """Erase cached model responses, which may contain submitted source text."""
    cache_path = Path(settings.llm_cache_path or (Path(settings.anek_data_dir) / "llm_response_cache.sqlite3"))
    if not cache_path.is_file():
        return
    with closing(sqlite3.connect(cache_path, timeout=10)) as connection:
        connection.execute("PRAGMA busy_timeout=10000")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_responses'"
        ).fetchone()
        if not exists:
            return
        with connection:
            connection.execute("DELETE FROM llm_responses")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")


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


class FallbackLLMClient:
    """Use the configured primary provider and fall back only on request failure."""

    def __init__(self, primary: LLMClient, secondary: LLMClient, *, propagate_quota: bool = False):
        self.primary = primary
        self.secondary = secondary
        self.propagate_quota = propagate_quota
        self.models_used: list[str] = []

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        for client in (self.primary, self.secondary):
            model = str(getattr(client, "model", client.__class__.__name__))
            if isinstance(client, CircuitBreakerLLMClient) and client.is_open(client.circuit_key):
                LOGGER.info("Skipping LLM model %s while its five-minute circuit is open", model)
                continue
            nested_models = getattr(client, "models_used", None)
            nested_count = len(nested_models) if nested_models is not None else 0
            try:
                result = client.chat_json(messages)
                if nested_models is not None and len(nested_models) > nested_count:
                    self.models_used.extend(nested_models[nested_count:])
                else:
                    self.models_used.append(model)
                return result
            except Exception as exc:
                LOGGER.warning("LLM model %s failed (%s); %s", model, type(exc).__name__,
                               "trying fallback" if client is self.primary else "no provider fallback remains")
                if client is self.primary and self.propagate_quota and isinstance(exc, QuotaPoolExhausted):
                    raise
                if client is self.secondary:
                    raise
        raise RuntimeError("All configured translation providers failed")


class LiveLLMClient:
    def __init__(
        self,
        settings: Settings,
        timeout: float = 60.0,
        *,
        raise_on_quota: bool = False,
        fallback_on_quota: bool = False,
    ):
        configured_keys = [
            key.strip().strip('"').strip("'")
            for key in settings.groq_api_keys.split(",")
            if key.strip().strip('"').strip("'")
        ]
        if not configured_keys and settings.llm_api_key:
            configured_keys = [settings.llm_api_key]
        if not settings.llm_base_url or not settings.llm_model or not configured_keys:
            raise ValueError("LLM_BASE_URL, LLM_MODEL, and GROQ_API_KEYS (or LLM_API_KEY) are required")
        self.url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        self.model = settings.llm_model
        self.circuit_key = f"{self.url}|{self.model}"
        self.keys = configured_keys
        self.current_key_index = 0
        self.cooldowns: dict[int, float] = {}
        self.invalid_keys: set[int] = set()
        self.raise_on_quota = raise_on_quota
        self.fallback_on_quota = fallback_on_quota
        self.timeout = timeout
        self.cache_path = Path(settings.llm_cache_path or (Path(settings.anek_data_dir) / "llm_response_cache.sqlite3"))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_cache()

    def _initialize_cache(self) -> None:
        with closing(sqlite3.connect(self.cache_path, timeout=10)) as connection:
            with connection:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS llm_responses ("
                    "cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, created_at REAL NOT NULL)"
                )

    def _cache_key(self, messages: list[dict[str, str]]) -> str:
        request_identity = json.dumps(
            {
                "url": self.url,
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(request_identity.encode("utf-8")).hexdigest()

    def _cache_get(self, cache_key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.cache_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            row = connection.execute("SELECT response_json FROM llm_responses WHERE cache_key = ?", (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_set(self, cache_key: str, result: dict[str, Any]) -> None:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with closing(sqlite3.connect(self.cache_path, timeout=10)) as connection:
            with connection:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute(
                    "INSERT OR REPLACE INTO llm_responses(cache_key, response_json, created_at) VALUES (?, ?, ?)",
                    (cache_key, encoded, time.time()),
                )

    def _available_key_index(self) -> int:
        while True:
            now = time.monotonic()
            for offset in range(len(self.keys)):
                index = (self.current_key_index + offset) % len(self.keys)
                if index not in self.invalid_keys and self.cooldowns.get(index, 0.0) <= now:
                    self.current_key_index = index
                    return index
            usable_indices = [index for index in range(len(self.keys)) if index not in self.invalid_keys]
            if not usable_indices:
                raise RuntimeError("All configured LLM key indexes were rejected as invalid")
            shortest_index = min(usable_indices, key=lambda index: self.cooldowns.get(index, 0.0))
            delay = max(0.0, self.cooldowns[shortest_index] - now)
            if self.fallback_on_quota:
                raise QuotaPoolExhausted(
                    f"LLM provider {self.model} has no available keys; shortest_retry_in={delay:.1f}s"
                )
            LOGGER.warning(
                "All %d LLM keys are on cooldown; waiting %.1f seconds for key index %d",
                len(self.keys), delay, shortest_index + 1,
            )
            time.sleep(delay)
            self.current_key_index = shortest_index

    @staticmethod
    def _is_quota_error(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            error = {}
        code = str(error.get("code", "")).casefold()
        error_type = str(error.get("type", "")).casefold()
        message = str(error.get("message", "")).casefold()
        return any(term in code or term in error_type or term in message for term in ("quota", "rate_limit", "rate limit"))

    @staticmethod
    def _retry_delay(response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        body = response.text[:4000]
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        if isinstance(error_body, dict):
            error = error_body.get("error", {})
            details = error.get("details", []) if isinstance(error, dict) else []
            if isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict):
                        retry_delay = detail.get("retryDelay")
                        if isinstance(retry_delay, str):
                            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s\s*", retry_delay)
                            if match:
                                return max(0.0, float(match.group(1)))
        patterns = (
            r"try again in\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
            r"retry after\s*(\d+(?:\.\d+)?)\s*seconds?",
            r"retry in\s*(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?",
        )
        match = re.search(patterns[0], body, re.IGNORECASE)
        if match:
            hours, minutes, seconds = (float(value or 0) for value in match.groups())
            return max(0.0, hours * 3600 + minutes * 60 + seconds)
        match = re.search(patterns[1], body, re.IGNORECASE)
        if match:
            return max(0.0, float(match.group(1)))
        match = re.search(patterns[2], body, re.IGNORECASE)
        if match:
            return max(0.0, float(match.group(1)))
        return _DEFAULT_COOLDOWN_SECONDS

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        cache_key = self._cache_key(messages)
        cached = self._cache_get(cache_key)
        if cached is not None:
            LOGGER.info("LLM response served from disk cache")
            return cached

        request_body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        service_error_attempts = 0
        quota_keys_this_request: set[int] = set()
        while True:
            key_index = self._available_key_index()
            response = httpx.post(
                self.url,
                headers={"Authorization": f"Bearer {self.keys[key_index]}"},
                json=request_body,
                timeout=self.timeout,
            )
            if response.status_code in {502, 503, 504} and service_error_attempts < 1:
                service_error_attempts += 1
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(8.0, max(0.0, float(retry_after)))
                except ValueError:
                    delay = 1.0
                LOGGER.warning(
                    "LLM model %s returned HTTP %d; retrying provider once with the same key in %.1fs",
                    self.model, response.status_code, delay,
                )
                time.sleep(delay)
                continue
            if self._is_quota_error(response):
                cooldown = self._retry_delay(response)
                if self.raise_on_quota:
                    raise RuntimeError(
                        f"LLM provider {self.model} returned a quota or rate-limit response "
                        f"(HTTP {response.status_code}; retry_in={cooldown:.1f})"
                    )
                self.cooldowns[key_index] = time.monotonic() + cooldown
                self.current_key_index = (key_index + 1) % len(self.keys)
                quota_keys_this_request.add(key_index)
                LOGGER.warning(
                    "LLM key index %d reached quota; cooling down for %.1f seconds",
                    key_index + 1, cooldown,
                )
                if self.fallback_on_quota and len(quota_keys_this_request | self.invalid_keys) >= len(self.keys):
                    raise QuotaPoolExhausted(
                        f"LLM provider {self.model} quota-limited every configured key; "
                        f"shortest_retry_in={cooldown:.1f}s"
                    )
                continue
            if response.status_code == 401:
                self.invalid_keys.add(key_index)
                self.current_key_index = (key_index + 1) % len(self.keys)
                LOGGER.error("LLM key index %d was rejected as invalid; skipping it", key_index + 1)
                continue
            if response.is_error:
                detail = response.text[:1200]
                raise RuntimeError(f"LLM provider returned HTTP {response.status_code}: {detail}")

            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("LLM response must be a JSON object")
            self._cache_set(cache_key, result)
            LOGGER.info("LLM key index %d answered", key_index + 1)
            return result


def create_llm_client(mock: bool = False) -> LLMClient:
    if mock:
        return MockLLMClient()
    from backend.config import get_settings

    return _with_circuit_breaker(LiveLLMClient(get_settings()))


class SarvamTranslationClient:
    """Use Sarvam text translation for target text and Sarvam chat for translation-only rewrites."""

    def __init__(self, settings: Settings):
        from sarvamai import SarvamAI

        self.translation_model = settings.sarvam_translation_model
        self.rewrite_model = settings.sarvam_rewrite_model
        self.model = self.translation_model
        self.models_used: list[str] = []
        self.client = SarvamAI(api_subscription_key=settings.sarvam_api_key, timeout=60.0)

    @staticmethod
    def _with_model_circuit(model: str, operation):
        key = f"sarvam|{model}"
        if CircuitBreakerLLMClient.is_open(key):
            raise ModelCircuitOpen(f"LLM model {model} is cooling down")
        try:
            result = operation()
        except Exception as exc:
            with CircuitBreakerLLMClient._lock:
                CircuitBreakerLLMClient._open_until[key] = time.monotonic() + _MODEL_CIRCUIT_SECONDS
            LOGGER.warning("LLM model %s failed (%s); circuit opened for 300 seconds", model, type(exc).__name__)
            raise
        with CircuitBreakerLLMClient._lock:
            CircuitBreakerLLMClient._open_until.pop(key, None)
        return result

    def _chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        response = self.client.chat.completions(
            model=self.rewrite_model,
            messages=messages,
            temperature=0.2,
            max_tokens=4096,
            response_format={"type": "json_object"},
            request_options={"timeout_in_seconds": 60, "max_retries": 0},
        )
        choices = getattr(response, "choices", None)
        if not choices:
            raise ValueError("Sarvam chat completion returned no choices")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                for part in content
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Sarvam chat completion returned empty text")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Sarvam returned a non-object JSON response")
        self.models_used.append(self.rewrite_model)
        return result

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system = messages[0]["content"] if messages else ""
        payload = json.loads(messages[-1]["content"]) if len(messages) > 1 else {}
        spans = payload.get("timed_source_spans")
        if "Translate the full source paragraph" in system and isinstance(spans, list):
            tone = str(payload.get("tone", "")).casefold()
            mode = "classic-colloquial" if "respectful" in tone else "modern-colloquial"
            if self.translation_model == "sarvam-translate:v1":
                mode = "formal"
            translations = []
            for span in spans:
                response = self._with_model_circuit(
                    self.translation_model,
                    lambda span=span: self.client.text.translate(
                        input=span["source_text"],
                        source_language_code=payload.get("source_language") or "auto",
                        target_language_code=payload["target_language"],
                        model=self.translation_model,
                        mode=mode,
                        request_options={"timeout_in_seconds": 60, "max_retries": 0},
                    ),
                )
                translated_text = getattr(response, "translated_text", None)
                if not isinstance(translated_text, str) or not translated_text.strip():
                    raise ValueError("Sarvam translation returned empty text")
                translations.append(translated_text.strip())
            # The existing pipeline validates and ranks three alternatives. The
            # language API returns one translation per call; keep its output as
            # the three identical deterministic candidates without extra calls.
            candidates = [
                {"translations": translations, "review_notes": []}
                for _ in range(3)
            ]
            self.models_used.append(self.translation_model)
            return {"candidates": candidates}
        return self._with_model_circuit(self.rewrite_model, lambda: self._chat_json(messages))


def create_translation_client(primary: LLMClient, settings: Settings) -> LLMClient:
    """Use Sarvam translation first, Groq next, and Gemini as the final fallback."""
    primary = _with_circuit_breaker(primary)
    gemini = _create_gemini_fallback_chain(settings)
    fallback: LLMClient = FallbackLLMClient(primary, gemini) if gemini else primary
    if settings.sarvam_api_key:
        return FallbackLLMClient(SarvamTranslationClient(settings), fallback)
    return fallback


def create_workflow_client(primary: LLMClient, settings: Settings) -> LLMClient:
    """Use Sarvam chat for translation QA and keep configured providers as fallbacks."""
    primary = _with_circuit_breaker(primary)
    gemini = _create_gemini_fallback_chain(settings)
    fallback: LLMClient = FallbackLLMClient(primary, gemini) if gemini else primary
    if settings.sarvam_api_key:
        return FallbackLLMClient(SarvamTranslationClient(settings), fallback)
    return fallback


def _create_gemini_fallback_chain(settings: Settings) -> LLMClient | None:
    gemini_keys = settings.llm_2_api_keys or settings.llm_2_api_key
    if not gemini_keys:
        return None
    models = [settings.llm_2_model]
    models.extend(model.strip() for model in settings.llm_2_fallback_models.split(",") if model.strip())
    # Preserve order while avoiding a repeated configured model.
    models = list(dict.fromkeys(models))
    clients = [_create_gemini_client(settings, gemini_keys, model) for model in models]
    chain: LLMClient = clients[-1]
    for client in reversed(clients[:-1]):
        chain = FallbackLLMClient(client, chain, propagate_quota=True)
    return chain


def _create_gemini_client(settings: Settings, gemini_keys: str, model: str) -> LiveLLMClient:
    gemini_settings = settings.model_copy(update={
        "llm_base_url": settings.llm_2_base_url,
        "llm_model": model,
        "llm_api_key": "",
        "groq_api_keys": gemini_keys,
    })
    # Try every Gemini key once for this request. When the entire Gemini pool is
    # limited, bubble up to the provider-level fallback instead of sleeping in
    # this worker indefinitely. Groq remains the translation fallback.
    return _with_circuit_breaker(LiveLLMClient(gemini_settings, fallback_on_quota=True))
