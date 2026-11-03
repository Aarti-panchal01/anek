"""Normalize numeral scripts and extract stable numeric tokens for checks."""

import re
from collections import Counter

_DIGIT_BASES = (0x0966, 0x09E6, 0x0AE6, 0x0A66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66)
_DIGITS = str.maketrans(
    {ord(chr(base + digit)): str(digit) for base in _DIGIT_BASES for digit in range(10)}
)


def normalize_digits(text: str) -> str:
    return text.translate(_DIGITS)


def extract_numbers(text: str) -> list[str]:
    normalized = normalize_digits(text)
    tokens = re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?:/\d+(?:[.,]\d+)*)?(?!\w)", normalized)
    return [token.replace(",", "") for token in tokens]


def compare_numbers(source: str, target: str) -> list[dict[str, str]]:
    expected = Counter(extract_numbers(source))
    actual = Counter(extract_numbers(target))
    failures = []
    for number, count in (expected - actual).items():
        failures.extend({"check": "number_missing", "detail": number} for _ in range(count))
    for number, count in (actual - expected).items():
        failures.extend({"check": "number_extra", "detail": number} for _ in range(count))
    return failures
