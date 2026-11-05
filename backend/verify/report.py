"""Build a small, honest verification report for one language run."""

from backend.config import Settings


def build_report(language: str, clips: list[dict], settings: Settings) -> dict:
    low_trust = language in settings.low_trust_verify_langs
    return {
        "language": language,
        "verification": "low trust" if low_trust else "standard",
        "verification_note": (
            "Number mismatches are flagged for review in Marathi and Punjabi; listen to the audio."
            if low_trust
            else None
        ),
        "clips": clips,
    }
