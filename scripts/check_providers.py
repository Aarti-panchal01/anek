"""Check configured live provider reachability without displaying credentials."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.config import LANGUAGES, get_settings
from backend.clients.llm import SarvamTranslationClient


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
    try:
        client = SarvamTranslationClient(settings)
    except ValueError:
        print("Sarvam translation: FAILED (not configured)")
        return 1
    failed = False
    for code, name in LANGUAGES.items():
        try:
            source_code = "hi-IN" if code == "en-IN" else "en-IN"
            source_text = "नमस्ते, यह एक छोटा प्रदाता परीक्षण है।" if source_code == "hi-IN" else "Hello, this is a short provider check."
            result = client.translate_text(
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
    print(f"GNANI_API_KEY: {'CONFIGURED' if settings.gnani_api_key else 'MISSING'} (Gnani STT/TTS credentials)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
