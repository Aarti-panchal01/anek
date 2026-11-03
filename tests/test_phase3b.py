from backend.pipeline.phase3b import create_srt, merge_incomplete_chunks, silence_chunks, transcript_sentences
from backend.brain.termsheet import TermSheet
from backend.clients.llm import MockLLMClient
from backend.pipeline.phase3b import clean_source_detail


def test_silence_chunks_prefer_detected_pause_boundaries_and_stay_below_hard_limit():
    ranges = silence_chunks(None, 60, [9.8, 19.7, 29.8, 39.6, 49.9])
    assert ranges[0] == (0.0, 9.8)
    assert ranges[-1][1] == 60
    assert all(end - start <= 25 for start, end in ranges)


def test_short_video_uses_its_silence_boundaries_instead_of_one_fixed_cut():
    ranges = silence_chunks(None, 20.625, [4.64, 7.37, 15.25])
    assert ranges == [(0.0, 7.37), (7.37, 15.25), (15.25, 20.625)]


def test_synthesis_sentences_and_srt_preserve_cue_times():
    assert transcript_sentences("First line. Second line! तीसरी पंक्ति।") == [
        "First line.", "Second line!", "तीसरी पंक्ति।"
    ]
    srt = create_srt([{"start_s": 0, "end_s": 2.5, "translation": "வணக்கம்"}])
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "வணக்கம்" in srt


def test_mid_sentence_silence_cut_is_merged_and_retranscribed():
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
    assert calls == [(0.0, 15.2)]
    assert [row["raw"] for row in result] == ["Choose a wig or a headcover?", "I made an app."]


def test_contextual_source_cleanup_corrects_product_and_duplicate_modal():
    client = MockLLMClient([{"cleaned_text": "I built a head and wonder which wig will might look better.", "changes": []}])
    cleaned, changes = clean_source_detail(
        client,
        "I built a head and wonder which wig will might look better.",
        TermSheet(),
        "en-IN",
        "This video explains choosing a wig and talking to a specialist.",
    )
    assert cleaned == "I built an app and wonder which wig might look better."
    assert len(changes) == 2
