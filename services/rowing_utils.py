"""
Shared constants and pure-Python helpers used across the app.

All exported names are public (no leading underscores). Safe to import anywhere —
no HyperDiv, no external I/O, no matplotlib.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Ranked event definitions
# ---------------------------------------------------------------------------

# (meters, display label)
RANKED_DISTANCES = [
    (100, "100m"),
    (500, "500m"),
    (1000, "1k"),
    (2000, "2k"),
    (5000, "5k"),
    (6000, "6k"),
    (10000, "10k"),
    (21097, "½ Marathon"),
    (42195, "Marathon"),
]

# (tenths of a second, display label)
RANKED_TIMES = [
    (600, "1 min"),
    (2400, "4 min"),
    (18000, "30 min"),
    (36000, "60 min"),
]

RANKED_DIST_SET = {d for d, _ in RANKED_DISTANCES}
RANKED_TIME_SET = {t for t, _ in RANKED_TIMES}
RANKED_DIST_VALUES = [d for d, _ in RANKED_DISTANCES]

# ---------------------------------------------------------------------------
# Quality-filter constants
# ---------------------------------------------------------------------------

PB_QUALITY_PCT = 35  # drop if > this % slower than the event's all-time best
SB_QUALITY_PCT = 10  # drop if > this % slower than best at same event within 8 months
ADJACENT_FILTER_PCT = (
    15  # drop if > this % slower than any longer-event best within 8 months
)
QUALITY_WINDOW_DAYS = 240  # ≈ 8 months; used by the rolling-window filters

# ---------------------------------------------------------------------------
# Miscellaneous constants
# ---------------------------------------------------------------------------

INTERVAL_WORKOUT_TYPES = {
    "FixedTimeInterval",
    "FixedDistanceInterval",
    "FixedCalorieInterval",
    "FixedWattMinuteInterval",
    "VariableInterval",
    "VariableIntervalUndefinedRest",
}

# Distinct palette for up to 8 seasons (H, S%, L%).
SEASON_PALETTE = [
    (217, 85, 55),  # blue
    (28, 90, 52),  # orange
    (155, 65, 42),  # teal
    (280, 62, 58),  # purple
    (5, 75, 52),  # red
    (185, 72, 42),  # cyan
    (45, 88, 48),  # amber
    (320, 65, 55),  # pink
]

# Categories excluded from the log-log power fit (too short / unreliable).
LOGLOG_EXCLUDED_CATS: frozenset = frozenset({("dist", 100), ("time", 600)})

# Pace axis clamp used by the chart and MP4 renderer.
PACE_MIN = 60.0  # sec/500m  (≈ 1:00 — faster than any realistic pace)
PACE_MAX = 400.0  # sec/500m  (≈ 6:40 — slower than any realistic pace)

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return date.min


def get_season(date_str: str) -> str:
    """Return season string e.g. '2024-25'.  Season runs May 1 → Apr 30."""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        if dt.month >= 5:
            return f"{dt.year}-{str(dt.year + 1)[2:]}"
        return f"{dt.year - 1}-{str(dt.year)[2:]}"
    except Exception:
        return "Unknown"


def age_from_dob(dob: str) -> int:
    """
    Compute age in whole years from a 'YYYY-MM-DD' date-of-birth string.
    Returns 0 if the string is absent, malformed, or in the future.
    """
    if not dob:
        return 0
    try:
        bd = date.fromisoformat(dob)
        today = date.today()
        return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    except (ValueError, TypeError):
        return 0


def profile_complete(profile: dict) -> bool:
    """Return True only if all fields required for RowingLevel / WR lookup are filled."""
    return (
        profile.get("gender") in ("Male", "Female")
        and age_from_dob(profile.get("dob", "")) > 0
        and float(profile.get("weight") or 0.0) > 0.0
    )


# ---------------------------------------------------------------------------
# Workout helpers
# ---------------------------------------------------------------------------


def compute_pace(r: dict) -> Optional[float]:
    """Return pace in seconds per 500m, or None if either field is missing."""
    t, d = r.get("time"), r.get("distance")
    if not t or not d:
        return None
    return (t / 10.0) / (d / 500.0)


def compute_watts(pace_sec: float) -> float:
    """Convert pace (sec/500m) to watts using the standard Concept2 formula."""
    return 2.80 * (500.0 / pace_sec) ** 3


def watts_to_pace(watts: float) -> float:
    """Convert watts to pace (sec/500m) — the inverse of compute_watts."""
    return 500.0 * (2.80 / watts) ** (1.0 / 3.0)


def workout_cat_key(r: dict) -> Optional[tuple]:
    """Category key: ('dist', meters) or ('time', tenths), or None."""
    dist, time = r.get("distance"), r.get("time")
    if dist in RANKED_DIST_SET:
        return ("dist", dist)
    if time in RANKED_TIME_SET:
        return ("time", time)
    return None


def apply_best_only(results: list, by_season: bool = False) -> list:
    """
    Keep the best result per ranked category, sorted by category order.

    by_season=False (default) — lifetime best per category.
    by_season=True            — season best per (season, category).
    """
    best: dict = {}
    for r in results:
        season = get_season(r.get("date", "")) if by_season else None
        dist, time = r.get("distance"), r.get("time")
        if dist in RANKED_DIST_SET:
            key = (season, "dist", dist) if by_season else ("dist", dist)
            prev = best.get(key)
            if prev is None or (
                r.get("time", float("inf")) < prev.get("time", float("inf"))
            ):
                best[key] = r
        elif time in RANKED_TIME_SET:
            key = (season, "time", time) if by_season else ("time", time)
            prev = best.get(key)
            if prev is None or (r.get("distance") or 0) > (prev.get("distance") or 0):
                best[key] = r

    dist_order = {d: i for i, (d, _) in enumerate(RANKED_DISTANCES)}
    time_order = {t: i for i, (t, _) in enumerate(RANKED_TIMES)}

    def _sort_key(r):
        dist, time = r.get("distance"), r.get("time")
        season = get_season(r.get("date", "")) if by_season else None
        if dist in RANKED_DIST_SET:
            return (0, dist_order.get(dist, 99)) + ((season,) if by_season else ())
        return (1, time_order.get(time, 99)) + ((season,) if by_season else ())

    return sorted(best.values(), key=_sort_key)


def apply_season_best_only(results: list) -> list:
    """Keep only the season best per (season, ranked category). Alias for apply_best_only(by_season=True)."""
    return apply_best_only(results, by_season=True)


def compute_featured_workouts(workouts: list, best_filter: str) -> list:
    """
    Return the subset of *workouts* that were ever a new personal best or
    season best at the moment they were performed.  These are the workouts
    that generate chart dots and timeline-slider annotations, and they form a
    much smaller set than the full quality-filtered list.

    workouts     — sorted newest-first (standard sync order).
    best_filter  — "PBs": running all-time best per event category.
                   "SBs": running season best per (season, event) category.
                   "All": treated as "SBs" (keeps annotation density manageable;
                          the simulation itself uses the full list for "All").

    Returns the subset sorted newest-first (same relative order, just filtered).
    """
    effective = "SBs" if best_filter == "All" else best_filter
    running_best: dict = {}
    featured: list = []

    for w in reversed(workouts):  # scan oldest → newest
        pace = compute_pace(w)
        cat = workout_cat_key(w)
        if pace is None or cat is None:
            continue
        key = (get_season(w.get("date", "")), cat) if effective == "SBs" else cat
        if key not in running_best or pace < running_best[key]:
            running_best[key] = pace
            featured.append(w)

    featured.reverse()  # restore newest-first order
    return featured


def compute_lifetime_bests(workouts: list) -> tuple[dict, dict]:
    """
    Return (lifetime_best, lifetime_best_anchor) derived from the given workout list.

    Applies a pace validity filter (PACE_MIN…PACE_MAX, non-None distance, known
    category) to each workout before considering it for a category best.

    lifetime_best        — {cat_key: best_pace_sec_per_500m}
    lifetime_best_anchor — {cat_key: distance_m_of_the_best_performance}
    """
    lifetime_best: dict = {}
    lifetime_best_anchor: dict = {}
    for w in workouts:
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        dist = w.get("distance")
        if not dist:
            continue
        cat = workout_cat_key(w)
        if cat is None:
            continue
        if cat not in lifetime_best or pace < lifetime_best[cat]:
            lifetime_best[cat] = pace
            lifetime_best_anchor[cat] = dist
    return lifetime_best, lifetime_best_anchor


def compute_duration_s(workout: dict) -> Optional[float]:
    """
    Return the total duration of a ranked workout in seconds, or None.

    Distance events: duration = pace × distance / 500
    Time events:     duration = time_tenths / 10  (the event definition itself)
    """
    dist = workout.get("distance")
    time = workout.get("time")
    if not time:
        return None
    if dist in RANKED_DIST_SET:
        # Distance event — duration is derived from actual recorded time
        return time / 10.0
    if time in RANKED_TIME_SET:
        # Timed event — the event duration is the definition (e.g. 30 min = 18000 tenths)
        return time / 10.0
    return None


# ---------------------------------------------------------------------------
# Prediction helpers
# ---------------------------------------------------------------------------


def pauls_law_pace(p1: float, d1: float, d2: float, k: float = 5.0) -> float:
    """Predict pace at d2 given known pace p1 at d1 (Paul's Law).

    k is the pace increase (sec/500m) per doubling of distance; defaults to the
    standard 5.0 but can be personalised via compute_pauls_constant().
    """
    return p1 + k * math.log2(d2 / d1)


def compute_pauls_constant(
    lifetime_best: dict, lifetime_best_anchor: dict
) -> Optional[float]:
    """Return the best-fit Paul's Law constant K for the rower's PBs.

    Uses regression through the origin across all ordered (anchor, target) PB
    pairs:  K = Σ(x·y) / Σ(x²)  where  x = log₂(d_target/d_anchor)
    and  y = pace_target − pace_anchor.

    Returns K rounded to 1 decimal place and clamped to [0.5, 15.0], or None
    if fewer than 2 PBs are available.
    """
    cats = [
        (pace, lifetime_best_anchor[cat])
        for cat, pace in lifetime_best.items()
        if lifetime_best_anchor.get(cat)
    ]
    if len(cats) < 2:
        return None
    sx2 = 0.0
    sxy = 0.0
    for i, (pi, di) in enumerate(cats):
        for j, (pj, dj) in enumerate(cats):
            if i == j:
                continue
            x = math.log2(dj / di)
            y = pj - pi
            sx2 += x * x
            sxy += x * y
    if sx2 < 1e-10:
        return None
    k = sxy / sx2
    return round(max(0.5, min(15.0, k)), 1)


def loglog_fit(lifetime_best: dict, lifetime_best_anchor: dict) -> Optional[tuple]:
    """
    Fit log(watts) = slope * log(dist) + intercept across all PB anchors,
    excluding categories in LOGLOG_EXCLUDED_CATS.
    Returns (slope, intercept) or None if fewer than 2 usable points.
    """
    pts = []
    for cat, pace in lifetime_best.items():
        if cat in LOGLOG_EXCLUDED_CATS:
            continue
        anchor = lifetime_best_anchor.get(cat)
        if not anchor:
            continue
        watts = compute_watts(pace)
        if watts > 0:
            pts.append((math.log(anchor), math.log(watts)))
    if len(pts) < 2:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxy = sum(x * y for x, y in pts)
    sxx = sum(x * x for x, _ in pts)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def loglog_predict_pace(slope: float, intercept: float, dist_m: float) -> float:
    """Return predicted pace (sec/500m) for dist_m using a log-log fit."""
    watts = math.exp(intercept + slope * math.log(dist_m))
    return 500.0 / (watts / 2.80) ** (1.0 / 3.0)


# ---------------------------------------------------------------------------
# Ranked-event classification + season helpers
# ---------------------------------------------------------------------------


def is_rankable_noninterval(r: dict) -> bool:
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
# Quality-filter passes
# ---------------------------------------------------------------------------


def _window_best(event_timeline: list, w_date: date, cat) -> float:
    """Return the best pace within ±QUALITY_WINDOW_DAYS of w_date for cat."""
    best = float("inf")
    for _dt, _c, _p in event_timeline:
        if _c == cat and abs((_dt - w_date).days) <= QUALITY_WINDOW_DAYS:
            if _p < best:
                best = _p
    return best


def _dominated_by_longer(
    dist_timeline: list, w_date: date, eff_dist, workout_pace: float
) -> bool:
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


def apply_quality_filters(workouts: list) -> list:
    """
    Apply the three quality-filter passes to a list of ranked workouts:

      1. Lifetime-PB quality filter — drop if > PB_QUALITY_PCT% slower than the
         event's all-time best.
      2. Rolling-window season-best filter — drop if > SB_QUALITY_PCT% slower
         than the best at the same event within ±QUALITY_WINDOW_DAYS.
      3. Rolling-window cross-event domination filter — drop a shorter-event
         workout if any longer-event performance within the window is more than
         ADJACENT_FILTER_PCT% faster (by pace).

    Returns the filtered list.
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


def workouts_before_date(
    rankable_efforts: list,
    timeline_date: date,
    selected_dists: set,
    selected_times: set,
    excluded_seasons: set,
    best_filter: str,
) -> list:
    """
    Return the workouts visible at *timeline_date*.

    Quality filters (PB quality, rolling-window SB quality, cross-event
    domination) are applied once upfront in power_curve_page.py before the
    simulation receives the data — whether a performance was max/near-max
    effort does not change as timeline_date advances.

    This function only applies:
      1. Date gate (workouts on or before timeline_date)
      2. Event filter (selected_dists / selected_times)
      3. Season filter (excluded_seasons)
      4. best_filter ("All" | "PBs" | "SBs")
    """
    date_str = timeline_date.isoformat()

    # 1. Date gate
    in_time = [w for w in rankable_efforts if (w.get("date") or "")[:10] <= date_str]

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
