"""
The filtered collection of the user's workouts, as the Power Curve page sees it.

Exported:
    FilterSpec         — frozen dataclass of the data-identity inputs.  Changing
                         any field invalidates the workout filtering pipeline.
                         Used as the cache key for ``build_workout_view``.
                             machine                 "All" | "RowErg" | ...
                             excluded_seasons        tuple[str, ...]
                             dist_enabled            tuple[bool, ...]   (RANKED_DISTANCES)
                             time_enabled            tuple[bool, ...]   (RANKED_TIMES)
                             best_filter             "All" | "PBs" | "SBs"

    WorkoutView        — frozen dataclass grouping the 4 pipeline stages plus
                         the derived ``all_seasons`` list.  Fields:
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

No HyperDiv dependency — safe to import from anywhere.  The companion axis-
bounds helper lives in ``components/power_curve_chart_config.py`` because
chart geometry is a chart-config concern, not a workouts concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    SEASON_PALETTE,
    apply_best_only,
    apply_quality_filters,
    apply_season_best_only,
    compute_featured_workouts,
    compute_pace,
    compute_watts,
    get_season,
    is_rankable_noninterval,
    parse_date,
    seasons_from,
    workout_cat_key,
)


# ───────────────────────────────────────────────────────────────────────────
# Filter value-object
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FilterSpec:
    """Data-identity inputs. Changing any of these invalidates the workout
    filtering pipeline (quality filters, event selection, best-filter)."""

    machine: str
    excluded_seasons: tuple  # tuple[str, ...]
    dist_enabled: tuple  # tuple[bool, ...], index-aligned with RANKED_DISTANCES
    time_enabled: tuple  # tuple[bool, ...], index-aligned with RANKED_TIMES
    best_filter: str  # "All" | "PBs" | "SBs"


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
        dist for i, (dist, _) in enumerate(RANKED_DISTANCES) if filters.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(RANKED_TIMES) if filters.time_enabled[i]
    }
    by_event: list = [
        w
        for w in quality
        if w.get("distance") in selected_dists or w.get("time") in selected_times
    ]

    # Stage 3 — apply best_filter for the chart/table display list.
    if filters.best_filter == "PBs":
        display = apply_best_only(by_event)
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
# JS-prop builders
# ───────────────────────────────────────────────────────────────────────────


def build_workouts_prop(
    workouts_list: list,
    sim_start,
    total_days: int,
    sorted_seasons: list,
) -> list:
    """JS-ready workout entries for the ``workouts`` prop of PowerCurveChart.

    Each entry:
        day         int    — days from sim_start
        season_idx  int    — index into sorted_seasons (oldest=0)
        cat_key     str    — "dist:2000" | "time:10800"
        dist_m      int    — meters (0 when missing)
        time_s      float  — duration in seconds (always available; for distance
                             events this is derived from pace × distance / 500)
        y_pace      float  — sec/500m
        y_watts     float  — watts
        date_label  str    — "Mar 14, 2026"
        wtype       str    — workout_type from the Concept2 record

    JS derives the x-axis value from (dist_m, time_s) based on x_mode, and the
    event_line tooltip from cat_key + _DIST_LABELS + y_pace.
    """
    season_idx_map = {s: i for i, s in enumerate(sorted_seasons)}
    out: list = []
    for w in workouts_list:
        p = compute_pace(w)
        d = w.get("distance")
        ck = workout_cat_key(w)
        if p is None or d is None or ck is None:
            continue
        dt = parse_date(w.get("date", ""))
        if dt.toordinal() < sim_start.toordinal():
            continue
        day = (dt - sim_start).days
        if day < 0 or day > total_days:
            continue
        etype, evalue = ck
        t_raw = w.get("time")
        time_s = (t_raw / 10.0) if t_raw else round(d * p / 500.0, 2)
        out.append(
            {
                "day": day,
                "season_idx": season_idx_map.get(get_season(w.get("date", "")), 0),
                "cat_key": f"{etype}:{evalue}",
                "dist_m": d,
                "time_s": time_s,
                "y_pace": round(p, 4),
                "y_watts": round(compute_watts(p), 1),
                "date_label": dt.strftime("%b %d, %Y"),
                "wtype": w.get("workout_type", ""),
            }
        )
    out.sort(key=lambda e: e["day"])
    return out


def build_season_meta(sorted_seasons: list) -> list:
    """Per-season palette entries aligned with season_idx (oldest=0)."""

    def _hsla(idx: int, lightness_offset: int, alpha: float) -> str:
        h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
        return f"hsla({h},{s}%,{max(l + lightness_offset, 0)}%,{alpha:.2f})"

    return [
        {
            "label": s,
            "color": _hsla(i, 0, 0.90),
            "dim_color": _hsla(i, 0, 0.40),
            "border_color": _hsla(i, -12, 1.0),
        }
        for i, s in enumerate(sorted_seasons)
    ]
