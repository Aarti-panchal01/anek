"""Deterministic language-agnostic placement for already generated speech takes."""

from __future__ import annotations

from copy import deepcopy


def schedule_language(
    clips: list[dict],
    video_duration_s: float,
    *,
    max_shift_s: float = 1.0,
    max_total_drift_s: float = 1.0,
    max_speedup: float = 1.15,
    tolerance_s: float = 0.1,
) -> dict:
    """Place speech without overlap, borrowing silence/slack before changing its rate.

    Input rows use start_s, end_s, speech_duration_s, lang, and optional
    timing_offset_s, edited, and locked fields. The function does no I/O and
    never mutates its inputs, so API and batch paths share identical behavior.
    """
    rows = sorted((deepcopy(row) for row in clips), key=lambda row: (float(row["start_s"]), str(row.get("id", ""))))
    for row in rows:
        row["start_s"] = float(row["start_s"])
        row["end_s"] = float(row["end_s"])
        row["speech_duration_s"] = max(0.0, float(row.get("speech_duration_s") or 0.0))
        row["placed_start_s"] = row["start_s"] + float(row.get("timing_offset_s") or 0.0)
        row["speedup"] = 1.0
        row["shorten_required"] = False
        row["fit_reason"] = None

    # Keep starts at or after their source slots. Use the actual silent gap,
    # then move following clips later within their own slack. Speed up only
    # the speech that still cannot fit after that local reflow.
    for index, row in enumerate(rows):
        previous_end = (rows[index - 1]["placed_start_s"] + rows[index - 1]["speech_duration_s"] / rows[index - 1]["speedup"]) if index else 0.0
        row["placed_start_s"] = max(row["start_s"], row["placed_start_s"])
        if not row.get("locked"):
            row["placed_start_s"] = max(row["placed_start_s"], previous_end)
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        available_end = float(next_row["placed_start_s"]) if next_row else float(video_duration_s)
        overrun = row["placed_start_s"] + row["speech_duration_s"] - available_end
        if overrun <= 1e-6:
            continue
        # Move the following take into its own trailing slack. A text edit
        # locks the wording, not its timeline position.
        if overrun > tolerance_s and next_row and not next_row.get("locked"):
            room_to_move = max(0.0, next_row["start_s"] + max_shift_s - next_row["placed_start_s"])
            later = min(overrun, room_to_move)
            next_row["placed_start_s"] += later
            available_end = next_row["placed_start_s"]
            overrun = row["placed_start_s"] + row["speech_duration_s"] - available_end
        if overrun > 1e-6:
            required = row["speech_duration_s"] / max(0.01, available_end - row["placed_start_s"])
            if required <= max_speedup + 1e-9:
                row["speedup"] = max(1.0, required)
                overrun = 0.0
        if overrun > 1e-6:
            row["shorten_required"] = True
            row["fit_reason"] = "Speech needs a shorter translation to fit without overlap."

    placements = []
    previous_end = 0.0
    total_drift = 0.0
    valid = True
    for row in rows:
        if placements and row["start_s"] - placements[-1]["source_end_s"] >= 0.5:
            total_drift = 0.0
        start = float(row["placed_start_s"])
        natural_duration = row["speech_duration_s"]
        duration = natural_duration / float(row["speedup"])
        end = start + duration
        shift = start - row["start_s"]
        total_drift += shift
        if shift < -1e-6 or abs(shift) > max_shift_s + 1e-6 or abs(total_drift) > max_total_drift_s + 1e-6:
            valid = False
        if start < previous_end - 1e-6 or end > float(video_duration_s) + 1e-6:
            valid = False
        if row["shorten_required"]:
            valid = False
        placements.append({
            "id": row.get("id"), "source_start_s": row["start_s"], "source_end_s": row["end_s"],
            "start_s": round(start, 6), "end_s": round(end, 6), "speech_duration_s": natural_duration,
            "speedup": round(float(row["speedup"]), 6), "shift_s": round(shift, 6),
            "shorten_required": bool(row["shorten_required"]), "fit_reason": row["fit_reason"],
            "locked": bool(row.get("locked")),
        })
        previous_end = end
    return {"placements": placements, "valid": valid, "video_duration_s": float(video_duration_s),
            "timing_health": {"speech_needed_s": round(sum(item["speech_duration_s"] for item in placements), 3),
                              "time_available_s": round(float(video_duration_s), 3),
                              "shorter_translations_needed": sum(item["shorten_required"] for item in placements)}}
