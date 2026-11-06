"""Check configured live provider reachability without displaying credentials."""
from __future__ import annotations

import os
import sys
import wave
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.config import LANGUAGES, get_settings


def _short_wav() -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 4_000)
    return stream.getvalue()


def _category(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401 or status == 403:
        return "authentication rejected"
    if status == 429:
        return "rate limited or quota exhausted"
    if status and int(status) >= 500:
        return "provider unavailable"
    if status:
        return f"HTTP {int(status)}"
    return "request failed"


def main() -> int:
    settings = get_settings()
    if not settings.sarvam_api_key:
        print("SARVAM_API_KEY: FAILED (not configured)")
        return 1
    from sarvamai import SarvamAI

    client = SarvamAI(api_subscription_key=settings.sarvam_api_key, timeout=45.0)
    failed = False
    for code, name in LANGUAGES.items():
        try:
            source_code = "hi-IN" if code == "en-IN" else "en-IN"
            source_text = "नमस्ते, यह एक छोटा प्रदाता परीक्षण है।" if source_code == "hi-IN" else "Hello, this is a short provider check."
            result = client.text.translate(
                input=source_text,
                source_language_code=source_code, target_language_code=code,
                model=settings.sarvam_translation_model, mode="formal",
                request_options={"timeout_in_seconds": 45, "max_retries": 0},
            )
            if not getattr(result, "translated_text", "").strip():
                raise RuntimeError("empty response")
            print(f"{name}: OK")
        except Exception as exc:
            failed = True
            print(f"{name}: FAILED ({_category(exc)})")
    try:
        transcript = client.speech_to_text.transcribe(
            file=("provider-check.wav", _short_wav(), "audio/wav"),
            model="saaras:v3", mode="transcribe", language_code="unknown",
            with_timestamps=True, input_audio_codec="wav",
            request_options={"timeout_in_seconds": 45, "max_retries": 0},
        )
        if not hasattr(transcript, "transcript"):
            raise RuntimeError("invalid response")
        print("Saaras STT: OK")
    except Exception as exc:
        failed = True
        print(f"Saaras STT: FAILED ({_category(exc)})")
    print(f"GNANI_API_KEY: {'CONFIGURED' if settings.gnani_api_key else 'MISSING'} (Gnani runtime auth check not requested by this script)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
