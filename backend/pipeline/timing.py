"""Deterministic placement and packing for dubbed speech takes."""

from __future__ import annotations

from copy import deepcopy


def pack_language(
    clips: list[dict],
    video_duration_s: float,
    *,
    max_shift_s: float = 1.5,
    max_speedup: float = 1.15,
    min_gap_s: float = 0.1,
    pack_all: bool = False,
) -> dict:
    """Find a low-cost ordered layout using a deterministic 100 ms grid.

    Speech is never placed before its source slot. Starts may move later by at
    most ``max_shift_s`` and speech may be accelerated up to ``max_speedup``.
    Manual placements are fixed unless ``pack_all`` is requested.
    """
    rows = sorted((deepcopy(row) for row in clips), key=lambda row: (float(row["start_s"]), str(row.get("id", ""))))
    if max_shift_s < 0 or max_speedup < 1 or min_gap_s < 0:
        raise ValueError("Invalid timing limits")
    if video_duration_s < 0:
        raise ValueError("Video duration must be non-negative")

    # Dynamic programming keeps the globally cheapest feasible prefix for
    # each candidate start/speed pair, avoiding the old local greedy failure.
    states: list[tuple[float, list[dict]]] = [(0.0, [])]
    for row in rows:
        source_start = float(row["start_s"])
        source_end = float(row["end_s"])
        speech = max(0.0, float(row.get("speech_duration_s") or 0.0))
        manual = bool((row.get("manual") or row.get("locked")) and not pack_all)
        trim_head = max(0.0, float(row.get("trim_head_s") or 0.0))
        trim_tail = max(0.0, float(row.get("trim_tail_s") or 0.0))
        effective_speech = max(0.0, speech - trim_head - trim_tail)
        if manual:
            starts = [round(float(row.get("placed_start_s", source_start)), 1)]
        else:
            last = min(video_duration_s, source_start + max_shift_s)
            starts = [round(source_start + i * 0.1, 1) for i in range(int((last - source_start) / 0.1 + 1e-7) + 1)]
            starts = [start for start in starts if start <= source_start + max_shift_s + 1e-7]
        speeds = [round(1.0 + i * 0.05, 3) for i in range(int((max_speedup - 1.0) / 0.05 + 1e-7) + 1)]
        if speeds[-1] < max_speedup - 1e-7:
            speeds.append(round(max_speedup, 4))
        next_states: list[tuple[float, list[dict]]] = []
        for prior_cost, prior in states:
            prior_end = prior[-1]["end_s"] if prior else 0.0
            for start in starts:
                if prior and start + 1e-7 < prior_end + min_gap_s:
                    continue
                if start + 1e-7 < source_start:
                    continue
                for speed in speeds:
                    end = start + effective_speech / speed
                    if end > video_duration_s + 1e-7:
                        continue
                    shift = start - source_start
                    # Shifting is preferable to compressing speech, but large
                    # shifts still cost more than small, natural speed changes.
                    cost = prior_cost + shift + (speed - 1.0) * 10.0
                    placement = {
                        "id": row.get("id"), "source_start_s": source_start, "source_end_s": source_end,
                        "start_s": round(start, 6), "end_s": round(end, 6),
                        "speech_duration_s": speech, "speedup": round(speed, 6),
                        "shift_s": round(shift, 6), "shorten_required": False,
                        "fit_reason": None, "locked": bool(row.get("locked")),
                        "manual": manual,
                        "trim_head_s": trim_head,
                        "trim_tail_s": trim_tail,
                        "pause_tightening": bool(row.get("pause_tightening")),
                    }
                    next_states.append((cost, [*prior, placement]))
        if not next_states:
            # Preserve ordered source placement and report the first impossible
            # take clearly instead of returning a false valid result.
            fallback = [*states[0][1]]
            desired_start = max(source_start, fallback[-1]["end_s"] + min_gap_s if fallback else 0.0)
            start = (round(float(row.get("placed_start_s", source_start)), 1) if manual else
                     min(desired_start, source_start + max_shift_s))
            end = start + effective_speech
            fallback.append({
                "id": row.get("id"), "source_start_s": source_start, "source_end_s": source_end,
                "start_s": round(start, 6), "end_s": round(end, 6), "speech_duration_s": speech,
                "speedup": 1.0, "shift_s": round(start - source_start, 6),
                "shorten_required": True,
                "fit_reason": f"Cannot fit within timing limits; exceeds the video by {max(0.0, end-video_duration_s):.1f}s.",
                "locked": bool(row.get("locked")), "manual": manual,
                "trim_head_s": trim_head,
                "trim_tail_s": trim_tail,
                "pause_tightening": bool(row.get("pause_tightening")),
            })
            states = [(float("inf"), fallback)]
            continue
        # For any end point, retain only its cheapest predecessor. Rounding to
        # milliseconds makes equivalent paths stable across platforms.
        best: dict[tuple[float, float], tuple[float, list[dict]]] = {}
        for state in next_states:
            last = state[1][-1]
            key = (last["start_s"], last["end_s"])
            if key not in best or state[0] < best[key][0]:
                best[key] = state
        states = sorted(best.values(), key=lambda item: item[0])[:64]

    if not rows:
        selected: list[dict] = []
    else:
        selected = min(states, key=lambda item: item[0])[1]
    valid = all(not item["shorten_required"] for item in selected)
    return {
        "placements": selected, "valid": valid, "video_duration_s": float(video_duration_s),
        "timing_health": {
            "speech_needed_s": round(sum(item["speech_duration_s"] for item in selected), 3),
            "time_available_s": round(float(video_duration_s), 3),
            "shorter_translations_needed": sum(item["shorten_required"] for item in selected),
        },
    }


def schedule_language(
    clips: list[dict], video_duration_s: float, *, max_shift_s: float = 1.5,
    max_total_drift_s: float | None = None, max_speedup: float = 1.15,
    tolerance_s: float = 0.1, pack_all: bool = False,
) -> dict:
    """Compatibility entry point used by processing and API callers."""
    del max_total_drift_s, tolerance_s
    return pack_language(clips, video_duration_s, max_shift_s=max_shift_s, max_speedup=max_speedup, pack_all=pack_all)
