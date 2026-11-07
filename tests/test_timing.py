import pytest

from backend.config import LANGUAGES
from backend.pipeline.timing import schedule_language


def geography_slots(lang):
    return [
        {"id": f"{lang}-1", "lang": lang, "start_s": 0.0, "end_s": 6.0, "speech_duration_s": 5.0},
        {"id": f"{lang}-2", "lang": lang, "start_s": 6.0, "end_s": 11.6, "speech_duration_s": 6.9},
        {"id": f"{lang}-3", "lang": lang, "start_s": 11.6, "end_s": 17.8, "speech_duration_s": 5.2},
    ]


def test_geography_sample_can_pack_all_speech_into_available_video_time():
    clips = [
        {"id": "one", "start_s": 0.0, "end_s": 6.0, "speech_duration_s": 4.5},
        {"id": "two", "start_s": 6.5, "end_s": 12.1, "speech_duration_s": 7.2},
        {"id": "three", "start_s": 12.4, "end_s": 18.6, "speech_duration_s": 6.3},
    ]
    result = schedule_language(clips, 18.6)
    assert result["valid"] is True
    assert [row["id"] for row in result["placements"]] == ["one", "two", "three"]
    assert all(a["end_s"] <= b["start_s"] + 1e-6 for a, b in zip(result["placements"], result["placements"][1:]))


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_pack_geography_case_all_languages(lang):
    clips = [
        {"id": f"{lang}-1", "lang": lang, "start_s": 0.0, "end_s": 6.0, "speech_duration_s": 4.5},
        {"id": f"{lang}-2", "lang": lang, "start_s": 6.5, "end_s": 12.1, "speech_duration_s": 7.2},
        {"id": f"{lang}-3", "lang": lang, "start_s": 12.4, "end_s": 18.6, "speech_duration_s": 6.3},
    ]
    result = schedule_language(clips, 18.6)
    placements = result["placements"]
    assert result["valid"] is True
    assert [item["id"] for item in placements] == [f"{lang}-1", f"{lang}-2", f"{lang}-3"]
    assert placements[0]["shift_s"] == 0.0
    assert placements[1]["shift_s"] == 0.0
    assert 0.5 <= placements[2]["shift_s"] <= 0.6
    assert all(1 <= item["speedup"] <= 1.15 for item in placements)
    assert all(item["start_s"] >= item["source_start_s"] for item in placements)
    assert all(left["end_s"] + 0.1 <= right["start_s"] + 1e-6 for left, right in zip(placements, placements[1:]))


@pytest.mark.parametrize("lang", sorted(LANGUAGES))
def test_elastic_timing_reflows_forward_without_starting_before_a_slot(lang):
    result = schedule_language(geography_slots(lang), 17.8)
    assert result["valid"] is True
    assert [row["shorten_required"] for row in result["placements"]] == [False, False, False]
    assert all(1.0 <= row["speedup"] <= 1.15 for row in result["placements"])
    assert result["placements"][1]["start_s"] >= 6.0
    assert result["placements"][2]["start_s"] >= 11.6
    assert all(row["start_s"] >= row["source_start_s"] for row in result["placements"])
    assert all(a["end_s"] <= b["start_s"] + 1e-6 for a, b in zip(result["placements"], result["placements"][1:]))
    assert schedule_language(geography_slots(lang), 17.8) == result


def test_manual_placement_does_not_move_unless_pack_all_is_requested():
    from backend.pipeline.timing import pack_language
    clips = [
        {"id": "a", "start_s": 0, "end_s": 3, "speech_duration_s": 2.0},
        {"id": "b", "start_s": 3, "end_s": 5, "speech_duration_s": 1.0, "placed_start_s": 4.0, "manual": True},
    ]
    result = pack_language(clips, 6.0)
    assert result["placements"][1]["start_s"] == 4.0
    assert result["placements"][1]["manual"] is True


def test_cumulative_shift_budget_resets_at_a_half_second_pause():
    clips = [
        {"id": "a", "start_s": 1, "end_s": 2, "speech_duration_s": 0.5, "timing_offset_s": -0.8},
        {"id": "b", "start_s": 3, "end_s": 4, "speech_duration_s": 0.5, "timing_offset_s": -0.8},
    ]
    result = schedule_language(clips, 4, max_shift_s=1, max_total_drift_s=1)
    assert result["valid"] is True


def test_only_overlong_clip_is_sped_up_while_following_clip_uses_its_slack():
    clips = [
        {"id": "before", "start_s": 0, "end_s": 4, "speech_duration_s": 3.8},
        {"id": "long", "start_s": 4.2, "end_s": 6, "speech_duration_s": 3.4, "edited": True},
        {"id": "after", "start_s": 6.2, "end_s": 10, "speech_duration_s": 2.8},
    ]
    result = schedule_language(clips, 10)
    assert result["valid"] is True
    before, long, after = result["placements"]
    assert before["speedup"] == 1.0
    assert long["speedup"] > 1.0
    assert after["start_s"] <= after["source_start_s"] + 1.5
    assert after["speedup"] <= 1.15
    assert all(a["end_s"] <= b["start_s"] + 1e-6 for a, b in zip(result["placements"], result["placements"][1:]))


def test_overlong_clip_uses_existing_silent_gap_without_speeding_up():
    clips = [
        {"id": "long", "start_s": 0, "end_s": 2, "speech_duration_s": 2.2},
        {"id": "next", "start_s": 2.5, "end_s": 4, "speech_duration_s": 1.0},
    ]
    result = schedule_language(clips, 4)
    assert result["valid"] is True
    assert result["placements"][0]["speedup"] == 1.0
    assert result["placements"][0]["end_s"] == pytest.approx(2.2)
    assert result["placements"][1]["start_s"] == 2.5


def test_locked_overlapping_speech_is_never_reported_as_a_valid_schedule():
    clips = [
        {"id": "a", "start_s": 0, "end_s": 2, "speech_duration_s": 2.5, "locked": True},
        {"id": "b", "start_s": 2, "end_s": 4, "speech_duration_s": 1.0, "locked": True},
    ]
    result = schedule_language(clips, 4)
    assert result["valid"] is False
    assert result["placements"][1]["start_s"] == 2


def test_pack_random_lists_preserve_order_bounds_and_non_overlap():
    import random
    from backend.pipeline.timing import pack_language

    randomizer = random.Random(412)
    for _ in range(20):
        starts = sorted(randomizer.uniform(0, 18) for _ in range(8))
        clips = [{
            "id": str(index), "start_s": start, "end_s": min(20, start + 1),
            "speech_duration_s": randomizer.uniform(0.2, 2.5),
        } for index, start in enumerate(starts)]
        result = pack_language(clips, 20, max_shift_s=3, max_speedup=1.25)
        placements = result["placements"]
        assert [item["id"] for item in placements] == [str(i) for i in range(len(clips))]
        assert all(item["start_s"] >= item["source_start_s"] - 1e-6 for item in placements)
        assert all(item["start_s"] <= item["source_start_s"] + 3 + 1e-6 for item in placements if not item["shorten_required"])
        assert all(item["end_s"] <= 20 + 1e-6 for item in placements if not item["shorten_required"])
        assert all(left["end_s"] + 0.1 <= right["start_s"] + 1e-6 for left, right in zip(placements, placements[1:]) if not left["shorten_required"] and not right["shorten_required"])
