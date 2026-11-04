"""Deterministic translation checks used before synthesis."""

import re

from backend.brain.numbers import compare_numbers
from backend.brain.termsheet import TermSheet

_SCRIPT_RANGES = {
    "bn-IN": (0x0980, 0x09FF),
    "gu-IN": (0x0A80, 0x0AFF),
    "hi-IN": (0x0900, 0x097F),
    "kn-IN": (0x0C80, 0x0CFF),
    "ml-IN": (0x0D00, 0x0D7F),
    "mr-IN": (0x0900, 0x097F),
    "pa-IN": (0x0A00, 0x0A7F),
    "ta-IN": (0x0B80, 0x0BFF),
    "te-IN": (0x0C00, 0x0C7F),
}


def _is_target_script(char: str, lang: str) -> bool:
    script_range = _SCRIPT_RANGES.get(lang)
    return script_range is not None and script_range[0] <= ord(char) <= script_range[1]


def run_checks(
    source: str,
    translation: str,
    target_lang: str,
    termsheet: TermSheet | dict | None = None,
    max_chars: int = 800,
    slot_seconds: float | None = None,
    chars_per_second: float = 12.0,
    fit_tolerance: float = 1.15,
) -> list[dict[str, str]]:
    failures = compare_numbers(source, translation)
    if termsheet:
        sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet)
        for term in sheet.terms:
            expected = term.targets.get(target_lang, term.term)
            if term.locked or term.treatment == "keep":
                if expected not in translation:
                    failures.append({"check": "term_missing", "detail": expected})
            elif term.treatment == "transliterate" and expected not in translation:
                failures.append({"check": "term_form_missing", "detail": expected})
            elif term.treatment == "translate" and term.targets.get(target_lang) and expected not in translation:
                failures.append({"check": "term_translation_missing", "detail": expected})
    if target_lang != "en-IN" and translation:
        script_text = translation
        if termsheet:
            sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet)
            for term in sheet.terms:
                if term.treatment == "keep":
                    script_text = script_text.replace(term.targets.get(target_lang, term.term), " ")
        chars = [char for char in script_text if char.isalpha()]
        if chars:
            script_chars = sum(_is_target_script(char, target_lang) for char in chars)
            # Spoken translations may retain common English terms in Latin
            # script; still require a meaningful target-script signal.
            if script_chars / len(chars) < 0.25:
                failures.append({"check": "wrong_script", "detail": target_lang})
    if len(translation) > max_chars:
        failures.append({"check": "too_many_characters", "detail": str(len(translation))})
    if slot_seconds is not None and len(translation) / chars_per_second > slot_seconds * fit_tolerance:
        failures.append({"check": "over_slot", "detail": f"{len(translation)} chars"})
    return failures
