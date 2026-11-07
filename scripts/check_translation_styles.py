"""Compare Mayura Natural and Pure output for fixed geography sample lines."""

from __future__ import annotations

import os
import unicodedata

from sarvamai import SarvamAI


SENTENCES = [
    "South Central part of Asia, if you are taking the location or referring to the location with respect to the continents.",
    "So we have studied two things. India is located on the northeastern hemisphere, along with it,",
    "India is located on the south central part of Asia. These two things form a very basic premise of",
]
LANGUAGES = {"hi-IN": "Hindi", "kn-IN": "Kannada"}


def latin_share(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    return (sum("LATIN" in unicodedata.name(char, "") for char in letters) / len(letters)) if letters else 0.0


def main() -> None:
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        raise SystemExit("SARVAM_API_KEY is required")
    client = SarvamAI(api_subscription_key=key)
    for code, name in LANGUAGES.items():
        for style, mode, output_script in (
            ("Natural", "modern-colloquial", "spoken-form-in-native"),
            ("Pure", "formal", "fully-native"),
        ):
            print(f"{name} | {style}")
            for sentence in SENTENCES:
                result = client.text.translate(
                    input=sentence,
                    source_language_code="en-IN",
                    target_language_code=code,
                    model="mayura:v1",
                    mode=mode,
                    output_script=output_script,
                )
                text = result.translated_text
                print(f"{text} | Latin letter share: {latin_share(text):.1%}")


if __name__ == "__main__":
    main()
