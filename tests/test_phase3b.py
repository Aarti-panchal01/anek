import io
import json
import struct
import wave

import pytest

from backend.config import LANGUAGES
from backend.pipeline.phase3b import (
    apply_speech_windows,
    assert_language_complete,
    assert_timing_invariants,
    build_neighbor_context,
    create_srt,
    contains_neighbor_content,
    merge_incomplete_chunks,
    revise_for_timing,
    shorter_timing_budget,
    INITIAL_TARGET_CHARS_PER_SECOND,
    normalize_provider_transcript,
    normalize_cleanup_changes,
    transcribe_generated_speech,
    timing_character_budget,
    translation_style_instruction,
    should_reuse_completed_clip,
    prepare_language_retry_checkpoint,
    source_rows_from_report,
    silence_chunks,
    targeted_silence_segments,
    transcript_sentences,
    trim_fixed_sentence_pauses,
    timing_fit_limit,
    voice_report_metadata,
)
from backend.brain.termsheet import TermSheet
from backend.clients.llm import MockLLMClient
from backend.pipeline.phase3b import clean_source_detail, meaning_check_reasons, synthesize_sentence_list


@pytest.mark.parametrize("target_lang", ["hi-IN", "kn-IN"])
def test_general_translation_style_preserves_english_geography_terms(target_lang):
    source = "So we have studied two things. India is located on the northeastern hemisphere, along with it,"
    instruction = translation_style_instruction("natural", target_lang)
    assert source.startswith("So we have studied two things.")
    assert "code-mixed" in instruction
    assert "northeastern hemisphere" in instruction
    assert "Latin script" in instruction
    pure_instruction = translation_style_instruction("pure", target_lang)
    assert "Translate all ordinary English words and phrases" in pure_instruction


@pytest.mark.parametrize("target_lang", ["hi-IN", "kn-IN"])
@pytest.mark.parametrize("clip_count", [1, 2, 4, 5])
def test_neighbor_context_is_index_safe_for_clip_counts_and_completion_order(target_lang, clip_count):
    rows = [{"cleaned": f"source {i}"} for i in range(clip_count)]
    # Deliberately insert completion out of order and leave a later clip pending.
    translations = {i: f"{target_lang} translation {i}" for i in reversed(range(max(0, clip_count - 1)))}
    for index in range(clip_count):
        context = build_neighbor_context(rows, translations, index)
        expected_indices = [n for n in (index - 1, index + 1) if 0 <= n < clip_count and n in translations]
        assert len(context) == len(expected_indices)
        assert all(f"source {n}" in item and f"translation {n}" in item for n, item in zip(expected_indices, context))


def test_neighbor_context_supports_merged_and_out_of_order_clip_rows():
    # A merged row is still one timing index; chronological index, not finish order, defines adjacency.
    rows = [{"cleaned": "first"}, {"cleaned": "merged second and third"}, {"cleaned": "last short tail"}]
    translations = {2: "last done first", 0: "first done second", 1: "merged done last"}
    assert build_neighbor_context(rows, translations, 1) == [
        "Source: first\nTranslation: first done second",
        "Source: last short tail\nTranslation: last done first",
    ]


def test_targeted_respeak_does_not_reuse_saved_clip_checkpoint_audio():
    assert should_reuse_completed_clip(0, None)
    assert should_reuse_completed_clip(0, 2)
    assert not should_reuse_completed_clip(2, 2)


def test_language_retry_discards_partial_audio_but_preserves_transcript_and_saved_edits():
    checkpoint = {
        "stt_cache": {"source": {"transcript": "saved source"}},
        "term_fixes": {"cleaned_rows": [{"cleaned": "saved source"}]},
        "clips": {
            "hi-IN": {"1": {"audio_key": "partial.wav"}},
            "kn-IN": {"0": {"audio_key": "kannada.wav"}},
        },
        "translations": {"hi-IN": {"0": "old wording"}, "kn-IN": {"0": "ಕನ್ನಡ"}},
    }

    prepare_language_retry_checkpoint(checkpoint, "hi-IN", {0: "AHEAD ಎಂದು ನಾನು ಮಾಡಿದೆ"})

    assert "hi-IN" not in checkpoint["clips"]
    assert checkpoint["clips"]["kn-IN"]["0"]["audio_key"] == "kannada.wav"
    assert checkpoint["translations"]["hi-IN"]["0"] == "AHEAD ಎಂದು ನಾನು ಮಾಡಿದೆ"
    assert checkpoint["term_fixes"]["cleaned_rows"][0]["cleaned"] == "saved source"
    assert checkpoint["stt_cache"]["source"]["transcript"] == "saved source"


def test_single_clip_redub_restores_full_source_index_map():
    report = {
        "source_language": "en-IN",
        "clips": [
            {"clip": 1, "start_s": 0.0, "end_s": 4.0, "timeline_start_s": 0.0,
             "source_raw": "first", "source_cleaned": "first"},
            {"clip": 2, "start_s": 4.0, "end_s": 8.0, "timeline_start_s": 4.1,
             "source_raw": "second", "source_cleaned": "second"},
            {"clip": 3, "start_s": 8.0, "end_s": 12.0, "timeline_start_s": 8.2,
             "source_raw": "third edited", "source_cleaned": "third edited"},
            {"clip": 4, "start_s": 12.0, "end_s": 16.0, "timeline_start_s": 12.0,
             "source_raw": "fourth", "source_cleaned": "fourth"},
        ],
    }
    rows = source_rows_from_report(report, "en-IN")

    assert len(rows) == 4
    assert rows[2]["raw"] == "third edited"
    assert rows[2]["timeline_start_s"] == pytest.approx(8.2)
    assert rows[0]["start"] == pytest.approx(0.0)
    assert rows[0]["lang"] == "en-IN"


def test_language_completion_invariant_preserves_short_final_clip_and_rejects_missing_translation():
    clips = [
        {"clip": 1, "start_s": 0.0, "end_s": 4.0, "timeline_start_s": 0.0,
         "timeline_audio_duration_s": 3.8, "translation": "पहला वाक्य"},
        {"clip": 2, "start_s": 4.0, "end_s": 9.7, "timeline_start_s": 4.0,
         "timeline_audio_duration_s": 5.2, "translation": "ಅಂತಿಮ ಚಿಕ್ಕ ವಾಕ್ಯ"},
    ]
    assert_language_complete(clips, source_clip_count=2, video_duration=10.0)
    with pytest.raises(ValueError, match="clip count"):
        assert_language_complete(clips[:-1], source_clip_count=2, video_duration=10.0)
    with pytest.raises(ValueError, match="saved translation"):
        assert_language_complete([{**clips[0], "translation": ""}, clips[1]], 2, 10.0)


def test_silence_chunks_prefer_detected_pause_boundaries_and_stay_below_hard_limit():
    ranges = silence_chunks(None, 60, [9.8, 19.7, 29.8, 39.6, 49.9])
    assert ranges[0] == (0.0, 9.8)
    assert ranges[-1][1] == 60
    assert all(end - start <= 25 for start, end in ranges)


def test_short_video_uses_its_silence_boundaries_instead_of_one_fixed_cut():
    ranges = silence_chunks(None, 20.625, [4.64, 7.37, 15.25])
    assert ranges == [(0.0, 7.37), (7.37, 15.25), (15.25, 20.625)]


def test_targeted_segments_prefer_three_to_eight_seconds_and_never_exceed_twelve():
    ranges = targeted_silence_segments(20.625, [0.16, 2.09, 4.65, 7.32, 10.89, 15.25, 20.38])
    assert all(end - start <= 12 for start, end in ranges)
    assert all(end - start >= 3 for start, end in ranges)
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 20.625


def test_targeted_segments_without_silences_stay_inside_video_and_hard_cap():
    ranges = targeted_silence_segments(20.625, [])
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 20.625
    assert all(3 <= end - start <= 12 for start, end in ranges)


def test_incomplete_transcript_merge_respects_hard_duration_limit():
    chunks = [
        {"start": 0.0, "end": 7.0, "raw": "An unfinished phrase", "lang": "en-IN"},
        {"start": 7.0, "end": 13.0, "raw": "continues here.", "lang": "en-IN"},
    ]
    merged = merge_incomplete_chunks(chunks, lambda start, end: {"transcript": "combined."}, max_seconds=12)
    assert merged[0]["cut_mid_sentence"] is True
    assert len(merged) == 2


def test_synthesis_sentences_and_srt_preserve_cue_times():
    assert transcript_sentences("First line. Second line! तीसरी पंक्ति।") == [
        "First line.", "Second line!", "तीसरी पंक्ति।"
    ]
    srt = create_srt([{"start_s": 0, "end_s": 2.5, "translation": "வணக்கம்"}])
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "வணக்கம்" in srt


@pytest.mark.parametrize("target_lang", LANGUAGES)
def test_all_target_languages_use_gnani_for_generated_speech_stt(target_lang):
    class GnaniSpy:
        def __init__(self):
            self.call = None

        def stt(self, audio, language_code, model):
            self.call = (audio, language_code, model)
            return {"success": True, "data": {"text": f"spoken {target_lang}"}}

    gnani = GnaniSpy()
    result = transcribe_generated_speech(gnani, b"generated wav", target_lang)

    assert gnani.call == (b"generated wav", target_lang, "prisma-v3.0")
    assert result["transcript"] == f"spoken {target_lang}"
    assert result["detected_language_code"] == target_lang
    # The first translation budget is identical for all languages, then timing
    # recovery learns the actual TTS rate from generated audio.
    assert INITIAL_TARGET_CHARS_PER_SECOND == 12.0
    assert timing_character_budget(3.9) == 47


def test_gnani_transcript_response_shapes_and_multiscript_sentence_boundaries():
    assert normalize_provider_transcript({"text": "  नमस्ते  "}) == "नमस्ते"
    assert normalize_provider_transcript({"data": {"transcript": "ನಮಸ್ಕಾರ"}}) == "ನಮಸ್ಕಾರ"
    with pytest.raises(ValueError, match="no transcript"):
        normalize_provider_transcript({"success": True, "data": {}})
    assert transcript_sentences("पहला वाक्य। दूसरा वाक्य! ಒಳ್ಳೆಯದು. ಕೊನೆಯದು\nअगली पंक्ति") == [
        "पहला वाक्य।", "दूसरा वाक्य!", "ಒಳ್ಳೆಯದು.", "ಕೊನೆಯದು", "अगली पंक्ति",
    ]
    assert transcript_sentences("First. 第二句。第三句！") == ["First.", "第二句。", "第三句！"]


def test_legacy_checkpoint_cleanup_notes_are_safe_for_language_retry():
    notes = normalize_cleanup_changes([
        "No cleanup changes were needed.",
        {"from": "a head", "to": "Anek", "reason": "mishearing corrected"},
    ])
    assert notes == [
        {"from": "", "to": "", "reason": "No cleanup changes were needed."},
        {"from": "a head", "to": "Anek", "reason": "mishearing corrected"},
    ]
    uncertain = [item.get("to", item["from"]) for item in notes
                 if "uncertain source retained" in item.get("reason", "").casefold()]
    assert uncertain == []


def test_sentence_join_rewrites_gnani_streaming_frame_count(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48_000)
        audio.writeframes(b"\x00\x00" * 1600)
    streaming_wav = bytearray(buffer.getvalue())
    streaming_wav[40:44] = struct.pack("<I", 0x7FFFFFFF)

    class PreviewClient:
        def synth_stock(self, text, voice, language):
            return bytes(streaming_wav)

    monkeypatch.setattr("backend.pipeline.phase3b.duration_of", lambda path: 1600 / 48_000)
    output, duration = synthesize_sentence_list(
        PreviewClient(), ["नमस्ते।"], None, tmp_path, "sentinel", "hi-IN", "Shivani"
    )

    with wave.open(str(output), "rb") as audio:
        assert audio.getnframes() == 1600
    assert duration == 1600 / 48_000


def test_stock_voice_report_does_not_claim_a_reference_upload():
    metadata = voice_report_metadata("Shivani", None)
    assert metadata["voice_mode"] == "stock"
    assert metadata["reference_audio_seconds"] is None
    assert "not applicable" in metadata["reference_preprocessing"]
    assert "voice_model" not in metadata


def test_mid_sentence_merge_does_not_cross_the_twelve_second_hard_cap():
    chunks = [
        {"start": 0.0, "end": 10.8, "raw": "Choose a wig or", "lang": "en-IN"},
        {"start": 10.8, "end": 15.2, "raw": "a headcover?", "lang": "en-IN"},
        {"start": 15.2, "end": 20.6, "raw": "I made an app.", "lang": "en-IN"},
    ]
    calls = []

    def transcribe(start, end):
        calls.append((start, end))
        return {"transcript": "Choose a wig or a headcover?", "detected_language_code": "en-IN"}

    result = merge_incomplete_chunks(chunks, transcribe)
    assert calls == []
    assert len(result) == 3
    assert result[0]["cut_mid_sentence"] is True
    assert all(row["end"] - row["start"] <= 12 for row in result)


def test_short_completed_clip_merges_into_following_span_with_consistent_timing():
    chunks = [
        {"start": 0.0, "end": 3.9, "raw": "This is one short sentence.", "lang": "en-IN"},
        {"start": 3.9, "end": 7.4, "raw": "Here is the next sentence.", "lang": "en-IN"},
    ]
    merged = merge_incomplete_chunks(
        chunks, lambda start, end: {"transcript": "This is one short sentence. Here is the next sentence."},
    )
    assert len(merged) == 1
    assert (merged[0]["start"], merged[0]["end"]) == (0.0, 7.4)


def test_uncertain_noun_is_kept_while_clear_grammar_cleanup_is_applied():
    client = MockLLMClient([{"cleaned_text": "That's why I built an app and wonder which wig might look better.", "changes": []}])
    cleaned, changes = clean_source_detail(
        client,
        "Thats why i built a head and wonder which wig might look better.",
        TermSheet(),
        "en-IN",
        "This video explains choosing a wig and talking to a specialist.",
    )
    assert cleaned == "That's why I built a head and wonder which wig might look better."
    assert len(changes) == 1
    assert any(item["to"] == "built a head" and "uncertain source retained" in item["reason"] for item in changes)


def test_uncertain_take_phrase_keeps_exact_asr_action():
    raw = "To make that choice feel more personal and easier, to take to a specialist."
    client = MockLLMClient([{"cleaned_text": "To make that choice feel more personal and easier, to take a specialist.", "changes": []}])
    cleaned, changes = clean_source_detail(client, raw, TermSheet(), "en-IN", raw)
    assert cleaned == raw
    assert any(item["to"] == "to take to a specialist" and "uncertain source retained" in item["reason"] for item in changes)


def test_meaning_check_flags_statement_to_instruction_and_agency_changes():
    reasons = meaning_check_reasons({
        "meaning_preserved": False,
        "speech_act_preserved": False,
        "agency_preserved": False,
        "issues": ["The statement became a command."],
    })
    assert "The statement became a command." in reasons
    assert any("Speech act changed" in item for item in reasons)
    assert any("Who performs the action" in item for item in reasons)


@pytest.mark.parametrize("target_lang", LANGUAGES)
def test_over_slot_retry_budget_and_pause_trim_are_language_agnostic(target_lang, tmp_path):
    current = "A longer translated sentence."
    reduced_budget = shorter_timing_budget(30, current)
    assert reduced_budget < 30

    class Translator:
        def chat_json(self, messages):
            self.payload = json.loads(messages[1]["content"])
            return {"translation": "A concise translation."}

    translator = Translator()
    revised = revise_for_timing(
        translator, "Full paragraph.", "Source sentence.", current,
        target_lang, 2.0, 12.0, "shorter", target_character_budget=reduced_budget,
    )
    assert revised == "A concise translation."
    assert translator.payload["target_character_budget"] == reduced_budget
    assert translator.payload["target_language"] == target_lang
    assert translator.payload["source_segment"] == "Source sentence."
    assert "full_source_paragraph" not in translator.payload

    class OverBudgetTranslator:
        def chat_json(self, messages):
            return {"translation": "x" * (reduced_budget + 1)}

    with pytest.raises(ValueError, match="strict"):
        revise_for_timing(
            OverBudgetTranslator(), "", "Source sentence.", current,
            target_lang, 2.0, 12.0, "shorter", target_character_budget=reduced_budget,
            neighbor_context=["Source: next sentence. Translation: अगला वाक्य।"],
        )
    assert contains_neighbor_content(
        "यह चुनाव अधिक व्यक्तिगत और आसान लगे", ["यह चुनाव अधिक व्यक्तिगत और आसान लगे"],
    )
    assert not contains_neighbor_content("यह एक नया संक्षिप्त वाक्य है", ["अगला विषय यहां शुरू होता है"])

    for index in range(2):
        with wave.open(str(tmp_path / f"{target_lang}_sentence_{index:02d}.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(100)
            audio.writeframes(b"\x01\x00" * 20)
    joined = trim_fixed_sentence_pauses(
        tmp_path, target_lang, 2, tmp_path / f"{target_lang}_trimmed.wav"
    )
    with wave.open(str(joined), "rb") as audio:
        assert audio.getnframes() == 40


@pytest.mark.parametrize("target_lang", LANGUAGES)
def test_timing_invariants_and_silent_gap_spill_for_every_language(target_lang):
    rows = [
        {"start": 0.0, "end": 5.0},
        {"start": 5.0, "end": 10.0},
    ]
    windows = apply_speech_windows(rows, [(4.8, 5.2)])
    assert windows[0]["end"] == pytest.approx(4.8)
    assert windows[0]["spill_end_s"] == pytest.approx(5.2)
    assert windows[1]["timeline_start_s"] == pytest.approx(5.2)
    assert timing_fit_limit(4.0, 0.05) == pytest.approx(4.05)
    assert timing_fit_limit(4.0, 0.30) == pytest.approx(4.10)

    clips = [
        {"start_s": windows[0]["start"], "end_s": windows[0]["end"],
         "timeline_start_s": 0.0, "timeline_audio_duration_s": 5.1},
        {"start_s": windows[1]["start"], "end_s": windows[1]["end"],
         "timeline_start_s": windows[1]["timeline_start_s"], "timeline_audio_duration_s": 4.8},
    ]
    assert_timing_invariants(clips, video_duration=10.0, export_duration=10.04)

    with pytest.raises(ValueError, match="overlaps"):
        assert_timing_invariants([clips[0], {**clips[1], "start_s": 4.9, "timeline_start_s": 5.0}], 10.0)
    with pytest.raises(ValueError, match="before its source slot"):
        assert_timing_invariants([clips[0], {**clips[1], "timeline_start_s": 5.1}], 10.0)
    with pytest.raises(ValueError, match="50 milliseconds"):
        assert_timing_invariants(clips, video_duration=10.0, export_duration=10.06)
