"""Run the text-only Brain against a JSON clip fixture."""

import argparse
import json
import sys
from pathlib import Path

from backend.brain.autocorrect import translate_with_autocorrect
from backend.brain.termsheet import TermSheet
from backend.clients.llm import MockLLMClient, create_llm_client
from backend.config import get_settings


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("target_lang")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    clip = json.loads(args.fixture.read_text(encoding="utf-8"))
    settings = get_settings()
    client = create_llm_client(mock=settings.llm_mock) if not args.mock else MockLLMClient(
        [
            {"translation": "ज़ोरवो की कीमत 498 रुपये है।", "terms_used": [], "notes": ""},
            {
                "translation": "Zorvo की कीमत 499 रुपये है। 12 मार्च 2026 को 9876543210 पर कॉल करें।",
                "terms_used": ["Zorvo"],
                "notes": "Preserved every number and protected brand.",
            },
        ]
    )
    sheet = TermSheet.model_validate(clip.get("termsheet", {}))
    result = translate_with_autocorrect(
        client,
        clip["source_text"],
        clip["source_lang"],
        args.target_lang,
        sheet,
        max_chars=clip.get("max_chars", 800),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] != "red" else 1


if __name__ == "__main__":
    raise SystemExit(main())
