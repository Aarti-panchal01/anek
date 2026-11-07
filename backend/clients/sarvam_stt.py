"""Sarvam Saaras source speech recognition client."""

from __future__ import annotations

from io import BytesIO

import httpx

from backend.config import Settings


class SarvamSTTClient:
    def __init__(self, settings: Settings, timeout: float = 90.0):
        keys = settings.resolved_sarvam_api_keys
        if not keys:
            raise ValueError("SARVAM_API_KEY is required for source transcription")
        self.api_key = keys[0]
        self.base_url = "https://api.sarvam.ai"
        self.timeout = timeout

    def transcribe(self, audio: bytes, language_code: str, model: str = "saaras:v3", **_options) -> dict:
        data = {"model": model}
        if language_code and language_code not in {"auto", "unknown"}:
            data["language_code"] = language_code
        response = httpx.post(
            f"{self.base_url}/speech-to-text",
            headers={"api-subscription-key": self.api_key},
            files={"file": ("chunk.wav", BytesIO(audio), "audio/wav")},
            data=data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        transcript = body.get("transcript") or body.get("text")
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("Sarvam source transcription returned no transcript text")
        return {"transcript": transcript.strip(), "detected_language_code": body.get("language_code") or language_code}


class MockSarvamSTTClient:
    def transcribe(self, audio: bytes, language_code: str, model: str = "saaras:v3", **_options) -> dict:
        return {"transcript": "Synthetic mock transcript", "detected_language_code": language_code}


def create_sarvam_stt_client(settings: Settings):
    if settings.llm_mock:
        return MockSarvamSTTClient()
    return SarvamSTTClient(settings)
