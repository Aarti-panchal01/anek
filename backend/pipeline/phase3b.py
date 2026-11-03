"""Quality-focused multi-language dubbing pass for short videos."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from backend.brain.autocorrect import combine_audio_verification
from backend.brain.checks import run_checks
from backend.brain.termsheet import TermSheet, extract_termsheet
from backend.clients.gnani import LiveGnaniClient
from backend.clients.llm import LiveLLMClient
from backend.config import Settings, get_settings
from backend.verify.flags import audio_number_status
from backend.verify.report import build_report

CONTENT_TONES = {
    "lecture": "patient teacher; explain clearly in everyday student-friendly speech",
    "explainer": "plain spoken explainer; concise, friendly, and easy to follow",
    "podcast": "conversational talk; warm, relaxed, and natural",
    "sports": "energetic live commentary; short spoken phrases",
    "business": "confident and approachable product or training explanation",
}


def run_command(args: list[str], capture: bool = True) -> str:
    result = subprocess.run(args, check=True, capture_output=capture, text=True)
    return result.stdout if capture else ""


def duration_of(path: Path) -> float:
    return float(run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).strip())


def silence_boundaries(audio_path: Path, duration: float) -> list[float]:
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-af", "silencedetect=noise=-34dB:d=0.30", "-f", "null", "NUL"],
        capture_output=True,
        text=True,
    )
    log = result.stderr
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", log)]
    return [max(0.0, min(duration, (start + end) / 2)) for start, end in zip(starts, ends)]


def silence_chunks(audio_path: Path, duration: float, boundaries: list[float]) -> list[tuple[float, float]]:
    cuts = [0.0]
    cursor = 0.0
    while duration - cursor > 25:
        choices = [point for point in boundaries if cursor + 5 <= point <= cursor + 15]
        cut = min(choices, key=lambda point: abs(point - (cursor + 10))) if choices else cursor + 12
        cuts.append(cut)
        cursor = cut
    cuts.append(duration)
    return list(zip(cuts, cuts[1:]))


def transcript_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?।॥])\s+", text) if part.strip()]


def prompt_json(client: LiveLLMClient, system: str, payload: dict) -> dict:
    return client.chat_json([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ])


def clean_source(client, source_text: str, terms: TermSheet, language: str) -> str:
    response = prompt_json(
        client,
        "Correct only clear speech-recognition errors using the supplied term sheet and context. Preserve meaning, numbers, names, and uncertainty. Do not translate or rewrite style. Return JSON with cleaned_text and changes (an array of {from,to,reason}).",
        {"transcript": source_text, "source_language": language, "term_sheet": terms.model_dump()},
    )
    return response["cleaned_text"]


def translation_candidates(
    client,
    source: str,
    source_lang: str,
    target_lang: str,
    terms: TermSheet,
    content_type: str,
    max_chars: int,
) -> list[str]:
    response = prompt_json(
        client,
        "Generate exactly three meaning-preserving spoken translations. Use the provided tone. Keep common English words that speakers naturally code-mix in Latin script when natural; retain approved terms exactly. Avoid formal textbook constructions. Preserve every number, date, amount, and unit. Return JSON: {candidates:[string,string,string]}.",
        {
            "source_text": source,
            "source_language": source_lang,
            "target_language": target_lang,
            "content_type": content_type,
            "tone": CONTENT_TONES[content_type],
            "max_chars": max_chars,
            "terms": terms.model_dump(),
        },
    )
    candidates = response.get("candidates", [])
    if not isinstance(candidates, list) or len(candidates) != 3 or not all(isinstance(item, str) for item in candidates):
        raise ValueError("LLM must return exactly three translation candidates")
    return candidates


def select_candidate(client, source: str, candidates: list[str], target_lang: str, slot_s: float, cps: float) -> tuple[str, int, str]:
    time_limit_chars = max(1, int(slot_s * cps))
    response = prompt_json(
        client,
        "Choose the most natural spoken candidate that preserves the meaning and fits the slot. Prefer everyday speech and natural Latin-script English code-mixing. Return JSON with selected_index (0, 1, or 2) and reason. Do not rewrite a candidate.",
        {
            "source_text": source,
            "target_language": target_lang,
            "slot_seconds": slot_s,
            "max_estimated_characters": time_limit_chars,
            "candidates": candidates,
        },
    )
    index = response.get("selected_index")
    if not isinstance(index, int) or index not in range(3):
        raise ValueError("LLM selected_index must be 0, 1, or 2")
    fitting = [i for i, text in enumerate(candidates) if len(text) <= time_limit_chars]
    if fitting and index not in fitting:
        index = min(fitting, key=lambda item: abs(len(candidates[item]) - time_limit_chars))
    return candidates[index], index, str(response.get("reason", ""))


def back_translate(client, source: str, translation: str, source_lang: str, target_lang: str) -> dict:
    return prompt_json(
        client,
        "Independently back-translate the target text into the source language, then assess whether its meaning matches the original. Return JSON with back_translation, meaning_preserved (boolean), and issues (array of strings).",
        {"source_text": source, "translation": translation, "source_language": source_lang, "target_language": target_lang},
    )


def denoise_reference(path: Path, work_dir: Path) -> tuple[Path, float]:
    cleaned = work_dir / "reference_clean.wav"
    run_command([
        "ffmpeg", "-y", "-v", "error", "-i", str(path), "-af",
        "afftdn=nf=-25",
        "-ac", "1", "-ar", "24000", str(cleaned),
    ])
    duration = duration_of(cleaned)
    if duration > 30:
        trimmed = work_dir / "reference_30s.wav"
        run_command(["ffmpeg", "-y", "-v", "error", "-i", str(cleaned), "-t", "30", str(trimmed)])
        cleaned = trimmed
        duration = duration_of(cleaned)
    if duration < 5:
        raise ValueError("Denoising left less than five seconds of usable reference voice")
    return cleaned, duration


def synthesize_sentence(client, text: str, embedding: dict, target: Path) -> float:
    target.write_bytes(client.synth(text, embedding))
    return duration_of(target)


def synthesize_sentence_list(client, sentences: list[str], embedding: dict, work_dir: Path, prefix: str) -> tuple[Path, float]:
    if not sentences:
        raise ValueError("Translation produced no sentence")
    paths = []
    for index, sentence in enumerate(sentences):
        if len(sentence) >= 300:
            raise ValueError("A synthesis sentence exceeds the documented safe 300-character limit")
        path = work_dir / f"{prefix}_sentence_{index:02d}.wav"
        synthesize_sentence(client, sentence, embedding, path)
        paths.append(path)
    output = paths[0]
    for index, path in enumerate(paths[1:], 1):
        combined = work_dir / f"{prefix}_crossfade_{index:02d}.wav"
        run_command([
            "ffmpeg", "-y", "-v", "error", "-i", str(output), "-i", str(path),
            "-filter_complex", "[0:a][1:a]acrossfade=d=0.10:c1=tri:c2=tri[a]", "-map", "[a]", str(combined),
        ])
        output = combined
    return output, duration_of(output)


def run_quality_pass(video: Path, reference: Path, targets: list[str], output_dir: Path, settings: Settings, content_type: str = "explainer") -> list[dict]:
    if not video.is_file() or not reference.is_file():
        raise FileNotFoundError("Video and reference audio must exist")
    duration = duration_of(video)
    if duration > settings.max_video_seconds:
        raise ValueError("Video exceeds the configured hard cap")
    output_dir.mkdir(parents=True, exist_ok=True)
    gnani = LiveGnaniClient(settings)
    llm = LiveLLMClient(settings)
    all_outputs = []

    with tempfile.TemporaryDirectory(prefix="anek-phase3b-") as temp_name:
        work = Path(temp_name)
        source_wav = work / "source.wav"
        run_command(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(source_wav)])
        boundaries = silence_boundaries(source_wav, duration)
        time_ranges = silence_chunks(source_wav, duration, boundaries)
        source_chunks = []
        for index, (start, end) in enumerate(time_ranges):
            chunk_path = work / f"source_{index:02d}.wav"
            run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-ac", "1", "-ar", "16000", str(chunk_path)])
            response = gnani.stt(chunk_path.read_bytes(), "indic-auto", "prisma-v3.0")
            source_chunks.append({"start": start, "end": end, "raw": response["transcript"], "lang": response.get("detected_language_code", "unknown")})
        langs = [row["lang"] for row in source_chunks if row["lang"] != "unknown"]
        source_lang = max(set(langs), key=langs.count) if langs else "unknown"
        term_sheet = extract_termsheet(llm, [row["raw"] for row in source_chunks], source_lang)
        cleaned_rows = []
        for row in source_chunks:
            row["cleaned"], row["cleanup_changes"] = clean_source_detail(llm, row["raw"], term_sheet, source_lang)
            cleaned_rows.append(row)

        clean_reference, reference_seconds = denoise_reference(reference, work)
        embedding = gnani.make_embedding(clean_reference.read_bytes())
        for target_lang in targets:
            language_rows = []
            for index, row in enumerate(cleaned_rows):
                slot = row["end"] - row["start"]
                candidates = translation_candidates(llm, row["cleaned"], source_lang, target_lang, term_sheet, content_type, max(80, int(slot * 15)))
                selected, selected_index, selection_reason = select_candidate(llm, row["cleaned"], candidates, target_lang, slot, 15)
                checks = run_checks(row["cleaned"], selected, target_lang, term_sheet, max_chars=1000)
                if checks:
                    raise ValueError(f"Selected {target_lang} translation failed checks: {checks}")
                sentence_audio, spoken_seconds = synthesize_sentence_list(gnani, transcript_sentences(selected), embedding, work, f"{target_lang}_{index}")
                if spoken_seconds > slot * settings.fit_tolerance:
                    shortened = prompt_json(llm, "Shorten this target-language text to fit its slot while preserving numbers, meaning, and terms. Return JSON with translation.", {"translation": selected, "slot_seconds": slot, "target_language": target_lang, "terms": term_sheet.model_dump()})["translation"]
                    selected = shortened
                    checks = run_checks(row["cleaned"], selected, target_lang, term_sheet, max_chars=1000)
                    if checks:
                        raise ValueError(f"Shortened {target_lang} translation failed checks: {checks}")
                    sentence_audio, spoken_seconds = synthesize_sentence_list(gnani, transcript_sentences(selected), embedding, work, f"{target_lang}_{index}_short")
                stretched = False
                if spoken_seconds > slot:
                    speed = spoken_seconds / slot
                    if speed > settings.max_stretch:
                        raise ValueError(f"{target_lang} clip needs {speed:.2f}x; MAX_STRETCH is {settings.max_stretch}")
                    fit_path = work / f"{target_lang}_{index}_fit.wav"
                    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(sentence_audio), "-filter:a", f"atempo={speed:.4f}", str(fit_path)])
                    sentence_audio = fit_path
                    stretched = True
                transcript = gnani.stt(sentence_audio.read_bytes(), target_lang, settings.verify_stt_model, format="transcribe")
                audio_flag = audio_number_status(selected, transcript["transcript"], target_lang, settings.low_trust_verify_langs)
                meaning = back_translate(llm, row["cleaned"], selected, source_lang, target_lang)
                reasons = list(audio_flag["reasons"])
                if meaning.get("meaning_preserved") is False:
                    reasons.extend(meaning.get("issues", []) or ["Back-translation meaning check disagreed."])
                status = "red" if audio_flag["status"] == "red" else "yellow" if reasons or stretched else "green"
                language_rows.append({
                    "clip": index + 1, "start_s": row["start"], "end_s": row["end"],
                    "source_raw": row["raw"], "source_cleaned": row["cleaned"],
                    "source_cleanup_changes": row["cleanup_changes"], "candidates": candidates,
                    "selected_candidate_index": selected_index, "selection_reason": selection_reason,
                    "translation": selected, "back_translation": meaning.get("back_translation", ""),
                    "meaning_preserved": meaning.get("meaning_preserved"), "audio_transcript": transcript["transcript"],
                    "status": status, "reasons": reasons, "time_stretched": stretched,
                    "audio_path": sentence_audio,
                })
            final_path = output_dir / f"{video.stem}_{target_lang}_phase3b_ai-dubbed.mp4"
            mux_outputs(video, language_rows, final_path, duration, work, settings)
            report = build_report(target_lang, [{key: val for key, val in row.items() if key != "audio_path"} for row in language_rows], settings)
            report.update({"source_language": source_lang, "content_type": content_type, "reference_audio_seconds": reference_seconds, "models": {"stt": "prisma-v3.0", "voice": "vachana-vc-v1", "translation": settings.llm_model}})
            report_path = final_path.with_suffix(".json")
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            all_outputs.append({"language": target_lang, "mp4": str(final_path), "report": str(report_path), "clips": language_rows})
    return all_outputs


def clean_source_detail(client, raw: str, terms: TermSheet, language: str) -> tuple[str, list[dict]]:
    response = prompt_json(
        client,
        "Correct only clear speech recognition mistakes using the approved terms. Do not translate, invent, or change numbers. Return JSON with cleaned_text and changes, where changes is an array of objects with from, to, reason.",
        {"transcript": raw, "source_language": language, "terms": terms.model_dump()},
    )
    return response["cleaned_text"], response.get("changes", [])


def mux_outputs(video: Path, clips: list[dict], output: Path, duration: float, work: Path, settings: Settings) -> None:
    inputs = []
    graph = []
    labels = []
    for index, clip in enumerate(clips):
        inputs.extend(["-i", str(clip["audio_path"])])
        delay = int(clip["start_s"] * 1000)
        graph.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
        labels.append(f"[a{index}]")
    graph.append("".join(labels) + f"amix=inputs={len(clips)}:duration=longest:normalize=0,apad=whole_dur={duration},loudnorm=I=-16:TP=-1.5:LRA=11[mix]")
    audio = work / f"{output.stem}_normalized.wav"
    run_command(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[mix]", "-t", str(duration), str(audio)])
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-metadata", "comment=AI dubbed", str(output)])
    if abs(duration_of(output) - duration) > 0.1:
        raise RuntimeError("Dubbed export duration differs from source by more than 0.1 seconds")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("reference_wav", type=Path)
    parser.add_argument("--targets", nargs="+", default=["hi-IN", "ta-IN", "te-IN"])
    parser.add_argument("--content-type", choices=CONTENT_TONES, default="explainer")
    parser.add_argument("--output-dir", type=Path, default=Path("samples"))
    args = parser.parse_args()
    result = run_quality_pass(args.video, args.reference_wav, args.targets, args.output_dir, get_settings(), args.content_type)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
