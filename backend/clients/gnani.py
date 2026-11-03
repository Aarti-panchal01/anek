"""Gnani client boundary; live request methods are added after doc verification."""

from typing import Protocol


class GnaniClient(Protocol):
    def stt(self, audio: bytes, language_code: str, model: str) -> dict: ...

    def make_embedding(self, audio: bytes) -> dict: ...

    def synth(self, text: str, speaker_embedding: dict) -> bytes: ...


class MockGnaniClient:
    def stt(self, audio: bytes, language_code: str, model: str) -> dict:
        return {"text": "Synthetic mock transcript", "language_code": language_code, "model": model}

    def make_embedding(self, audio: bytes) -> dict:
        return {"embedding": "mock-embedding", "shape": [1, 768], "dtype": "mock"}

    def synth(self, text: str, speaker_embedding: dict) -> bytes:
        return b"mock-audio"


def create_gnani_client(mock: bool = False) -> GnaniClient:
    if mock:
        return MockGnaniClient()
    raise NotImplementedError("Live Gnani methods require the documented request shapes.")
