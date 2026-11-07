import pytest

from backend.config import LANGUAGES
from backend.pipeline.timing import schedule_language


def geography_slots(lang):
    return [
        {"id": f"{lang}-1", "lang": lang, "start_s": 0.0, "end_s": 6.0, "speech_duration_s": 5.0},
        {"id": f"{lang}-2", "lang": lang, "start_s": 6.0, "end_s": 11.6, "speech_duration_s": 6.9},
        {"id": f"{lang}-3", "lang": lang, "start_s": 11.6, "end_s": 17.8, "speech_duration_s": 5.2},
    ]


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_elastic_timing_borrows_spare_time_for_all_targets(lang):
    result = schedule_language(geography_slots(lang), 17.8)
    assert result["valid"] is True
    assert [row["shorten_required"] for row in result["placements"]] == [False, False, False]
    assert [row["speedup"] for row in result["placements"]] == [1.0, 1.0, 1.0]
    assert result["placements"][1]["start_s"] == pytest.approx(5.0)
    assert result["placements"][2]["start_s"] == pytest.approx(11.9)
    assert all(a["end_s"] <= b["start_s"] + 1e-6 for a, b in zip(result["placements"], result["placements"][1:]))
    assert schedule_language(geography_slots(lang), 17.8) == result


def test_scheduler_uses_speed_then_requests_shorter_translation_and_resets_drift_at_pauses():
    clips = [
        {"id": "a", "start_s": 0, "end_s": 4, "speech_duration_s": 4.0},
        {"id": "b", "start_s": 4, "end_s": 6, "speech_duration_s": 2.2},
        {"id": "c", "start_s": 6, "end_s": 8, "speech_duration_s": 2.5, "locked": True},
        {"id": "d", "start_s": 8, "end_s": 9, "speech_duration_s": 2.0},
    ]
    result = schedule_language(clips, 10, max_shift_s=1, max_total_drift_s=1, max_speedup=1.15)
    placements = result["placements"]
    assert placements[1]["speedup"] > 1
    assert placements[1]["shorten_required"] is False
    assert placements[3]["shorten_required"] is True


def test_cumulative_shift_budget_resets_at_a_half_second_pause():
    clips = [
        {"id": "a", "start_s": 1, "end_s": 2, "speech_duration_s": 0.5, "timing_offset_s": -0.8},
        {"id": "b", "start_s": 3, "end_s": 4, "speech_duration_s": 0.5, "timing_offset_s": -0.8},
    ]
    result = schedule_language(clips, 4, max_shift_s=1, max_total_drift_s=1)
    assert result["valid"] is True


def test_locked_overlapping_speech_is_never_reported_as_a_valid_schedule():
    clips = [
        {"id": "a", "start_s": 0, "end_s": 2, "speech_duration_s": 2.5, "locked": True},
        {"id": "b", "start_s": 2, "end_s": 4, "speech_duration_s": 1.0, "locked": True},
    ]
    result = schedule_language(clips, 4)
    assert result["valid"] is False
    assert result["placements"][1]["start_s"] == 2
