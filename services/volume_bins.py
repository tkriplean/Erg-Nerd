"""
Volume aggregation and Zone-Spread binning for the Workouts charts.

This module replaces the v1 watts-classification scheme with a multi-band
duration-saturation classification that consumes the per-workout
``_zone_time_fractions`` field (set by
:func:`components.add_metrics.add_metrics`).

The seven-bin layout is preserved for downstream compatibility — bin 0 is
Rest (interval rest distance) and bins 1–6 are the six duration bands of
the multi-band intensity model, ordered shortest → longest:

    1  Sprint     20-second EMA band     (red)
    2  Anaerobic  90-second EMA band     (orange)
    3  VO2max     5-minute EMA band      (yellow)
    4  Threshold  20-minute EMA band     (green)
    5  Tempo      60-minute EMA band     (blue)
    6  Endurance  2-hour EMA band        (light blue)

A workout's volume is distributed across bins 1–6 by its
``_zone_time_fractions``: bin meters = total_work_meters × fraction.  This
keeps the Volume page's stacked-bar shape but gives each bar a meaningful
duration-band semantics (a workout that spent most of its work-seconds at
watts closest to ``RW_20min`` shows up as Threshold-coloured volume).

Exported:
    BIN_NAMES                   — ordered display names (index 0 = Rest)
    BIN_COLORS                  — list of (dark_rgba, light_rgba) per bin
    N_BINS                      — number of bins (7)
    Z1_BINS / Z2_BINS / Z3_BINS — three-zone groupings under the new names
    POWER_ZONE_DEFINITION_TEXT  — one-line human definition per bin index
    POWER_ZONE_FILTER_TEXT      — one-line description of each bin's
                                  filter-pass threshold
    BAND_TO_BIN                 — mapping from ZONE_BANDS_S seconds → bin idx
    workout_zone_meters()       — per-bin meter counts for one workout, derived
                                  from ``_zone_time_fractions`` × distance
    power_bin_passes()          — true if a workout's zone fractions clear
                                  the filter threshold for that bin
    swatch_svg()                — data-URI SVG color swatch for legends
    aggregate_workouts()        — group all workouts by week / month / season × bin

    Severity binning (used by the Volume page's Workout Severity mode):

    SEVERITY_BIN_NAMES          — ordered display names for the severity
                                  buckets (Rest / Low / Moderate / High /
                                  Maximal / Unrated)
    workout_severity_bin_meters()  — per-bin meter counts for one workout,
                                     based on its ``_severity`` bucket
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Bin definitions
# ---------------------------------------------------------------------------
# Index 0 = Rest (interval rest distance), 1-6 = duration bands shortest →
# longest.  See the module docstring for the band → bin mapping.

BIN_NAMES = (
    "Rest",
    "Sprint",
    "Anaerobic",
    "VO2max",
    "Threshold",
    "Tempo",
    "Endurance",
)
N_BINS = len(BIN_NAMES)

# (dark_rgba, light_rgba) per bin — indexed identically to BIN_NAMES.  The
# palette is unchanged from the v1 watts-classification scheme so the chart
# legend and existing user mental model carry through.
BIN_COLORS = (
    ("rgba(120,120,120,1)", "rgba(155,155,155,1)"),  # 0 Rest
    ("rgba(215,55,55,1)", "rgba(195,35,35,1)"),  # 1 Sprint     (red)
    ("rgba(225,125,35,1)", "rgba(205,95,15,1)"),  # 2 Anaerobic  (orange)
    ("rgba(205,190,50,1)", "rgba(180,160,15,1)"),  # 3 VO2max     (yellow)
    ("rgba(55,180,80,1)", "rgba(25,150,50,1)"),  # 4 Threshold  (green)
    ("rgba(50,130,220,1)", "rgba(20,105,195,1)"),  # 5 Tempo      (blue)
    ("rgba(115,170,230,1)", "rgba(80,140,205,1)"),  # 6 Endurance  (light blue)
)

# Mapping: ZONE_BANDS_S durations (seconds) → bin index.  Lives here (not in
# erg_stress.py) because volume_bins is the canonical home for bin-indexed
# UI metadata (colours, names).  Imported by the chart builder + legends.
BAND_TO_BIN: dict[int, int] = {
    20: 1,
    90: 2,
    300: 3,
    1200: 4,
    3600: 5,
    7200: 6,
}

# Bin-index → band-seconds, the inverse mapping.  Used to translate the
# closest-RW-band tally back to a bin index for stacked bars.
BIN_TO_BAND: tuple[Optional[int], ...] = (
    None,  # 0 Rest — not a duration band
    20,
    90,
    300,
    1200,
    3600,
    7200,
)


def zone_fractions_to_bin_list(d: Optional[dict]) -> tuple[float]:
    """
    Convert a ``{band_seconds: fraction}`` dict to a seven-element bin list
    aligned with :data:`BIN_NAMES` (index 0 is Rest, always 0.0).

    The JS table plugin's stacked spread bar consumes a uniform-shape list of
    per-bin numeric weights — passing fractions directly works (relative
    widths are computed inside the bar builder).  Returns a list of zeros
    when ``d`` is None or empty.
    """
    bins = [0.0] * N_BINS
    if not d:
        return bins
    for band_s, frac in d.items():
        idx = BAND_TO_BIN.get(int(band_s))
        if idx is not None:
            bins[idx] = float(frac)
    return tuple(bins)


# 3-zone model — maps the six duration-band bins onto the Z1/Z2/Z3 framework
# used in the volume distribution table and intervals tab.  Boundaries shift
# slightly relative to the v1 watts scheme: under duration-band semantics,
# Z3 covers the three short, supra-threshold bands; Z1 covers the two
# long-aerobic bands.
Z3_BINS: frozenset = frozenset({1, 2, 3})  # Sprint + Anaerobic + VO2max
Z2_BINS: frozenset = frozenset({4})  # Threshold
Z1_BINS: frozenset = frozenset({5, 6})  # Tempo + Endurance

# Threshold each bin's time fraction must clear for the filter legend to
# consider that band "present" in a workout.  Uniform 10% across all six
# bands; first-cut value subject to per-band tuning after real-data
# exposure.  Bin 0 (Rest) is not filterable.
ZONE_FILTER_THRESHOLD = 0.10

# One-line definition per bin, indexed by bin index (0 = Rest).  Each band is
# the EMA of power with that time constant; the duration is what you need
# to *sustain* a power level for the band's saturation to fully reflect it.
POWER_ZONE_DEFINITION_TEXT: dict[int, str] = {
    0: "Interval rest distance — not counted toward zone spread.",
    1: "Sprint band — power closest to RW(20 s).  Very short maximal efforts.",
    2: "Anaerobic band — power closest to RW(90 s).  ~1–2 minute supra-threshold efforts.",
    3: "VO2max band — power closest to RW(5 min).  3–8 minute hard efforts.",
    4: "Threshold band — power closest to RW(20 min).  10–30 minute threshold work.",
    5: "Tempo band — power closest to RW(60 min).  Sustained sub-threshold tempo work.",
    6: "Endurance band — power closest to RW(2 h).  Long aerobic / Z2 cruising.",
}

# Filter rule descriptions for chip tooltips.  All six bands share the same
# threshold so the language is uniform.
POWER_ZONE_FILTER_TEXT: dict[int, str] = {
    i: f"Selected: workouts with ≥{int(ZONE_FILTER_THRESHOLD * 100)}% of work time at watts closest to {BIN_NAMES[i]}'s reference watts."
    for i in range(1, N_BINS)
}


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


def workout_zone_meters(workout: dict) -> tuple:
    """
    Return ``[m0, m1, …, m6]`` — meter counts per zone bin for one workout.

    Bin 0 is interval rest distance.  Bins 1–6 are the six duration bands;
    each bin's meters are the workout's total work meters scaled by the
    workout's ``_zone_time_fractions[band]`` value (i.e. the time fraction
    of work-seconds at watts closest to that band's reference watts).

    Returns a zero vector when ``_zone_time_fractions`` is missing — this
    happens for workouts without resolvable reference watts at the
    workout's date.
    """
    bins = _empty_bins()

    if workout["is_interval"]:
        rest_dist = workout.get("rest_distance") or 0
        if rest_dist > 0:
            bins[0] += rest_dist
        # Sum work meters across non-rest intervals.
        intervals = (workout.get("workout") or {}).get("intervals") or []
        work_m = 0.0
        for iv in intervals:
            if (iv.get("type") or "").lower() == "rest":
                continue
            iv_dist = iv.get("distance") or 0
            if iv_dist > 0:
                work_m += iv_dist
        if work_m <= 0:
            work_m = workout.get("distance") or 0
    else:
        work_m = workout.get("distance") or 0

    if work_m <= 0:
        return tuple(bins)

    fractions = workout.get("_zone_time_fractions")
    if not fractions:
        # No zone summary — leave bins 1-6 at zero rather than guessing.
        return tuple(bins)

    for band_s, frac in fractions.items():
        bin_idx = BAND_TO_BIN.get(int(band_s))
        if bin_idx is None:
            continue
        bins[bin_idx] += float(work_m) * float(frac)

    return tuple(bins)


def power_bin_passes(bm: list, bin_idx: int) -> bool:
    """
    Return True if a workout's bin-meters vector has enough volume in
    ``bin_idx`` for the filter legend to consider that zone "present".

    Under the new zone-spread semantics, *all* six bands share a uniform
    threshold (``ZONE_FILTER_THRESHOLD``).  Bin 0 (Rest) is not filterable.
    """
    if bin_idx <= 0 or bin_idx >= N_BINS:
        return False
    work_total = sum(bm[1:])
    if not work_total:
        return False
    return bm[bin_idx] / work_total >= ZONE_FILTER_THRESHOLD


# ---------------------------------------------------------------------------
# Severity binning (used by the Volume page's "Workout Severity" mode)
# ---------------------------------------------------------------------------
# Bin layout (parallel to BIN_NAMES shape so the chart builder can treat them
# identically):
#     0  Rest        interval rest meters (grey, same as zone-spread mode)
#     1  Low         workout._severity == "Low"
#     2  Moderate    workout._severity == "Moderate"
#     3  High        workout._severity == "High"
#     4  Maximal     workout._severity == "Maximal"
#     5  Unrated     no resolvable severity (ESS computation failed)

SEVERITY_BIN_NAMES: list[str] = [
    "Rest",
    "Low",
    "Moderate",
    "High",
    "Maximal",
    "Unrated",
]
N_SEVERITY_BINS = len(SEVERITY_BIN_NAMES)
_SEVERITY_BIN_INDEX: dict[str, int] = {
    "Low": 1,
    "Moderate": 2,
    "High": 3,
    "Maximal": 4,
}


def workout_severity_bin_meters(
    workout: dict, severity_category: Optional[str]
) -> list[float]:
    """
    Return ``[rest_m, low_m, moderate_m, high_m, maximal_m, unrated_m]`` for
    one workout.

    Rest distance follows the same rule as :func:`workout_zone_meters` —
    interval ``rest_distance`` lands in bin 0; non-interval workouts have 0
    rest.  All work meters land in the single bucket matching
    ``severity_category`` (Low/Moderate/High/Maximal), or bin 5 (Unrated) when
    the category is None.
    """
    bins = [0.0] * N_SEVERITY_BINS

    is_interval = workout["is_interval"]

    if is_interval:
        rest_dist = workout.get("rest_distance") or 0
        if rest_dist > 0:
            bins[0] += rest_dist
        # Sum work meters from each non-rest interval.
        intervals = (workout.get("workout") or {}).get("intervals") or []
        work_m = 0.0
        for iv in intervals:
            if (iv.get("type") or "").lower() == "rest":
                continue
            iv_dist = iv.get("distance") or 0
            if iv_dist > 0:
                work_m += iv_dist
        if work_m <= 0:
            # Fall back to top-level distance if intervals didn't itemise work.
            work_m = workout.get("distance") or 0
    else:
        work_m = workout.get("distance") or 0

    if work_m > 0:
        idx = _SEVERITY_BIN_INDEX.get(severity_category or "", 5)
        bins[idx] += work_m

    return bins


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
    *,
    bin_fn=None,
) -> dict:
    """
    Aggregate meter counts per (week / month / season) × bin.

    Parameters
    ----------
    all_workouts:
        Full workout list from concept2.get_all_results().
    bin_fn:
        Optional callable(workout) → list[float].  When provided, replaces
        the default ``workout_zone_meters(w)`` call, allowing callers to
        supply an alternate binning strategy (e.g. quality-bin or HR-zone
        binning).

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

    _effective_bin_fn = bin_fn if bin_fn is not None else workout_zone_meters

    def _add(bucket: dict, key: str, bin_idx: int, meters: float) -> None:
        if key not in bucket:
            bucket[key] = {"bins": _empty_bins(), "total": 0.0}
        bucket[key]["bins"][bin_idx] += meters
        bucket[key]["total"] += meters

    for w in all_workouts:
        dt = w["date_dt"]
        if dt == date.min:
            continue

        wk = _week_key(dt)
        mo = _month_key(dt)
        sea = w["season"]

        for bin_idx, meters in enumerate(_effective_bin_fn(w)):
            if meters > 0:
                _add(weeks, wk, bin_idx, meters)
                _add(months, mo, bin_idx, meters)
                _add(seasons, sea, bin_idx, meters)

    return {"weeks": weeks, "months": months, "seasons": seasons}
