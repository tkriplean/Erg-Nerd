"""
Workouts page — pace-vs-date scatter chart + recent-workouts table.
"""

import hyperdiv as hd

from components.workouts_chart_builder import workouts_chart
from components.concept2_sync import get_all_workouts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def workouts_page() -> None:
    """Top-level component for the Workouts page."""
    result = get_all_workouts()

    if not result:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    all_workouts = result[1]
    with hd.box(padding=2, min_height="80vh", gap=2):
        workouts_chart(all_workouts)
