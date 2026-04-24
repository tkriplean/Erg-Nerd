"""
Volume aggregation and power-zone binning for the Sessions chart.

Power zones are defined in watts, against a **time-aware** fitness reference:
for each workout we look up the rower's reference watts at that workout's date
(via :mod:`services.reference_watts`), then classify each meter into one of
six physiological zones (1–6) plus a Rest bin (0) for interval rest distance.

The classification is in watts, not pace — watts is the physiologically
correct unit of intensity and is directly comparable across events (1k, 2k,
60min live on the same axis, unlike their paces).  Higher watts ⇒ more
intense ⇒ lower bin index among 1–6.

Exported:
    BIN_NAMES                   — ordered list of bin display names (index 0 = Rest)
    BIN_COLORS                  — list of (dark_rgba, light_rgba) per bin
    N_BINS                      — number of bins (7)
    Z1_BINS                     — frozenset of bin indices for easy zone (5, 6)
    Z2_BINS                     — frozenset of bin indices for threshold zone (4)
    Z3_BINS                     — frozenset of bin indices for hard zone (1, 2, 3)
    POWER_INTENSITY_WEIGHTS     — 7-element per-bin weights for the 0–100 score
    POWER_ZONE_DEFINITION_TEXT  — one-line human definition per bin index
    POWER_ZONE_FILTER_TEXT      — one-line human description of each bin's
                                  filter-pass threshold
    power_intensity_score()     — compute a workout's 0–100 power-intensity score
    power_bin_passes()          — true if a workout's bin meters pass the zone
                                  threshold used by the Intervals-page filter
    compute_bin_thresholds()    — build watts cutoffs from a reference-watts dict
    classify_watts()            — map a watts value → bin index
    workout_bin_meters()        — per-bin meter counts for a single workout
    workout_power_intensity()   — single-workout score using date-appropriate
                                  thresholds (for the sessions-page hook)
    bin_bar_svg()               — data-URI SVG stacked bar from bin meters
    swatch_svg()                — data-URI SVG color swatch for legends
    aggregate_workouts()        — group all workouts by week / month / season × bin
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Optional

from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    PACE_MAX,
    PACE_MIN,
    compute_pace,
    compute_watts,
    get_season,
    parse_date,
    watts_to_pace,
)

# ---------------------------------------------------------------------------
# Bin definitions
# ---------------------------------------------------------------------------
# Index 0 = Rest (interval rest distance), 1-6 = power zones fastest → slowest.

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
    ("rgba(120,120,120,1)", "rgba(155,155,155,1)"),  # 0 Rest
    ("rgba(215,55,55,1)", "rgba(195,35,35,1)"),  # 1 Fast
    ("rgba(225,125,35,1)", "rgba(205,95,15,1)"),  # 2 2k
    ("rgba(205,190,50,1)", "rgba(180,160,15,1)"),  # 3 5k
    ("rgba(55,180,80,1)", "rgba(25,150,50,1)"),  # 4 Threshold
    ("rgba(50,130,220,1)", "rgba(20,105,195,1)"),  # 5 Fast Aerobic
    ("rgba(115,170,230,1)", "rgba(80,140,205,1)"),  # 6 Slow Aerobic
]

# 3-zone model — maps the 6 power bins onto the Z1/Z2/Z3 framework used in
# the volume distribution table and interval tab.
Z3_BINS: frozenset = frozenset({1, 2, 3})  # Fast + 2k + 5k  (above LT2)
Z2_BINS: frozenset = frozenset({4})  # Threshold        (LT1–LT2)
Z1_BINS: frozenset = frozenset({5, 6})  # Fast+Slow Aero   (below LT1)

# Linear weights per bin index for the 0–100 power-intensity score.
# Score = Σ (meters_in_bin / work_meters × weight), ignoring bin 0 (Rest).
# Fastest zone → 100, slowest → 0.
POWER_INTENSITY_WEIGHTS: list[int] = [0, 100, 80, 60, 40, 20, 0]

# One-line definition per bin, indexed by bin index (0 = Rest).  Thresholds
# are personalised via compute_bin_thresholds(); these strings describe the
# midpoint-based rule without quoting the user's current numbers.
POWER_ZONE_DEFINITION_TEXT: dict[int, str] = {
    0: "Interval rest distance — not counted toward intensity.",
    1: "Higher watts than the midpoint between your 1k and 2k watts.",
    2: "Between midpoint(1k, 2k) and midpoint(2k, 5k) — near 2k race power.",
    3: "Between midpoint(2k, 5k) and midpoint(5k, 60min) — near 5k race power.",
    4: "Between midpoint(5k, 60min) and midpoint(60min, marathon) — threshold.",
    5: "From threshold down to the watts corresponding to ~3 s/500m slower than marathon pace.",
    6: "Lower watts than the fast-aerobic cutoff — base / easy work.",
}

# Threshold each bin must clear (as a fraction of a workout's total work
# meters) for the Intervals-page filter legend to consider the zone "present".
# A value of None means the zone uses a compound rule implemented in
# power_bin_passes() below.  Consumed by chip tooltips and the filter.
POWER_ZONE_FILTER_TEXT: dict[int, str] = {
    1: "Selected: workouts with ≥5% of work meters in Fast.",
    2: "Selected: workouts with ≥10% of work meters in 2k.",
    3: "Selected: workouts with ≥15% of work meters in 5k.",
    4: "Selected: workouts with ≥25% of work meters in Threshold.",
    5: "Selected: workouts with ≥50% of work meters in Fast+Slow Aerobic combined.",
    6: "Selected: workouts with >30% of work meters in Slow Aerobic and >50% "
    "in Fast+Slow Aerobic combined.",
}

# ---------------------------------------------------------------------------
# Threshold computation from a reference-watts dict
# ---------------------------------------------------------------------------

# Events needed for the five zone boundaries (cat_key tuples from workout_cat_key).
_CK_1K = ("dist", 1000)
_CK_2K = ("dist", 2000)
_CK_5K = ("dist", 5000)
_CK_60MIN = ("time", 36000)
_CK_MARATHON = ("dist", 42195)


def compute_bin_thresholds(ref_watts: Optional[dict]) -> Optional[dict]:
    """
    Compute watts cutoffs for the 6 power bins from a reference-watts dict.

    ``ref_watts`` maps cat_key tuples → watts (the output of
    :func:`services.reference_watts.get_reference_watts`).  It must contain at
    least the 1k, 2k, 5k, 60min and marathon entries for a meaningful result;
    if any is missing this returns None.

    Returns
    -------
    dict with keys (watts values; higher = more intense):
        fast_lower_w      — watts > this → Fast bin
        two_k_lower_w     — watts > this → 2k bin
        five_k_lower_w    — watts > this → 5k bin
        threshold_lower_w — watts > this → Threshold bin
        fast_aero_lower_w — watts > this → Fast Aerobic bin
    or None if insufficient data.
    """
    if not ref_watts:
        return None

    w1k = ref_watts.get(_CK_1K)
    w2k = ref_watts.get(_CK_2K)
    w5k = ref_watts.get(_CK_5K)
    w60 = ref_watts.get(_CK_60MIN)
    wmara = ref_watts.get(_CK_MARATHON)

    # The zone boundaries require all five anchors; without them the
    # reference-watts index did not converge for this rower / machine.
    if w1k is None or w2k is None or w5k is None or w60 is None or wmara is None:
        return None

    def _mid(a: float, b: float) -> float:
        return (a + b) / 2.0

    # Fast-aerobic floor is defined in pace terms (marathon pace + 3 s/500m)
    # — translate at the edge so the semantics match the legacy implementation.
    mara_pace = watts_to_pace(wmara)
    fast_aero_watts = compute_watts(mara_pace + 3.0)

    return {
        "fast_lower_w": _mid(w1k, w2k),
        "two_k_lower_w": _mid(w2k, w5k),
        "five_k_lower_w": _mid(w5k, w60),
        "threshold_lower_w": _mid(w60, wmara),
        "fast_aero_lower_w": fast_aero_watts,
    }


def classify_watts(watts: float, thresholds: Optional[dict]) -> int:
    """
    Return the bin index (1–6) for a given watts value.

    0 = Rest (used only for interval rest distance, not set here).
    1 = Fast, 2 = 2k, 3 = 5k, 4 = Threshold, 5 = Fast Aerobic, 6 = Slow Aerobic.

    Higher watts ⇒ more intense ⇒ lower bin index.  If thresholds is None, all
    meters fall into bin 6 (Slow Aerobic) — the caller can still display
    totals without zone coloring.
    """
    if thresholds is None:
        return 6
    if watts > thresholds["fast_lower_w"]:
        return 1
    if watts > thresholds["two_k_lower_w"]:
        return 2
    if watts > thresholds["five_k_lower_w"]:
        return 3
    if watts > thresholds["threshold_lower_w"]:
        return 4
    if watts > thresholds["fast_aero_lower_w"]:
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
    Return ``[m0, m1, …, m6]`` — meter counts per power bin for one workout.

    Bin 0 is Rest (interval rest distance only).
    Bins 1–6 are power zones ordered fastest → slowest (see BIN_NAMES).

    For interval workouts each interval is classified by its own watts
    (from ``compute_watts(interval_pace)``).  The top-level ``rest_distance``
    goes into bin 0.

    For steady-state workouts the session's overall watts set a single bin.
    """
    bins = _empty_bins()

    if workout.get("workout_type") in INTERVAL_WORKOUT_TYPES:
        # Rest distance → bin 0
        rest_dist = workout.get("rest_distance") or 0
        if rest_dist > 0:
            bins[0] += rest_dist

        # Each work interval classified by its own pace → watts
        intervals = (workout.get("workout") or {}).get("intervals") or []
        for iv in intervals:
            iv_dist = iv.get("distance") or 0
            iv_time = iv.get("time") or 0  # tenths of seconds
            if iv_dist <= 0 or iv_time <= 0:
                continue
            iv_pace = (iv_time / 10.0) / (iv_dist / 500.0)
            if iv_pace < PACE_MIN or iv_pace > PACE_MAX:
                continue
            bins[classify_watts(compute_watts(iv_pace), thresholds)] += iv_dist
    else:
        dist = workout.get("distance") or 0
        if dist > 0:
            pace = compute_pace(workout)
            if pace is not None and PACE_MIN <= pace <= PACE_MAX:
                bins[classify_watts(compute_watts(pace), thresholds)] += dist

    return bins


def power_intensity_score(bin_meters: list) -> Optional[float]:
    """
    Return a 0–100 weighted-average intensity score for a workout's power bins.

    Bin 0 (Rest) is excluded from both the weights and the denominator.
    Returns None when a workout has no classifiable work meters — callers
    render that as "—" and sort it last.
    """
    work = bin_meters[1:]
    total = sum(work)
    if total <= 0:
        return None
    weights = POWER_INTENSITY_WEIGHTS[1:]
    return sum((m / total) * w for m, w in zip(work, weights))


def power_bin_passes(bm: list, bin_idx: int) -> bool:
    """
    Return True if a workout's power-bin vector has enough meters in
    ``bin_idx`` for the Intervals-page filter legend to consider that zone
    "present".  Thresholds are the per-zone heuristics documented in
    POWER_ZONE_FILTER_TEXT.  Bin 0 (Rest) is not filterable.
    """
    work_total = sum(bm[1:])
    if not work_total:
        return False
    if bin_idx == 1:
        return bm[1] / work_total >= 0.05
    if bin_idx == 2:
        return bm[2] / work_total >= 0.10
    if bin_idx == 3:
        return bm[3] / work_total >= 0.15
    if bin_idx == 4:
        return bm[4] / work_total >= 0.25
    if bin_idx == 5:
        return (bm[5] + bm[6]) / work_total >= 0.50
    if bin_idx == 6:
        return (bm[6] / work_total > 0.30) and ((bm[5] + bm[6]) / work_total > 0.50)
    return False


def workout_power_intensity(workout: dict, all_workouts: list) -> Optional[float]:
    """
    Single-workout Power Intensity score using date-appropriate thresholds.

    Intended for the sessions-page quality metric.  Resolves reference watts
    at the workout's own date, derives thresholds, then scores that workout.
    """
    # Local import — avoid a circular dependency with reference_watts, which
    # doesn't import this module today but might in the future.
    from services.reference_watts import get_reference_watts

    d = parse_date(workout.get("date", ""))
    if d == date.min:
        return None
    ref = get_reference_watts(d, all_workouts)
    th = compute_bin_thresholds(ref)
    return power_intensity_score(workout_bin_meters(workout, th))


def bin_bar_svg(
    bin_meters: list,
    width: int = 160,
    height: int = 8,
    is_dark: bool = False,
) -> str:
    """
    Return a ``data:image/svg+xml;base64,…`` URI for a stacked horizontal
    bar showing work-meter fraction in each power zone (bin 0 / Rest excluded).

    colors are taken from ``BIN_COLORS`` (dark or light variant).
    Segments smaller than 2 % of work total are omitted to avoid hairlines.
    """
    work = bin_meters[1:]  # bins 1-6 only (skip Rest)
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
        rects = [
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="#d1d5db"/>'
        ]

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}">' + "".join(rects) + "</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def swatch_svg(color: str, size: int = 12, radius: int = 2) -> str:
    """
    Return a ``data:image/svg+xml;base64,…`` URI for a small filled square
    with rounded corners, suitable for use as a color swatch in legends.

    ``color`` should be any valid SVG fill string (e.g. an rgba() value from
    ``BIN_COLORS``).  ``size`` is the square side-length in pixels.
    Use ``hd.image(src=swatch_svg(color), width=…, height=…)`` instead of
    ``hd.box(background_color=color)`` to stay within HyperDiv's color system.
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
        supply an alternate binning strategy (e.g. time-aware per-workout
        thresholds, or HR-zone binning).

    Returns
    -------
    {
        "weeks":   { "YYYY-Www":  {"bins": [m0, m1, …, m6], "total": float} },
        "months":  { "YYYY-MM":   {"bins": […],               "total": float} },
        "seasons": { "YYYY-YY":   {"bins": […],               "total": float} },
    }
    """
    weeks: dict = {}
    months: dict = {}
    seasons: dict = {}

    _effective_bin_fn = (
        bin_fn if bin_fn is not None else (lambda w: workout_bin_meters(w, thresholds))
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

        wk = _week_key(dt)
        mo = _month_key(dt)
        sea = get_season(date_str)

        for bin_idx, meters in enumerate(_effective_bin_fn(w)):
            if meters > 0:
                _add(weeks, wk, bin_idx, meters)
                _add(months, mo, bin_idx, meters)
                _add(seasons, sea, bin_idx, meters)

    return {"weeks": weeks, "months": months, "seasons": seasons}
