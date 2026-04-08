"""
Sessions tab — pace-vs-date scatter chart + recent-workouts table.
"""

import hyperdiv as hd

from services.rowing_utils import compress_workouts, decompress_workouts
from components.sessions_chart_builder import sessions_chart


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def sessions_tab(client, user_id: str) -> None:
    """Top-level component for the Sessions tab."""

    sync_state = hd.state(written=False, initial_workouts=None, initial_loaded=False)

    # Step 1: one-time load of workouts from localStorage
    if not sync_state.initial_loaded:
        ls_wkts = hd.local_storage.get_item("workouts")
        if not ls_wkts.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return
        sync_state.initial_workouts = decompress_workouts(ls_wkts.result) if ls_wkts.result else {}
        sync_state.initial_loaded = True

    # Step 2: background sync with API
    task = hd.task()

    def _fetch(client, initial):
        return client.get_all_results(initial)

    task.run(_fetch, client, sync_state.initial_workouts)

    if task.running:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return

    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return

    workouts_dict, all_workouts = task.result

    # Step 3: write updated workouts back to localStorage (once per sync)
    if not sync_state.written:
        hd.local_storage.set_item("workouts", compress_workouts(workouts_dict))
        sync_state.written = True

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    with hd.box(padding=(2, 2, 2, 2)):
        sessions_chart(all_workouts)
