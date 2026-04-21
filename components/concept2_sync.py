"""
Shared Concept2-origin data loaders.

Two render-top helpers that any tab can call.  Both handle their own lifecycle
(load, fetch, cache, progress/loading UI) so the caller gets the ready data
back or ``None`` while still loading.

    concept2_sync(client) -> (workouts_dict, sorted_workouts) | None
        Load, sync, and persist the user's own workout history.  Handles the
        one-time localStorage read, background API sync via
        ``client.get_all_results()``, and the write-back to localStorage once
        the sync completes.

    load_world_record_data(state, profile) -> wr_data | None
        Fetch world-class CP data for the user's (gender, age, weight) bucket
        from services.concept2_records.  Caches in ``state.wr_data`` and
        flips ``state.wr_fetch_done`` when the task completes.

Usage
-----
    from components.concept2_sync import concept2_sync, load_world_record_data

    def my_tab(client, user_id: str) -> None:
        result = concept2_sync(client)
        if result is None:
            return
        workouts_dict, all_workouts = result
        wr_data = load_world_record_data(state, profile) if need_wr else None
        ...
"""

from datetime import datetime

import hyperdiv as hd

from config import SYNTHETIC_MODE
from services.local_storage_compression import compress_workouts, decompress_workouts
from services.concept2_records import (
    age_category as wr_age_category,
    weight_class_str as wr_weight_class_str,
    fetch_wr_data,
)
from services.rowing_utils import age_from_dob


def _fmt_month_year(date_str: str) -> str:
    """'2019-03-14' → 'Mar 2019'"""
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %Y")
    except Exception:
        return date_str[:7]


def concept2_sync(client) -> tuple | None:
    """
    Load, sync, and persist workout data.  Returns (workouts_dict, sorted_list)
    when ready, or None while the component is still loading.
    """
    # ── Step 1: one-time localStorage read ───────────────────────────────────
    sync_state = hd.state(
        written=False, initial_workouts=None, initial_loaded=False, synth_cache=None
    )

    if not sync_state.initial_loaded:
        ls_wkts = hd.local_storage.get_item("workouts")
        if not ls_wkts.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return None
        sync_state.initial_workouts = (
            decompress_workouts(ls_wkts.result) if ls_wkts.result else {}
        )
        sync_state.initial_loaded = True

    # ── Step 2: background API sync ──────────────────────────────────────────
    progress = hd.state(pages=0, total=0, api_total=None, earliest_date=None)
    task = hd.task()

    def _fetch(client, initial, progress):
        def on_progress(pages_fetched, workouts_cached, api_total, earliest_date):
            progress.pages = pages_fetched
            progress.total = workouts_cached
            progress.api_total = api_total
            progress.earliest_date = earliest_date

        return client.get_all_results(initial, on_progress=on_progress)

    task.run(_fetch, client, sync_state.initial_workouts, progress)

    # ── Step 3: handle result ────────────────────────────────────────────────
    if task.done and not task.error:
        workouts_dict, sorted_workouts = task.result
        if not sync_state.written:
            # Write real data only — synthetic workouts must never reach localStorage.
            hd.local_storage.set_item("workouts", compress_workouts(workouts_dict))
            sync_state.written = True
        if SYNTHETIC_MODE:
            if sync_state.synth_cache is None:
                from services.synthetic_data import augment_with_synthetic

                sync_state.synth_cache = augment_with_synthetic(workouts_dict)
            return sync_state.synth_cache
        return workouts_dict, sorted_workouts

    # Loading UI
    if task.running:
        with hd.box(align="center", padding=4, gap=2):
            if progress.pages >= 2 and progress.api_total:
                pct = min(100, round(progress.total / progress.api_total * 100))
                with hd.box(width=32):
                    hd.progress_bar(value=pct)
            else:
                hd.spinner()
            if progress.pages == 0:
                hd.text("Loading workout history…", font_color="neutral-500")
            else:
                if progress.api_total:
                    count_line = f"Syncing {progress.total:,} of {progress.api_total:,} workouts…"
                else:
                    count_line = f"Syncing {progress.total:,} workouts…"
                hd.text(count_line, font_color="neutral-500")
                if progress.earliest_date:
                    hd.text(
                        f"Back to {_fmt_month_year(progress.earliest_date)}",
                        font_color="neutral-400",
                        font_size="small",
                    )
        return None

    # Error UI
    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return None

    return None


# ---------------------------------------------------------------------------
# World-class CP data fetch
# ---------------------------------------------------------------------------


def load_world_record_data(state, profile: dict):
    """
    Manage the background task that fetches world-class CP data for the
    user's (gender, age, weight) bucket.

    Caches result in ``state.wr_data`` and flips ``state.wr_fetch_done`` when
    the task completes.  Returns ``state.wr_data`` (``None`` until the fetch
    finishes or if the API returned nothing).

    The Power Curve page's ``manage_animation_bundle`` folds
    ``state.wr_fetch_done`` and ``state.wr_fetch_key`` into its bundle_key so
    that the animation bundle rebuilds once the fetch completes — otherwise
    the y-bounds baked at the pre-fetch render would persist even after WR
    data arrived.
    """
    gender_raw = profile.get("gender", "")  # "Male" or "Female"
    if gender_raw not in ("Male", "Female"):
        return None
    gender_api = "M" if gender_raw == "Male" else "F"
    age = age_from_dob(profile.get("dob", ""))
    weight_raw = profile.get("weight") or 0.0
    weight_unit = profile.get("weight_unit", "kg")
    weight_kg = weight_raw * 0.453592 if weight_unit == "lbs" else float(weight_raw)
    if age is None or weight_kg <= 0:
        return None

    age_cat = wr_age_category(age)
    wt_class = wr_weight_class_str(weight_kg, gender_api, age)
    fetch_key = f"{gender_api}|{age_cat}|{wt_class}"

    # Reset when profile changes so the fetch task re-fires.
    if fetch_key != state.wr_fetch_key:
        state.wr_fetch_key = fetch_key
        state.wr_fetch_done = False
        state.wr_data = None

    with hd.scope(f"wr_task_{fetch_key}"):
        wr_task = hd.task()
        if not wr_task.running and not wr_task.done:
            wr_task.run(fetch_wr_data, gender_api, age, weight_kg)
        if wr_task.done and not state.wr_fetch_done:
            state.wr_fetch_done = True
            state.wr_data = wr_task.result  # None if API returned nothing

    return state.wr_data
