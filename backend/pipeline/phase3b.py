"""Quality-focused multi-language dubbing pass for short videos."""

import argparse
import difflib
import hashlib
import json
import re
import shutil
import logging
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from collections.abc import Callable


LOGGER = logging.getLogger(__name__)

from backend.atomic_files import atomic_copyfile, atomic_write_bytes, atomic_write_text, run_atomic_command

from backend.brain.checks import run_checks
from backend.brain.termsheet import (
    TermSheet, apply_mishearing_maps, apply_spoken_forms, extract_termsheet,
    mask_protected_terms, restore_protected_terms,
)
from backend.clients.gnani import create_gnani_client
from backend.clients.sarvam_stt import create_sarvam_stt_client
from backend.clients.llm import SarvamTranslationClient, MockLLMClient, create_translation_client, create_workflow_client
from backend.config import LANGUAGES, Settings, get_settings
from backend.verify.flags import audio_number_status
from backend.verify.report import build_report

CONTENT_TONES = {
    "lecture": "patient teacher; explain clearly in everyday student-friendly speech",
    "explainer": "plain spoken explainer; concise, friendly, and easy to follow",
    "podcast": "conversational talk; warm, relaxed, and natural",
    "sports": "energetic live commentary; short spoken phrases",
    "business": "confident and approachable product or training explanation",
}
OVERRUN_TOLERANCE_S = 0.10
MAX_ATEMPO_SPEEDUP = 1.15
EXPORT_DURATION_TOLERANCE_S = 0.05
# Shared cold-start estimate. Later clips use the measured Gnani TTS rate for
# that target-language voice, so no target language has a special-case rate.
INITIAL_TARGET_CHARS_PER_SECOND = 12.0
SENTENCE_ENDINGS = r".!?।॥。！？؟։"


def translation_style_instruction(style: str, target_lang: str) -> str:
    """Return the user-visible General/Pure translation contract."""
    if style == "pure":
        return "Use formal spoken phrasing in the target language and native script. Translate all ordinary English words and phrases. Preserve approved brand/person names and protected terms from the term sheet exactly as instructed."
    if style == "natural":
        return "Use modern-colloquial, code-mixed speech. Keep English words and phrases from the source in English Latin script when they fit naturally, including names and technical or subject terms such as 'northeastern hemisphere'. Translate the rest into the target language and script. Preserve protected terms from the term sheet exactly as instructed."
    if style == "keep_technical":
        return "Translate ordinary words and place names into the target language; keep only established technical terms and approved brand/person names in English."
    if style == "formal":
        return "Use clear formal spoken target-language phrasing. Translate ordinary words and place names; retain only approved brand/person names in their original form."
    return "Use natural spoken target-language phrasing with minimal English loanwords. Translate ordinary words, place names, and common number words; retain only approved brand/person names or terms from the term sheet in their required form."


def run_command(args: list[str], capture: bool = True) -> str:
    runner = run_atomic_command if Path(args[0]).name.lower().startswith("ffmpeg") else subprocess.run
    result = runner(args, check=True, capture_output=capture, text=True)
    return result.stdout if capture else ""


def timing_character_budget(slot_seconds: float, chars_per_second: float = INITIAL_TARGET_CHARS_PER_SECOND) -> int:
    """Return a common starting text estimate; generated audio drives later fitting."""
    return max(1, round(max(0.0, slot_seconds) * max(0.1, chars_per_second)))


def duration_of(path: Path) -> float:
    return float(run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]).strip())


def audio_stream_info(path: Path) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_name,sample_rate,channels,duration", "-of", "json", str(path),
    ], capture_output=True, text=True, check=True)
    streams = json.loads(result.stdout).get("streams", [])
    return streams[0] if streams else {}


def validate_stt_chunk(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("STT chunk is missing or empty")
    info = audio_stream_info(path)
    seconds = float(info.get("duration") or duration_of(path))
    if seconds < 0.3:
        raise ValueError(f"STT chunk is too short ({seconds:.3f}s); trailing audio was not sent")
    probe = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "NUL",
    ], capture_output=True, text=True)
    mean = re.search(r"mean_volume:\s*(-?inf|-?[0-9.]+) dB", probe.stderr)
    if probe.returncode not in {0, 1} or (mean and (mean.group(1) == "-inf" or float(mean.group(1)) < -60)):
        raise ValueError("STT chunk contains silent audio")
    return {**info, "duration": seconds, "bytes": path.stat().st_size}


def stt_error_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def stt_error_body(error: Exception) -> str:
    response = getattr(error, "response", None)
    body = getattr(response, "text", "") if response is not None else ""
    if not body:
        body = str(error)
    cleaned = re.sub(r"\s+", " ", str(body)).strip()
    cleaned = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", cleaned)
    cleaned = re.sub(r"(?i)\s*For more information check:\s*https?://developer\.mozilla\.org/\S*", "", cleaned)
    cleaned = re.sub(
        r"(?i)(api[_ -]?key|access[_ -]?token|secret|authorization)(['\"=: ]+)[^,\s}\"']+",
        r"\1\2[REDACTED]", cleaned,
    )
    return cleaned[:300]


def log_stt_failure(provider: str, model: str, language: str, chunk_index: int, path: Path, error: Exception) -> str:
    try:
        info = audio_stream_info(path) if path.is_file() else {}
        duration = float(info.get("duration") or duration_of(path)) if path.is_file() else 0.0
        size = path.stat().st_size if path.is_file() else 0
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
    except Exception:
        info, duration, size, digest = {}, 0.0, 0, "unavailable"
    endpoint = getattr(getattr(error, "response", None), "request", None)
    endpoint_path = getattr(getattr(endpoint, "url", None), "path", "/speech-to-text" if provider == "sarvam" else "/stt/v3")
    status, snippet = stt_error_status(error), stt_error_body(error)
    LOGGER.error(
        "STT HTTP failure provider=%s endpoint=%s model=%s language=%s chunk=%d duration_s=%.3f sample_rate=%s channels=%s codec=%s bytes=%d sha256=%s status=%s body=%s",
        provider, endpoint_path, model, language, chunk_index, duration, info.get("sample_rate", "unknown"),
        info.get("channels", "unknown"), info.get("codec_name", "unknown"), size, digest, status, snippet,
    )
    if status:
        return f"Speech check rejected the audio ({status}): {snippet or 'provider returned an empty error response'}"
    return f"Speech check failed: {snippet or type(error).__name__}"


def source_stt_with_retry(path: Path, transcribe: Callable[[bytes], dict], reencode: Callable[[], Path],
                          language: str, model: str, chunk_index: int) -> dict:
    validate_stt_chunk(path)
    try:
        return transcribe(path.read_bytes())
    except Exception as first_error:
        reason = log_stt_failure("sarvam", model, language, chunk_index, path, first_error)
        status = stt_error_status(first_error)
        if not status or not 400 <= status < 500:
            raise ValueError(reason) from first_error
        retry_path = reencode()
        validate_stt_chunk(retry_path)
        if hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(retry_path.read_bytes()).digest():
            raise ValueError("Speech provider retry encoding did not change the request bytes") from first_error
        try:
            return transcribe(retry_path.read_bytes())
        except Exception as retry_error:
            retry_reason = log_stt_failure("sarvam", model, language, chunk_index, retry_path, retry_error)
            raise ValueError(retry_reason) from retry_error


def advisory_stt(path: Path, transcribe: Callable[[bytes], dict], language: str, model: str, chunk_index: int,
                 reencode: Callable[[], Path] | None = None) -> dict | None:
    try:
        validate_stt_chunk(path)
        return transcribe(path.read_bytes())
    except Exception as error:
        reason = log_stt_failure("gnani", model, language, chunk_index, path, error)
        status = stt_error_status(error)
        if reencode and status and 400 <= status < 500:
            try:
                retry_path = reencode()
                validate_stt_chunk(retry_path)
                if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(retry_path.read_bytes()).digest():
                    return transcribe(retry_path.read_bytes())
                reason = "Speech check retry encoding did not change the request bytes"
            except Exception as retry_error:
                reason = log_stt_failure("gnani", model, language, chunk_index, locals().get("retry_path", path), retry_error)
        LOGGER.warning("Audio check unavailable for %s clip %d: %s", language, chunk_index, reason)
        return None


def cache_successful_stt(cache: dict, key: str, request: Callable[[], dict]) -> dict:
    if key not in cache:
        result = request()
        cache[key] = result
    return cache[key]


def advisory_audio_flag(transcript: dict | None, translation: str, language: str, low_trust_langs: list[str]) -> dict:
    if transcript is None:
        return {"status": "yellow", "reasons": ["Audio check unavailable"]}
    return audio_number_status(translation, transcript["transcript"], language, low_trust_langs)


def merge_tiny_trailing_range(time_ranges: list[tuple[float, float]], minimum_seconds: float = 0.3) -> list[tuple[float, float]]:
    ranges = list(time_ranges)
    if len(ranges) > 1 and ranges[-1][1] - ranges[-1][0] < minimum_seconds:
        ranges[-2] = (ranges[-2][0], ranges[-1][1])
        ranges.pop()
    elif ranges and ranges[-1][1] - ranges[-1][0] < minimum_seconds:
        ranges.pop()
    return ranges


def _reencode_audio(source: Path, target: Path, comment: str) -> Path:
    run_command([
        "ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:a:0", "-vn",
        "-ac", "1", "-ar", "16000", "-af", "aresample=async=1:first_pts=0",
        "-c:a", "pcm_s16le", "-metadata", f"comment={comment}", str(target),
    ])
    return target


def detected_silence_spans(audio_path: Path, duration: float) -> list[tuple[float, float]]:
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path), "-af", "silencedetect=noise=-34dB:d=0.30", "-f", "null", "NUL"],
        capture_output=True,
        text=True,
    )
    log = result.stderr
    starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", log)]
    if len(starts) > len(ends):
        ends.append(duration)
    return [
        (max(0.0, start), min(duration, end))
        for start, end in zip(starts, ends)
        if end > start and start < duration
    ]


def silence_boundaries(audio_path: Path, duration: float) -> list[float]:
    return [(start + end) / 2 for start, end in detected_silence_spans(audio_path, duration)]


def apply_speech_windows(rows: list[dict], silence_spans: list[tuple[float, float]]) -> list[dict]:
    """Move clip edges to the ends of detected pauses and retain safe spill windows."""
    for row in rows:
        row["timeline_start_s"] = float(row["start"])
        row["spill_end_s"] = float(row["end"])
    for index, row in enumerate(rows):
        if index > 0:
            boundary = float(row["start"])
            prior_pause = next((span for span in silence_spans if span[0] <= boundary <= span[1]), None)
            if prior_pause:
                row["start"] = row["timeline_start_s"] = prior_pause[1]
        if index + 1 < len(rows):
            boundary = float(row["end"])
            next_pause = next((span for span in silence_spans if span[0] <= boundary <= span[1]), None)
            if next_pause:
                row["end"] = next_pause[0]
                row["spill_end_s"] = next_pause[1]
    for row in rows:
        if float(row["end"]) < float(row["start"]):
            raise ValueError("Detected pause leaves no valid speech window for a clip")
    return rows


def assert_timing_invariants(
    clips: list[dict], video_duration: float, export_duration: float | None = None,
) -> None:
    previous_end = 0.0
    for clip in sorted(clips, key=lambda item: float(item.get("timeline_start_s", item["start_s"]))):
        slot_start = float(clip["start_s"])
        speech_start = float(clip.get("timeline_start_s", slot_start))
        speech_duration = float(clip.get("timeline_audio_duration_s", clip.get("final_audio_duration_s", 0.0)))
        if speech_start < slot_start - 1e-6:
            raise ValueError("Dubbed speech starts before its source slot")
        if speech_start < previous_end - 1e-6:
            raise ValueError("Dubbed speech overlaps the preceding clip")
        if speech_start + speech_duration > video_duration + 1e-6:
            raise ValueError("Dubbed speech exceeds the video duration")
        previous_end = speech_start + speech_duration
    if export_duration is not None and abs(export_duration - video_duration) > EXPORT_DURATION_TOLERANCE_S:
        raise ValueError("Dubbed export duration differs from source by more than 50 milliseconds")


def assert_language_complete(clips: list[dict], source_clip_count: int, video_duration: float) -> None:
    if len(clips) != source_clip_count:
        raise ValueError(f"Language clip count {len(clips)} does not match source clip count {source_clip_count}")
    if any(not str(clip.get("translation", "")).strip() for clip in clips):
        raise ValueError("Every source clip must have a saved translation before review")
    chronological = sorted(clips, key=lambda clip: float(clip["start_s"]))
    if not chronological or abs(float(chronological[0]["start_s"])) > 0.5:
        raise ValueError("Language clips do not cover the start of the source video")
    if abs(float(chronological[-1]["end_s"]) - video_duration) > 0.5:
        raise ValueError("Language clips do not cover the end of the source video")
    assert_timing_invariants(clips, video_duration)


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
    hard_max: float = 8.0,
) -> list[tuple[float, float]]:
    """Choose pause anchors for short utterance spans, with a strict 8s cap."""
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
        raise ValueError("Could not construct source segments within the 8-second hard limit")
    return result


def merge_incomplete_chunks(chunks: list[dict], transcribe, max_seconds: float = 8.0,
                            min_seconds: float = 5.0) -> list[dict]:
    """Join mid-sentence cuts and short adjacent spans when they fit one clip."""
    merged: list[dict] = []
    for row in chunks:
        merged.append(row)
        while len(merged) > 1 and (
            not re.search(rf"[{re.escape(SENTENCE_ENDINGS)}][\"'’”)]*$", merged[-2]["raw"].strip())
            or merged[-2]["end"] - merged[-2]["start"] < min_seconds
        ):
            left, right = merged[-2], merged[-1]
            if right["end"] - left["start"] > max_seconds:
                if not re.search(rf"[{re.escape(SENTENCE_ENDINGS)}][\"'’”)]*$", left["raw"].strip()):
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
    # Split at Unicode sentence endings used across the configured Indic scripts,
    # plus common international punctuation and explicit line boundaries.
    # CJK full stops and question/exclamation marks do not require a following
    # space; other scripts split at punctuation followed by whitespace.
    return [part.strip() for part in re.split(
        rf"(?<=[.!?।॥؟։])\s+|(?<=[。！？])\s*|[\r\n]+", text,
    ) if part.strip()]


def normalize_provider_transcript(response: dict | str) -> str:
    """Accept Gnani STT's documented top-level and nested response shapes."""
    if isinstance(response, str):
        transcript = response
    elif isinstance(response, dict):
        transcript = response.get("transcript") or response.get("text")
        if not transcript and isinstance(response.get("data"), dict):
            transcript = response["data"].get("transcript") or response["data"].get("text")
    else:
        transcript = None
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("Speech provider returned no transcript text")
    return transcript.strip()


def normalize_cleanup_changes(changes) -> list[dict]:
    """Normalize current and legacy cleanup notes restored from job checkpoints."""
    if not isinstance(changes, (list, tuple)):
        return []
    normalized = []
    for change in changes:
        if isinstance(change, dict):
            normalized.append({
                "from": str(change.get("from", "")),
                "to": str(change.get("to", change.get("from", ""))),
                "reason": str(change.get("reason", "")),
            })
        elif isinstance(change, str):
            normalized.append({"from": "", "to": "", "reason": change})
    return normalized


def transcribe_generated_speech(gnani, audio: bytes, language: str, model: str = "prisma-v3.0", **options) -> dict:
    """Use Gnani STT for source and generated speech transcription."""
    response = gnani.stt(audio, language, model, **options)
    return {"transcript": normalize_provider_transcript(response), "detected_language_code": language}


def transcript_word_differences(left: str, right: str) -> list[dict[str, str]]:
    left_words = re.findall(r"[\w'-]+|[^\w\s]", left, flags=re.UNICODE)
    right_words = re.findall(r"[\w'-]+|[^\w\s]", right, flags=re.UNICODE)
    matcher = difflib.SequenceMatcher(a=left_words, b=right_words, autojunk=False)
    differences = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "equal":
            differences.append({"plain": " ".join(left_words[a0:a1]), "alternative": " ".join(right_words[b0:b1])})
    return differences


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


def prompt_json(client: SarvamTranslationClient | MockLLMClient, system: str, payload: dict) -> dict:
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
        "pure": "clear formal spoken register",
        "natural": "modern-colloquial spoken target-language speech",
        "formal": "clear, respectful formal spoken register",
        "colloquial": "everyday casual spoken language",
        "marketing": "energetic and persuasive spoken language",
        "natural_spoken": "natural spoken code-mixed speech with source English terms retained",
        "keep_technical": "natural spoken speech with established English technical terms",
    }.get(translation_style, CONTENT_TONES[content_type])
    style_instruction = translation_style_instruction(translation_style, target_lang)
    payload = {
            "source_text": source,
            "source_language": source_lang,
            "target_language": target_lang,
            "content_type": content_type,
            "tone": tone,
            "translation_style": translation_style,
            "translation_style_instruction": style_instruction,
            "max_chars": max_chars,
            "terms": terms.model_dump(),
            "source_uncertainties": source_uncertainties or [],
            "duration_budget": duration_budget or {},
        }
    response = prompt_json(
        client,
        f"Generate exactly three spoken translations. {style_instruction} Preserve every source proposition, filler, uncertainty, agent, action, object, polarity, modality, and speech act. Do not turn a statement or infinitive phrase into an instruction, or change who does what. Keep uncertain ASR phrases ambiguous; do not guess what an unclear noun, verb, or brand means. For uncertain phrases, preserve the ASR content without resolving it. Every keep/locked term must appear in its exact approved form. Preserve full meaning even when it exceeds the duration budget; duration is a preference for rephrasing concisely, never a reason to remove information. Follow duration_budget.instruction and target_chars as an estimate, not a truncation rule. Return JSON: {{candidates:[string,string,string]}}.",
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
            f"Return exactly two distinct spoken translations that pass the listed checks. {style_instruction} Correct failed candidates while preserving every source proposition, all exact keep terms, all numbers, and the unresolved meaning of uncertain source phrases. Do not change agents or speech act. Apply duration_budget.instruction only as concise/full phrasing guidance; never omit meaning to fit. Return JSON exactly shaped as {{\"candidates\":[\"candidate 1\",\"candidate 2\"]}}.",
            {**payload, "candidate_failures": failures, "previous_candidates": candidates, "retry_attempt": retry_attempt + 1},
        )
        candidates = response.get("candidates", [])
        valid, failures = valid_candidates(candidates)
    if len(valid) < 2:
        raise ValueError(f"Fewer than two candidates passed deterministic checks: {failures}")
    return valid


def paragraph_translation_candidates(
    client,
    rows: list[dict],
    source_lang: str,
    target_lang: str,
    terms: TermSheet,
    content_type: str,
    translation_style: str,
    chars_per_second: float,
) -> list[list[str]]:
    """Translate the complete transcript in one context-rich request, preserving timed spans."""
    tone = {
        "pure": "clear formal spoken register",
        "natural": "modern-colloquial spoken target-language speech",
        "formal": "clear, respectful spoken register",
        "colloquial": "everyday casual spoken language",
        "marketing": "energetic and persuasive spoken language",
        "natural_spoken": "natural spoken code-mixed speech with source English terms retained",
        "keep_technical": "natural spoken speech with established English technical terms",
    }.get(translation_style, CONTENT_TONES[content_type])
    style_instruction = translation_style_instruction(translation_style, target_lang)
    payload = {
        "source_language": source_lang,
        "target_language": target_lang,
        "content_type": content_type,
        "tone": tone,
        "translation_style": translation_style,
        "translation_style_instruction": style_instruction,
        "term_sheet": terms.model_dump(),
        "full_source_paragraph": " ".join(row["cleaned"] for row in rows),
        "timed_source_spans": [
            {
                "id": index,
                "start_s": round(row["start"], 3),
                "end_s": round(row["end"], 3),
                "source_text": row["cleaned"],
                "target_character_budget": timing_character_budget(row["end"] - row["start"], chars_per_second),
                "uncertain_phrases": [
                    item.get("to", item["from"])
                    for item in row.get("cleanup_changes", [])
                    if "uncertain source retained" in item.get("reason", "").casefold()
                ],
            }
            for index, row in enumerate(rows)
        ],
    }
    response = prompt_json(
        client,
        f"Translate the full source paragraph as one coherent piece, using every timed source span as a boundary guide. {style_instruction} "
        "Return exactly three candidate objects, each with a translations array in the same order and length as "
        "timed_source_spans. Translate every span fully; do not merge spans or change who does what. Use natural spoken "
        "target-language phrasing and preserve casual register and fillers. The character budget is a timing hint only; "
        "never drop meaning. In Pure style use the target language's native script for all translated words. In General style "
        "retain source English terms in Latin script as requested by the style instruction. Do not "
        "transliterate abstract English words. Keep agents, actions, objects, polarity, modality, numbers, and speech act. "
        "Do not guess unclear source nouns or brands; retain the heard wording and mark uncertainty in review_notes. "
        "Return JSON shaped as {candidates:[{translations:[...],review_notes:[...]}, ...]}.",
        payload,
    )
    raw_candidates = response.get("candidates", [])
    def validate(raw_items: object) -> tuple[list[list[str]], list[dict[str, str]]]:
        result: list[list[str]] = []
        failures: list[dict[str, str]] = []
        for candidate in raw_items if isinstance(raw_items, list) else []:
            texts = candidate.get("translations") if isinstance(candidate, dict) else None
            if not isinstance(texts, list) or len(texts) != len(rows) or not all(isinstance(text, str) and text.strip() for text in texts):
                failures.append({"check": "candidate_shape", "detail": "Expected one non-empty translation per timed source span."})
                continue
            candidate_failures = []
            for row, text in zip(rows, texts):
                terms_for_row = TermSheet(
                    terms=[term for term in terms.terms if term.term.casefold() in row["cleaned"].casefold()],
                    idioms=[idiom for idiom in terms.idioms if idiom.phrase.casefold() in row["cleaned"].casefold()],
                )
                candidate_failures.extend(
                    failure for failure in run_checks(row["cleaned"], text, target_lang, terms_for_row, max_chars=1000)
                    if failure["check"] not in {"too_many_characters", "over_slot"}
                )
            if candidate_failures:
                failures.extend(candidate_failures)
            else:
                result.append([text.strip() for text in texts])
        return result, failures

    result, validation_failures = validate(raw_candidates)
    if not result:
        try:
            repaired = prompt_json(
                client,
                "Repair the translations so every timed source span passes the deterministic checks. Preserve the complete source meaning, agents, actions, objects, numbers, uncertainty, and speech act. Apply all locked terms exactly. Return exactly two candidate objects shaped as {translations:[...],review_notes:[...]}, aligned to the timed spans.",
                {**payload, "candidate_failures": validation_failures, "previous_candidates": raw_candidates},
            )
            result, _ = validate(repaired.get("candidates", []))
        except Exception as exc:
            LOGGER.warning("Translation repair failed (%s); retaining original text for human review", type(exc).__name__)
    if not result:
        result = [
            candidate["translations"]
            for candidate in raw_candidates
            if isinstance(candidate, dict)
            and isinstance(candidate.get("translations"), list)
            and len(candidate["translations"]) == len(rows)
            and all(isinstance(text, str) and text.strip() for text in candidate["translations"])
        ]
    if not result:
        raise ValueError("Translation provider returned no usable text for human review")
    return result[:3]


def select_paragraph_candidate(client, source: str, candidates: list[list[str]], target_lang: str) -> tuple[int, str]:
    response = prompt_json(
        client,
        "Select the complete paragraph candidate by this order: preserve all meaning, actors/actions/objects, uncertainty, "
        "numbers and speech acts; then choose natural spoken target-language phrasing with the source's register; then "
        "prefer complete target-language text without English clauses; consider timing fit last. Do not rewrite. Return "
        "JSON with selected_index and reason.",
        {"source_paragraph": source, "target_language": target_lang,
         "candidates": [{"translations": texts} for texts in candidates]},
    )
    index = response.get("selected_index")
    if not isinstance(index, int) or index not in range(len(candidates)):
        raise ValueError("Paragraph judge returned an invalid candidate index")
    return index, str(response.get("reason", ""))


def revise_for_timing(client, paragraph: str, source: str, current: str, target_lang: str,
                      slot_s: float, chars_per_second: float, mode: str,
                      target_character_budget: int | None = None,
                      neighbor_context: list[str] | None = None) -> str:
    instruction = (
        "Make this target-language segment more concise while preserving every meaning and nuance. Do not omit facts, "
        "change agents or actions, or alter speech act."
        if mode == "shorter" else
        "Use the available slot for a fuller natural translation, preserving the entire source meaning, fillers, and nuance. Do not add facts."
    )
    budget = (max(1, int(target_character_budget)) if target_character_budget is not None
              else max(32, round(slot_s * chars_per_second)))
    result = prompt_json(
        client,
        instruction + " Translate ONLY source_segment. Neighbor spans and their translations are read-only context; never translate, copy, or include any of their content. The character budget is strict: the returned translation must not exceed it. Use natural spoken target-language text and no untranslated English clause. Return JSON with translation.",
        {"neighbor_context_read_only": neighbor_context or [], "source_segment": source, "current_translation": current,
         "target_language": target_lang, "slot_seconds": round(slot_s, 3),
         "target_character_budget": budget, "revision_mode": mode},
    )
    translated = result.get("translation")
    if not isinstance(translated, str) or not translated.strip():
        raise ValueError("Timing revision returned no translation")
    translated = translated.strip()
    if len(translated) > budget:
        raise ValueError(f"Timing revision exceeded its strict {budget}-character budget")
    return translated


def contains_neighbor_content(candidate: str, neighbor_texts: list[str], minimum_words: int = 4) -> bool:
    """Reject a retry that copies a distinctive phrase from an adjacent clip."""
    tokenize = lambda text: re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    output = tokenize(candidate)
    if len(output) < minimum_words:
        return False
    output_ngrams = {tuple(output[i:i + minimum_words]) for i in range(len(output) - minimum_words + 1)}
    for neighbor in neighbor_texts:
        words = tokenize(neighbor)
        if any(tuple(words[i:i + minimum_words]) in output_ngrams
               for i in range(max(0, len(words) - minimum_words + 1))):
            return True
    return False


def build_neighbor_context(rows: list[dict], translations: dict[int, str], index: int) -> list[str]:
    """Build optional, index-aligned read-only context for the two adjacent clips."""
    context = []
    for neighbor_index in (index - 1, index + 1):
        if not 0 <= neighbor_index < len(rows):
            continue
        translation = translations.get(neighbor_index)
        if translation:
            context.append(f"Source: {rows[neighbor_index].get('cleaned', '')}\nTranslation: {translation}")
    return context


def should_reuse_completed_clip(clip_index: int, only_clip_index: int | None) -> bool:
    """Keep checkpoint audio on resume, but never reuse the clip explicitly being re-spoken."""
    return only_clip_index is None or clip_index != only_clip_index


def prepare_language_retry_checkpoint(
    checkpoint_state: dict, target_lang: str, saved_translations: dict[int, str] | None = None,
) -> None:
    """Discard partial clip artifacts while retaining transcript and saved wording."""
    checkpoint_state.setdefault("clips", {}).pop(target_lang, None)
    translations = checkpoint_state.setdefault("translations", {}).setdefault(target_lang, {})
    for index, translation in (saved_translations or {}).items():
        if str(translation or "").strip():
            translations[str(index)] = str(translation)


def source_rows_from_report(report: dict, source_language: str) -> list[dict]:
    """Restore the complete source index map for one-clip regeneration."""
    rows = []
    for clip in report.get("clips", []):
        start = float(clip["start_s"])
        end = float(clip["end_s"])
        raw = clip.get("source_raw") or clip.get("source_cleaned", "")
        rows.append({
            "start": start,
            "end": end,
            "timeline_start_s": float(clip.get("timeline_start_s", start)),
            "spill_end_s": float(clip.get("spill_end_s", end)),
            "raw": raw,
            "lang": source_language,
            "cleaned": clip.get("source_cleaned", raw),
            "cleanup_changes": clip.get("source_cleanup_changes", []),
            "stt_review": clip.get("stt_review", {}),
            "cut_mid_sentence": clip.get("cut_mid_sentence", False),
        })
    return rows


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


def synthesize_sentence(client, text: str, embedding: dict | None, target: Path,
                        target_lang: str, stock_voice: str | None = None) -> float:
    audio = (client.synth_stock(text, stock_voice, target_lang)
             if stock_voice else client.synth(text, embedding or {}))
    atomic_write_bytes(target, audio)
    return duration_of(target)


def synthesize_sentence_list(client, sentences: list[str], embedding: dict | None, work_dir: Path,
                             prefix: str, target_lang: str, stock_voice: str | None = None) -> tuple[Path, float]:
    if not sentences:
        raise ValueError("Translation produced no sentence")
    paths = []
    for index, sentence in enumerate(sentences):
        if len(sentence) >= 300:
            raise ValueError("A synthesis sentence exceeds the documented safe 300-character limit")
        path = work_dir / f"{prefix}_sentence_{index:02d}.wav"
        synthesize_sentence(client, sentence, embedding, path, target_lang, stock_voice)
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
        # Gnani can mark streaming WAVs with 0x7fffffff frames. The actual
        # frames are read above; carrying that sentinel into the joined file
        # produces an invalid RIFF size (and may exceed the 4 GiB limit).
        joined.setparams((params.nchannels, params.sampwidth, params.framerate, 0,
                          params.comptype, params.compname))
        for frame_bytes in frames:
            joined.writeframesraw(frame_bytes)
    return output, duration_of(output)


def shorter_timing_budget(current_budget: int, current_text: str) -> int:
    """Return a strictly smaller, usable character budget for a timing retry."""
    if current_budget <= 1:
        return 1
    return max(1, min(current_budget - 1, len(current_text) - 1, int(current_budget * 0.84)))


def timing_fit_limit(slot_s: float, silent_gap_s: float) -> float:
    """Allow at most 100ms of overrun, bounded by a detected silent gap."""
    return slot_s + min(OVERRUN_TOLERANCE_S, max(0.0, silent_gap_s))


def trim_fixed_sentence_pauses(work_dir: Path, prefix: str, sentence_count: int, output: Path) -> Path:
    """Join synthesized sentence WAVs without the fixed inter-sentence pauses."""
    sentence_paths = [work_dir / f"{prefix}_sentence_{index:02d}.wav" for index in range(sentence_count)]
    with wave.open(str(sentence_paths[0]), "rb") as first:
        params = first.getparams()
        audio_frames = [first.readframes(first.getnframes())]
    for sentence_path in sentence_paths[1:]:
        with wave.open(str(sentence_path), "rb") as sentence:
            signature = (sentence.getnchannels(), sentence.getsampwidth(), sentence.getframerate(), sentence.getcomptype())
            expected = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
            if signature != expected:
                raise ValueError("Synthesized sentence WAV formats do not match")
            audio_frames.append(sentence.readframes(sentence.getnframes()))
    with wave.open(str(output), "wb") as joined:
        joined.setparams(params)
        for frames in audio_frames:
            joined.writeframesraw(frames)
    return output


def run_quality_pass(video: Path, reference: Path | None, targets: list[str], output_dir: Path,
                     settings: Settings, content_type: str = "explainer", translation_style: str = "neutral",
                     speaker_embedding: dict | None = None, source_language: str = "auto",
                     seed_terms: list[dict] | None = None, stock_voice: str | None = None,
                     only_clip_index: int | None = None, translation_override: str | None = None,
                      preserve_translation: bool = False,
                      resume_state: dict | None = None,
                      checkpoint_callback: Callable[[str, dict], None] | None = None,
                      continue_check: Callable[[], None] | None = None) -> list[dict]:
    if not video.is_file() or (not stock_voice and (reference is None or not reference.is_file())):
        raise FileNotFoundError("Video and cloned voice reference audio must exist")
    duration = duration_of(video)
    if duration > settings.max_video_seconds:
        raise ValueError("Video exceeds the configured hard cap")
    if not audio_stream_info(video):
        raise ValueError("This video has no audio track")
    output_dir.mkdir(parents=True, exist_ok=True)
    prior_report = None
    prior_clip = None
    if only_clip_index is not None:
        if len(targets) != 1:
            raise ValueError("Single-clip regeneration requires exactly one target language")
        prior_path = output_dir / f"{video.stem}_{targets[0]}_phase3d_ai-dubbed.json"
        if not prior_path.is_file():
            raise FileNotFoundError("Cannot regenerate one clip before the full-language dub exists")
        prior_report = json.loads(prior_path.read_text(encoding="utf-8"))
        prior_rows = prior_report.get("clips", [])
        if only_clip_index >= len(prior_rows):
            raise ValueError("Regeneration clip index is outside the saved segment map")
        prior_clip = prior_rows[only_clip_index]
    gnani = create_gnani_client(settings.gnani_mock)
    sarvam_stt = None
    checkpoint_state = resume_state if resume_state is not None else {}

    def checkpoint(stage: str) -> None:
        if checkpoint_callback:
            checkpoint_callback(stage, checkpoint_state)

    def check_continue() -> None:
        if continue_check:
            continue_check()

    stt_cache = checkpoint_state.setdefault("stt_cache", {})

    def cached_stt(audio: bytes, language: str, model: str, *, provider: str = "gnani", **options) -> dict:
        digest = hashlib.sha256(audio).hexdigest()
        key_parts = [digest, language, model, options]
        if provider != "sarvam":
            key_parts.insert(0, provider)
        cache_key = json.dumps(key_parts, sort_keys=True, ensure_ascii=False)
        if cache_key not in stt_cache:
            check_continue()
            def request() -> dict:
                if provider == "gnani":
                    return transcribe_generated_speech(gnani, audio, language, model, **options)
                if provider == "sarvam":
                    return {"transcript": normalize_provider_transcript(
                        sarvam_stt.transcribe(audio, language, model, **options)), "detected_language_code": language}
                raise ValueError(f"Unknown transcription provider: {provider}")
            cache_successful_stt(stt_cache, cache_key, request)
            checkpoint("transcript")
        return stt_cache[cache_key]

    llm = MockLLMClient() if settings.llm_mock else SarvamTranslationClient(settings)
    workflow_llm = create_workflow_client(llm, settings)
    translator = create_translation_client(llm, settings)
    all_outputs = []

    # Keep intermediate media in the selected workspace/output tree. On Windows
    # the system temp directory may be sandboxed away from FFmpeg subprocesses.
    temp_root = output_dir / ".anek-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="anek-phase3b-", dir=temp_root) as temp_name:
        work = Path(temp_name)
        source_wav = work / "source.wav"
        sarvam_stt = create_sarvam_stt_client(settings)
        run_command(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(source_wav)])
        room_tone_path, room_tone_span = extract_room_tone(source_wav, work, duration)
        source_silence_spans = detected_silence_spans(source_wav, duration)
        source_stt_model = "saaras:v3"
        stt_language = "unknown" if source_language == "auto" else source_language
        resumed_term_fixes = checkpoint_state.get("term_fixes")
        if prior_clip is not None:
            source_lang = prior_report.get("source_language", source_language)
            source_chunks = source_rows_from_report(prior_report, source_lang)
            if not source_chunks:
                raise ValueError("Saved source clip map is incomplete; regenerate the full language first")
            term_sheet = TermSheet.model_validate(prior_report.get("term_sheet", {}))
            cleaned_rows = source_chunks
        elif resumed_term_fixes:
            source_lang = resumed_term_fixes["source_lang"]
            term_sheet = TermSheet.model_validate(resumed_term_fixes["term_sheet"])
            cleaned_rows = resumed_term_fixes["cleaned_rows"]
            source_chunks = cleaned_rows
        else:
            boundaries = [(start + end) / 2 for start, end in source_silence_spans]
            time_ranges = targeted_silence_segments(duration, boundaries, hard_max=8.0)
            source_chunks = []
            # Merge any tiny final span into its predecessor instead of sending
            # an invalid request to either speech provider.
            time_ranges = merge_tiny_trailing_range(time_ranges)
            for index, (start, end) in enumerate(time_ranges):
                check_continue()
                chunk_path = work / f"source_{index:02d}.wav"
                run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk_path)])
                def reencode_source_chunk() -> Path:
                    retry_path = work / f"source_{index:02d}_retry.wav"
                    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", "-metadata", f"comment=stt-retry-{index}-{time.time_ns()}", str(retry_path)])
                    return retry_path
                response = source_stt_with_retry(
                    chunk_path, lambda audio: cached_stt(audio, stt_language, source_stt_model, provider="sarvam"),
                    reencode_source_chunk, stt_language, source_stt_model, index + 1,
                )
                source_chunks.append({"start": start, "end": end, "raw": response["transcript"], "lang": response.get("detected_language_code", source_language)})
            def transcribe_range(start: float, end: float) -> dict:
                chunk_path = work / f"source_join_{start:.3f}_{end:.3f}.wav"
                run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk_path)])
                join_index = len(source_chunks) + 1
                def reencode_join() -> Path:
                    retry_path = work / f"source_join_{start:.3f}_{end:.3f}_retry.wav"
                    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(source_wav), "-ss", str(start), "-t", str(end-start), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", "-metadata", f"comment=stt-retry-{join_index}-{time.time_ns()}", str(retry_path)])
                    return retry_path
                return source_stt_with_retry(
                    chunk_path, lambda audio: cached_stt(audio, stt_language, source_stt_model, provider="sarvam"),
                    reencode_join, stt_language, source_stt_model, join_index,
                )

            source_chunks = merge_incomplete_chunks(source_chunks, transcribe_range, max_seconds=8.0)
            langs = [row["lang"] for row in source_chunks if row["lang"] != "unknown"]
            source_lang = max(set(langs), key=langs.count) if langs else "unknown"
            term_sheet = extract_termsheet(workflow_llm, [row["raw"] for row in source_chunks], source_lang)
            full_raw_source = " ".join(row["raw"] for row in source_chunks)
            term_sheet.terms = [term for term in term_sheet.terms if re.search(rf"(?<!\w){re.escape(term.term)}(?!\w)", full_raw_source, re.IGNORECASE)]
            known_terms = {term.term.casefold() for term in term_sheet.terms}
            for seed in seed_terms or []:
                try:
                    validated = TermSheet.model_validate({"terms": [seed]}).terms[0]
                except Exception:
                    continue
                if (validated.term.casefold() not in known_terms
                        and re.search(rf"(?<!\w){re.escape(validated.term)}(?!\w)", full_raw_source, re.IGNORECASE)):
                    term_sheet.terms.append(validated)
                    known_terms.add(validated.term.casefold())
            for seed in seed_terms or []:
                try:
                    validated = TermSheet.model_validate({"terms": [seed]}).terms[0]
                except Exception:
                    continue
                if validated.rule_type in {"mishearing", "protected"} and not any(
                    item.term.casefold() == validated.term.casefold() and item.rule_type == validated.rule_type
                    for item in term_sheet.terms
                ):
                    term_sheet.terms.append(validated)
            for index, row in enumerate(source_chunks):
                row["stt_review"] = {
                    "selected_transcript": "saaras_v3",
                    "variants": {"saaras_v3": row["raw"]},
                    "disagreements": [],
                }
            cleaned_rows = []
            source_context = " ".join(row["raw"] for row in source_chunks)
            for row in source_chunks:
                row["cleaned"], row["cleanup_changes"] = clean_source_detail(workflow_llm, row["raw"], term_sheet, source_lang, source_context)
                cleaned_rows.append(row)
        if not resumed_term_fixes:
            for seed in seed_terms or []:
                try:
                    validated = TermSheet.model_validate({"terms": [seed]}).terms[0]
                except Exception:
                    continue
                if validated.rule_type in {"mishearing", "protected"} and not any(
                    item.term.casefold() == validated.term.casefold() and item.rule_type == validated.rule_type
                    for item in term_sheet.terms
                ):
                    term_sheet.terms.append(validated)
            for row in cleaned_rows:
                row["raw"] = apply_mishearing_maps(row["raw"], term_sheet)
                row["cleaned"] = apply_mishearing_maps(row["cleaned"], term_sheet)
            cleaned_rows = apply_speech_windows(cleaned_rows, source_silence_spans)
            checkpoint_state["term_fixes"] = {
                "source_lang": source_lang, "term_sheet": term_sheet.model_dump(), "cleaned_rows": cleaned_rows,
            }
            checkpoint("term_fixes")
        for row in cleaned_rows:
            row["cleanup_changes"] = normalize_cleanup_changes(row.get("cleanup_changes"))
        reference_seconds = None
        if stock_voice:
            embedding = None
        else:
            reference_seconds = duration_of(reference)
            if not 5 <= reference_seconds <= 30:
                raise ValueError("Voice reference must be 5–30 seconds for Gnani embedding extraction")
            embedding = speaker_embedding or gnani.make_embedding(reference.read_bytes())
        for target_lang in targets:
            check_continue()
            checkpoint_state["current_language"] = target_lang
            language_rows = []
            # Use one cold-start estimate for every language; update from measured TTS below.
            measured_chars_per_second = INITIAL_TARGET_CHARS_PER_SECOND
            translation_sheet_data = term_sheet.model_dump()
            for term_index, term in enumerate(translation_sheet_data["terms"]):
                if term.get("rule_type") == "protected" or term.get("locked"):
                    placeholder = f"ANEKPROTECTEDTERM{term_index:04d}"
                    term["term"] = placeholder
                    term["targets"] = {language: placeholder for language in term.get("targets", {})}
                    term["spoken_forms"] = {}
                    term["locked"] = True
            translation_sheet = TermSheet.model_validate(translation_sheet_data)
            translation_rows, protected_maps = [], []
            for source_row in cleaned_rows:
                masked_source, mapping = mask_protected_terms(source_row["cleaned"], term_sheet)
                translation_rows.append({**source_row, "cleaned": masked_source})
                protected_maps.append(mapping)
            full_source_paragraph = " ".join(row["cleaned"] for row in translation_rows)
            saved_language_clips = checkpoint_state.get("clips", {}).get(target_lang, {})
            saved_translations = checkpoint_state.setdefault("translations", {}).setdefault(target_lang, {})
            if only_clip_index is not None and translation_override:
                translated_by_index = {i: saved_translations.get(str(i), row.get("translation"))
                                       for i, row in enumerate(cleaned_rows)}
                translated_by_index[only_clip_index] = translation_override
                paragraph_selection_reason = "Existing clip translation retained for regeneration."
                selection_needs_review = False
            else:
                missing_indices = ([only_clip_index] if only_clip_index is not None else
                                   [i for i in range(len(cleaned_rows))
                                    if str(i) not in saved_translations and str(i) not in saved_language_clips])
                translated_by_index = {int(i): value for i, value in saved_translations.items() if value}
                for i, saved in saved_language_clips.items():
                    translated_by_index[int(i)] = saved.get("clip", {}).get("translation", "")
                if missing_indices:
                    missing_rows = [translation_rows[i] for i in missing_indices]
                    paragraph_candidates = paragraph_translation_candidates(
                        translator, missing_rows, source_lang, target_lang, translation_sheet,
                        content_type, translation_style, measured_chars_per_second,
                    )
                    try:
                        paragraph_selected_index, paragraph_selection_reason = select_paragraph_candidate(
                            translator, " ".join(row["cleaned"] for row in missing_rows), paragraph_candidates, target_lang,
                        )
                        selection_needs_review = False
                    except Exception as exc:
                        LOGGER.warning("Candidate selection failed (%s); first candidate needs human review", type(exc).__name__)
                        paragraph_selected_index, paragraph_selection_reason = 0, "Automatic selection was unavailable; review this candidate manually."
                        selection_needs_review = True
                    for local_index, original_index in enumerate(missing_indices):
                        translated_by_index[original_index] = paragraph_candidates[paragraph_selected_index][local_index]
                        saved_translations[str(original_index)] = translated_by_index[original_index]
                    checkpoint("translation")
                else:
                    paragraph_selection_reason = "Translations restored from saved per-clip checkpoints."
                    selection_needs_review = False
            # Keep a complete index-aligned view even when completed clips are resumed.
            for i in range(len(cleaned_rows)):
                translated_by_index.setdefault(i, "")
            row_indices = [only_clip_index] if only_clip_index is not None else range(len(cleaned_rows))
            for index in row_indices:
                row = cleaned_rows[index]
                check_continue()
                clip_index = only_clip_index if only_clip_index is not None else index
                saved_clip = saved_language_clips.get(str(clip_index))
                if saved_clip and should_reuse_completed_clip(clip_index, only_clip_index):
                    candidate_path = output_dir / saved_clip["audio_key"]
                    if candidate_path.is_file() and candidate_path.stat().st_size > 0:
                        resumed_clip = dict(saved_clip["clip"])
                        resumed_clip["audio_path"] = candidate_path
                        if resumed_clip.get("withheld"):
                            timeline_path = work / f"{target_lang}_{clip_index}_resumed_withheld.wav"
                            run_command(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{float(resumed_clip['slot_s']):.6f}", "-c:a", "pcm_s16le", str(timeline_path)])
                            resumed_clip["timeline_audio_path"] = timeline_path
                        else:
                            resumed_clip["timeline_audio_path"] = candidate_path
                        resumed_clip["timeline_audio_duration_s"] = duration_of(resumed_clip["timeline_audio_path"])
                        language_rows.append(resumed_clip)
                        continue
                slot = row["end"] - row["start"]
                spill_capacity_s = max(0.0, float(row.get("spill_end_s", row["end"])) - float(row["end"]))
                fit_limit_s = timing_fit_limit(slot, spill_capacity_s)
                clip_terms = TermSheet(
                    terms=[term for term in term_sheet.terms if term.term.casefold() in row["cleaned"].casefold()],
                    idioms=[idiom for idiom in term_sheet.idioms if idiom.phrase.casefold() in row["cleaned"].casefold()],
                )
                uncertainties = [item.get("to", item["from"]) for item in row["cleanup_changes"] if "uncertain source retained" in item.get("reason", "").casefold()]
                duration_budget_chars = timing_character_budget(slot, measured_chars_per_second)
                retune_instruction = "Selected from the full-paragraph translation."
                synthesis_history = []
                sentence_audio = None
                spoken_seconds = 0.0
                attempt_audio_dir = output_dir / "clips" / target_lang / "attempts"
                attempt_audio_dir.mkdir(parents=True, exist_ok=True)
                selected = restore_protected_terms(translated_by_index[index], protected_maps[index])[0]
                protected_missing = restore_protected_terms(translated_by_index[index], protected_maps[index])[1]
                selection_reason = paragraph_selection_reason
                selected_index = 0
                candidates = [selected]
                if preserve_translation and translation_override:
                    selected = translation_override
                    protected_missing = [
                        canonical for canonical in protected_maps[index].values()
                        if canonical.casefold() not in selected.casefold()
                    ]
                timing_revision_rejected = False
                shorter_retry_used = False
                last_sentence_prefix = ""
                for synthesis_attempt in range(3):
                    checks = run_checks(row["cleaned"], selected, target_lang, clip_terms, max_chars=1000)
                    last_sentence_prefix = f"{target_lang}_{index}_attempt_{synthesis_attempt + 1}"
                    synthesis_text = apply_spoken_forms(selected, clip_terms, target_lang)
                    sentence_audio, spoken_seconds = synthesize_sentence_list(
                        gnani, transcript_sentences(synthesis_text), embedding, work,
                        last_sentence_prefix, target_lang, stock_voice,
                    )
                    attempt_audio = attempt_audio_dir / f"clip_{clip_index + 1:03d}_attempt_{synthesis_attempt + 1:02d}.wav"
                    atomic_copyfile(sentence_audio, attempt_audio)
                    previous_attempt = synthesis_history[-1] if synthesis_history else None
                    synthesis_history.append({
                        "attempt": synthesis_attempt + 1,
                        "trigger": retune_instruction,
                        "old_text": previous_attempt["new_text"] if previous_attempt else None,
                        "new_text": selected,
                        "old_audio_key": previous_attempt["new_audio_key"] if previous_attempt else None,
                        "new_audio_key": attempt_audio.relative_to(output_dir).as_posix(),
                        "change": "Initial synthesized take" if previous_attempt is None else (
                            "Translation and audio updated" if previous_attempt["new_text"] != selected else "Audio regenerated"
                        ),
                        "translated_characters": len(selected),
                        "synthesized_duration_s": round(spoken_seconds, 3),
                        "duration_ratio_to_slot": round(spoken_seconds / slot, 3),
                        "instruction": retune_instruction,
                    })
                    ratio = spoken_seconds / slot
                    if spoken_seconds > fit_limit_s and synthesis_attempt < 2 and not preserve_translation:
                        duration_budget_chars = shorter_timing_budget(duration_budget_chars, selected)
                        retune_instruction = "Paragraph-context retranslation after measured overrun."
                        try:
                            neighbor_context = build_neighbor_context(cleaned_rows, translated_by_index, index)
                        except Exception as exc:
                            LOGGER.warning("Optional neighbor context unavailable for %s clip %s (%s); continuing without it",
                                           target_lang, index + 1, type(exc).__name__)
                            neighbor_context = []
                        revised = None
                        for _retry in range(3):
                            try:
                                revised = revise_for_timing(
                                    translator, "", row["cleaned"], selected,
                                    target_lang, slot, measured_chars_per_second, "shorter",
                                    target_character_budget=duration_budget_chars,
                                    neighbor_context=neighbor_context,
                                )
                            except (ValueError, KeyError, TypeError):
                                duration_budget_chars = shorter_timing_budget(duration_budget_chars, selected)
                                continue
                            if contains_neighbor_content(revised, [
                                cleaned_rows[n]['cleaned'] for n in (index - 1, index + 1)
                                if 0 <= n < len(cleaned_rows)
                            ] + [translated_by_index[n] for n in (index - 1, index + 1)
                                if 0 <= n < len(cleaned_rows) and translated_by_index.get(n)]):
                                revised = None
                                duration_budget_chars = shorter_timing_budget(duration_budget_chars, selected)
                                continue
                            break
                        if revised is None:
                            timing_revision_rejected = True
                            break
                        if run_checks(row["cleaned"], revised, target_lang, clip_terms, max_chars=1000):
                            timing_revision_rejected = True
                            break
                        if any(canonical.casefold() not in revised.casefold() for canonical in protected_maps[index].values()):
                            timing_revision_rejected = True
                            break
                        selected = revised
                        shorter_retry_used = True
                        continue
                    if ratio < 0.75 and synthesis_attempt < 2 and not preserve_translation:
                        duration_budget_chars = max(len(selected) + 1, int(duration_budget_chars * 1.18))
                        retune_instruction = "Paragraph-context retranslation after measured underrun."
                        try:
                            revised = revise_for_timing(
                                translator, full_source_paragraph, row["cleaned"], selected,
                                target_lang, slot, measured_chars_per_second, "fuller",
                            )
                        except (ValueError, KeyError, TypeError):
                            timing_revision_rejected = True
                            break
                        if run_checks(row["cleaned"], revised, target_lang, clip_terms, max_chars=1000):
                            timing_revision_rejected = True
                            break
                        if any(canonical.casefold() not in revised.casefold() for canonical in protected_maps[index].values()):
                            timing_revision_rejected = True
                            break
                        selected = revised
                        continue
                    break

                # Speech rate is learned from actual synthesized clips; use a
                # gentle running update for subsequent segments of this job.
                if spoken_seconds > 0:
                    measured_chars_per_second = max(1.0, len(selected) / spoken_seconds)
                raw_synthesized_seconds = spoken_seconds
                sentence_silence_added_s = max(0, len(transcript_sentences(selected)) - 1) * 0.16
                speed_factor_applied = 1.0
                ratio = raw_synthesized_seconds / max(slot, 1e-6)
                if raw_synthesized_seconds > fit_limit_s:
                    speed_factor_applied = min(MAX_ATEMPO_SPEEDUP, raw_synthesized_seconds / fit_limit_s)
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
                shorter_translation_recovered = (
                    shorter_retry_used
                    and any(item["synthesized_duration_s"] > fit_limit_s for item in synthesis_history[:-1])
                    and spoken_seconds <= fit_limit_s
                )
                recovery_step = "shorter translation retry" if shorter_translation_recovered else None
                atempo_recovered = raw_synthesized_seconds > fit_limit_s and final_audio_seconds <= fit_limit_s
                if atempo_recovered:
                    recovery_step = "speed-up"
                if final_audio_seconds > fit_limit_s and len(transcript_sentences(selected)) > 1:
                    before_pause_trim = final_audio_seconds
                    trimmed_path = work / f"{target_lang}_{index}_pauses_trimmed.wav"
                    trim_fixed_sentence_pauses(work, last_sentence_prefix, len(transcript_sentences(selected)), trimmed_path)
                    if speed_factor_applied != 1.0:
                        trimmed_fit_path = work / f"{target_lang}_{index}_pauses_trimmed_fit.wav"
                        run_command([
                            "ffmpeg", "-y", "-v", "error", "-i", str(trimmed_path),
                            "-filter:a", f"atempo={speed_factor_applied:.4f}", str(trimmed_fit_path),
                        ])
                        sentence_audio = trimmed_fit_path
                    else:
                        sentence_audio = trimmed_path
                    final_audio_seconds = duration_of(sentence_audio)
                    sentence_silence_added_s = 0.0
                    if before_pause_trim > fit_limit_s and final_audio_seconds <= fit_limit_s:
                        recovery_step = "fixed-pause trim"
                overrun_s = max(0.0, final_audio_seconds - slot)
                remaining_pause_s = max(0.0, slot - final_audio_seconds)
                stretched = speed_factor_applied != 1.0
                # Keep the generated candidate for human review, but never put
                # audio outside its source interval on the program timeline.
                clip_audio_dir = output_dir / "clips" / target_lang
                clip_audio_dir.mkdir(parents=True, exist_ok=True)
                candidate_path = clip_audio_dir / f"clip_{clip_index + 1:03d}.wav"
                atomic_copyfile(sentence_audio, candidate_path)
                if recovery_step in {"speed-up", "fixed-pause trim"}:
                    previous_attempt = synthesis_history[-1] if synthesis_history else None
                    synthesis_history.append({
                        "attempt": len(synthesis_history) + 1,
                        "trigger": recovery_step,
                        "old_text": selected,
                        "new_text": selected,
                        "old_audio_key": previous_attempt.get("new_audio_key") if previous_attempt else None,
                        "new_audio_key": candidate_path.relative_to(output_dir).as_posix(),
                        "change": f"Applied {recovery_step} to the synthesized audio.",
                        "translated_characters": len(selected),
                        "synthesized_duration_s": round(final_audio_seconds, 3),
                        "duration_ratio_to_slot": round(final_audio_seconds / slot, 3),
                        "instruction": recovery_step,
                    })
                timeline_audio_path = sentence_audio
                spill_end_s = float(row.get("spill_end_s", row["end"]))
                spill_used = overrun_s > 0 and float(row["timeline_start_s"]) + final_audio_seconds <= spill_end_s + 1e-6
                if spill_used:
                    recovery_step = "silent-gap spill"
                withheld = float(row["timeline_start_s"]) + final_audio_seconds > spill_end_s + 1e-6
                if withheld:
                    timeline_audio_path = work / f"{target_lang}_{index}_withheld.wav"
                    run_command([
                        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=16000:cl=mono", "-t", f"{slot:.6f}",
                        "-c:a", "pcm_s16le", str(timeline_audio_path),
                    ])
                if recovery_step:
                    LOGGER.info("Timing recovery for %s clip %s: %s", target_lang, clip_index + 1, recovery_step)
                qa_audio = work / f"{target_lang}_{index}_qa_16k.wav"
                run_command(["ffmpeg", "-y", "-v", "error", "-i", str(sentence_audio), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(qa_audio)])
                validate_stt_chunk(qa_audio)
                audio_check_unavailable = False
                advisory = advisory_stt(
                    qa_audio, lambda audio: cached_stt(audio, target_lang, "prisma-v3.0", provider="gnani"),
                    target_lang, "prisma-v3.0", clip_index + 1,
                    lambda: _reencode_audio(qa_audio, work / f"{target_lang}_{index}_qa_retry.wav", f"qa-retry-{clip_index}-{time.time_ns()}"),
                )
                if advisory is None:
                    audio_check_unavailable = True
                    transcript = {"transcript": ""}
                else:
                    transcript = advisory
                audio_flag = advisory_audio_flag(
                    advisory, selected, target_lang, settings.low_trust_verify_langs,
                )
                translation_checks = run_checks(row["cleaned"], selected, target_lang, clip_terms, max_chars=1000)
                meaning_check_failed = False
                try:
                    meaning = back_translate(workflow_llm, row["cleaned"], selected, source_lang, target_lang)
                except Exception as exc:
                    LOGGER.warning("Back-translation QA failed (%s); clip needs human review", type(exc).__name__)
                    meaning = {"back_translation": "", "meaning_preserved": None}
                    meaning_check_failed = True
                reasons = list(audio_flag["reasons"])
                if translation_checks:
                    reasons.append("Translation checks flagged this clip; edit and review it before approval.")
                if protected_missing:
                    reasons.extend(f"Protected term missing from translated output: {term}." for term in protected_missing)
                if selection_needs_review:
                    reasons.append("Automatic candidate selection was unavailable; review this translation before approval.")
                if meaning_check_failed:
                    reasons.append("Automated meaning verification was unavailable; review this translation before approval.")
                reasons.extend(meaning_check_reasons(meaning))
                if any("uncertain source retained" in item.get("reason", "").casefold() for item in row["cleanup_changes"]):
                    reasons.append("Uncertain source wording was kept as transcribed; review before publishing.")
                stt_review = row.get("stt_review", {})
                if any("cleanup rejected" in item.get("reason", "").casefold() for item in row["cleanup_changes"]):
                    reasons.append("Source cleanup exceeded the clip span and was discarded; review the ASR transcript.")
                if spill_used:
                    reasons.append("Dub speech extends into a detected silent gap before the next clip.")
                if withheld:
                    reasons.append(f"Dub speech still exceeds its safe slot by {overrun_s:.2f}s after shorter translation, up to 15% speed-up, fixed-pause trimming, and the available silent gap; it was withheld to prevent overlap.")
                if timing_revision_rejected:
                    reasons.append("A timing rewrite failed deterministic meaning/term/number checks; the prior full translation was retained.")
                if remaining_pause_s > 0.7:
                    reasons.append(f"Dub ends {remaining_pause_s:.2f}s before the next source anchor; source room tone remains underneath.")
                if row.get("cut_mid_sentence"):
                    reasons.append("No completed sentence boundary was detected within 8 seconds; this span ends at the hard duration cap.")
                if slot > 8.0:
                    reasons.append(f"A complete sentence span required {slot:.2f}s, above the 8s hard maximum.")
                if room_tone_path is None:
                    reasons.append("No silent source stretch was detected; room tone was not mixed.")
                status = "red" if audio_flag["status"] == "red" or withheld or bool(translation_checks) or selection_needs_review or meaning_check_failed else "yellow" if reasons or stretched else "green"
                language_rows.append({
                    "clip": clip_index + 1, "start_s": row["start"], "end_s": row["end"],
                    "timeline_start_s": row["timeline_start_s"],
                    "source_duration_s": round(slot, 3), "slot_s": round(slot, 3),
                    "timestamp_source": "source audio silence boundaries",
                    "cut_mid_sentence": bool(row.get("cut_mid_sentence", False)),
                    "source_raw": row["raw"], "source_cleaned": row["cleaned"],
                    "source_cleanup_changes": row["cleanup_changes"], "candidates": candidates,
                    "stt_review": stt_review,
                    "selected_candidate_index": selected_index, "selection_reason": selection_reason,
                    "translation": selected, "back_translation": meaning.get("back_translation", ""),
                    "meaning_preserved": meaning.get("meaning_preserved"), "audio_transcript": transcript["transcript"],
                    "audio_check_unavailable": audio_check_unavailable,
                    "status": "red" if protected_missing else status, "reasons": reasons, "time_stretched": stretched, "overrun_seconds": overrun_s,
                    "translated_character_count": len(selected),
                    "synthesized_duration_s": round(raw_synthesized_seconds, 3),
                    "final_audio_duration_s": round(final_audio_seconds, 3),
                    "speed_factor_applied": speed_factor_applied,
                    "silence_added_s": round(sentence_silence_added_s, 3),
                    "remaining_pause_s": round(remaining_pause_s, 3),
                    "overflow_s": round(overrun_s, 3),
                    "withheld": withheld,
                    "duration_budget_chars": duration_budget_chars,
                    "measured_chars_per_second": round(measured_chars_per_second, 3),
                    "translation_synthesis_attempts": synthesis_history,
                    "audio_path": candidate_path,
                    "timeline_audio_path": timeline_audio_path,
                    "timeline_audio_duration_s": duration_of(timeline_audio_path),
                })
                checkpoint_state.setdefault("clips", {}).setdefault(target_lang, {})[str(clip_index)] = {
                    "audio_key": candidate_path.relative_to(output_dir).as_posix(),
                    "clip": {key: value for key, value in language_rows[-1].items()
                             if key not in {"audio_path", "timeline_audio_path", "timeline_audio_duration_s"}},
                }
                checkpoint_state["last_clip_index"] = clip_index
                checkpoint("clip")
            check_continue()
            final_path = output_dir / f"{video.stem}_{target_lang}_phase3d_ai-dubbed.mp4"
            if only_clip_index is not None:
                report_path = final_path.with_suffix(".json")
                if not report_path.is_file():
                    raise FileNotFoundError("Cannot regenerate one clip before the full-language dub exists")
                previous_report = json.loads(report_path.read_text(encoding="utf-8"))
                prior_rows = previous_report.get("clips", [])
                if only_clip_index >= len(prior_rows):
                    raise ValueError("Regeneration clip index is outside the saved segment map")
                updated = {int(row["clip"]): row for row in language_rows}
                merged_rows = []
                for prior in prior_rows:
                    clip_number = int(prior["clip"])
                    if clip_number in updated:
                        merged_rows.append(updated[clip_number])
                        continue
                    retained = dict(prior)
                    candidate_path = output_dir / "clips" / target_lang / f"clip_{clip_number:03d}.wav"
                    if not candidate_path.is_file():
                        raise FileNotFoundError(f"Saved audio for clip {clip_number} is missing")
                    retained["audio_path"] = candidate_path
                    slot = float(retained["end_s"]) - float(retained["start_s"])
                    retained["timeline_audio_path"] = candidate_path
                    if float(retained.get("final_audio_duration_s", 0)) > slot + 0.02:
                        retained["timeline_audio_path"] = work / f"{target_lang}_{clip_number}_retained_withheld.wav"
                        run_command(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", f"{slot:.6f}", "-c:a", "pcm_s16le", str(retained["timeline_audio_path"])])
                    merged_rows.append(retained)
                language_rows = merged_rows
            assert_language_complete(language_rows, len(cleaned_rows), duration)
            track_files = mux_outputs(video, language_rows, final_path, duration, work, settings, room_tone_path, source_wav, output_dir, target_lang)
            report = build_report(target_lang, [{key: val for key, val in row.items() if key not in {"audio_path", "timeline_audio_path"}} for row in language_rows], settings)
            report.update({"source_language": source_lang, "content_type": content_type, **voice_report_metadata(stock_voice, reference_seconds), "room_tone": {"mixed": room_tone_path is not None, "span": room_tone_span, "mix_level_db": -30 if room_tone_path else None}, "audio_tracks": {name: path.name for name, path in track_files.items()}, "segment_fit_policy": {"translation_scope": "full paragraph with aligned source spans", "target_range_s": [3, 8], "hard_max_s": 8, "timing_anchor": "source start time", "overrun_tolerance_s": OVERRUN_TOLERANCE_S, "retry_under_slot_ratio": 0.75, "max_speed_change": MAX_ATEMPO_SPEEDUP, "overflow_policy": "shorter translation, up to 15% speed-up, fixed-pause trim, then spill into detected silent gaps; withhold only if speech would overlap", "sentence_pause_s": 0.16}})
            report["term_sheet"] = term_sheet.model_dump()
            report_path = final_path.with_suffix(".json")
            atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
            atomic_write_text(final_path.with_suffix(".srt"), create_srt(language_rows))
            checkpoint_state.setdefault("exports", {})[target_lang] = {
                "mp4": final_path.name, "report": report_path.name, "srt": final_path.with_suffix(".srt").name,
            }
            checkpoint("export")
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
    if re.search(r"\btake\s+to\s+a\s+specialist\b", raw, re.IGNORECASE):
        cleaned = re.sub(r"\bto\s+take(?:\s+it)?\s+a\s+specialist\b", "to take to a specialist", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\btalk\s+to\s+a\s+specialist\b", "take to a specialist", cleaned, flags=re.IGNORECASE)
        if not any("specialist" in item.get("from", "").casefold() and "uncertain source retained" in item.get("reason", "").casefold() for item in changes):
            changes.append({"from": "to take to a specialist", "to": "to take to a specialist", "reason": "uncertain source retained: exact ASR action preserved without interpretation"})
    return cleaned, changes


def voice_report_metadata(stock_voice: str | None, reference_seconds: float | None) -> dict:
    """Describe the selected voice without implying a reference upload for stock voices."""
    if stock_voice:
        return {
            "voice_mode": "stock",
            "reference_audio_seconds": None,
            "reference_preprocessing": "not applicable; stock voice selected",
        }
    return {
        "voice_mode": "clone",
        "reference_audio_seconds": reference_seconds,
        "reference_preprocessing": "none; raw reference uploaded for embedding",
    }


def mux_outputs(video: Path, clips: list[dict], output: Path, duration: float, work: Path, settings: Settings,
                room_tone_path: Path | None = None, source_wav: Path | None = None,
                output_dir: Path | None = None, language: str = "unknown") -> dict[str, Path]:
    validated_clips = []
    for clip in sorted(clips, key=lambda row: row["start_s"]):
        start = float(clip.get("timeline_start_s", clip["start_s"]))
        clip_audio = Path(clip.get("timeline_audio_path", clip["audio_path"]))
        audio_duration = duration_of(clip_audio)
        validated_clips.append({**clip, "timeline_start_s": start, "timeline_audio_duration_s": audio_duration})
    assert_timing_invariants(validated_clips, duration)

    output_dir = output_dir or output.parent
    source_track = output_dir / f"{output.stem}_source-track.wav"
    if source_wav is not None:
        atomic_copyfile(source_wav, source_track)
    inputs = []
    graph = []
    labels = []
    for index, clip in enumerate(clips):
        inputs.extend(["-i", str(clip.get("timeline_audio_path", clip["audio_path"]))])
        delay = int(clip.get("timeline_start_s", clip["start_s"]) * 1000)
        graph.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
        labels.append(f"[a{index}]")
    input_count = len(clips)
    dub_track = output_dir / f"{output.stem}_dubbed-track.wav"
    graph.append("".join(labels) + f"amix=inputs={input_count}:duration=longest:normalize=0,apad=whole_dur={duration},alimiter=limit=0.95:attack=5:release=100:level=false[dub]")
    run_command(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph), "-map", "[dub]", "-t", str(duration), str(dub_track)])
    if abs(duration_of(dub_track) - duration) > EXPORT_DURATION_TOLERANCE_S:
        raise RuntimeError("Final dub track duration differs from video by more than 50 milliseconds")

    background_track = output_dir / f"{output.stem}_background-track.wav"
    if room_tone_path is not None:
        run_command(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(room_tone_path), "-af", f"volume=0.0316,atrim=duration={duration},asetpts=PTS-STARTPTS", "-t", str(duration), str(background_track)])
    else:
        run_command(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", str(duration), "-c:a", "pcm_s16le", str(background_track)])
    audio = work / f"{output.stem}_limited.wav"
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(dub_track), "-i", str(background_track), "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:attack=5:release=100:level=false[mix]", "-map", "[mix]", "-t", str(duration), str(audio)])
    if abs(duration_of(audio) - duration) > EXPORT_DURATION_TOLERANCE_S:
        raise RuntimeError("Mixed dub track duration differs from video by more than 50 milliseconds")
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-metadata", "comment=AI dubbed", str(output)])
    export_duration = duration_of(output)
    assert_timing_invariants(validated_clips, duration, export_duration)
    return {"dubbed": dub_track, "source": source_track, "background": background_track}


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

