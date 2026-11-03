"""Run deterministic text-checker evaluation against synthetic probes.

This intentionally does not claim speech recognition, translation quality, or
native-speaker accuracy. The fixtures carry source transcripts and checker
probes; recording and native review are required for those measurements.
"""

import json
from collections import defaultdict
from pathlib import Path

from backend.brain.checks import run_checks
from backend.brain.termsheet import Term, TermSheet
from backend.config import LANGUAGES, get_settings
from backend.clients.llm import MockLLMClient
from backend.brain.autocorrect import translate_with_autocorrect

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"
OUTPUT = ROOT.parent.parent / "eval_report.json"
EVAL_MD = ROOT.parent.parent / "EVAL.md"

SCRIPT_LETTERS = {
    "bn-IN": "এটি একটি পরীক্ষামূলক বাক্য যাচাই করে",
    "en-IN": "This is a synthetic checker probe",
    "gu-IN": "આ એક કૃત્રિમ ચકાસણી વાક્ય છે",
    "hi-IN": "यह एक कृत्रिम जाँच वाक्य है",
    "kn-IN": "ಇದು ಕೃತಕ ಪರಿಶೀಲನಾ ವಾಕ್ಯವಾಗಿದೆ",
    "ml-IN": "ഇത് ഒരു കൃത്രിമ പരിശോധനാ വാക്യമാണ്",
    "mr-IN": "हे एक कृत्रिम तपासणी वाक्य आहे",
    "pa-IN": "ਇਹ ਇੱਕ ਕ੍ਰਿਤ੍ਰਿਮ ਜਾਂਚ ਵਾਕ ਹੈ",
    "ta-IN": "இது ஒரு செயற்கை சரிபார்ப்பு வாக்கியம்",
    "te-IN": "ఇది ఒక కృత్రిమ తనిఖీ వాక్యం",
}


def load_fixtures() -> list[dict]:
    paths = sorted(FIXTURES.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"No evaluation fixtures found in {FIXTURES}")
    fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if len(fixtures) < 10:
        raise ValueError("Phase 6 expects at least ten authored source transcript fixtures")
    return fixtures


def evaluate_language(lang: str, fixtures: list[dict]) -> dict:
    metrics = {"caught": 0, "wrong_total": 0, "false_alarms": 0, "correct_total": 0,
               "fixed": 0, "fixable_total": 0, "broken": 0}
    misses = []
    for fixture in fixtures:
        source = fixture.get(
            "checker_probe_source",
            "Zorvo costs 47 rupees. Call 9876543210 on 12 May 2027. Save 25 percent.",
        )
        sheet = TermSheet(terms=[Term(term="Zorvo", type="brand", treatment="keep", targets={lang: "Zorvo"}, locked=True)])
        valid = f"{SCRIPT_LETTERS[lang]} Zorvo 47 9876543210 12 2027 25"
        clean_failures = run_checks(source, valid, lang, sheet)
        metrics["correct_total"] += 1
        if clean_failures:
            metrics["false_alarms"] += 1
            misses.append({"fixture": fixture["id"], "case": "correct probe flagged", "checks": clean_failures})

        mutations = {
            "wrong_price": valid.replace("47", "48", 1),
            "dropped_number": valid.replace(" 25", "", 1),
            "translated_brand": valid.replace("Zorvo", "Зорво", 1),
            "wrong_phone_digit": valid.replace("9876543210", "9876543219", 1),
        }
        for case, corrupted in mutations.items():
            metrics["wrong_total"] += 1
            failures = run_checks(source, corrupted, lang, sheet)
            if failures:
                metrics["caught"] += 1
            else:
                misses.append({"fixture": fixture["id"], "case": case, "checks": []})

        bad = valid.replace("47", "48", 1)
        corrected = translate_with_autocorrect(
            MockLLMClient([{"translation": bad}, {"translation": valid}]),
            source, "en-IN", lang, sheet, max_retries=1,
        )
        metrics["fixable_total"] += 1
        if corrected["attempts"] and not corrected["checks"]:
            metrics["fixed"] += 1
        elif corrected["attempts"]:
            misses.append({"fixture": fixture["id"], "case": "autocorrect failed", "checks": corrected["checks"]})

        invented = valid.replace(SCRIPT_LETTERS[lang], SCRIPT_LETTERS[lang] + SCRIPT_LETTERS[lang][:2], 1)
        broken = translate_with_autocorrect(
            MockLLMClient([{"translation": bad}, {"translation": invented}]),
            source, "en-IN", lang, sheet, max_retries=1,
        )
        # Compare against the exact authored expected string, not merely the
        # deterministic checker; extra ungrounded content is still a broken fix.
        if broken["attempts"] and broken["translation"] != valid:
            metrics["broken"] += 1

    return {
        "metrics": {
            "caught": {"count": metrics["caught"], "of": metrics["wrong_total"]},
            "false_alarms": {"count": metrics["false_alarms"], "of": metrics["correct_total"]},
            "fixed": {"count": metrics["fixed"], "of": metrics["fixable_total"]},
            "broken": {"count": metrics["broken"]},
        },
        "misses": misses,
        "credit_cost": 0,
    }


def main() -> int:
    settings = get_settings()
    fixtures = load_fixtures()
    report = {
        "evaluation_type": "offline synthetic deterministic text-checker probes",
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "enabled_languages": settings.enabled_langs,
        "disabled_languages": sorted(set(LANGUAGES) - set(settings.enabled_langs)),
        "fixtures": [{"id": item["id"], "audio_available": bool(item.get("audio_path")),
                      "native_verified": item.get("verified_by_native", False)} for item in fixtures],
        "languages": {lang: evaluate_language(lang, fixtures) for lang in settings.enabled_langs},
        "limitations": [
            "Metrics cover deterministic number and protected-brand checks plus scripted autocorrect probes only.",
            "The ten fixtures have authored source transcripts but no audio recordings; ASR, synthesis, timing, and voice cloning were not evaluated.",
            "Reference translations are not native-speaker verified; this run does not measure semantic fidelity or naturalness.",
            "A public demo URL has not been configured.",
        ],
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Anek evaluation", "", f"Generated: {report['generated_at']}", "",
             "Offline synthetic deterministic text-checker probes. No audio or native translation quality is claimed.", "",
             "| Language | Caught | False alarms | Fixed | Broken | Misses |", "|---|---:|---:|---:|---:|---:|"]
    for lang, result in report["languages"].items():
        m = result["metrics"]
        lines.append(f"| {lang} | {m['caught']['count']}/{m['caught']['of']} | {m['false_alarms']['count']}/{m['false_alarms']['of']} | {m['fixed']['count']}/{m['fixed']['of']} | {m['broken']['count']} | {len(result['misses'])} |")
    lines += ["", "## Misses", ""]
    for lang, result in report["languages"].items():
        lines.append(f"### {lang}")
        lines += [f"- {miss['fixture']}: {miss['case']} — {miss['checks']}" for miss in result["misses"]] or ["- None in this synthetic run."]
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in report["limitations"]]
    lines += ["", "Credits: 0 (offline evaluation).", ""]
    EVAL_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUTPUT} and {EVAL_MD}; evaluated {len(report['languages'])} enabled languages across {len(fixtures)} transcript fixtures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
