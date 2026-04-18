"""
Animation lifecycle for the Power Curve page.

This module owns the HyperDiv plumbing that sits between the pure
``build_timeline_payload`` producer in ``power_curve_timeline.py`` and the
Chart.js-driven animation on the browser side.  Everything here reads/writes
``hd.state`` and runs under ``hd.scope`` / ``hd.task`` — it's the only part of
the animation story that is *not* pure Python.

Exported:
    manage_animation_bundle(state, view, ..., at_today) -> str
        Compute the split data_key / style_key, manage the cached
        ``state.sim_bundle_data`` (heavy keyframes, keyed by data_key) and
        ``state.sim_bundle`` (final js_payload, re-wrapped on any style
        change), launch the background ``build_keyframes`` task, and return
        the ``sim_command`` (``"play"`` | ``"pause"`` | ``"stop"``) that the
        chart plugin consumes.

        Key split:
          data_key   — workout identity + predictor + x_mode + show_components
                       + show_watts + WC fetch state.  Change → re-run the
                       expensive keyframe loop.
          style_key  — log_x, log_y, overlay_bests, x_bounds, y_bounds.
                       Change → only re-wrap the cached bundle_data (O(1) —
                       no task).  Bounds live on the style side because they
                       depend on log_x and are injected by wrap_payload.

        The combined (data_key, style_key) forms the ``bundle_key`` the JS
        compares to decide whether to re-apply the bundle.

    load_world_record_data(state, profile) -> wr_data | None
        Background fetch for world-class CP data.  Caches in ``state.wr_data``
        and flips ``state.wr_fetch_done`` when the task finishes — those two
        feed into ``manage_animation_bundle``'s bundle_key so a stale bundle
        from before the fetch completes gets invalidated.

    lookup_bundle_entry(lookup, day) -> dict
        Latest keyframe entry at or before ``day`` from the pred_table_lookup,
        returning ``{"pred_rows": [...], "pauls_k_fit": float|None,
        "accuracy": {...}}``.  When ``day`` is ``None``, returns the final
        keyframe (end of timeline).

Why a separate module?

    Previously these helpers lived inline in ``power_curve_page.py`` alongside
    the orchestrator, layout, and sub-views.  The animation story has its own
    cohesive invariant set (bundle_key, task scope names, sim_command
    semantics, pauls_k_fit travelling with keyframes) that is orthogonal to
    the rest of the page.  Extracting it shrinks the orchestrator and makes
    the bundle lifecycle readable top-to-bottom.

    Also: the old ``state._pauls_k_fit`` bridge between slow and fast paths
    is gone.  pauls_k_fit now travels inside ``pred_table_lookup`` entries,
    so the fast path reads it from the bundle atomically with pred_rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import hyperdiv as hd

from services.concept2_records import (
    age_category as wr_age_category,
    weight_class_str as wr_weight_class_str,
    fetch_wr_data,
)
from services.rowing_utils import age_from_dob

from components.power_curve_pipeline import WorkoutView
from components.power_curve_timeline import build_keyframes, wrap_payload


# ---------------------------------------------------------------------------
# pred_table_lookup accessor
# ---------------------------------------------------------------------------


def lookup_bundle_entry(lookup: dict, day: int | None) -> dict:
    """
    Latest keyframe entry at or before ``day`` from ``pred_table_lookup``.

    Returns ``{"pred_rows": [...], "pauls_k_fit": float|None, "accuracy": {}}``
    — the empty dict when the lookup is empty or no keyframe at or before
    ``day`` exists.

    ``day=None`` means "end of timeline" — returns the final keyframe.
    """
    empty = {"pred_rows": [], "pauls_k_fit": None, "accuracy": {}}
    if not lookup:
        return empty
    if day is None:
        return lookup.get(max(lookup.keys()), empty)
    return lookup.get(max((d for d in lookup if d <= day), default=0), empty)


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

    ``manage_animation_bundle`` folds ``state.wr_fetch_done`` and
    ``state.wr_fetch_key`` into its bundle_key so that the animation bundle
    rebuilds once the fetch completes — otherwise the y-bounds baked at the
    pre-fetch render would persist even after WR data arrived.
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


# ---------------------------------------------------------------------------
# Animation bundle lifecycle
# ---------------------------------------------------------------------------


def manage_animation_bundle(
    state,
    *,
    view: WorkoutView,
    sim_start: date,
    total_days: int,
    selected_dists: set,
    selected_times: set,
    excluded_seasons: tuple,
    show_watts: bool,
    is_dark: bool,
    x_bounds,
    y_bounds,
    rl_predictions: dict,
    all_seasons: list,
    wr_data,
    at_today: bool,
) -> str:
    """
    Manage the animation bundle lifecycle with split data_key / style_key.

    Returns ``sim_command``: ``"play"`` | ``"pause"`` | ``"stop"``.

    sim_command semantics:
      * ``"play"``   — playing, not at today, bundle ready → JS ticks forward
      * ``"pause"``  — bundle not ready yet (hold JS), or paused with a bundle
      * ``"stop"``   — at_today (timeline_day is None) or no bundle at all

    Caching strategy:
      * ``state.sim_bundle_data`` holds the heavy keyframe dict, keyed by
        ``state.sim_data_key``.  Invalidated + rebuilt via ``build_keyframes``
        in a background task whenever any data input changes.
      * ``state.sim_bundle`` is the final js_payload sent to Chart.js — always
        derived synchronously from ``bundle_data`` via ``wrap_payload``.
        Style toggles (log axes, overlay selection) bypass the task entirely.
    """
    _data_key = hashlib.md5(
        json.dumps(
            [
                state.chart_predictor,
                state.best_filter,
                sorted(list(selected_dists)),
                sorted(list(selected_times)),
                sorted(list(excluded_seasons)),
                show_watts,
                state.chart_x_metric,
                state.chart_show_components,
                state.chart_compare_wc,
                # wr_data identity — rebuild once the WC fetch completes.
                state.wr_fetch_done,
                state.wr_fetch_key,
                # (hash(FilterSpec), workout_count) — workout pipeline identity.
                list(state._view_key),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]

    _style_key = hashlib.md5(
        json.dumps(
            [
                state.chart_log_x,
                state.chart_log_y,
                state.overlay_bests,
                # x_bounds / y_bounds are injected by wrap_payload, not baked
                # into bundle_data — include them here so the bundle re-wraps
                # when they change (e.g. log_x toggles → bounds recompute).
                list(x_bounds) if x_bounds else None,
                list(y_bounds) if y_bounds else None,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:8]

    _bundle_key = f"{_data_key}-{_style_key}"

    # ── Data-side: rebuild keyframes when data inputs change ─────────────────
    if state.sim_data_key != _data_key:
        state.sim_bundle_data = None
        state.sim_pred_lookup = {}
        state.sim_data_key = _data_key
        # Force a re-wrap too since the underlying data changed.
        state.sim_bundle = None
        state.sim_bundle_key = ""

    if state.sim_bundle_data is None:
        # Heavy: build keyframes in a background task.
        with hd.scope(f"sim_bundle_{_data_key}"):
            _bt = hd.task()
            if not _bt.running and not _bt.done:
                _bt.run(
                    build_keyframes,
                    view.efforts_filtered_by_event,
                    view.quality_efforts,
                    view.featured_efforts,
                    sim_start=sim_start,
                    total_days=total_days,
                    best_filter=state.best_filter,
                    dist_enabled=state.dist_enabled,
                    time_enabled=state.time_enabled,
                    show_watts=show_watts,
                    is_dark=is_dark,
                    x_mode=state.chart_x_metric,
                    x_bounds=x_bounds,
                    y_bounds=y_bounds,
                    predictor=state.chart_predictor,
                    show_components=state.chart_show_components,
                    rl_predictions=rl_predictions,
                    all_seasons=all_seasons,
                    wr_data=wr_data,
                )
            if _bt.done:
                if _bt.result:
                    bundle_data, pred_lookup = _bt.result
                    state.sim_bundle_data = bundle_data
                    state.sim_pred_lookup = pred_lookup
                elif _bt.error:
                    # Task failed — stop playing and surface the error.
                    state.sim_playing = False
                    hd.alert(
                        f"Animation bundle failed: {_bt.error}",
                        variant="danger",
                        closable=True,
                    )

    # ── Style-side: cheap re-wrap on every render ────────────────────────────
    # When only style changed, keyframes stay cached and this is the only
    # work we do.  When data changed, we also fall through here once the task
    # completes and populates sim_bundle_data.
    if state.sim_bundle_data is not None and state.sim_bundle_key != _bundle_key:
        state.sim_bundle = wrap_payload(
            state.sim_bundle_data,
            log_x=state.chart_log_x,
            log_y=state.chart_log_y,
            overlay_bests=state.overlay_bests,
            bundle_key=_bundle_key,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
        )
        state.sim_bundle_key = _bundle_key

    # Derive sim_command.  Seeking is handled entirely in JS via the integrated
    # scrubber; Python only signals play / pause / stop.
    if state.sim_playing and not at_today and state.sim_bundle is not None:
        return "play"
    elif state.sim_playing and state.sim_bundle is None:
        return "pause"  # bundle not ready yet — hold JS
    elif at_today:
        return "stop"
    elif state.sim_bundle is not None:
        return "pause"
    else:
        return "stop"
