"""Single-provider Sarvam text translation and structured chat client."""

from __future__ import annotations

import json
import hashlib
import logging
import random
import re
import threading
import time
from collections import deque
from typing import Any, Protocol

from backend.config import LANGUAGES, Settings

LOGGER = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    code = "provider_down"


class RateLimitedError(ProviderError):
    code = "rate_limited"


class UnsupportedLanguageError(ProviderError):
    code = "unsupported_language"


class BadInputError(ProviderError):
    code = "bad_input"


class CredentialError(ProviderError):
    code = "credential_rejected"


class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class SharedSarvamLimiter:
    """One process-wide limiter: max 60 starts/minute and three concurrent calls."""

    _semaphore = threading.BoundedSemaphore(3)
    _lock = threading.Lock()
    _starts: deque[float] = deque()
    _unavailable_until = 0.0
    _key_unavailable_until: dict[str, float] = {}

    @classmethod
    def call(cls, operation, *, respect_breaker: bool = True):
        with cls._lock:
            if respect_breaker and time.monotonic() < cls._unavailable_until:
                raise ProviderError("Sarvam circuit is open after a recent provider failure")
        with cls._semaphore:
            while True:
                with cls._lock:
                    now = time.monotonic()
                    while cls._starts and now - cls._starts[0] >= 60.0:
                        cls._starts.popleft()
                    if len(cls._starts) < 60:
                        cls._starts.append(now)
                        break
                    delay = max(0.01, 60.0 - (now - cls._starts[0]))
                time.sleep(delay)
            return operation()


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _provider_call(operation, *, attempts: int = 4, respect_breaker: bool = True):
    for attempt in range(attempts):
        try:
            result = SharedSarvamLimiter.call(operation, respect_breaker=respect_breaker)
            with SharedSarvamLimiter._lock:
                SharedSarvamLimiter._unavailable_until = 0.0
            return result
        except Exception as exc:
            if isinstance(exc, ProviderError) and "circuit is open" in str(exc):
                raise
            status = _status_code(exc)
            if status in {401, 403, 402}:
                error_type = CredentialError
            elif status == 429:
                error_type = RateLimitedError
            elif status is not None and status >= 500:
                error_type = ProviderError
            elif status is not None and status >= 400:
                error_type = BadInputError
            else:
                error_type = ProviderError
            retryable = status == 429 or (status is not None and status >= 500)
            if retryable and attempt + 1 < attempts:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.25))
                continue
            if respect_breaker and error_type not in {CredentialError, RateLimitedError, BadInputError}:
                with SharedSarvamLimiter._lock:
                    SharedSarvamLimiter._unavailable_until = time.monotonic() + 300.0
            raise error_type(f"Sarvam request failed ({error_type.code})") from exc


class MockLLMClient:
    def __init__(self, responses: list[Any] | None = None):
        self.responses = list(responses or [])
        self.models_used: list[str] = ["mock"]

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        system = messages[0].get("content", "") if messages else ""
        try:
            payload = json.loads(messages[-1].get("content", "{}")) if len(messages) > 1 else {}
        except (TypeError, ValueError):
            payload = {}
        if "Correct only clear speech" in system:
            return {"cleaned_text": payload.get("transcript", ""), "changes": [], "uncertain_spans": []}
        if "Translate the full source paragraph" in system:
            phrase = "नमस्ते, यह एक नमूना अनुवाद है।" if payload.get("target_language") == "hi-IN" else "ನಮಸ್ಕಾರ, ಇದು ಮಾದರಿ ಅನುವಾದವಾಗಿದೆ."
            spans = payload.get("timed_source_spans", [])
            return {"candidates": [{"translations": [phrase for _ in spans], "review_notes": []} for _ in range(3)]}
        if "Select the complete paragraph candidate" in system or "Choose by strict priority" in system:
            return {"selected_index": 0, "reason": "Deterministic mock selection."}
        if "Independently back-translate" in system:
            return {"back_translation": payload.get("source_text", ""), "meaning_preserved": True,
                    "speech_act_preserved": True, "agency_preserved": True, "issues": [], "severity": "none"}
        if "target_character_budget" in payload or payload.get("revision_mode"):
            return {"translation": payload.get("current_translation") or payload.get("source_segment", "")}
        if "terms" in system.casefold() and "idioms" in system.casefold():
            return {"terms": [], "idioms": []}
        return {"ok": True, "mock": True}


def _split_at_character_limit(text: str, limit: int) -> list[str]:
    """Split provider input without losing text, preferring whitespace boundaries."""
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > limit:
        cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts or [""]


_PROTECTED_NUMBER_RE = re.compile(
    r"(?i)(?:\bRs\.?\s*)?\b\d{1,3}(?:,\d{2})*,\d{3}\b|"
    r"\b\d{5}\s+\d{5}\b|\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM)\b|\b\d+(?:\.\d+)?%?\b"
)


def _protect_number_tokens(text: str) -> tuple[str, dict[str, str]]:
    """Keep numeric identifiers and formatted values stable through translation."""
    values: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = f"ZXQ{len(values)}XZ"
        values[token] = match.group(0)
        return token

    return _PROTECTED_NUMBER_RE.sub(replace, text), values


def _restore_number_tokens(text: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        text = re.sub(re.escape(token), lambda _match: value, text, flags=re.IGNORECASE)
    return text


class SarvamTranslationClient:
    """Sarvam Translate/Mayura for translations and Sarvam chat for structured QA."""

    def __init__(self, settings: Settings):
        self.api_keys = settings.resolved_sarvam_api_keys
        if not self.api_keys:
            raise ValueError("SARVAM_API_KEY is required")
        from sarvamai import SarvamAI

        self.translation_model = settings.sarvam_translation_model
        self.rewrite_model = settings.sarvam_rewrite_model
        self.model = self.translation_model
        self.models_used: list[str] = []
        self.clients = [SarvamAI(api_subscription_key=key, timeout=60.0) for key in self.api_keys]
        self.client = self.clients[0]
        self._key_fingerprints = [hashlib.sha256(key.encode("utf-8")).hexdigest() for key in self.api_keys]
        self._rotation_index = 0

    def _call_with_key_rotation(self, operation):
        clients = getattr(self, "clients", [self.client])
        fingerprints = getattr(self, "_key_fingerprints", ["legacy-key"])
        now = time.monotonic()
        indices = list(range(len(clients)))
        if indices:
            shift = getattr(self, "_rotation_index", 0) % len(indices)
            indices = indices[shift:] + indices[:shift]
        attempted = []
        last_key_error: ProviderError | None = None
        for index in indices:
            fingerprint = fingerprints[index]
            with SharedSarvamLimiter._lock:
                unavailable_until = SharedSarvamLimiter._key_unavailable_until.get(fingerprint, 0.0)
            if unavailable_until > now:
                continue
            attempted.append(index)
            try:
                result = _provider_call(
                    lambda: operation(clients[index]), attempts=1, respect_breaker=False,
                )
                self._rotation_index = (index + 1) % len(clients)
                return result
            except (CredentialError, RateLimitedError) as exc:
                last_key_error = exc
                with SharedSarvamLimiter._lock:
                    SharedSarvamLimiter._key_unavailable_until[fingerprint] = time.monotonic() + 300.0
                LOGGER.warning("Sarvam key slot %d cooling down for 5 minutes after %s", index + 1, exc.code)
            except ProviderError:
                raise
        if last_key_error is not None:
            raise ProviderError("All configured Sarvam keys are unavailable or quota-limited") from last_key_error
        if attempted:
            raise ProviderError("Sarvam is temporarily unavailable; retry after the provider cooldown")
        raise ProviderError("All configured Sarvam keys are in a 5-minute cooldown")

    def translate_text(self, **options):
        return self._call_with_key_rotation(lambda client: client.text.translate(**options))

    def _chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = self._call_with_key_rotation(lambda client: client.chat.completions(
                model=self.rewrite_model, messages=messages, temperature=0.2, max_tokens=4096,
                response_format={"type": "json_object"},
                request_options={"timeout_in_seconds": 60, "max_retries": 0},
            ))
            choices = getattr(response, "choices", None)
            content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
            if not isinstance(content, str) or not content.strip():
                raise BadInputError("Sarvam chat returned empty structured text")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise BadInputError("Sarvam chat returned an invalid structured response")
            self.models_used.append(self.rewrite_model)
            return result
        except ProviderError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BadInputError("Sarvam returned invalid structured text") from exc

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            system = messages[0]["content"] if messages else ""
            payload = json.loads(messages[-1]["content"]) if len(messages) > 1 else {}
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BadInputError("Sarvam request payload is invalid") from exc
        spans = payload.get("timed_source_spans")
        if "Translate the full source paragraph" in system and isinstance(spans, list):
            target = payload.get("target_language")
            if target not in LANGUAGES:
                raise UnsupportedLanguageError(f"Unsupported target language: {target}")
            style = str(payload.get("translation_style", "natural")).casefold()
            tone = str(payload.get("tone", "")).casefold()
            mode = "formal" if style == "pure" else ("classic-colloquial" if "respectful" in tone else "modern-colloquial")
            if self.translation_model == "sarvam-translate:v1":
                mode = "formal"
            output_script = "fully-native" if style == "pure" else "spoken-form-in-native"
            translations = []
            used_models: list[str] = []
            for span in spans:
                source_language = payload.get("source_language") or "auto"
                if source_language == target:
                    translations.append(str(span.get("source_text", "")).strip())
                    continue
                try:
                    source_parts = _split_at_character_limit(str(span.get("source_text", "")), 1000)
                    translated_parts = []
                    for source_part in source_parts:
                        protected_input, number_tokens = _protect_number_tokens(source_part)
                        translate_options = dict(
                            input=protected_input,
                            source_language_code=source_language,
                            target_language_code=target,
                            model=self.translation_model,
                            mode=mode,
                            request_options={"timeout_in_seconds": 60, "max_retries": 0},
                        )
                        if self.translation_model == "mayura:v1" and target != "en-IN":
                            translate_options["output_script"] = output_script
                        try:
                            result = self.translate_text(**translate_options)
                        except ProviderError:
                            if self.translation_model != "mayura:v1":
                                raise
                            translate_options.update(model="sarvam-translate:v1", mode="formal")
                            translate_options.pop("output_script", None)
                            LOGGER.warning("Mayura translation failed; using sarvam-translate:v1 formal fallback")
                            # A failed Mayura request opens its circuit. The separately
                            # configured formal fallback is still attempted once.
                            result = self.translate_text(**translate_options)
                        translated = getattr(result, "translated_text", None)
                        if not isinstance(translated, str) or not translated.strip():
                            raise ProviderError("Sarvam translation returned no text")
                        translated_parts.append(_restore_number_tokens(translated.strip(), number_tokens))
                        if translate_options["model"] not in used_models:
                            used_models.append(translate_options["model"])
                except ProviderError:
                    raise
                translations.append(" ".join(translated_parts))
            self.models_used.extend(model for model in used_models if model not in self.models_used)
            return {"candidates": [{"translations": translations, "review_notes": []} for _ in range(3)]}
        return self._chat_json(messages)


def create_llm_client(mock: bool = False) -> LLMClient:
    if mock:
        return MockLLMClient()
    from backend.config import get_settings
    return SarvamTranslationClient(get_settings())


def create_translation_client(primary: LLMClient, settings: Settings) -> LLMClient:
    return primary if settings.llm_mock else SarvamTranslationClient(settings)


def create_workflow_client(primary: LLMClient, settings: Settings) -> LLMClient:
    return primary if settings.llm_mock else SarvamTranslationClient(settings)
