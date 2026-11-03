"""Measure cloned-voice speech and Prisma number recognition by target language."""

import argparse
import re
import sys
import wave
from pathlib import Path

from dotenv import set_key

from backend.clients.gnani import LiveGnaniClient
from backend.config import get_settings

TEST_SENTENCES = {
    "bn-IN": "জরভো পণ্যের দাম 47 টাকা, এবং বছর 2026।",
    "en-IN": "The Zorvo product costs 47 rupees in the year 2026.",
    "gu-IN": "ઝોર્વો ઉત્પાદનની કિંમત 47 રૂપિયા છે અને વર્ષ 2026 છે.",
    "hi-IN": "ज़ोरवो उत्पाद की कीमत 47 रुपये है और वर्ष 2026 है।",
    "kn-IN": "ಜೋರ್ವೋ ಉತ್ಪನ್ನದ ಬೆಲೆ 47 ರೂಪಾಯಿ ಮತ್ತು ವರ್ಷ 2026.",
    "ml-IN": "സോർവോ ഉൽപ്പന്നത്തിന്റെ വില 47 രൂപയും വർഷം 2026 ആണ്.",
    "mr-IN": "झोरवो उत्पादनाची किंमत 47 रुपये आणि वर्ष 2026 आहे.",
    "pa-IN": "ਜ਼ੋਰਵੋ ਉਤਪਾਦ ਦੀ ਕੀਮਤ 47 ਰੁਪਏ ਹੈ ਅਤੇ ਸਾਲ 2026 ਹੈ।",
    "ta-IN": "சோர்வோ பொருளின் விலை 47 ரூபாய், ஆண்டு 2026.",
    "te-IN": "జోర్వో ఉత్పత్తి ధర 47 రూపాయలు, సంవత్సరం 2026.",
}

DIGIT_TRANSLATION = str.maketrans(
    {
        ord(chr(script_zero + digit)): str(digit)
        for script_zero in (0x0966, 0x09E6, 0x0AE6, 0x0A66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66)
        for digit in range(10)
    }
)


def read_wav(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as audio_file:
        duration = audio_file.getnframes() / audio_file.getframerate()
    if not 5 <= duration <= 30:
        raise ValueError(f"Reference WAV must be 5-30 seconds; got {duration:.1f}s")
    return path.read_bytes(), duration


def extract_numbers(text: str) -> list[str]:
    normalized = text.translate(DIGIT_TRANSLATION)
    return re.findall(r"(?<!\w)\d+(?:[.,]\d+)*(?!\w)", normalized)


def run_gate(reference_path: Path, output_dir: Path) -> list[dict[str, str]]:
    settings = get_settings()
    client = LiveGnaniClient(settings)
    reference_audio, _ = read_wav(reference_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    embedding = client.make_embedding(reference_audio)
    results = []

    for language_code, sentence in TEST_SENTENCES.items():
        output_path = output_dir / f"out_{language_code}.wav"
        try:
            synthesized = client.synth(sentence, embedding)
            output_path.write_bytes(synthesized)
            transcript = client.stt(
                synthesized,
                language_code,
                "prisma-v3.0",
                format="transcribe",
            )
            seen_numbers = extract_numbers(transcript["transcript"])
            passed = extract_numbers(sentence) == seen_numbers
            results.append(
                {
                    "language": language_code,
                    "status": "PASS" if passed else "FAIL",
                    "numbers": ",".join(seen_numbers) or "none",
                    "audio": str(output_path),
                }
            )
        except Exception as error:
            results.append(
                {
                    "language": language_code,
                    "status": "ERROR",
                    "numbers": type(error).__name__,
                    "audio": str(output_path) if output_path.exists() else "not created",
                }
            )

    enabled = [row["language"] for row in results if row["status"] == "PASS"]
    env_path = Path(".env")
    if env_path.exists():
        set_key(str(env_path), "ENABLED_LANGS", ",".join(enabled))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_wav", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()

    try:
        results = run_gate(args.reference_wav, args.output_dir)
    except Exception as error:
        print(f"Cloning gate could not start: {error}", file=sys.stderr)
        return 1

    print("Language | Result | Transcript numbers | Audio")
    print("---------|--------|--------------------|------")
    for result in results:
        print(
            f"{result['language']} | {result['status']} | {result['numbers']} | "
            f"{result['audio']}"
        )
    print("Credits per call: not reported in the documented API responses.")
    print("Check the Gnani dashboard for the measured credit balance change.")
    print(f"Enabled languages: {', '.join(r['language'] for r in results if r['status'] == 'PASS')}")
    return 0 if all(result["status"] != "ERROR" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
