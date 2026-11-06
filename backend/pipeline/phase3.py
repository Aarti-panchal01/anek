"""One-video Hindi dubbing run for Phase 3."""

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from backend.atomic_files import atomic_write_bytes, atomic_write_text, run_atomic_command
from backend.brain.autocorrect import translate_with_autocorrect, combine_audio_verification
from backend.brain.termsheet import extract_termsheet
from backend.pipeline.phase3b import back_translate, meaning_check_reasons
from backend.clients.gnani import LiveGnaniClient
from backend.clients.llm import SarvamSpeechClient, SarvamTranslationClient, create_translation_client, create_workflow_client
from backend.config import Settings, get_settings
from backend.verify.flags import audio_number_status
from backend.verify.report import build_report


def run_command(arguments: list[str]) -> str:
    runner = run_atomic_command if Path(arguments[0]).name.lower().startswith("ffmpeg") else subprocess.run
    result = runner(arguments, check=True, capture_output=True, text=True)
    return result.stdout


def probe_duration(path: Path) -> float:
    output = run_command(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
    )
    return float(output.strip())


def split_audio(audio_path: Path, work_dir: Path, duration: float) -> list[tuple[Path, float, float]]:
    boundaries = list(range(0, int(duration), 12))
    segments = []
    for index, start in enumerate(boundaries):
        end = min(start + 12, duration)
        segment_path = work_dir / f"source_{index:02d}.wav"
        run_command(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(audio_path), "-ss", str(start),
                "-t", str(end - start), "-ac", "1", "-ar", "16000", str(segment_path),
            ]
        )
        segments.append((segment_path, float(start), float(end)))
    return segments


def create_srt(clips: list[dict]) -> str:
    def timestamp(seconds: float) -> str:
        millis = round(seconds * 1000)
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        seconds_part, millis = divmod(millis, 1000)
        return f"{hours:02}:{minutes:02}:{seconds_part:02},{millis:03}"

    return "\n\n".join(
        f"{index}\n{timestamp(item['start_s'])} --> {timestamp(item['end_s'])}\n"
        f"{item['translation']}"
        for index, item in enumerate(clips, 1)
    ) + "\n"


def run_phase3(video_path: Path, reference_path: Path, output_path: Path, settings: Settings) -> dict:
    if not video_path.is_file() or not reference_path.is_file():
        raise FileNotFoundError("Video and reference WAV must both exist")
    duration = probe_duration(video_path)
    if duration > settings.max_video_seconds:
        raise ValueError(f"Video is {duration:.1f}s; maximum is {settings.max_video_seconds}s")
    with tempfile.TemporaryDirectory(prefix="anek-phase3-") as temp_name:
        work_dir = Path(temp_name)
        source_audio = work_dir / "source.wav"
        run_command(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(video_path), "-vn", "-ac", "1",
                "-ar", "16000", str(source_audio),
            ]
        )
        chunks = split_audio(source_audio, work_dir, duration)
        gnani = LiveGnaniClient(settings)
        sarvam_stt = SarvamSpeechClient(settings)
        llm = SarvamTranslationClient(settings)
        workflow_llm = create_workflow_client(llm, settings)
        translator = create_translation_client(llm, settings)
        ref_audio = reference_path.read_bytes()
        with wave.open(str(reference_path), "rb") as wav:
            ref_duration = wav.getnframes() / wav.getframerate()
        if not 5 <= ref_duration <= 30:
            raise ValueError("Reference WAV must be 5-30 seconds")
        embedding = gnani.make_embedding(ref_audio)

        transcripts = []
        for audio_path, start, end in chunks:
            response = sarvam_stt.stt(audio_path.read_bytes(), language_code="unknown", format="transcribe")
            transcripts.append(
                {
                    "start_s": start,
                    "end_s": end,
                    "text": response["transcript"],
                    "source_lang": response.get("detected_language_code", "unknown"),
                    "audio_path": audio_path,
                }
            )
        source_langs = [row["source_lang"] for row in transcripts if row["source_lang"] != "unknown"]
        source_lang = max(set(source_langs), key=source_langs.count) if source_langs else "unknown"
        term_sheet = extract_termsheet(workflow_llm, [row["text"] for row in transcripts], source_lang)

        results = []
        for index, row in enumerate(transcripts):
            translation_result = translate_with_autocorrect(
                translator,
                row["text"],
                source_lang,
                "hi-IN",
                term_sheet,
                max_chars=280,
            )
            translated = translation_result["translation"]
            speech = gnani.synth(translated, embedding)
            speech_path = work_dir / f"dub_{index:02d}.wav"
            atomic_write_bytes(speech_path, speech)
            audio_duration = probe_duration(speech_path)
            slot_duration = row["end_s"] - row["start_s"]
            if audio_duration > slot_duration * settings.fit_tolerance:
                raise ValueError(f"Hindi clip {index + 1} exceeds its timeline slot")
            if audio_duration > slot_duration:
                stretch = min(audio_duration / slot_duration, settings.max_stretch)
                fitted_path = work_dir / f"fit_{index:02d}.wav"
                run_command(
                    [
                        "ffmpeg", "-y", "-v", "error", "-i", str(speech_path), "-filter:a",
                        f"atempo={stretch:.4f}", str(fitted_path),
                    ]
                )
                speech_path = fitted_path
            verification = sarvam_stt.stt(speech_path.read_bytes(), "hi-IN", format="transcribe")
            audio_status = audio_number_status(
                translated, verification["transcript"], "hi-IN", settings.low_trust_verify_langs
            )
            text_status = {
                "status": translation_result["status"],
                "reasons": translation_result["reasons"],
            }
            final_status = combine_audio_verification(
                translated,
                verification["transcript"],
                "hi-IN",
                text_status,
                settings.low_trust_verify_langs,
            )
            meaning = back_translate(workflow_llm, row["text"], translated, source_lang, "hi-IN")
            meaning_reasons = meaning_check_reasons(meaning)
            if meaning_reasons:
                final_status["status"] = "yellow" if final_status["status"] != "red" else "red"
                final_status["reasons"] = list(dict.fromkeys(final_status["reasons"] + meaning_reasons))
            results.append(
                {
                    "index": index,
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "source_text": row["text"],
                    "translation": translated,
                    "audio_transcript": verification["transcript"],
                    "status": final_status["status"],
                    "reasons": final_status["reasons"],
                    "back_translation": meaning.get("back_translation", ""),
                    "meaning_preserved": meaning.get("meaning_preserved"),
                    "attempts": translation_result["attempts"],
                    "audio_path": speech_path,
                }
            )

        delayed = work_dir / "dub_track.wav"
        # Build the speech track by placing each generated segment at its source offset.
        filters = "".join(f"[{index}:a]adelay={int(row['start_s'] * 1000)}|{int(row['start_s'] * 1000)}[a{index}];" for index, row in enumerate(results))
        inputs = [part for row in results for part in ("-i", str(row["audio_path"]))]
        labels = "".join(f"[a{index}]" for index in range(len(results)))
        filter_graph = filters + f"{labels}amix=inputs={len(results)}:duration=longest:normalize=0,apad=whole_dur={duration}[mix]"
        run_command(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", filter_graph, "-map", "[mix]", "-t", str(duration), str(delayed)])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(video_path), "-i", str(delayed),
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-metadata", "comment=AI dubbed", "-metadata",
                "title=Anek AI dubbed", str(output_path),
            ]
        )
        srt_path = output_path.with_suffix(".srt")
        atomic_write_text(srt_path, create_srt(results))
        clean_results = [{key: value for key, value in row.items() if key != "audio_path"} for row in results]
        report = build_report("hi-IN", clean_results, settings)
        report.update({"source_language": source_lang, "export_label": "AI dubbed"})
        report_path = output_path.with_suffix(".json")
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        if abs(probe_duration(output_path) - duration) > 0.1:
            raise RuntimeError("Export duration self-check failed")
        return {"output": output_path, "srt": srt_path, "report": report_path, "clips": clean_results}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("reference_wav", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run_phase3(args.video, args.reference_wav, args.output, get_settings())
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
