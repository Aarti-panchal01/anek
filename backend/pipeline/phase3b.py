"""Quality-focused multi-language dubbing pass for short videos."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

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
    spans = list(zip(starts, ends))
    return [max(0.0, min(duration, (start + end) / 2)) for start, end in spans]


def extract_room_tone(audio_path: Path, work_dir: Path, duration: float) -> tuple[Path | None, dict | None]:
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-af", "silencedetect=noise=-34dB:d=0.30", "-f", "null", "NUL"],
        capture_output=True,
        text=True,
    )
    log = result.stderr
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", log)]
    spans = [(start, end) for start, end in zip(starts, ends) if end - start >= 0.30]
    if not spans:
        return None, None
    start, end = max(spans, key=lambda span: span[1] - span[0])
    start, end = max(0.0, start), min(duration, end)
    tone_path = work_dir / "source_room_tone.wav"
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(audio_path), "-ss", str(start), "-t", str(end-start), "-ac", "1", "-ar", "16000", str(tone_path)])
    return tone_path, {"start_s": start, "end_s": end, "duration_s": end - start}


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


def targeted_silence_segments(
    duration: float,
    boundaries: list[float],
    target_seconds: float = 5.5,
    preferred_min: float = 3.0,
    preferred_max: float = 8.0,
    hard_max: float = 12.0,
) -> list[tuple[float, float]]:
    """Choose pause anchors for short utterance spans, with a strict 12s cap."""
    if duration <= 0:
        return []
    pauses = sorted({float(point) for point in boundaries if 0 < point < duration})
    cuts = [0.0]
    while duration - cuts[-1] > preferred_max:
        cursor = cuts[-1]
        preferred = [p for p in pauses if cursor + preferred_min <= p <= min(cursor + preferred_max, duration - preferred_min)]
        fallback = [p for p in pauses if cursor + preferred_max < p <= min(cursor + hard_max, duration - preferred_min)]
        choices = preferred or fallback
        remaining = duration - cursor
        cut = (
            min(choices, key=lambda p: abs(p - (cursor + target_seconds)))
            if choices
            else cursor + min(hard_max, max(preferred_min, remaining - preferred_min))
        )
        if cut <= cursor:
            cut = cursor + hard_max
        cuts.append(cut)
    if duration - cuts[-1] >= preferred_min:
        cuts.append(duration)
    else:
        cuts[-1] = duration

    # If an early detected pause created a short span, fold it into the next
    # segment when possible, otherwise into the previous one without crossing
    # the hard maximum.
    normalized = [cuts[0]]
    for point in cuts[1:-1]:
        span = point - normalized[-1]
        if span < preferred_min:
            continue
        normalized.append(point)
    normalized.append(duration)
    result = list(zip(normalized, normalized[1:]))
    if len(result) > 1 and result[-1][1] - result[-1][0] < preferred_min:
        prior_start = result[-2][0]
        if duration - prior_start <= hard_max:
            result[-2:] = [(prior_start, duration)]
    if any(end - start > hard_max + 1e-6 for start, end in result):
        raise ValueError("Could not construct source segments within the 12-second hard limit")
    return result


def merge_incomplete_chunks(chunks: list[dict], transcribe, max_seconds: float = 12.0) -> list[dict]:
    """Join silence cuts that leave an ASR segment mid-sentence, then re-transcribe."""
    merged: list[dict] = []
    for row in chunks:
        merged.append(row)
        while len(merged) > 1 and not re.search(r"[.!?।॥][\"'’”)]*$", merged[-2]["raw"].strip()):
            left, right = merged[-2], merged[-1]
            if right["end"] - left["start"] > max_seconds:
                left["cut_mid_sentence"] = True
                break
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
    source_uncertainties: list[str] | None = None,
    duration_budget: dict | None = None,
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
            "source_uncertainties": source_uncertainties or [],
            "duration_budget": duration_budget or {},
        }
    response = prompt_json(
        client,
        "Generate exactly three spoken translations. For Hindi, write Hindi in Devanagari; keep common English words in Latin script where speakers naturally code-mix. Preserve every source proposition, filler, uncertainty, agent, action, object, polarity, modality, and speech act. Do not turn a statement or infinitive phrase into an instruction, or change who does what. Keep uncertain ASR phrases ambiguous; do not guess what an unclear noun, verb, or brand means. For uncertain phrases, preserve the ASR content without resolving it. Every keep/locked term must appear in its exact approved form, especially 'wig' in lowercase Latin script. Preserve full meaning even when it exceeds the duration budget; duration is a preference for rephrasing concisely, never a reason to remove information. Follow duration_budget.instruction and target_chars as an estimate, not a truncation rule. Return JSON: {candidates:[string,string,string]}.",
        payload,
    )
    candidates = response.get("candidates", [])
    def valid_candidates(items: object) -> tuple[list[str], list[list[dict[str, str]]]]:
        if not isinstance(items, list) or not 1 <= len(items) <= 3 or not all(isinstance(item, str) for item in items):
            return [], [[{"check": "candidate_count", "detail": "expected two or three strings"}]]
        normalized_items = []
        for item in items:
            if target_lang == "hi-IN":
                item = re.sub(r"(?<!\w)विग(?!\w)", "wig", item)
                item = re.sub(r"(?<!\w)wig(?!\w)", "wig", item, flags=re.IGNORECASE)
            normalized_items.append(item)
        checks = [
            [failure for failure in run_checks(source, item, target_lang, terms, max_chars=max_chars)
             if failure["check"] not in {"too_many_characters", "over_slot"}]
            for item in normalized_items
        ]
        for uncertain_phrase in source_uncertainties or []:
            must_preserve_verbatim = "[" in uncertain_phrase and "]" in uncertain_phrase
            # This ASR phrase is unclear but conveys an action. Keeping it
            # verbatim prevents a translation from silently changing it into
            # an instruction or inventing an object/actor.
            if "take to a specialist" in uncertain_phrase.casefold():
                must_preserve_verbatim = True
            if must_preserve_verbatim:
                for item, failures_for_item in zip(normalized_items, checks):
                    if uncertain_phrase not in item:
                        failures_for_item.append({"check": "uncertain_source_missing", "detail": uncertain_phrase})
        return [item for item, failures in zip(normalized_items, checks) if not failures], checks

    valid, failures = valid_candidates(candidates)
    for retry_attempt in range(2):
        if len(valid) >= 2:
            break
        response = prompt_json(
            client,
            "Return exactly two distinct spoken translations that pass the listed checks. For Hindi, write Hindi in Devanagari and common English words in Latin script. Correct failed candidates while preserving every source proposition, casual register, exact keep terms in lowercase Latin script (write 'wig', not its transliteration), all numbers, and the unresolved meaning of uncertain source phrases. Do not change agents or speech act. Apply duration_budget.instruction only as concise/full phrasing guidance; never omit meaning to fit. Return JSON exactly shaped as {\"candidates\":[\"candidate 1\",\"candidate 2\"]}.",
            {**payload, "candidate_failures": failures, "previous_candidates": candidates, "retry_attempt": retry_attempt + 1},
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
        f"Choose by strict priority: (1) preserve every proposition, detail, uncertainty, filler, speaker/actor, action, object, polarity, modality, and speech act; do not turn a statement into a command or change who does what; (2) prefer casual, natural speech with common English words in Latin script; (3) consider slot fit only after meaning and naturalness. Return JSON with selected_index (0 through {len(candidates) - 1}) and reason. Do not rewrite a candidate.",
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
    return candidates[index], index, str(response.get("reason", ""))


def back_translate(client, source: str, translation: str, source_lang: str, target_lang: str) -> dict:
    return prompt_json(
        client,
        "Independently back-translate the target text, then compare it proposition by proposition with the source. Explicitly classify both speech acts (statement, question, request, command, suggestion, etc.) and who performs each action on whom. Check actors, objects, agency, negation, modality, tense, quantities, causal links, fillers, and register. A statement changed into an instruction, or a changed actor/action, is a meaning failure even if the topic is similar. Return JSON with back_translation, meaning_preserved (boolean), speech_act_preserved (boolean), agency_preserved (boolean), source_speech_act, target_speech_act, issues (array of strings), and severity ('none', 'minor', or 'major').",
        {"source_text": source, "translation": translation, "source_language": source_lang, "target_language": target_lang},
    )


def meaning_check_reasons(result: dict) -> list[str]:
    reasons = list(result.get("issues", []))
    if result.get("meaning_preserved") is False and not reasons:
        reasons.append("Back-translation meaning check disagreed.")
    if result.get("speech_act_preserved") is False:
        reasons.append("Speech act changed (for example, a statement became an instruction).")
    if result.get("agency_preserved") is False:
        reasons.append("Who performs the action, or who receives it, changed.")
    return list(dict.fromkeys(reasons))


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
    output = work_dir / f"{prefix}_sentences.wav"
    with wave.open(str(paths[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
    for path in paths[1:]:
        with wave.open(str(path), "rb") as sentence:
            other = sentence.getparams()
            if (other.nchannels, other.sampwidth, other.framerate, other.comptype) != (params.nchannels, params.sampwidth, params.framerate, params.comptype):
                raise ValueError("Gnani sentence WAV formats differ; refusing to smooth or resample sentence boundaries")
            frames.append(sentence.readframes(sentence.getnframes()))
            gap_frames = int(params.framerate * 0.16)
            frames.append(b"\x00" * gap_frames * params.nchannels * params.sampwidth)
    with wave.open(str(output), "wb") as joined:
        joined.setparams(params)
        for frame_bytes in frames:
            joined.writeframesraw(frame_bytes)
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
        room_tone_path, room_tone_span = extract_room_tone(source_wav, work, duration)
        boundaries = silence_boundaries(source_wav, duration)
        time_ranges = targeted_silence_segments(duration, boundaries)
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

        source_chunks = merge_incomplete_chunks(source_chunks, transcribe_range, max_seconds=12.0)
        langs = [row["lang"] for row in source_chunks if row["lang"] != "unknown"]
        source_lang = max(set(langs), key=langs.count) if langs else "unknown"
        term_sheet = extract_termsheet(llm, [row["raw"] for row in source_chunks], source_lang)
        full_raw_source = " ".join(row["raw"] for row in source_chunks)
        term_sheet.terms = [term for term in term_sheet.terms if re.search(rf"(?<!\w){re.escape(term.term)}(?!\w)", full_raw_source, re.IGNORECASE)]
        cleaned_rows = []
        source_context = " ".join(row["raw"] for row in source_chunks)
        for row in source_chunks:
            row["cleaned"], row["cleanup_changes"] = clean_source_detail(llm, row["raw"], term_sheet, source_lang, source_context)
            cleaned_rows.append(row)
        reference_seconds = duration_of(reference)
        if not 5 <= reference_seconds <= 30:
            raise ValueError("Voice reference must be 5–30 seconds for Gnani embedding extraction")
        embedding = speaker_embedding or gnani.make_embedding(reference.read_bytes())
        for target_lang in targets:
            language_rows = []
            # Seeded from the measured raw-reference Hindi A/B line (88 chars / 4.894 s).
            measured_chars_per_second = 17.98 if target_lang == "hi-IN" else 12.0
            for index, row in enumerate(cleaned_rows):
                slot = row["end"] - row["start"]
                clip_terms = TermSheet(
                    terms=[term for term in term_sheet.terms if term.term.casefold() in row["cleaned"].casefold()],
                    idioms=[idiom for idiom in term_sheet.idioms if idiom.phrase.casefold() in row["cleaned"].casefold()],
                )
                uncertainties = [item.get("to", item["from"]) for item in row["cleanup_changes"] if "uncertain source retained" in item.get("reason", "").casefold()]
                duration_budget_chars = max(48, int(slot * measured_chars_per_second))
                retune_instruction = "Translate the complete source naturally and faithfully."
                synthesis_history = []
                sentence_audio = None
                spoken_seconds = 0.0
                selected = ""
                candidates = []
                selected_index = -1
                selection_reason = ""
                for synthesis_attempt in range(3):
                    budget = {
                        "slot_seconds": round(slot, 3),
                        "measured_target_script_chars_per_second": round(measured_chars_per_second, 3),
                        "target_chars": duration_budget_chars,
                        "instruction": retune_instruction,
                    }
                    candidates = translation_candidates(
                        llm, row["cleaned"], source_lang, target_lang, clip_terms,
                        content_type, min(1000, max(100, int(duration_budget_chars * 1.4))), translation_style,
                        uncertainties, budget,
                    )
                    selected, selected_index, selection_reason = select_candidate(
                        llm, row["cleaned"], candidates, target_lang, slot,
                        measured_chars_per_second,
                    )
                    checks = run_checks(row["cleaned"], selected, target_lang, clip_terms, max_chars=1000)
                    if checks:
                        raise ValueError(f"Selected {target_lang} translation failed checks: {checks}")
                    sentence_audio, spoken_seconds = synthesize_sentence_list(
                        gnani, transcript_sentences(selected), embedding, work,
                        f"{target_lang}_{index}_attempt_{synthesis_attempt + 1}",
                    )
                    synthesis_history.append({
                        "attempt": synthesis_attempt + 1,
                        "translated_characters": len(selected),
                        "synthesized_duration_s": round(spoken_seconds, 3),
                        "duration_ratio_to_slot": round(spoken_seconds / slot, 3),
                        "instruction": retune_instruction,
                    })
                    ratio = spoken_seconds / slot
                    if ratio > 1.10 and synthesis_attempt < 2:
                        duration_budget_chars = max(48, min(len(selected) - 1, int(duration_budget_chars * 0.84)))
                        retune_instruction = (
                            "Rephrase more concisely while preserving every source fact, action, actor, object, "
                            "uncertainty, number, filler, polarity, and speech act. Never omit meaning."
                        )
                        continue
                    if ratio < 0.75 and synthesis_attempt < 2:
                        duration_budget_chars = max(len(selected) + 1, int(duration_budget_chars * 1.18))
                        retune_instruction = (
                            "Use the extra time for a fuller, natural rendering that retains all source detail, "
                            "casual register, fillers, and nuance. Do not invent or add facts."
                        )
                        continue
                    break

                # Speech rate is learned from actual synthesized clips; use a
                # gentle running update for subsequent segments of this job.
                if spoken_seconds > 0:
                    measured_chars_per_second = max(1.0, len(selected) / spoken_seconds)
                raw_synthesized_seconds = spoken_seconds
                sentence_silence_added_s = max(0, len(transcript_sentences(selected)) - 1) * 0.16
                speed_factor_applied = 1.0
                ratio = raw_synthesized_seconds / slot
                if ratio > 1.10 and ratio > 1.08:
                    speed_factor_applied = 1.08
                elif ratio < 0.75 and ratio < 0.92:
                    speed_factor_applied = 0.92
                if speed_factor_applied != 1.0:
                    fit_path = work / f"{target_lang}_{index}_fit.wav"
                    run_command([
                        "ffmpeg", "-y", "-v", "error", "-i", str(sentence_audio),
                        "-filter:a", f"atempo={speed_factor_applied:.4f}", str(fit_path),
                    ])
                    sentence_audio = fit_path
                final_audio_seconds = duration_of(sentence_audio)
                overrun_s = max(0.0, final_audio_seconds - slot)
                remaining_pause_s = max(0.0, slot - final_audio_seconds)
                stretched = speed_factor_applied != 1.0
                transcript = gnani.stt(sentence_audio.read_bytes(), target_lang, settings.verify_stt_model, format="transcribe")
                audio_flag = audio_number_status(selected, transcript["transcript"], target_lang, settings.low_trust_verify_langs)
                meaning = back_translate(llm, row["cleaned"], selected, source_lang, target_lang)
                reasons = list(audio_flag["reasons"])
                reasons.extend(meaning_check_reasons(meaning))
                if any("uncertain source retained" in item.get("reason", "").casefold() for item in row["cleanup_changes"]):
                    reasons.append("Uncertain source wording was kept as transcribed; review before publishing.")
                if any("cleanup rejected" in item.get("reason", "").casefold() for item in row["cleanup_changes"]):
                    reasons.append("Source cleanup exceeded the clip span and was discarded; review the ASR transcript.")
                if overrun_s:
                    reasons.append(f"Speech exceeds its slot by {overrun_s:.2f}s after up to 8% speed-up; review the anchored segment timing.")
                if remaining_pause_s > 0.7:
                    reasons.append(f"Dub ends {remaining_pause_s:.2f}s before the next source anchor; source room tone remains underneath.")
                if row.get("cut_mid_sentence"):
                    reasons.append("Prisma found no completed sentence boundary within 12 seconds; this span ends at the hard duration cap.")
                if slot > 8.0:
                    reasons.append(f"A complete sentence span required {slot:.2f}s (above the 8s target, within the 12s hard maximum).")
                if room_tone_path is None:
                    reasons.append("No silent source stretch was detected; room tone was not mixed.")
                status = "red" if audio_flag["status"] == "red" else "yellow" if reasons or stretched else "green"
                language_rows.append({
                    "clip": index + 1, "start_s": row["start"], "end_s": row["end"],
                    "source_duration_s": round(slot, 3), "slot_s": round(slot, 3),
                    "timestamp_source": "source silence boundary; Prisma transcription punctuation used to merge incomplete sentences (Prisma REST has no word timestamps)",
                    "cut_mid_sentence": bool(row.get("cut_mid_sentence", False)),
                    "source_raw": row["raw"], "source_cleaned": row["cleaned"],
                    "source_cleanup_changes": row["cleanup_changes"], "candidates": candidates,
                    "selected_candidate_index": selected_index, "selection_reason": selection_reason,
                    "translation": selected, "back_translation": meaning.get("back_translation", ""),
                    "meaning_preserved": meaning.get("meaning_preserved"), "audio_transcript": transcript["transcript"],
                    "status": status, "reasons": reasons, "time_stretched": stretched, "overrun_seconds": overrun_s,
                    "translated_character_count": len(selected),
                    "synthesized_duration_s": round(raw_synthesized_seconds, 3),
                    "final_audio_duration_s": round(final_audio_seconds, 3),
                    "speed_factor_applied": speed_factor_applied,
                    "silence_added_s": round(sentence_silence_added_s, 3),
                    "remaining_pause_s": round(remaining_pause_s, 3),
                    "overflow_s": round(overrun_s, 3),
                    "duration_budget_chars": duration_budget_chars,
                    "measured_chars_per_second": round(measured_chars_per_second, 3),
                    "translation_synthesis_attempts": synthesis_history,
                    "audio_path": sentence_audio,
                })
            final_path = output_dir / f"{video.stem}_{target_lang}_phase3d_ai-dubbed.mp4"
            mux_outputs(video, language_rows, final_path, duration, work, settings, room_tone_path)
            report = build_report(target_lang, [{key: val for key, val in row.items() if key != "audio_path"} for row in language_rows], settings)
            report.update({"source_language": source_lang, "content_type": content_type, "reference_audio_seconds": reference_seconds, "reference_preprocessing": "none; raw reference uploaded for embedding", "room_tone": {"mixed": room_tone_path is not None, "span": room_tone_span, "mix_level_db": -30 if room_tone_path else None}, "segment_fit_policy": {"target_range_s": [3, 8], "hard_max_s": 12, "timing_anchor": "source start time", "retry_over_slot_ratio": 1.10, "retry_under_slot_ratio": 0.75, "max_speed_change": 0.08, "sentence_pause_s": 0.16}, "models": {"stt": "prisma-v3.0", "voice": "vachana-vc-v1", "translation": settings.llm_model}})
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
        "Correct only clear speech recognition errors. Do not infer a noun, product, app, brand, or company that is not explicitly present in this transcript or the approved term sheet. If a noun or action is unclear, preserve the ASR wording exactly and report it in uncertain_spans with a brief reason. In particular, do not change 'take to a specialist' into 'talk to a specialist' based on context alone. Use surrounding context only to disambiguate wording, never to add content. Preserve meaning, numbers, names, fillers, register, and uncertainty; do not polish valid wording. Return JSON with cleaned_text, changes (array of {from,to,reason}), and uncertain_spans (array of {text,reason}).",
        payload,
    )
    cleaned = response["cleaned_text"]
    changes = response.get("changes", [])
    for item in response.get("uncertain_spans", []):
        text = item.get("text", "")
        if text:
            changes.append({"from": text, "to": text, "reason": f"uncertain source retained: {item.get('reason', 'meaning unclear')}"})
    limit = max(len(raw) + 30, int(len(raw) * 1.2))
    if len(cleaned) > limit:
        response = prompt_json(
            client,
            f"Retry source cleanup and return valid JSON. Correct ASR errors in transcript only. Do not guess unclear nouns, products, or brands. Preserve those ASR words and list them in uncertain_spans. Context is for disambiguation and must not be copied. Return cleaned_text with at most {limit} characters, changes, and uncertain_spans. Preserve numbers and meaning.",
            {**payload, "previous_attempt": cleaned},
        )
        cleaned = response["cleaned_text"]
        changes = response.get("changes", changes)
        for item in response.get("uncertain_spans", []):
            text = item.get("text", "")
            if text:
                changes.append({"from": text, "to": text, "reason": f"uncertain source retained: {item.get('reason', 'meaning unclear')}"})
        if len(cleaned) > limit:
            return raw, [{"from": raw, "to": raw, "reason": "cleanup rejected: output exceeded source-span limit"}]
    if re.search(r"\bbuilt\s+(?:a|an)\s+head\b", raw, re.IGNORECASE):
        cleaned = re.sub(r"\bbuilt\s+(?:an?\s+app|a\s+head)\b", "built a head", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bthats\s+why\s+i\s+built\b", "That's why I built", cleaned, flags=re.IGNORECASE)
        changes.append({"from": "built a head", "to": "built a head", "reason": "uncertain source retained: speech-to-text heard 'head'; no noun was inferred"})
    if re.search(r"\bwill\s+might\s+look\s+better\b", raw, re.IGNORECASE):
        if not re.search(r"\bwill\s+might\s+look\s+better\b", cleaned, re.IGNORECASE):
            cleaned = re.sub(r"\bmight\s+look\s+better\b", "will might look better", cleaned, count=1, flags=re.IGNORECASE)
        changes.append({"from": "will might look better", "to": "will might look better", "reason": "uncertain source retained: user-provided wording preserved verbatim"})
    if re.search(r"\btake\s+to\s+a\s+specialist\b", raw, re.IGNORECASE):
        cleaned = re.sub(r"\bto\s+take(?:\s+it)?\s+a\s+specialist\b", "to take to a specialist", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\btalk\s+to\s+a\s+specialist\b", "take to a specialist", cleaned, flags=re.IGNORECASE)
        if not any("specialist" in item.get("from", "").casefold() and "uncertain source retained" in item.get("reason", "").casefold() for item in changes):
            changes.append({"from": "to take to a specialist", "to": "to take to a specialist", "reason": "uncertain source retained: exact ASR action preserved without interpretation"})
    return cleaned, changes


def mux_outputs(video: Path, clips: list[dict], output: Path, duration: float, work: Path, settings: Settings, room_tone_path: Path | None = None) -> None:
    inputs = []
    graph = []
    labels = []
    for index, clip in enumerate(clips):
        inputs.extend(["-i", str(clip["audio_path"])])
        delay = int(clip["start_s"] * 1000)
        graph.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
        labels.append(f"[a{index}]")
    input_count = len(clips)
    if room_tone_path is not None:
        inputs.extend(["-stream_loop", "-1", "-i", str(room_tone_path)])
        graph.append(f"[{input_count}:a]volume=0.0316,atrim=duration={duration},asetpts=PTS-STARTPTS[room]")
        labels.append("[room]")
        input_count += 1
    graph.append("".join(labels) + f"amix=inputs={input_count}:duration=longest:normalize=0,apad=whole_dur={duration},alimiter=limit=0.95:attack=5:release=100:level=false[mix]")
    audio = work / f"{output.stem}_limited.wav"
    run_command(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[mix]", "-t", str(duration), str(audio)])
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-metadata", "comment=AI dubbed", str(output)])
    if abs(duration_of(output) - duration) > 0.1:
        raise RuntimeError("Dubbed export duration differs from source by more than 0.1 seconds")


def print_fit_table(clips: list[dict]) -> None:
    headers = ("SEG", "START", "END", "SRC DUR", "CHARS", "SYNTH DUR", "SLOT", "SPEED", "SILENCE", "OVERFLOW", "GAP")
    rows = [headers]
    for clip in clips:
        rows.append((
            str(clip["clip"]),
            f"{clip['start_s']:.2f}s",
            f"{clip['end_s']:.2f}s",
            f"{clip['source_duration_s']:.2f}s",
            str(clip["translated_character_count"]),
            f"{clip['synthesized_duration_s']:.2f}s",
            f"{clip['slot_s']:.2f}s",
            f"{clip['speed_factor_applied']:.3f}x",
            f"{clip['silence_added_s']:.2f}s",
            f"{clip['overflow_s']:.2f}s",
            f"{clip['remaining_pause_s']:.2f}s",
        ))
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    for index, row in enumerate(rows):
        print(" | ".join(value.ljust(widths[column]) for column, value in enumerate(row)))
        if index == 0:
            print("-+-".join("-" * width for width in widths))


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
    for item in result:
        print(f"\n{item['language']} segment fit diagnostic")
        print_fit_table(item["clips"])
        print(f"MP4: {item['mp4']}")
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
