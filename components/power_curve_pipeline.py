"""
Workout-filtering pipeline for the Power Curve page.

Exported:
    WorkoutView        — frozen dataclass grouping the 4 pipeline stages plus
                         the derived ``all_seasons`` list.  Fields match the
                         state-variable names previously cached separately:
                             quality_efforts
                             efforts_filtered_by_event
                             efforts_filtered_by_event_and_display
                             featured_efforts
                             all_seasons

    build_workout_view(raw_workouts, filters)  → WorkoutView
        Pure, memoizable.  One traversal through all stages.  Replaces the 4
        parallel caches + 4 hand-rolled string keys that used to gate each
        stage independently — a single ``hash(filters)`` now invalidates the
        whole pipeline atomically.

    compute_axis_bounds(quality_efforts, show_watts, use_duration, log_x)
        Pure helper (previously ``_compute_axis_bounds`` in power_curve_page).
        Lives here because its sole input is ``quality_efforts`` — the first
        pipeline stage.

No HyperDiv dependency — safe to import from anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.ranked_filters import (
    apply_quality_filters,
    is_rankable_noninterval,
    seasons_from,
)
from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    apply_best_only,
    apply_season_best_only,
    compute_featured_workouts,
    compute_pace,
    compute_watts,
    get_season,
)

from components.power_curve_state import FilterSpec


# ───────────────────────────────────────────────────────────────────────────
# Pipeline value-object
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkoutView:
    """One object, four stages.

    Stage 1 — quality_efforts: workouts matching the machine filter, passing
        rankability + quality gates, with excluded-seasons removed.
    Stage 2 — efforts_filtered_by_event: stage 1 ∩ selected distances/times.
    Stage 3 — efforts_filtered_by_event_and_display: stage 2 after applying
        the best_filter ("All"|"PBs"|"SBs") — the list shown in chart + table.
    Stage 4 — featured_efforts: the subset of stage 2 that ever set a new
        historical PB or SB.  Drives slider annotations and the slow-path
        date-slice for chart rendering.

    all_seasons is derived from stage 1 via ``seasons_from`` (newest-first).
    """

    quality_efforts: list
    efforts_filtered_by_event: list
    efforts_filtered_by_event_and_display: list
    featured_efforts: list
    all_seasons: tuple


# ───────────────────────────────────────────────────────────────────────────
# Builder
# ───────────────────────────────────────────────────────────────────────────


def build_workout_view(raw_workouts: list, filters: FilterSpec) -> WorkoutView:
    """Run the full filtering pipeline; return a WorkoutView.

    Pure: no HyperDiv, no I/O.  Called once per render in power_curve_page
    whenever ``hash(filters)`` (plus workout-count) changes.
    """
    # Stage 1 — quality_efforts.
    excl = set(filters.excluded_seasons)
    quality: list = [
        w
        for w in raw_workouts
        if (filters.machine == "All" or w.get("type") == filters.machine)
        and is_rankable_noninterval(w)
        and get_season(w.get("date", "")) not in excl
    ]
    quality = apply_quality_filters(quality)
    all_seasons = tuple(seasons_from(quality))

    # Stage 2 — filter by selected distance/time events.
    selected_dists = {
        dist
        for i, (dist, _) in enumerate(RANKED_DISTANCES)
        if filters.dist_enabled[i]
    }
    selected_times = {
        tenths
        for i, (tenths, _) in enumerate(RANKED_TIMES)
        if filters.time_enabled[i]
    }
    by_event: list = [
        w
        for w in quality
        if w.get("distance") in selected_dists or w.get("time") in selected_times
    ]

    # Stage 3 — apply best_filter for the chart/table display list.
    if filters.best_filter == "PBs":
        display: list = apply_best_only(by_event)
    elif filters.best_filter == "SBs":
        display = apply_season_best_only(by_event)
    else:
        display = by_event

    # Stage 4 — featured workouts (new-PB/SB set points); drives annotations
    # and the slow-path date slice.
    featured: list = compute_featured_workouts(by_event, filters.best_filter)

    return WorkoutView(
        quality_efforts=quality,
        efforts_filtered_by_event=by_event,
        efforts_filtered_by_event_and_display=display,
        featured_efforts=featured,
        all_seasons=all_seasons,
    )


# ───────────────────────────────────────────────────────────────────────────
# Axis bounds — pure helper, previously _compute_axis_bounds in page.py
# ───────────────────────────────────────────────────────────────────────────


def compute_axis_bounds(
    quality_efforts: list,
    show_watts: bool,
    use_duration: bool,
    log_x: bool,
) -> tuple:
    """Stable x/y bounds from all-time PBs so the chart doesn't rescale when
    the user toggles individual events.  Returns (x_bounds, y_bounds);
    either may be None if data is insufficient."""
    bests = apply_best_only(quality_efforts)
    if not bests:
        return None, None
    bp = [p for w in bests if (p := compute_pace(w)) and 60 < p < 400]
    if use_duration:
        bx = [
            w.get("distance") * p / 500
            for w in bests
            if w.get("distance") and (p := compute_pace(w)) and 60 < p < 400
        ]
    else:
        bx = [w.get("distance") for w in bests if w.get("distance")]
    if not bp or not bx:
        return None, None
    xr, xR = min(bx), max(bx)
    x_bounds = (
        (xr / 1.45, xR * 1.45)
        if log_x
        else (
            max(0, xr - max((xR - xr) * 0.1, xr * 0.1)),
            xR + max((xR - xr) * 0.1, xr * 0.1),
        )
    )
    by = [compute_watts(p) if show_watts else p for p in bp]
    yr, yR = min(by), max(by)
    ypad = max((yR - yr) * 0.15, 5 if not show_watts else 2)
    return x_bounds, (yr - ypad, yR + ypad)
