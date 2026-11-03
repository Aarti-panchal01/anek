"""Central prompt templates for structured translation and term operations."""

TERM_SHEET_SYSTEM = """Identify named entities, brands, products, people, places, units, numbers, jargon, and idioms. Return one JSON object with a 'terms' array and an 'idioms' array. Each term has term, type, treatment (keep/transliterate/translate), targets (language-code to approved form), locked, note. The only allowed type values are brand, person, place, number, unit, jargon, idiom. The only allowed treatment values are keep, transliterate, translate. Use only provided transcript content; do not invent terms."""

TRANSLATE_SYSTEM = """Translate for meaning and natural register, not word-for-word. Preserve all numbers, dates, currency, phone digits, and units exactly. Apply every protected term as specified. Be concise enough for the provided character budget. Return JSON with translation, terms_used, and notes."""

AUTOCORRECT_SYSTEM = """Fix only the listed validation failures by retranslating the source. Preserve all numbers, locked terms, and meaning. Return JSON with translation, terms_used, and notes."""


def translation_messages(
    clip_text: str,
    target_lang: str,
    source_lang: str,
    term_sheet: dict,
    tone_hint: str = "neutral",
    max_chars: int = 800,
    context: str = "",
    failures: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    system = AUTOCORRECT_SYSTEM if failures else TRANSLATE_SYSTEM
    payload = {
        "source_text": clip_text,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "tone_hint": tone_hint,
        "max_chars": max_chars,
        "neighbor_context": context,
        "term_sheet": term_sheet,
        "validation_failures": failures or [],
    }
    import json

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
