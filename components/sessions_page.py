"""
Sessions tab — pace-vs-date scatter chart + recent-workouts table.
"""

import hyperdiv as hd

from components.sessions_chart_builder import sessions_chart
from components.concept2_sync import concept2_sync
from services.rowing_utils import get_season


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def sessions_page(client, user_id: str, excluded_seasons=(), machine="All") -> None:
    """Top-level component for the Sessions tab."""

    result = concept2_sync(client)
    if result is None:
        return
    _workouts_dict, all_workouts = result

    # Apply global filters
    if excluded_seasons:
        all_workouts = [
            w for w in all_workouts
            if get_season(w.get("date", "")) not in set(excluded_seasons)
        ]
    if machine != "All":
        all_workouts = [w for w in all_workouts if w.get("type") == machine]

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    with hd.box(padding=(2, 2, 2, 2), gap=2):
        sessions_chart(all_workouts)
