"""Print a no-score Hindi translation comparison for Groq and Gemini."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from backend.clients.llm import LiveLLMClient
from backend.config import get_settings

ADDED_SENTENCE = "Hair loss during cancer treatment can change how someone feels about their appearance."
STYLE_GUIDE = """Translate into natural spoken Hindi as an educated speaker would say it.
Translate ordinary words instead of copying English clauses. Write common loanwords such as cancer, doctor, app, wig, phone, and video in Devanagari for speech synthesis (कैंसर, डॉक्टर, ऐप, विग, फ़ोन, वीडियो), unless an approved term sheet says otherwise. Do not transliterate abstract English words. Preserve the speaker's person, agency, actions, objects, modality, polarity, fillers, and casual register. Do not omit or add meaning to fit. The phrase 'built a head' is uncertain ASR wording: translate its literal best reading and mark it for human review; do not guess a different noun or phrase. Return JSON with translation and review_note strings."""


def load_sentences(report_path: Path) -> list[tuple[str, str]]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for clip in report.get("clips", []):
        source = str(clip.get("source_cleaned") or clip.get("source_raw") or "").strip()
        for sentence in re.split(r"(?<=[.!?])\s+", source):
            sentence = sentence.strip()
            if sentence:
                found.append((f"Clip {clip.get('clip', len(found) + 1)}", sentence))
    found.append(("Added comparison", ADDED_SENTENCE))
    return found


def translate(client: LiveLLMClient, source: str) -> tuple[str, str]:
    messages = [
        {"role": "system", "content": STYLE_GUIDE},
        {"role": "user", "content": f"Translate this source into Hindi. Source: {source}"},
    ]
    for attempt in range(4):
        try:
            response = client.chat_json(messages)
            break
        except RuntimeError as exc:
            message = str(exc)
            if attempt == 3:
                raise
            if "HTTP 503" in message:
                time.sleep(2 ** (attempt + 1))
                continue
            quota_retry = re.search(r"HTTP 429; retry_in=(\d+(?:\.\d+)?)", message)
            if quota_retry:
                delay = float(quota_retry.group(1))
                if delay > 120:
                    raise
                time.sleep(max(1.0, delay + 1.0))
                continue
            raise
    translation = response.get("translation")
    note = response.get("review_note", "")
    if not isinstance(translation, str) or not translation.strip():
        raise ValueError("Provider returned no translation string")
    return translation.strip(), str(note).strip()


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clip-report",
        type=Path,
        default=Path("samples/test_clip_hi-IN_phase3c_ai-dubbed.json"),
        help="Phase 3c report containing the reviewed source transcript",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/translation_bakeoff_hindi.md"),
    )
    parser.add_argument(
        "--gemini-model",
        default=None,
        help="Override LLM_2_MODEL for this comparison (for example, gemini-3.5-flash)",
    )
    args = parser.parse_args()

    settings = get_settings()
    gemini_keys = settings.llm_2_api_keys or settings.llm_2_api_key
    if not gemini_keys:
        raise SystemExit("LLM_2_API_KEYS (or LLM_2_API_KEY) is missing or empty")
    groq = LiveLLMClient(settings)
    gemini_model = args.gemini_model or settings.llm_2_model
    gemini_settings = settings.model_copy(update={
        "llm_base_url": settings.llm_2_base_url,
        "llm_model": gemini_model,
        "llm_api_key": "",
        "groq_api_keys": gemini_keys,
    })
    gemini = LiveLLMClient(gemini_settings)

    rows: list[str] = [
        "| # | Source | Groq (`" + settings.llm_model + "`) | Gemini (`" + gemini_model + "`) |",
        "|---:|---|---|---|",
    ]
    for index, (origin, source) in enumerate(load_sentences(args.clip_report), start=1):
        outputs: dict[str, str] = {}
        for label, client in (("Groq", groq), ("Gemini", gemini)):
            try:
                translation, note = translate(client, source)
                outputs[label] = translation + (f" _(Review: {note})_" if note else "")
            except Exception as exc:  # Keep the other provider's output useful if one is unavailable.
                outputs[label] = f"PROVIDER ERROR: {str(exc)[:240]}"
        rows.append(
            f"| {index} · {markdown_cell(origin)} | {markdown_cell(source)} "
            f"| {markdown_cell(outputs['Groq'])} | {markdown_cell(outputs['Gemini'])} |"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(rows) + "\n"
    args.output.write_text(content, encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(content, end="")
    print(f"\nSaved comparison: {args.output.resolve()}")


if __name__ == "__main__":
    main()
