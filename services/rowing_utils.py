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
            if prev is None or (r.get("time") or float("inf")) < (
                prev.get("time") or float("inf")
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
