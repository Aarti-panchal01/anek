"""OpenAI-compatible JSON chat client with key rotation and a local response cache."""

import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from backend.config import Settings

LOGGER = logging.getLogger(__name__)
_DEFAULT_COOLDOWN_SECONDS = 60.0


class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class QuotaPoolExhausted(RuntimeError):
    """Raised after every key for a provider has been quota-limited."""


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
            if response.status_code in {502, 503, 504} and service_error_attempts < 3:
                service_error_attempts += 1
                delay = 2 ** service_error_attempts
                self.current_key_index = (key_index + 1) % len(self.keys)
                LOGGER.warning(
                    "LLM model %s returned HTTP %d on key index %d; retry %d/3 with key index %d in %ds",
                    self.model, response.status_code, key_index + 1, service_error_attempts,
                    self.current_key_index + 1, delay,
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

    return LiveLLMClient(get_settings())


def create_translation_client(primary: LLMClient, settings: Settings) -> LLMClient:
    """Prefer Gemini for translation and fall back to the configured Groq pool."""
    gemini = _create_gemini_fallback_chain(settings)
    return FallbackLLMClient(gemini, primary) if gemini else primary


def create_workflow_client(primary: LLMClient, settings: Settings) -> LLMClient:
    """Use Groq first for cleanup/checks, with Gemini as a provider fallback."""
    gemini = _create_gemini_fallback_chain(settings)
    return FallbackLLMClient(primary, gemini) if gemini else primary


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
    return LiveLLMClient(gemini_settings, fallback_on_quota=True)
