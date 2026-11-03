"""Compare cloned speech from two unprocessed voice-reference files."""

import argparse
from pathlib import Path

from backend.clients.gnani import LiveGnaniClient
from backend.config import get_settings

DEFAULT_HINDI_LINE = "इस चुनाव को ज़्यादा व्यक्तिगत और आसान बनाने के लिए, आप किसी विशेषज्ञ से बात कर सकते हैं।"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-reference", type=Path, default=Path("samples/my_voice.wav"))
    parser.add_argument("--alternative-reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("samples/phase3c-voice-ab"))
    parser.add_argument("--hindi-line", default=DEFAULT_HINDI_LINE)
    args = parser.parse_args()

    for path in (args.raw_reference, args.alternative_reference):
        if not path.is_file():
            parser.error(f"Reference audio does not exist: {path}")
    if len(args.hindi_line) >= 300:
        parser.error("Hindi comparison line must be under the documented 300-character text limit")

    client = LiveGnaniClient(get_settings())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for label, reference in (("raw_sample", args.raw_reference), ("alternative", args.alternative_reference)):
        # Upload the original file bytes as-is: no denoising, trimming, gain change,
        # resampling, or loudness normalization is applied to either reference.
        embedding = client.make_embedding(reference.read_bytes())
        output = args.output_dir / f"{label}_same_hi-IN_line.wav"
        output.write_bytes(client.synth(args.hindi_line, embedding))
        print(f"{label}: {output}")
    print(f"Identical Hindi line: {args.hindi_line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
