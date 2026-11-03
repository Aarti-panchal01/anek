"""Bounded translation retries with an audit log of every attempted change."""

from typing import Any

from backend.brain.checks import run_checks
from backend.brain.prompts import translation_messages
from backend.brain.termsheet import TermSheet
from backend.clients.llm import LLMClient
from backend.verify.flags import audio_number_status


def translate_with_autocorrect(
    client: LLMClient,
    source: str,
    source_lang: str,
    target_lang: str,
    termsheet: TermSheet | dict | None = None,
    max_chars: int = 800,
    tone_hint: str = "neutral",
    max_retries: int = 2,
) -> dict[str, Any]:
    sheet = termsheet if isinstance(termsheet, TermSheet) else TermSheet.model_validate(termsheet or {})
    messages = translation_messages(source, target_lang, source_lang, sheet.model_dump(), tone_hint, max_chars)
    attempts = []
    result = client.chat_json(messages)
    current = result["translation"]
    failures = run_checks(source, current, target_lang, sheet, max_chars)

    for attempt_number in range(1, max_retries + 1):
        if not failures:
            break
        retry_messages = translation_messages(
            source,
            target_lang,
            source_lang,
            sheet.model_dump(),
            tone_hint,
            max_chars,
            failures=failures,
        )
        new_result = client.chat_json(retry_messages)
        new_translation = new_result["translation"]
        new_failures = run_checks(source, new_translation, target_lang, sheet, max_chars)
        attempts.append(
            {
                "n": attempt_number,
                "trigger_checks": failures,
                "old_text": current,
                "new_text": new_translation,
                "result": "passed" if not new_failures else "failed",
            }
        )
        current, failures = new_translation, new_failures

    status = "red" if failures else ("yellow" if attempts else "green")
    reasons = [f"{failure['check']}: {failure['detail']}" for failure in failures]
    if attempts and not failures:
        reasons.append("Auto-corrected; please review the change.")
    return {
        "translation": current,
        "draft_translation": current,
        "attempts": attempts,
        "checks": failures,
        "status": status,
        "reasons": reasons,
    }


def combine_audio_verification(
    translation: str, transcript: str, lang: str, text_status: dict[str, Any], low_trust_langs: list[str]
) -> dict[str, Any]:
    audio_result = audio_number_status(translation, transcript, lang, low_trust_langs)
    if text_status["status"] == "red":
        return text_status
    if audio_result["status"] == "red":
        return audio_result
    if audio_result["status"] == "yellow" or text_status["status"] == "yellow":
        return {
            "status": "yellow",
            "reasons": list(text_status["reasons"]) + list(audio_result["reasons"]),
        }
    return {"status": "green", "reasons": []}
