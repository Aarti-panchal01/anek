"""Compare Natural and Pure Mayura output across configured target languages."""

from __future__ import annotations

import os
import argparse
import sys
import unicodedata
from pathlib import Path

from sarvamai import SarvamAI
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.brain.numbers import compare_numbers
from backend.config import LANGUAGES


SENTENCES = [
    "South Central part of Asia, if you are taking the location or referring to the location with respect to the continents.",
    "So we have studied two things. India is located on the northeastern hemisphere, along with it,",
    "India is located on the south central part of Asia. These two things form a very basic premise of",
    "Zorvo's annual plan costs Rs 1,00,000, and you can reach us on 98765 43210 between 9 AM and 6 PM.",
    "हमने दो चीज़ें पढ़ीं और भारत उत्तरी गोलार्ध में स्थित है।",
]
HINDI_SENTENCES = [
    "एशिया का दक्षिण मध्य भाग, यदि आप स्थान ले रहे हैं या महाद्वीपों के संदर्भ में स्थान का उल्लेख कर रहे हैं।",
    "हमने दो चीज़ें पढ़ीं। भारत उत्तर-पूर्वी गोलार्ध में स्थित है, इसके साथ ही,",
    "भारत एशिया के दक्षिण मध्य भाग में स्थित है। ये दोनों बातें एक बहुत बुनियादी आधार बनाती हैं।",
    "ज़ोर्वो की वार्षिक योजना की कीमत 1,00,000 रुपये है। आप हमसे 98765 43210 पर सुबह 9 बजे से शाम 6 बजे के बीच संपर्क कर सकते हैं।",
    "हमने दो चीज़ें पढ़ीं और भारत उत्तरी गोलार्ध में स्थित है।",
]
LANGUAGE_NAMES = {"bn-IN":"Bengali", "en-IN":"English", "gu-IN":"Gujarati", "hi-IN":"Hindi", "kn-IN":"Kannada", "ml-IN":"Malayalam", "mr-IN":"Marathi", "pa-IN":"Punjabi", "ta-IN":"Tamil", "te-IN":"Telugu"}
NUMBER_SENTENCE = 4
NUMBER_SOURCE = "Zorvo's annual plan costs Rs 1,00,000, and you can reach us on 98765 43210 between 9 AM and 6 PM."


def latin_share(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    return (sum("LATIN" in unicodedata.name(char, "") for char in letters) / len(letters)) if letters else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--langs", nargs="+", choices=LANGUAGES, default=list(LANGUAGES))
    parser.add_argument("--style", choices=("Natural", "Pure", "both"), default="both")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    key = os.environ.get("SARVAM_API_KEY")
    if not key:
        raise SystemExit("SARVAM_API_KEY is required")
    client = SarvamAI(api_subscription_key=key)
    styles = (
            ("Natural", "modern-colloquial", "spoken-form-in-native"),
            ("Pure", "formal", "fully-native"),
    )
    if args.style != "both":
        styles = tuple(s for s in styles if s[0] == args.style)
    output = []
    summaries = []
    for code in args.langs:
        name = LANGUAGE_NAMES[code]
        for style, mode, output_script in styles:
            shares = []
            number_result = "n/a"
            for index, sentence in enumerate(SENTENCES, 1):
                source_text = HINDI_SENTENCES[index - 1] if code == "en-IN" else sentence
                source_code = "hi-IN" if code == "en-IN" else "en-IN"
                try:
                    result = client.text.translate(
                        input=source_text,
                        source_language_code=source_code,
                        target_language_code=code,
                        model="mayura:v1",
                        mode=mode,
                        output_script=output_script,
                    )
                    translated = result.translated_text
                except Exception as exc:
                    output.append(f"{name} | {style} | sentence {index} | REQUEST ERROR: {type(exc).__name__}: {exc}")
                    if index == NUMBER_SENTENCE:
                        number_result = "REQUEST ERROR"
                    continue
                share = latin_share(translated)
                shares.append(share)
                output.append(f"{name} | {style} | sentence {index} | {translated} | Latin letter share: {share:.1%}")
                if index == NUMBER_SENTENCE:
                    missing = compare_numbers(source_text, translated)
                    number_result = "PASS" if not missing else "FAIL: " + ", ".join(item.get("detail", "number") for item in missing)
                    kinds = []
                    for token in (("1,00,000", "98765 43210", "9 AM", "6 PM") if code != "en-IN" else ("1,00,000", "98765 43210", "सुबह 9", "शाम 6")):
                        kinds.append(f"{token}={_number_form(translated, token, not missing)}")
                    output.append("  Number forms: " + "; ".join(kinds) + f" | app checker: {number_result}")
            summaries.append((name, style, max(shares, default=0.0), number_result))
    output.append("\nSUMMARY (ASCII)")
    output.append("language | style | max_latin_share | number_check")
    output.extend(f"{name} | {style} | {share:.1%} | {check}" for name, style, share, check in summaries)
    rendered = "\n".join(output)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")


def _number_form(text: str, source: str, passed: bool) -> str:
    if source in text:
        return "digits"
    native_digits = "०१२३४५६७८९٠١٢٣٤٥٦٧٨٩"
    if any(ch in native_digits for ch in text):
        return "native numerals"
    return "number words" if passed else "missing"


if __name__ == "__main__":
    main()
