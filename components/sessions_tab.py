"""
Sessions tab — pace-vs-date scatter chart + recent-workouts table.
"""

import hyperdiv as hd

from services.concept2 import get_client, load_local_workouts
from components.sessions_chart_builder import sessions_chart


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------

def sessions_tab() -> None:
    """Top-level component for the Sessions tab."""

    task = hd.task()

    def _fetch():
        client = get_client()
        if client is None:
            local = load_local_workouts()
            workouts = list(local.values())
            workouts.sort(key=lambda r: r.get("date", ""), reverse=True)
            return workouts
        return client.get_all_results()

    task.run(_fetch)

    if task.running:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return

    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return

    all_workouts = task.result or []

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    with hd.box(padding=(2, 2, 2, 2)):
        hd.h3("Sessions")
        sessions_chart(all_workouts)
