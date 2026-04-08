"""
Volume aggregation and pace-zone binning for the Sessions chart.

Pace zones are defined relative to "reference SBs" — the best performance
at key distances/times within ±365 days of today, with log-log power-law
fallback for events without recent data.

Exported:
    BIN_NAMES               — ordered list of bin display names (index 0 = Rest)
    BIN_COLORS              — list of (dark_rgba, light_rgba) per bin
    N_BINS                  — number of bins (7)
    Z1_BINS                 — frozenset of bin indices for the easy zone (5, 6)
    Z2_BINS                 — frozenset of bin indices for threshold zone (4)
    Z3_BINS                 — frozenset of bin indices for hard zone (1, 2, 3)
    get_reference_sbs()     — find recent SBs for key events
    compute_bin_thresholds()— build pace cutoffs from reference SBs + loglog fallback
    classify_pace()         — map a pace value → bin index
    workout_bin_meters()    — per-bin meter counts for a single workout
    bin_bar_svg()           — data-URI SVG stacked bar from bin meter counts
    swatch_svg()            — data-URI SVG colour swatch for legends
    aggregate_workouts()    — group all workouts by week / month / season × bin
"""

from __future__ import annotations

import base64
from datetime import date, timedelta
from typing import Optional

from services.rowing_utils import (
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    INTERVAL_WORKOUT_TYPES,
    PACE_MIN,
    PACE_MAX,
    parse_date,
    compute_pace,
    get_season,
    workout_cat_key,
    loglog_fit,
    loglog_predict_pace,
)

# ---------------------------------------------------------------------------
# Bin definitions
# ---------------------------------------------------------------------------
# Index 0 = Rest (interval rest distance), 1-6 = pace zones fastest → slowest.

BIN_NAMES = [
    "Rest",
    "Fast",
    "2k",
    "5k",
    "Threshold",
    "Fast Aerobic",
    "Slow Aerobic",
]
N_BINS = len(BIN_NAMES)

# (dark_rgba, light_rgba) per bin — indexed identically to BIN_NAMES.
BIN_COLORS = [
    ("rgba(120,120,120,0.65)", "rgba(155,155,155,0.65)"),   # 0 Rest
    ("rgba(215,55,55,0.85)",   "rgba(195,35,35,0.85)"),     # 1 Fast
    ("rgba(225,125,35,0.85)",  "rgba(205,95,15,0.85)"),     # 2 2k
    ("rgba(205,190,50,0.85)",  "rgba(180,160,15,0.85)"),    # 3 5k
    ("rgba(55,180,80,0.85)",   "rgba(25,150,50,0.85)"),     # 4 Threshold
    ("rgba(50,130,220,0.85)",  "rgba(20,105,195,0.85)"),    # 5 Fast Aerobic
    ("rgba(115,170,230,0.75)", "rgba(80,140,205,0.75)"),    # 6 Slow Aerobic
]

# 3-zone model — maps the 6 pace bins onto the Z1/Z2/Z3 framework used in the
# volume distribution table and interval tab.
Z3_BINS: frozenset = frozenset({1, 2, 3})   # Fast + 2k + 5k  (above LT2)
Z2_BINS: frozenset = frozenset({4})          # Threshold        (LT1–LT2)
Z1_BINS: frozenset = frozenset({5, 6})       # Fast+Slow Aero   (below LT1)

# ---------------------------------------------------------------------------
# Key events for reference-SB lookup
# ---------------------------------------------------------------------------

# Each entry: (event_type, event_value, label, representative_dist_for_loglog)
# representative_dist_for_loglog is used when the event has no direct SB and
# we need a distance to feed into the log-log predictor.
_SB_LOOKUP = [
    ("dist", 1000,  "1k",       1000),
    ("dist", 2000,  "2k",       2000),
    ("dist", 5000,  "5k",       5000),
    ("time", 36000, "60min",    10000),   # 36000 tenths = 60 min; ~10k as proxy
    ("dist", 42195, "marathon", 42195),
]


def get_reference_sbs(all_workouts: list, today: Optional[date] = None) -> dict:
    """
    For each key event, find the best pace within ±365 days of today.
    Only considers non-interval workouts.

    Returns
    -------
    dict: {label: pace_sec_per_500m}  e.g. {"1k": 85.4, "2k": 92.1, ...}
    """
    today = today or date.today()
    window_start = today - timedelta(days=365)
    window_end   = today + timedelta(days=365)

    sbs: dict = {}
    for w in all_workouts:
        if w.get("workout_type") in INTERVAL_WORKOUT_TYPES:
            continue
        dt = parse_date(w.get("date", ""))
        if dt < window_start or dt > window_end:
            continue
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        dist = w.get("distance")
        time_val = w.get("time")
        for etype, evalue, label, _ in _SB_LOOKUP:
            if etype == "dist" and dist == evalue:
                if label not in sbs or pace < sbs[label]:
                    sbs[label] = pace
            elif etype == "time" and time_val == evalue:
                if label not in sbs or pace < sbs[label]:
                    sbs[label] = pace
    return sbs


def compute_bin_thresholds(
    ref_sbs: dict,
    all_workouts: Optional[list] = None,
) -> Optional[dict]:
    """
    Compute pace cutoffs for the 7 pace bins.

    Strategy:
    1. Use ref_sbs for known event paces.
    2. For missing events, try log-log power-law prediction from lifetime bests
       across all non-interval ranked workouts.
    3. Fill any remaining gaps with simple proportional extrapolations.
    4. Return None if we cannot determine at least p2k and p5k.

    Returns
    -------
    dict with keys:
        fast_upper      — pace < this → Fast bin
        two_k_upper     — pace < this → 2k bin
        five_k_upper    — pace < this → 5k bin
        threshold_upper — pace < this → Threshold bin
        fast_aero_upper — pace < this → Fast Aerobic bin
    or None if insufficient data.
    """
    # Build log-log fallback from lifetime ranked bests.
    fit_params = None
    if all_workouts is not None:
        lb: dict = {}
        lb_anchor: dict = {}
        for w in all_workouts:
            if w.get("workout_type") in INTERVAL_WORKOUT_TYPES:
                continue
            dist = w.get("distance")
            time_val = w.get("time")
            if dist not in RANKED_DIST_SET and time_val not in RANKED_TIME_SET:
                continue
            cat = workout_cat_key(w)
            if cat is None:
                continue
            pace = compute_pace(w)
            if pace is None or pace < PACE_MIN or pace > PACE_MAX:
                continue
            if cat not in lb or pace < lb[cat]:
                lb[cat] = pace
                lb_anchor[cat] = dist or 0
        if len(lb) >= 2:
            fit_params = loglog_fit(lb, lb_anchor)

    def predict(label: str, proxy_dist: int) -> Optional[float]:
        """Return SB pace for label, or loglog prediction at proxy_dist."""
        if label in ref_sbs:
            return ref_sbs[label]
        if fit_params is not None:
            try:
                return loglog_predict_pace(*fit_params, proxy_dist)
            except Exception:
                pass
        return None

    p1k      = predict("1k",       1000)
    p2k      = predict("2k",       2000)
    p5k      = predict("5k",       5000)
    p60min   = predict("60min",    10000)
    pmarathon = predict("marathon", 42195)

    # Require at least p2k and p5k for meaningful binning.
    if p2k is None or p5k is None:
        return None

    # Fill remaining gaps via simple proportional extrapolation.
    if p1k is None:
        p1k = p2k * 0.96          # ~4% faster than 2k is a reasonable floor
    if p60min is None:
        p60min = p5k * 1.10       # ~10% slower than 5k
    if pmarathon is None:
        pmarathon = p60min * 1.15  # ~15% slower than 60min

    def _mid(a: float, b: float) -> float:
        return (a + b) / 2.0

    return {
        "fast_upper":      _mid(p1k, p2k),
        "two_k_upper":     _mid(p2k, p5k),
        "five_k_upper":    _mid(p5k, p60min),
        "threshold_upper": _mid(p60min, pmarathon),
        "fast_aero_upper": pmarathon + 3.0,
    }


def classify_pace(pace: float, thresholds: Optional[dict]) -> int:
    """
    Return the bin index (1–6) for a given pace.

    0 = Rest (used only for interval rest distance, not set here).
    1 = Fast, 2 = 2k, 3 = 5k, 4 = Threshold, 5 = Fast Aerobic, 6 = Slow Aerobic.

    If thresholds is None, all meters fall into bin 6 (Slow Aerobic) — the
    caller can still display totals without zone colouring.
    """
    if thresholds is None:
        return 6
    if pace < thresholds["fast_upper"]:
        return 1
    if pace < thresholds["two_k_upper"]:
        return 2
    if pace < thresholds["five_k_upper"]:
        return 3
    if pace < thresholds["threshold_upper"]:
        return 4
    if pace < thresholds["fast_aero_upper"]:
        return 5
    return 6


# ---------------------------------------------------------------------------
# Date-bucketing helpers
# ---------------------------------------------------------------------------

def _week_key(dt: date) -> str:
    """ISO year-week string sortable as a plain string: 'YYYY-Www'."""
    iso = dt.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _month_key(dt: date) -> str:
    """'YYYY-MM' — sortable as a plain string."""
    return dt.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Per-workout bin helpers
# ---------------------------------------------------------------------------

def _empty_bins() -> list:
    return [0.0] * N_BINS


def workout_bin_meters(workout: dict, thresholds: Optional[dict]) -> list:
    """
    Return ``[m0, m1, …, m6]`` — meter counts per pace bin for one workout.

    Bin 0 is Rest (interval rest distance only).
    Bins 1–6 are pace zones ordered fastest → slowest (see BIN_NAMES).

    For interval workouts each interval is classified by its own pace
    (``interval_time / 10 / (interval_dist / 500)``).  The top-level
    ``rest_distance`` goes into bin 0.

    For steady-state workouts the session's overall pace sets a single bin.
    """
    bins = _empty_bins()

    if workout.get("workout_type") in INTERVAL_WORKOUT_TYPES:
        # Rest distance → bin 0
        rest_dist = workout.get("rest_distance") or 0
        if rest_dist > 0:
            bins[0] += rest_dist

        # Each work interval classified by its own pace
        intervals = (workout.get("workout") or {}).get("intervals") or []
        for iv in intervals:
            iv_dist = iv.get("distance") or 0
            iv_time = iv.get("time") or 0  # tenths of seconds
            if iv_dist <= 0 or iv_time <= 0:
                continue
            iv_pace = (iv_time / 10.0) / (iv_dist / 500.0)
            if iv_pace < PACE_MIN or iv_pace > PACE_MAX:
                continue
            bins[classify_pace(iv_pace, thresholds)] += iv_dist
    else:
        dist = workout.get("distance") or 0
        if dist > 0:
            pace = compute_pace(workout)
            if pace is not None and PACE_MIN <= pace <= PACE_MAX:
                bins[classify_pace(pace, thresholds)] += dist

    return bins


def bin_bar_svg(
    bin_meters: list,
    width: int = 160,
    height: int = 8,
    is_dark: bool = False,
) -> str:
    """
    Return a ``data:image/svg+xml;base64,…`` URI for a stacked horizontal
    bar showing work-meter fraction in each pace zone (bin 0 / Rest excluded).

    Colours are taken from ``BIN_COLORS`` (dark or light variant).
    Segments smaller than 2 % of work total are omitted to avoid hairlines.
    """
    work = bin_meters[1:]   # bins 1-6 only (skip Rest)
    total = sum(work)

    x = 0
    rects: list[str] = []
    if total > 0:
        for i, m in enumerate(work):
            if m <= 0:
                continue
            f = m / total
            if f < 0.02:
                continue
            w = round(f * width)
            if w <= 0:
                continue
            color = BIN_COLORS[i + 1][0 if is_dark else 1]
            rects.append(
                f'<rect x="{x}" y="0" width="{w}" height="{height}"'
                f' fill="{color}"/>'
            )
            x += w

    if not rects:
        rects = [f'<rect x="0" y="0" width="{width}" height="{height}" fill="#d1d5db"/>']

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}">'
        + "".join(rects)
        + "</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def swatch_svg(color: str, size: int = 12, radius: int = 2) -> str:
    """
    Return a ``data:image/svg+xml;base64,…`` URI for a small filled square
    with rounded corners, suitable for use as a colour swatch in legends.

    ``color`` should be any valid SVG fill string (e.g. an rgba() value from
    ``BIN_COLORS``).  ``size`` is the square side-length in pixels.
    Use ``hd.image(src=swatch_svg(color), width=…, height=…)`` instead of
    ``hd.box(background_color=color)`` to stay within HyperDiv's colour system.
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
        f'<rect width="{size}" height="{size}" rx="{radius}" ry="{radius}" fill="{color}"/>'
        f"</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# ---------------------------------------------------------------------------
# Main aggregation function
# ---------------------------------------------------------------------------


def aggregate_workouts(
    all_workouts: list,
    thresholds: Optional[dict] = None,
    machine_filter: Optional[set] = None,
    *,
    bin_fn=None,
) -> dict:
    """
    Aggregate meter counts per (week / month / season) × bin.

    Parameters
    ----------
    all_workouts:
        Full workout list from concept2.get_all_results().
    thresholds:
        Output of compute_bin_thresholds(); if None and bin_fn is also None,
        all meters are binned into Slow Aerobic (useful for totals-only display).
        Ignored when bin_fn is provided.
    machine_filter:
        If not None, only include workouts whose 'type' field is in this set.
    bin_fn:
        Optional callable(workout) → list[float].  When provided, replaces the
        default ``workout_bin_meters(w, thresholds)`` call, allowing callers to
        supply an alternate binning strategy (e.g. HR-zone binning).

    Returns
    -------
    {
        "weeks":   { "YYYY-Www":  {"bins": [m0, m1, …, m6], "total": float} },
        "months":  { "YYYY-MM":   {"bins": […],               "total": float} },
        "seasons": { "YYYY-YY":   {"bins": […],               "total": float} },
    }
    """
    weeks:   dict = {}
    months:  dict = {}
    seasons: dict = {}

    _effective_bin_fn = bin_fn if bin_fn is not None else (
        lambda w: workout_bin_meters(w, thresholds)
    )

    def _add(bucket: dict, key: str, bin_idx: int, meters: float) -> None:
        if key not in bucket:
            bucket[key] = {"bins": _empty_bins(), "total": 0.0}
        bucket[key]["bins"][bin_idx] += meters
        bucket[key]["total"] += meters

    for w in all_workouts:
        mtype = w.get("type", "")
        if machine_filter is not None and mtype not in machine_filter:
            continue

        date_str = w.get("date", "")
        if not date_str:
            continue
        dt = parse_date(date_str)
        if dt == date.min:
            continue

        wk  = _week_key(dt)
        mo  = _month_key(dt)
        sea = get_season(date_str)

        for bin_idx, meters in enumerate(_effective_bin_fn(w)):
            if meters > 0:
                _add(weeks,   wk,  bin_idx, meters)
                _add(months,  mo,  bin_idx, meters)
                _add(seasons, sea, bin_idx, meters)

    return {"weeks": weeks, "months": months, "seasons": seasons}
