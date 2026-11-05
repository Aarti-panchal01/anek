"""Map verification findings to human-readable clip statuses."""

from backend.brain.numbers import compare_numbers


def audio_number_status(
    translation: str,
    transcript: str,
    lang: str,
    low_trust_langs: list[str] | None = None,
) -> dict[str, object]:
    mismatches = compare_numbers(translation, transcript)
    if not mismatches:
        return {"status": "green", "reasons": []}
    if lang in (low_trust_langs or []):
        return {
            "status": "yellow",
            "reasons": [
                "Verification confidence is lower for numbers in this language; listen to the dubbed audio."
            ],
        }
    return {
        "status": "red",
        "reasons": [f"Audio number mismatch: {item['detail']}" for item in mismatches],
    }
