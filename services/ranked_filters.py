"""
Quality-filter logic, season/ranked helpers, and simulation date-gating.

Exported:
  is_ranked_noninterval()  — True if workout matches a ranked event and is not an interval
  seasons_from()           — sorted list of seasons (newest-first) present in results
  apply_quality_filters()  — apply the three quality-filter passes to a workout list
  sim_workouts_at()        — return workouts visible at a given simulation date
"""

from __future__ import annotations

from datetime import date

from services.rowing_utils import (
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    INTERVAL_WORKOUT_TYPES,
    PB_QUALITY_PCT,
    SB_QUALITY_PCT,
    ADJACENT_FILTER_PCT,
    QUALITY_WINDOW_DAYS,
    parse_date,
    compute_pace,
    get_season,
    workout_cat_key,
    apply_best_only,
    apply_season_best_only,
)


# ---------------------------------------------------------------------------
# Ranked-specific helpers
# ---------------------------------------------------------------------------


def is_ranked_noninterval(r: dict) -> bool:
    """True if the workout matches a ranked distance or time and is not an interval."""
    if r.get("workout_type") in INTERVAL_WORKOUT_TYPES:
        return False
    dist = r.get("distance")
    time = r.get("time")
    return dist in RANKED_DIST_SET or time in RANKED_TIME_SET


def seasons_from(results: list) -> list:
    """Sorted list of seasons (newest first) present in the given results."""
    return sorted(
        {get_season(r["date"]) for r in results if r.get("date")},
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Quality filter helpers (private)
# ---------------------------------------------------------------------------


def _window_best(event_timeline: list, w_date: date, cat) -> float:
    """Return the best pace within ±QUALITY_WINDOW_DAYS of w_date for cat."""
    best = float("inf")
    for _dt, _c, _p in event_timeline:
        if _c == cat and abs((_dt - w_date).days) <= QUALITY_WINDOW_DAYS:
            if _p < best:
                best = _p
    return best


def _dominated_by_longer(dist_timeline: list, w_date: date, eff_dist, workout_pace: float) -> bool:
    """Return True if any longer-event performance within the window is > ADJ_THRESH faster."""
    _adj_thresh = 1.0 + ADJACENT_FILTER_PCT / 100.0
    for _dt, _d, _p in dist_timeline:
        if (
            _d > eff_dist
            and abs((_dt - w_date).days) <= QUALITY_WINDOW_DAYS
            and workout_pace >= _p * _adj_thresh
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Top-level quality filter
# ---------------------------------------------------------------------------


def apply_quality_filters(
    workouts: list,
    selected_dists: set,
    selected_times: set,
    excluded_seasons: set,
) -> list:
    """
    Apply the three quality-filter passes to a list of ranked workouts:

      1. Lifetime-PB quality filter — drop if > PB_QUALITY_PCT% slower than the
         event's all-time best.
      2. Rolling-window season-best filter — drop if > SB_QUALITY_PCT% slower
         than the best at the same event within ±QUALITY_WINDOW_DAYS.
      3. Rolling-window cross-event domination filter — drop a shorter-event
         workout if any longer-event performance within the window is more than
         ADJACENT_FILTER_PCT% faster (by pace).

    Returns the filtered list.  selected_dists, selected_times, and
    excluded_seasons are not applied here — they are applied in power_curve_page.py
    after this call.
    """
    # 1) Lifetime-PB quality filter
    _cat_pb: dict = {}
    for _w in workouts:
        _p = compute_pace(_w)
        _c = workout_cat_key(_w)
        if _p is not None and _c is not None:
            if _c not in _cat_pb or _p < _cat_pb[_c]:
                _cat_pb[_c] = _p
    _pb_thresh = 1.0 + PB_QUALITY_PCT / 100.0
    workouts = [
        w
        for w in workouts
        if (c := workout_cat_key(w)) is not None
        and (p := compute_pace(w)) is not None
        and p <= _cat_pb.get(c, float("inf")) * _pb_thresh
    ]

    # 2) Rolling-window season-best filter
    _sb_thresh = 1.0 + SB_QUALITY_PCT / 100.0
    _event_timeline: list = []  # (date, cat_key, pace)
    for _w in workouts:
        _p = compute_pace(_w)
        _c = workout_cat_key(_w)
        _dt = parse_date(_w.get("date", ""))
        if _p is not None and _c is not None and _dt != date.min:
            _event_timeline.append((_dt, _c, _p))

    workouts = [
        w
        for w in workouts
        if (c := workout_cat_key(w)) is not None
        and (p := compute_pace(w)) is not None
        and (dt := parse_date(w.get("date", ""))) != date.min
        and p <= _window_best(_event_timeline, dt, c) * _sb_thresh
    ]

    # 3) Rolling-window cross-event domination filter
    _dist_timeline: list = []  # (date, effective_dist, pace)
    for _w in workouts:
        _p = compute_pace(_w)
        _d = _w.get("distance")
        _dt = parse_date(_w.get("date", ""))
        if _p is not None and _d and _dt != date.min:
            _dist_timeline.append((_dt, _d, _p))

    workouts = [
        w
        for w in workouts
        if (p := compute_pace(w)) is not None
        and (d := w.get("distance"))
        and (dt := parse_date(w.get("date", ""))) != date.min
        and not _dominated_by_longer(_dist_timeline, dt, d, p)
    ]

    return workouts


# ---------------------------------------------------------------------------
# Simulation date-gating
# ---------------------------------------------------------------------------


def sim_workouts_at(
    all_ranked_raw: list,
    sim_date: date,
    selected_dists: set,
    selected_times: set,
    excluded_seasons: set,
    best_filter: str,
) -> list:
    """
    Return the workouts visible at *sim_date*.

    Quality filters (PB quality, rolling-window SB quality, cross-event
    domination) are applied once upfront in power_curve_page.py before the
    simulation receives the data — whether a performance was max/near-max
    effort does not change as sim_date advances.

    This function only applies:
      1. Date gate (workouts on or before sim_date)
      2. Event filter (selected_dists / selected_times)
      3. Season filter (excluded_seasons)
      4. best_filter ("All" | "PBs" | "SBs")
    """
    date_str = sim_date.isoformat()

    # 1. Date gate
    in_time = [w for w in all_ranked_raw if (w.get("date") or "")[:10] <= date_str]

    # 2. Event filter
    in_time = [
        w
        for w in in_time
        if w.get("distance") in selected_dists or w.get("time") in selected_times
    ]

    # 3. Season filter
    in_time = [
        w for w in in_time if get_season(w.get("date", "")) not in excluded_seasons
    ]

    # 4. best_filter
    if best_filter == "PBs":
        return apply_best_only(in_time)
    if best_filter == "SBs":
        return apply_season_best_only(in_time)
    return in_time
