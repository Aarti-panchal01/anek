"""Term-sheet extraction and response validation."""

from typing import Literal
import re

from pydantic import BaseModel, Field, field_validator

from backend.brain.prompts import TERM_SHEET_SYSTEM
from backend.clients.llm import LLMClient

_LOANWORD_TARGETS = {
    "wig": {
        "bn-IN": "উইগ", "en-IN": "wig", "gu-IN": "વિગ", "hi-IN": "विग",
        "kn-IN": "ವಿಗ್", "ml-IN": "വിഗ്", "mr-IN": "विग", "pa-IN": "ਵਿੱਗ",
        "ta-IN": "விக்", "te-IN": "విగ్",
    },
}


class Term(BaseModel):
    term: str
    type: Literal["brand", "person", "place", "number", "unit", "jargon", "idiom"]
    treatment: Literal["keep", "transliterate", "translate"]
    targets: dict[str, str] = Field(default_factory=dict)
    locked: bool = False
    note: str = ""
    rule_type: Literal["standard", "protected", "mishearing"] = "standard"
    heard_variants: list[str] = Field(default_factory=list)
    spoken_forms: dict[str, str] = Field(default_factory=dict)
    save_global: bool = False

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        aliases = {
            "product": "brand",
            "company": "brand",
            "medical": "jargon",
            "profession": "jargon",
            "technical": "jargon",
        }
        return aliases.get(value, value)


class Idiom(BaseModel):
    phrase: str
    meaning: str
    clip_ids: list[int] = Field(default_factory=list)


class TermSheet(BaseModel):
    terms: list[Term] = Field(default_factory=list)
    idioms: list[Idiom] = Field(default_factory=list)


def apply_mishearing_maps(text: str, termsheet: TermSheet | dict | None) -> str:
    """Replace heard variants with canonical terms before source translation."""
    sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet or {})
    for term in sheet.terms:
        if term.rule_type != "mishearing":
            continue
        for variant in sorted(term.heard_variants, key=len, reverse=True):
            if variant.strip():
                text = re.sub(rf"(?<!\w){re.escape(variant.strip())}(?!\w)", term.term, text, flags=re.IGNORECASE)
    return text


def mask_protected_terms(text: str, termsheet: TermSheet | dict | None) -> tuple[str, dict[str, str]]:
    """Mask protected terms with stable placeholders for translation."""
    sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet or {})
    mapping: dict[str, str] = {}
    for index, term in enumerate(sheet.terms):
        if term.rule_type != "protected" and not term.locked:
            continue
        placeholder = f"ANEKPROTECTEDTERM{index:04d}"
        pattern = rf"(?<!\w){re.escape(term.term)}(?!\w)"
        text, count = re.subn(pattern, placeholder, text, flags=re.IGNORECASE)
        if count:
            mapping[placeholder] = term.term
    return text, mapping


def restore_protected_terms(text: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
    missing = []
    for placeholder, canonical in mapping.items():
        if placeholder in text:
            text = text.replace(placeholder, canonical)
        elif canonical.casefold() not in text.casefold():
            missing.append(canonical)
    return text, missing


def apply_spoken_forms(text: str, termsheet: TermSheet | dict | None, language: str) -> str:
    sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet or {})
    for term in sheet.terms:
        spoken = term.spoken_forms.get(language)
        if spoken:
            text = re.sub(rf"(?<!\w){re.escape(term.term)}(?!\w)", spoken, text, flags=re.IGNORECASE)
    return text


def extract_termsheet(
    client: LLMClient, clips: list[str], source_lang: str, glossary: list[str] | None = None
) -> TermSheet:
    import json

    payload = {
        "clips": clips,
        "source_lang": source_lang,
        "glossary": glossary or [],
    }
    messages = [
        {"role": "system", "content": TERM_SHEET_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    for attempt in range(2):
        result = client.chat_json(messages)
        try:
            sheet = TermSheet.model_validate(result)
            for term in sheet.terms:
                if term.locked or term.type in {"brand", "person", "place", "number", "unit"}:
                    continue
                normalized = re.sub(r"[^A-Za-z0-9]", "", term.term)
                if normalized and normalized.isupper():
                    term.treatment = "keep"
                    continue
                loanword_targets = _LOANWORD_TARGETS.get(term.term.strip().casefold())
                if loanword_targets:
                    term.type = "jargon"
                    term.treatment = "transliterate"
                    term.targets = loanword_targets.copy()
                    term.note = "Common loanword; render in the target script for synthesis."
                else:
                    # Ordinary phrases and generic jargon must translate; they are not locked English terms.
                    term.treatment = "translate"
                    term.targets = {}
                    term.note = "Generic phrase; translate naturally in context."
            return sheet
        except Exception:
            if attempt:
                raise
            messages.append({"role": "assistant", "content": json.dumps(result)})
            messages.append(
                {"role": "user", "content": "Return the same data as valid JSON matching the required schema."}
            )
    raise RuntimeError("Term sheet validation failed")
