import io
import struct
import wave

from backend.pipeline.phase3b import create_srt, merge_incomplete_chunks, silence_chunks, targeted_silence_segments, transcript_sentences, voice_report_metadata
from backend.brain.termsheet import TermSheet
from backend.clients.llm import MockLLMClient
from backend.pipeline.phase3b import clean_source_detail, meaning_check_reasons, synthesize_sentence_list


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
    assert metadata["voice_model"] == "Shivani"


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
