"""Single-provider Sarvam text translation and structured chat client."""

from __future__ import annotations

import json
import logging
import random
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


class LLMClient(Protocol):
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


class SharedSarvamLimiter:
    """One process-wide limiter: max 60 starts/minute and three concurrent calls."""

    _semaphore = threading.BoundedSemaphore(3)
    _lock = threading.Lock()
    _starts: deque[float] = deque()
    _unavailable_until = 0.0

    @classmethod
    def call(cls, operation):
        with cls._lock:
            if time.monotonic() < cls._unavailable_until:
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


def _provider_call(operation, *, attempts: int = 4):
    for attempt in range(attempts):
        try:
            result = SharedSarvamLimiter.call(operation)
            with SharedSarvamLimiter._lock:
                SharedSarvamLimiter._unavailable_until = 0.0
            return result
        except Exception as exc:
            if isinstance(exc, ProviderError) and "circuit is open" in str(exc):
                raise
            status = _status_code(exc)
            if status == 429:
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


class SarvamTranslationClient:
    """Sarvam Translate/Mayura for translations and Sarvam chat for structured QA."""

    def __init__(self, settings: Settings):
        if not settings.sarvam_api_key:
            raise ValueError("SARVAM_API_KEY is required")
        from sarvamai import SarvamAI

        self.translation_model = settings.sarvam_translation_model
        self.rewrite_model = settings.sarvam_rewrite_model
        self.model = self.translation_model
        self.models_used: list[str] = []
        self.client = SarvamAI(api_subscription_key=settings.sarvam_api_key, timeout=60.0)

    def _chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        try:
            response = _provider_call(lambda: self.client.chat.completions(
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
            tone = str(payload.get("tone", "")).casefold()
            mode = "classic-colloquial" if "respectful" in tone else "modern-colloquial"
            if self.translation_model == "sarvam-translate:v1":
                mode = "formal"
            translations = []
            for span in spans:
                try:
                    result = _provider_call(lambda span=span: self.client.text.translate(
                        input=span["source_text"],
                        source_language_code=payload.get("source_language") or "auto",
                        target_language_code=target,
                        model=self.translation_model,
                        mode=mode,
                        request_options={"timeout_in_seconds": 60, "max_retries": 0},
                    ))
                except ProviderError:
                    raise
                translated = getattr(result, "translated_text", None)
                if not isinstance(translated, str) or not translated.strip():
                    raise ProviderError("Sarvam translation returned no text")
                translations.append(translated.strip())
            self.models_used.append(self.translation_model)
            return {"candidates": [{"translations": translations, "review_notes": []} for _ in range(3)]}
        return self._chat_json(messages)


class SarvamSpeechClient:
    """Saaras v3 REST transcription. Callers split long audio at silence boundaries."""

    def __init__(self, settings: Settings):
        self.mock = bool(settings.llm_mock)
        if self.mock:
            self.client = None
            return
        if not settings.sarvam_api_key:
            raise ValueError("SARVAM_API_KEY is required")
        from sarvamai import SarvamAI
        self.client = SarvamAI(api_subscription_key=settings.sarvam_api_key, timeout=60.0)

    def stt(self, audio: bytes, language_code: str = "unknown", **options) -> dict[str, Any]:
        if self.mock:
            return {"transcript": "Synthetic mock transcript", "detected_language_code": language_code,
                    "timestamps": None}
        try:
            result = _provider_call(lambda: self.client.speech_to_text.transcribe(
                file=("audio.wav", audio, "audio/wav"),
                model="saaras:v3", mode="transcribe", language_code=language_code,
                with_timestamps=True, input_audio_codec="wav",
                request_options={"timeout_in_seconds": 60, "max_retries": 0},
            ))
        except ProviderError:
            raise
        transcript = getattr(result, "transcript", None)
        if not isinstance(transcript, str):
            raise BadInputError("Sarvam transcription returned no transcript")
        timestamps = getattr(result, "timestamps", None)
        return {"transcript": transcript,
                "detected_language_code": getattr(result, "language_code", None) or language_code,
                "timestamps": timestamps.model_dump() if hasattr(timestamps, "model_dump") else timestamps}


def create_llm_client(mock: bool = False) -> LLMClient:
    if mock:
        return MockLLMClient()
    from backend.config import get_settings
    return SarvamTranslationClient(get_settings())


def create_translation_client(primary: LLMClient, settings: Settings) -> LLMClient:
    return primary if settings.llm_mock else SarvamTranslationClient(settings)


def create_workflow_client(primary: LLMClient, settings: Settings) -> LLMClient:
    return primary if settings.llm_mock else SarvamTranslationClient(settings)
