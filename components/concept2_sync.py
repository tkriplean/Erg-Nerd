"""
Shared workout-sync component.

Every tab that needs workout data calls concept2_sync(client) at the top of its
render function.  The function handles the full lifecycle:

  1. One-time load of the compressed workout blob from browser localStorage
     (guarded by initial_loaded so the read is never repeated after a write).
  2. Background API sync via client.get_all_results(), with a page-level
     progress indicator while fetching.
  3. Writing the updated (and re-compressed) blob back to localStorage, once,
     after the sync completes.

Return value
------------
  (workouts_dict, sorted_workouts)  — when the sync is complete
  None                              — while loading or on error
                                      (the component renders its own UI)

Usage
-----
    from components.concept2_sync import concept2_sync

    def my_tab(client, user_id: str) -> None:
        result = concept2_sync(client)
        if result is None:
            return
        workouts_dict, all_workouts = result
        ...
"""

import hyperdiv as hd

from services.rowing_utils import compress_workouts, decompress_workouts


def concept2_sync(client) -> tuple | None:
    """
    Load, sync, and persist workout data.  Returns (workouts_dict, sorted_list)
    when ready, or None while the component is still loading.
    """
    # ── Step 1: one-time localStorage read ───────────────────────────────────
    sync_state = hd.state(written=False, initial_workouts=None, initial_loaded=False)

    if not sync_state.initial_loaded:
        ls_wkts = hd.local_storage.get_item("workouts")
        if not ls_wkts.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return None
        sync_state.initial_workouts = decompress_workouts(ls_wkts.result) if ls_wkts.result else {}
        sync_state.initial_loaded = True

    # ── Step 2: background API sync ──────────────────────────────────────────
    progress = hd.state(pages=0, total=0)
    task = hd.task()

    def _fetch(client, initial, progress):
        def on_progress(pages_fetched, workouts_cached):
            progress.pages = pages_fetched
            progress.total = workouts_cached
        return client.get_all_results(initial, on_progress=on_progress)

    task.run(_fetch, client, sync_state.initial_workouts, progress)

    # ── Step 3: handle result ────────────────────────────────────────────────
    if task.done and not task.error:
        workouts_dict, sorted_workouts = task.result
        if not sync_state.written:
            hd.local_storage.set_item("workouts", compress_workouts(workouts_dict))
            sync_state.written = True
        return workouts_dict, sorted_workouts

    # Loading UI
    if task.running:
        with hd.box(align="center", padding=4, gap=1):
            hd.spinner()
            if progress.pages == 0:
                hd.text("Loading workout history…", font_color="neutral-500")
            else:
                hd.text(
                    f"Page {progress.pages} fetched — {progress.total:,} workouts loaded so far…",
                    font_color="neutral-500",
                )
        return None

    # Error UI
    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return None

    return None
