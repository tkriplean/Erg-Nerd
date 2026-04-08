"""
Shared constants and pure-Python helpers used across the app.

All exported names are public (no leading underscores). Safe to import anywhere —
no HyperDiv, no external I/O, no matplotlib.
"""

from __future__ import annotations

import base64
import json
import math
import zlib
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


def format_time(tenths: int) -> str:
    """
    Format a duration stored as tenths of a second into the same string the
    Concept2 API returns as 'time_formatted'.

    Examples:
        71      → '0:07.1'
        4608    → '7:40.8'
        84254   → '2:20:25.4'
    """
    t = int(tenths)
    frac = t % 10
    total_s = t // 10
    secs = total_s % 60
    total_m = total_s // 60
    mins = total_m % 60
    hours = total_m // 60
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}.{frac}"
    return f"{mins}:{secs:02d}.{frac}"


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


def pauls_law_pace(p1: float, d1: float, d2: float) -> float:
    """Predict pace at d2 given known pace p1 at d1 (Paul's Law)."""
    return p1 + 5.0 * math.log2(d2 / d1)


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
# Workout localStorage compression
# ---------------------------------------------------------------------------

# Fields that are always redundant and never used by the app.
_WORKOUT_PERM_DROP = frozenset({
    "real_time",
    "calories_total",   # dropped from top-level summary …
    "timezone",
    "date_utc",
    "user_id",
    "privacy",
    # time_formatted is NOT permanently dropped — for interval workouts it
    # carries work-only time (total minus rest) which differs from format_time(time).
    # It is dropped conditionally in _compress_one_workout() when redundant.
})

# Fields whose default value is implied; omitted on compress, restored on
# decompress.  Saves space without losing any information.
_WORKOUT_DEFAULTS = {
    "verified": True,
    "type": "rower",
    "comments": None,
    "ranked": False,
}


def _hr_empty(hr) -> bool:
    """True when a heart_rate value carries no real data."""
    if hr is None or hr == {}:
        return True
    if isinstance(hr, dict):
        return all(v == 0 for v in hr.values())
    return False


def _compress_one_workout(w: dict) -> dict:
    out = {}
    for k, v in w.items():
        if k in _WORKOUT_PERM_DROP:
            continue
        if k in _WORKOUT_DEFAULTS and v == _WORKOUT_DEFAULTS[k]:
            continue
        # Drop time_formatted when it's identical to what format_time() produces
        # (true for JustRow, FixedDistanceSplits, FixedTimeSplits). For interval
        # workouts it carries work-only time and must be kept.
        if k == "time_formatted" and v == format_time(w.get("time", 0)):
            continue
        if k == "heart_rate" and _hr_empty(v):
            continue
        if k == "workout" and isinstance(v, dict):
            # Strip targets, calories_total from splits, empty heart_rate,
            # always-constant split type, and always-zero wattminutes_total.
            splits = v.get("splits") or []
            new_splits = []
            for s in splits:
                ns = {}
                for sk, sv in s.items():
                    if sk in ("calories_total", "type", "wattminutes_total"):
                        continue
                    if sk == "heart_rate" and _hr_empty(sv):
                        continue
                    ns[sk] = sv
                new_splits.append(ns)
            new_wo = {wk: wv for wk, wv in v.items()
                      if wk not in ("targets", "splits")}
            if new_splits:
                new_wo["splits"] = new_splits
            out[k] = new_wo
            continue
        out[k] = v
    return out


def _decompress_one_workout(w: dict) -> dict:
    """Restore default-value fields stripped during compression."""
    out = dict(w)
    for k, default in _WORKOUT_DEFAULTS.items():
        if k not in out:
            out[k] = default
    return out


def compress_workouts(workouts_dict: dict) -> str:
    """
    Serialize and compress a workout dict for browser localStorage storage.

    Before compression, redundant and always-default fields are stripped from
    each workout (and from split sub-dicts).  Default-value fields are
    restored transparently by decompress_workouts().

    The pruned dict is then JSON-serialized, compressed with zlib (level=9),
    and base64-encoded to a plain ASCII string for localStorage.setItem().

    Typical end-to-end reduction vs. raw JSON: ~8–10×.
    """
    pruned = {k: _compress_one_workout(v) for k, v in workouts_dict.items()}
    raw = json.dumps(pruned).encode()
    return base64.b64encode(zlib.compress(raw, level=9)).decode()


def decompress_workouts(stored: str) -> dict:
    """
    Reverse of compress_workouts(). Returns the workout dict, or {} on error.
    Restores default-value fields (verified, type, comments, ranked) that were
    omitted during compression.
    """
    try:
        raw = json.loads(zlib.decompress(base64.b64decode(stored)))
        return {k: _decompress_one_workout(v) for k, v in raw.items()}
    except Exception:
        return {}
