"""Inspect source audio chunks and run the configured STT provider for a project."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db
from backend.clients.gnani import create_gnani_client
from backend.clients.sarvam_stt import create_sarvam_stt_client
from backend.config import get_settings
from backend.pipeline.phase3b import (
    audio_stream_info,
    detected_silence_spans,
    duration_of,
    merge_tiny_trailing_range,
    stt_error_body,
    targeted_silence_segments,
    validate_stt_chunk,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--provider", choices=("sarvam", "gnani"), default="sarvam")
    args = parser.parse_args()
    with db.connect() as connection:
        project = connection.execute("SELECT video_path FROM projects WHERE id=?", (args.project_id,)).fetchone()
    if not project or not project["video_path"]:
        parser.error("project or source video was not found")
    video = Path(project["video_path"])
    if not video.is_file():
        parser.error("project source video file was not found")

    settings = get_settings()
    duration = duration_of(video)
    source_language = "unknown"
    model = "saaras:v3" if args.provider == "sarvam" else "prisma-v3.0"
    sarvam = create_sarvam_stt_client(settings) if args.provider == "sarvam" else None
    gnani = create_gnani_client(settings.gnani_mock) if args.provider == "gnani" else None
    with tempfile.TemporaryDirectory(prefix="anek-check-stt-") as temp:
        root = Path(temp)
        source_wav = root / "source.wav"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-i", str(video), "-map", "0:a:0", "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(source_wav),
        ], check=True)
        silences = detected_silence_spans(source_wav, duration)
        ranges = merge_tiny_trailing_range(targeted_silence_segments(
            duration, [(start + end) / 2 for start, end in silences], hard_max=8.0,
        ))
        for index, (start, end) in enumerate(ranges, start=1):
            chunk = root / f"chunk_{index:03d}.wav"
            info = {}
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", f"{start:.3f}",
                "-t", f"{end-start:.3f}", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(chunk),
            ], check=True)
            try:
                info = validate_stt_chunk(chunk)
                if args.provider == "sarvam":
                    response = sarvam.transcribe(chunk.read_bytes(), source_language, model)
                    text = response.get("transcript", "")
                else:
                    response = gnani.stt(chunk.read_bytes(), source_language, model, format="transcribe")
                    text = response.get("text") or response.get("transcript") or ""
                print(json.dumps({
                    "chunk": index, "duration_s": round(info["duration"], 3), "bytes": info["bytes"],
                    "codec": info.get("codec_name"), "sample_rate": info.get("sample_rate"),
                    "channels": info.get("channels"), "provider": args.provider, "model": model,
                    "status": "OK", "result": str(text)[:300],
                }, ensure_ascii=False))
            except Exception as error:
                response = getattr(error, "response", None)
                body = stt_error_body(error)
                try:
                    info = info or audio_stream_info(chunk)
                except Exception:
                    pass
                print(json.dumps({
                    "chunk": index, "duration_s": round(end - start, 3),
                    "bytes": chunk.stat().st_size if chunk.exists() else 0,
                    "codec": info.get("codec_name", "unknown"),
                    "sample_rate": info.get("sample_rate", "unknown"),
                    "channels": info.get("channels", "unknown"),
                    "provider": args.provider, "model": model,
                    "status": getattr(response, "status_code", "ERROR"), "result": body[:300],
                }, ensure_ascii=False))
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
