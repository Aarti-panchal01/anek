"""Normalize numeral scripts and extract stable numeric tokens for checks."""

import re
from collections import Counter

_DIGIT_BASES = (0x0966, 0x09E6, 0x0AE6, 0x0A66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66)
_DIGITS = str.maketrans(
    {ord(chr(base + digit)): str(digit) for base in _DIGIT_BASES for digit in range(10)}
)
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "एक": "1", "एकम्": "1", "दो": "2", "दोनों": "2", "दोनो": "2", "तीन": "3", "चार": "4",
    "ಒಂದು": "1", "ಒಂದೇ": "1", "ಎರಡು": "2", "ಮೂರು": "3", "ನಾಲ್ಕು": "4",
}


def normalize_digits(text: str) -> str:
    return text.translate(_DIGITS)


def extract_numbers(text: str) -> list[str]:
    normalized = normalize_digits(text)
    tokens = re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:/\d+(?:[.,]\d+)*)?(?!\w)", normalized)
    tokens = [token.replace(",", "") for token in tokens]
    # Indic vowel signs are Unicode combining marks and Python's `\w` does
    # not include them. Keep each Indic word intact so forms like Hindi `दो`
    # and Kannada `ಎರಡು` can match number words.
    words = re.findall(r"[\w\u0900-\u0dff]+|[^\W\d_]+", normalized.casefold(), flags=re.UNICODE)
    return tokens + [_NUMBER_WORDS[word] for word in words if word in _NUMBER_WORDS]


def compare_numbers(source: str, target: str) -> list[dict[str, str]]:
    expected = Counter(extract_numbers(source))
    actual = Counter(extract_numbers(target))
    failures = []
    for number, count in (expected - actual).items():
        failures.extend({"check": "number_missing", "detail": number} for _ in range(count))
    for number, count in (actual - expected).items():
        failures.extend({"check": "number_extra", "detail": number} for _ in range(count))
    return failures
