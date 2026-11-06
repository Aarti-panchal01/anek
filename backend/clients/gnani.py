"""Gnani REST client using the request shapes in its public API docs."""

import json
import math
import struct
import time
import wave
from io import BytesIO
from typing import Protocol

import httpx

from backend.config import Settings


class GnaniClient(Protocol):
    def stt(self, audio: bytes, language_code: str, model: str) -> dict: ...

    def make_embedding(self, audio: bytes) -> dict: ...

    def synth(self, text: str, speaker_embedding: dict) -> bytes: ...

    def synth_stock(self, text: str, voice: str, language: str, speed: float = 1.0) -> bytes: ...


class MockGnaniClient:
    def stt(self, audio: bytes, language_code: str, model: str) -> dict:
        return {"text": "Synthetic mock transcript", "language_code": language_code, "model": model}

    def make_embedding(self, audio: bytes) -> dict:
        return {"embedding": "mock-embedding", "shape": [1, 768], "dtype": "mock"}

    def synth(self, text: str, speaker_embedding: dict) -> bytes:
        return self._wav()

    def synth_stock(self, text: str, voice: str, language: str, speed: float = 1.0) -> bytes:
        return self._wav()

    @staticmethod
    def _wav() -> bytes:
        """Return a short valid tone so browser and ffmpeg smoke tests can play mocks."""
        sample_rate = 24_000
        frames = 9_600
        output = BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(b"".join(
                struct.pack("<h", int(4_000 * math.sin(2 * math.pi * 440 * index / sample_rate)))
                for index in range(frames)
            ))
        return output.getvalue()


class LiveGnaniClient:
    def __init__(self, settings: Settings, timeout: float = 60.0):
        if not settings.gnani_api_key:
            raise ValueError("GNANI_API_KEY is required for live requests")
        self.base_url = settings.gnani_base_url.rstrip("/")
        self.headers = {"X-API-Key-ID": settings.gnani_api_key}
        self.timeout = timeout

    def stt(
        self,
        audio: bytes,
        language_code: str,
        model: str,
        keyterms: list[str] | None = None,
        format: str | None = None,
    ) -> dict:
        form = {"model": model, "language_code": language_code}
        if format is not None:
            form["format"] = format
        if keyterms:
            form["keyterms"] = json.dumps(keyterms, ensure_ascii=False)
        for attempt in range(2):
            response = httpx.post(
                f"{self.base_url}/stt/v3",
                headers=self.headers,
                files={"audio_file": ("clip.wav", audio, "audio/wav")},
                data=form,
                timeout=self.timeout,
            )
            if response.status_code not in {502, 503, 504} or attempt == 1:
                break
            time.sleep(1.5)
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError(f"Gnani STT failed: {body.get('error', body)}")
        return body

    def make_embedding(self, audio: bytes) -> dict:
        response = httpx.post(
            f"{self.base_url}/api/v1/tts/voice-clone/embeddings",
            headers=self.headers,
            files={"audio_file": ("reference.wav", audio, "audio/wav")},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError(f"Gnani embedding failed: {body}")
        return body["data"]["voice_clone_embedding"]

    def synth(self, text: str, speaker_embedding: dict) -> bytes:
        if len(text) >= 300:
            raise ValueError("Voice-clone synthesis text must be under 300 characters")
        payload = {
            "text": text,
            "model": "vachana-vc-v1",
            "audio_config": {
                "sample_rate": 44100,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": "wav",
            },
            "speaker_embedding": speaker_embedding,
        }
        response = httpx.post(
            f"{self.base_url}/api/v1/tts/inference",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.content

    def synth_stock(self, text: str, voice: str, language: str, speed: float = 1.0) -> bytes:
        if len(text) >= 800:
            raise ValueError("Timbre synthesis text must be under 800 characters")
        if not 0.85 <= speed <= 1.15:
            raise ValueError("Timbre v3 speed must be between 0.85 and 1.15")
        payload = {
            "text": text,
            "voice": voice,
            "model": "timbre-v3.0",
            "language": language,
            "speed": speed,
            "audio_config": {
                "sample_rate": 48000,
                "num_channels": 1,
                "sample_width": 2,
                "encoding": "linear_pcm",
                "container": "wav",
            },
        }
        response = httpx.post(
            f"{self.base_url}/api/v1/tts/inference",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.content


def create_gnani_client(mock: bool = False) -> GnaniClient:
    if mock:
        return MockGnaniClient()
    from backend.config import get_settings

    return LiveGnaniClient(get_settings())
