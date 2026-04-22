"""
Rankings-browser modal for the Rank Page.

Renders a scrollable dialog listing the ranking pool for one event, with the
user's row highlighted and auto-scrolled into view on open. Caps the visible
list at the top 500 plus a ±25-row window around the user's position to keep
very long lists responsive.
"""

from __future__ import annotations

from typing import Optional

import hyperdiv as hd

from services.formatters import fmt_split, format_time, fmt_distance


def _pace_tenths_for(event_kind: str, event_value: int, value_tenths: int) -> Optional[int]:
    """Return pace in tenths-of-a-second per 500m for a ranking entry."""
    if value_tenths is None or value_tenths <= 0:
        return None
    if event_kind == "dist":
        # value_tenths = tenths of a second for event_value meters
        return int(round(value_tenths * 500 / event_value))
    # time event — event_value = tenths of a second, value_tenths = meters
    if value_tenths <= 0:
        return None
    return int(round(event_value * 500 / value_tenths))


def _result_str(event_kind: str, event_value: int, value_tenths: int) -> str:
    if value_tenths is None:
        return "—"
    if event_kind == "dist":
        return format_time(value_tenths)
    return fmt_distance(value_tenths)


def render_rankings_modal(
    dialog: hd.dialog,
    *,
    pool: list[dict],
    event_kind: str,
    event_value: int,
    user_rank: int,
    user_value_tenths: int,
    user_row_label: str,
    user_age: int = 0,
    user_date_label: str = "",
) -> None:
    """Populate ``dialog`` with a rankings table. Call while dialog is open."""
    total = len(pool)

    # Sort pool best → worst for display.
    if event_kind == "dist":
        ordered = sorted(pool, key=lambda e: e.get("value_tenths") or 10**12)
    else:
        ordered = sorted(pool, key=lambda e: -(e.get("value_tenths") or 0))

    # Determine rows to show: top 500 + ±25 around user's position.
    top_cap = 500
    ctx_radius = 25
    show_top = min(total, top_cap)
    window_lo = max(0, user_rank - 1 - ctx_radius)
    window_hi = min(total, user_rank + ctx_radius)

    if window_lo < show_top:
        # Overlap — single block.
        segments = [(0, max(show_top, window_hi))]
    else:
        segments = [(0, show_top), (window_lo, window_hi)]

    with dialog:
        with hd.box(gap=0.5, width="min(960px, 92vw)"):
            hd.text(
                f"{total:,} ranked performances",
                font_color="neutral-500",
                font_size="small",
            )

            with hd.box(
                direction="horizontal",
                gap=0,
                padding=0.3,
                background_color="neutral-50",
                font_weight="bold",
                font_size="small",
            ):
                hd.text("Rank", width="4rem")
                hd.text("Name", width="14rem")
                hd.text("Age", width="3rem")
                hd.text("Country", width="5rem")
                hd.text("Pace", width="5rem")
                hd.text("Result", width="6rem")
                hd.text("Verified", width="5rem")

            with hd.box(
                max_height="60vh",
                overflow="auto",
                gap=0,
            ) as scroll_box:
                prev_hi = 0
                for (seg_lo, seg_hi) in segments:
                    if seg_lo > prev_hi:
                        with hd.box(
                            direction="horizontal",
                            padding=0.3,
                            background_color="neutral-50",
                            font_color="neutral-500",
                            font_size="small",
                        ):
                            hd.text(f"… ({seg_lo - prev_hi:,} rows hidden) …")
                    for i in range(seg_lo, seg_hi):
                        entry = ordered[i]
                        rank_1b = i + 1
                        is_user = rank_1b == user_rank and (
                            entry.get("value_tenths") == user_value_tenths
                        )
                        bg = "primary-100" if is_user else None
                        v = entry.get("value_tenths") or 0
                        pt = _pace_tenths_for(event_kind, event_value, v)
                        result = _result_str(event_kind, event_value, v)
                        with hd.scope(f"row_{i}"):
                            with hd.box(
                                direction="horizontal",
                                padding=0.3,
                                background_color=bg,
                                font_size="small",
                                border_bottom="1px solid neutral-100",
                            ):
                                hd.text(f"{rank_1b:,}", width="4rem")
                                hd.text(entry.get("name", ""), width="14rem")
                                hd.text(str(entry.get("age", "")), width="3rem")
                                hd.text(entry.get("country", ""), width="5rem")
                                hd.text(fmt_split(pt) if pt else "—", width="5rem")
                                hd.text(result, width="6rem")
                                hd.text(entry.get("verified", ""), width="5rem")

                    prev_hi = seg_hi

            if user_rank > 0:
                hd.text(
                    f"{user_row_label} — rank {user_rank:,} of {total:,}",
                    font_color="neutral-600",
                    font_size="small",
                )
