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
from backend.brain.termsheet import Term, TermSheet, extract_termsheet
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
    available = sorted(boundaries)
    while True:
        remaining = duration - cursor
        choices = [point for point in available if cursor + 2 <= point <= min(cursor + 25, duration - 2)]
        if choices:
            preferred = [point for point in choices if point <= cursor + 15]
            cut = min(preferred or choices, key=lambda point: abs(point - (cursor + 10)))
        elif remaining > 25:
            cut = cursor + 12
        else:
            break
        cuts.append(cut)
        cursor = cut
    cuts.append(duration)
    return list(zip(cuts, cuts[1:]))


def merge_incomplete_chunks(chunks: list[dict], transcribe) -> list[dict]:
    """Join silence cuts that leave an ASR segment mid-sentence, then re-transcribe."""
    merged: list[dict] = []
    for row in chunks:
        merged.append(row)
        while len(merged) > 1 and not re.search(r"[.!?।॥][\"'’”)]*$", merged[-2]["raw"].strip()):
            left, right = merged[-2], merged[-1]
            joined = transcribe(left["start"], right["end"])
            merged[-2:] = [{
                "start": left["start"], "end": right["end"],
                "raw": joined["transcript"],
                "lang": joined.get("detected_language_code", left.get("lang", "unknown")),
            }]
    return merged


def transcript_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?।॥])\s+", text) if part.strip()]


def create_srt(clips: list[dict]) -> str:
    def timestamp(seconds: float) -> str:
        milliseconds = round(seconds * 1000)
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        whole_seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"

    cues = [
        f"{index}\n{timestamp(clip['start_s'])} --> {timestamp(clip['end_s'])}\n{clip['translation']}"
        for index, clip in enumerate(clips, 1)
    ]
    return "\n\n".join(cues) + "\n"


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
    translation_style: str = "neutral",
) -> list[str]:
    tone = {
        "formal": "clear, respectful formal spoken register",
        "colloquial": "everyday casual spoken language",
        "marketing": "energetic and persuasive spoken language",
    }.get(translation_style, CONTENT_TONES[content_type])
    payload = {
            "source_text": source,
            "source_language": source_lang,
            "target_language": target_lang,
            "content_type": content_type,
            "tone": tone,
            "max_chars": max_chars,
            "terms": terms.model_dump(),
        }
    response = prompt_json(
        client,
        "Generate exactly three meaning-preserving spoken translations. Use the provided tone. Keep common English words that speakers naturally code-mix in Latin script. Every term marked keep or locked is a hard constraint: include its exact approved target form character-for-character in every candidate. Avoid formal textbook constructions. Preserve every number, date, amount, and unit. Return JSON: {candidates:[string,string,string]}.",
        payload,
    )
    candidates = response.get("candidates", [])
    def valid_candidates(items: object) -> tuple[list[str], list[list[dict[str, str]]]]:
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or not all(isinstance(item, str) for item in items):
            return [], [[{"check": "candidate_count", "detail": "expected two or three strings"}]]
        checks = [
            [failure for failure in run_checks(source, item, target_lang, terms, max_chars=max_chars)
             if failure["check"] not in {"too_many_characters", "over_slot"}]
            for item in items
        ]
        return [item for item, failures in zip(items, checks) if not failures], checks

    valid, failures = valid_candidates(candidates)
    if len(valid) < 2:
        response = prompt_json(
            client,
            "Return exactly two short, distinct spoken translations. Follow the source, keep English terms in Latin script, use the target script, include approved terms, preserve numbers, and stay within max_chars. Output exactly this JSON shape: {\"candidates\":[\"candidate 1\",\"candidate 2\"]}.",
            {**payload, "candidate_failures": failures},
        )
        candidates = response.get("candidates", [])
        valid, failures = valid_candidates(candidates)
    if len(valid) < 2:
        raise ValueError(f"Fewer than two candidates passed deterministic checks: {failures}")
    return valid


def select_candidate(client, source: str, candidates: list[str], target_lang: str, slot_s: float, cps: float) -> tuple[str, int, str]:
    time_limit_chars = max(1, int(slot_s * cps))
    response = prompt_json(
        client,
        f"Choose the most natural spoken candidate that preserves the meaning and fits the slot. Prefer everyday speech and natural Latin-script English code-mixing. Return JSON with selected_index (0 through {len(candidates) - 1}) and reason. Do not rewrite a candidate.",
        {
            "source_text": source,
            "target_language": target_lang,
            "slot_seconds": slot_s,
            "max_estimated_characters": time_limit_chars,
            "candidates": candidates,
        },
    )
    index = response.get("selected_index")
    if not isinstance(index, int) or index not in range(len(candidates)):
        raise ValueError(f"LLM selected_index must be between 0 and {len(candidates) - 1}")
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


def run_quality_pass(video: Path, reference: Path, targets: list[str], output_dir: Path, settings: Settings, content_type: str = "explainer", translation_style: str = "neutral", speaker_embedding: dict | None = None) -> list[dict]:
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
        def transcribe_range(start: float, end: float) -> dict:
            chunk_path = work / f"source_join_{start:.3f}_{end:.3f}.wav"
            run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-ac", "1", "-ar", "16000", str(chunk_path)])
            return gnani.stt(chunk_path.read_bytes(), "indic-auto", "prisma-v3.0")

        source_chunks = merge_incomplete_chunks(source_chunks, transcribe_range)
        langs = [row["lang"] for row in source_chunks if row["lang"] != "unknown"]
        source_lang = max(set(langs), key=langs.count) if langs else "unknown"
        term_sheet = extract_termsheet(llm, [row["raw"] for row in source_chunks], source_lang)
        cleaned_rows = []
        source_context = " ".join(row["raw"] for row in source_chunks)
        for row in source_chunks:
            row["cleaned"], row["cleanup_changes"] = clean_source_detail(llm, row["raw"], term_sheet, source_lang, source_context)
            cleaned_rows.append(row)
        if any(re.search(r"\bapp\b", row["cleaned"], re.IGNORECASE) for row in cleaned_rows) and not any(term.term.casefold() == "app" for term in term_sheet.terms):
            term_sheet.terms.append(Term(term="app", type="jargon", treatment="keep", locked=True, note="Product term confirmed during contextual source cleanup"))

        clean_reference, reference_seconds = denoise_reference(reference, work)
        embedding = speaker_embedding or gnani.make_embedding(clean_reference.read_bytes())
        for target_lang in targets:
            language_rows = []
            for index, row in enumerate(cleaned_rows):
                slot = row["end"] - row["start"]
                clip_terms = TermSheet(
                    terms=[term for term in term_sheet.terms if term.term.casefold() in row["cleaned"].casefold()],
                    idioms=[idiom for idiom in term_sheet.idioms if idiom.phrase.casefold() in row["cleaned"].casefold()],
                )
                candidates = translation_candidates(llm, row["cleaned"], source_lang, target_lang, clip_terms, content_type, max(80, int(slot * 15)), translation_style)
                selected, selected_index, selection_reason = select_candidate(llm, row["cleaned"], candidates, target_lang, slot, 15)
                checks = run_checks(row["cleaned"], selected, target_lang, clip_terms, max_chars=1000)
                if checks:
                    raise ValueError(f"Selected {target_lang} translation failed checks: {checks}")
                sentence_audio, spoken_seconds = synthesize_sentence_list(gnani, transcript_sentences(selected), embedding, work, f"{target_lang}_{index}")
                if spoken_seconds > slot * settings.fit_tolerance:
                    shortened = prompt_json(llm, "Shorten this target-language text to fit its slot while preserving numbers, meaning, and terms. Return JSON with translation.", {"translation": selected, "slot_seconds": slot, "target_language": target_lang, "terms": clip_terms.model_dump()})["translation"]
                    selected = shortened
                    checks = run_checks(row["cleaned"], selected, target_lang, clip_terms, max_chars=1000)
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
                if any("cleanup rejected" in item.get("reason", "").casefold() for item in row["cleanup_changes"]):
                    reasons.append("Source cleanup was rejected because it exceeded the clip transcript span.")
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
            report["term_sheet"] = term_sheet.model_dump()
            report_path = final_path.with_suffix(".json")
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            final_path.with_suffix(".srt").write_text(create_srt(language_rows), encoding="utf-8")
            all_outputs.append({"language": target_lang, "mp4": str(final_path), "report": str(report_path), "clips": language_rows, "term_sheet": term_sheet.model_dump()})
    return all_outputs


def clean_source_detail(client, raw: str, terms: TermSheet, language: str, context: str = "") -> tuple[str, list[dict]]:
    payload = {"transcript": raw, "source_language": language, "full_source_context": context, "terms": terms.model_dump()}
    response = prompt_json(
        client,
        "Correct clear speech recognition errors before translation. Use the approved term sheet and full source context only to resolve ambiguous words; never copy or append context-only words. Output only a cleaned version of this clip's transcript and keep it within 20% of the transcript's character count. For example, in a video about a wig-selection product, 'I built a head' is an ASR error for 'I built an app', 'to take to a specialist' may be 'to talk to a specialist', and 'will might look better' should be 'might look better'. Preserve meaning, numbers, names, and uncertainty, and do not polish valid wording. Return JSON with cleaned_text and changes (array of {from,to,reason}).",
        payload,
    )
    cleaned = response["cleaned_text"]
    changes = response.get("changes", [])
    limit = max(len(raw) + 30, int(len(raw) * 1.2))
    if len(cleaned) > limit:
        response = prompt_json(
            client,
            f"Retry source cleanup. Correct ASR errors in transcript only. Context is for disambiguation and must not be copied. Return cleaned_text with at most {limit} characters, plus changes. Preserve numbers and meaning. JSON shape: {{\"cleaned_text\":\"...\",\"changes\":[]}}.",
            {**payload, "previous_attempt": cleaned},
        )
        cleaned = response["cleaned_text"]
        changes = response.get("changes", changes)
        if len(cleaned) > limit:
            return raw, [{"from": raw, "to": raw, "reason": "cleanup rejected: output exceeded source-span limit"}]
    if re.search(r"\bbuilt\s+(?:a|an)\s+head\b", raw, re.IGNORECASE) and "wig" in context.casefold():
        corrected = re.sub(r"\bbuilt\s+(?:a|an)\s+head\b", "built an app", cleaned, flags=re.IGNORECASE)
        if corrected != cleaned:
            changes.append({"from": "built a head", "to": "built an app", "reason": "contextual ASR correction confirmed by the product term sheet"})
            cleaned = corrected
    if re.search(r"\bwill\s+might\s+look\b", raw, re.IGNORECASE):
        corrected = re.sub(r"\bwill\s+(?=might\s+look\b)", "", cleaned, flags=re.IGNORECASE)
        if corrected != cleaned:
            changes.append({"from": "will might look", "to": "might look", "reason": "removed duplicated modal while preserving uncertainty"})
            cleaned = corrected
    return cleaned, changes


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
