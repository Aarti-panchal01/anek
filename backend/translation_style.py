"""Shared policy for which clips may be retranslated when project style changes."""


def style_switch_eligible(row) -> bool:
    """Preserve text edited by a person, explicitly locked, or already verified."""
    return not bool(row["human_edited"]) and not bool(row["timing_locked"]) and not bool(row["user_verified"])


def eligible_style_switch_rows(rows):
    """Return only clips that a style update is permitted to replace."""
    return [row for row in rows if style_switch_eligible(row)]
