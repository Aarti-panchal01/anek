from backend.pipeline.phase3b import create_srt, silence_chunks, transcript_sentences


def test_silence_chunks_prefer_detected_pause_boundaries_and_stay_below_hard_limit():
    ranges = silence_chunks(None, 60, [9.8, 19.7, 29.8, 39.6, 49.9])
    assert ranges[0] == (0.0, 9.8)
    assert ranges[-1][1] == 60
    assert all(end - start <= 25 for start, end in ranges)


def test_synthesis_sentences_and_srt_preserve_cue_times():
    assert transcript_sentences("First line. Second line! तीसरी पंक्ति।") == [
        "First line.", "Second line!", "तीसरी पंक्ति।"
    ]
    srt = create_srt([{"start_s": 0, "end_s": 2.5, "translation": "வணக்கம்"}])
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "வணக்கம்" in srt
