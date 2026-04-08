"""
Sessions tab — pace-vs-date scatter chart + recent-workouts table.
"""

import hyperdiv as hd

from components.sessions_chart_builder import sessions_chart
from components.workout_sync import workout_sync


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def sessions_tab(client, user_id: str) -> None:
    """Top-level component for the Sessions tab."""

    result = workout_sync(client)
    if result is None:
        return
    _workouts_dict, all_workouts = result

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    with hd.box(padding=(2, 2, 2, 2)):
        sessions_chart(all_workouts)
