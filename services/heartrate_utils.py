"""
services/heartrate_utils.py

Heart rate validation, zone classification, and meter binning for the
volume chart's HR mode.

Zone model: % of HRmax (simple 5-zone).
Bins mirror the 7-slot shape used by services/volume_bins.py so that
aggregate_workouts() can accept workout_hr_meters() as a drop-in bin_fn.

Exported:
    HR_ZONE_NAMES               — 7-element list matching volume_bins.BIN_NAMES shape
    HR_ZONE_COLORS              — 7-element list of (dark_rgba, light_rgba) pairs
    HR_Z1_BINS                  — frozenset of bin indices for easy zone (4, 5)
    HR_Z2_BINS                  — frozenset of bin indices for tempo zone (3)
    HR_Z3_BINS                  — frozenset of bin indices for hard zone (1, 2)
    HR_INTENSITY_WEIGHTS        — 7-element per-bin weights for the 0–100 score
    HR_ZONE_DEFINITION_TEXT     — one-line human definition per bin index
    HR_ZONE_FILTER_TEXT         — one-line description of the filter threshold

    is_valid_hr(val, max_hr)        → bool
    estimate_max_hr(workouts)       → int | None
    resolve_max_hr(profile, workouts) → (int | None, bool)  # (max_hr, is_estimated)
    hr_zone_idx(avg_hr, max_hr)     → int 1–5
    workout_hr_meters(workout, max_hr) → list[float]  (7 bins, same shape as workout_bin_meters)
    hr_intensity_score(hr_bin_meters) → float | None  (0–100 weighted average)
    hr_bin_passes(hr_bin_meters, idx) → bool  (filter-threshold test)
    hr_coverage(workouts)           → (int, int)  # (with_hr, total)

Bin layout (matches pace zone index convention):
    0  Rest          interval rest meters (grey)
    1  Z5 Max        > 90 % HRmax         (red)
    2  Z4 Threshold  80–90 %              (orange)
    3  Z3 Tempo      70–80 %              (yellow-green)
    4  Z2 Aerobic    60–70 %              (teal-blue)
    5  Z1 Recovery   < 60 %               (light blue)
    6  No HR         missing / invalid    (neutral grey)

Stacked-bar draw order (bottom → top): [6, 5, 4, 3, 2, 1, 0]
Same as pace mode — No HR sits at the visual bottom, Z5 near the top, Rest
as a thin grey cap.  This is the value to pass as draw_order= to
build_volume_chart_config().

Outlier detection (initial scope):
    • hr ≤ 0 or missing          → invalid (monitor not worn)
    • hr < 40 or hr > 220         → physiologically impossible
    • hr > max_hr × 1.05          → artifact above known maximum
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Bin metadata
# ---------------------------------------------------------------------------

HR_ZONE_NAMES: list[str] = [
    "Rest",  # 0
    "Z5 Max",  # 1 — > 90 % HRmax
    "Z4 Threshold",  # 2 — 80–90 %
    "Z3 Tempo",  # 3 — 70–80 %
    "Z2 Aerobic",  # 4 — 60–70 %
    "Z1 Recovery",  # 5 — < 60 %
    "No HR",  # 6 — no valid HR data
]

# (dark_rgba, light_rgba) per bin — intentionally parallel to BIN_COLORS so
# the chart builder can treat them identically.
HR_ZONE_COLORS: list[tuple[str, str]] = [
    ("rgba(120,120,120,0.65)", "rgba(155,155,155,0.65)"),  # 0 Rest (same as pace)
    ("rgba(215,55,55,0.85)", "rgba(195,35,35,0.85)"),  # 1 Z5 Max (red)
    ("rgba(225,125,35,0.85)", "rgba(205,95,15,0.85)"),  # 2 Z4 Threshold (orange)
    ("rgba(205,190,50,0.85)", "rgba(180,160,15,0.85)"),  # 3 Z3 Tempo (yellow-green)
    ("rgba(50,130,220,0.85)", "rgba(20,105,195,0.85)"),  # 4 Z2 Aerobic (blue)
    ("rgba(115,170,230,0.75)", "rgba(80,140,205,0.75)"),  # 5 Z1 Recovery (light blue)
    ("rgba(195,195,195,0.45)", "rgba(210,210,210,0.55)"),  # 6 No HR (neutral grey)
]

# Draw order for stacked bar (bottom → top): No HR, Z1, Z2, Z3, Z4, Z5, Rest
HR_ZONE_DRAW_ORDER: list[int] = [1, 2, 3, 4, 5, 0, 6]

# 3-zone model for the distribution table
HR_Z1_BINS: frozenset = frozenset({4, 5})  # Z2 Aerobic + Z1 Recovery  (easy)
HR_Z2_BINS: frozenset = frozenset({3})  # Z3 Tempo                  (moderate)
HR_Z3_BINS: frozenset = frozenset({1, 2})  # Z5 Max + Z4 Threshold     (hard)

# Linear weights per bin index for the 0–100 HR-intensity score.
# Score = Σ (meters_in_bin / meaningful_meters × weight); Rest and No-HR
# are excluded from both the weights and the denominator.
HR_INTENSITY_WEIGHTS: list[int] = [0, 100, 75, 50, 25, 0, 0]

# One-line definition per bin index.  Consumed by chip tooltips.
HR_ZONE_DEFINITION_TEXT: dict[int, str] = {
    0: "Interval rest — not counted toward intensity.",
    1: "Above 90% of your max HR.",
    2: "80–90% of your max HR.",
    3: "70–80% of your max HR.",
    4: "60–70% of your max HR.",
    5: "Below 60% of your max HR.",
    6: "HR data unavailable for these meters.",
}

# Filter threshold description for each bin (fraction of HR-classified
# meters, excluding Rest and No HR).  Used by chip tooltips.
HR_ZONE_FILTER_TEXT: dict[int, str] = {
    1: "Selected: workouts with ≥5% of HR-classified meters in Z5 Max.",
    2: "Selected: workouts with ≥10% of HR-classified meters in Z4 Threshold.",
    3: "Selected: workouts with ≥20% of HR-classified meters in Z3 Tempo.",
    4: "Selected: workouts with ≥40% of HR-classified meters in Z2 Aerobic.",
    5: "Selected: workouts with ≥40% of HR-classified meters in Z1 Recovery.",
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def is_valid_hr(val, max_hr: Optional[int] = None) -> bool:
    """
    Return True if *val* is a plausible heart rate reading.

    Checks:
      • present and positive (> 0)
      • within physiological range 40–220 bpm
      • not more than 5 % above *max_hr* (if provided) — flags artifact spikes
    """
    if val is None or val <= 0:
        return False
    if val < 40 or val > 220:
        return False
    if max_hr and val > max_hr * 1.05:
        return False
    return True


def _extract_hr(hr_dict) -> Optional[int]:
    """Return hr_dict['average'] if valid, else None."""
    if not hr_dict:
        return None
    val = hr_dict.get("average")
    if val and val > 0:
        return int(val)
    return None


# ---------------------------------------------------------------------------
# Max HR resolution
# ---------------------------------------------------------------------------


def estimate_max_hr(workouts: list) -> Optional[int]:
    """
    Estimate max HR as the 98th-percentile of valid HR readings across all
    workouts.  Considers top-level workout HR, per-split HR, and per-interval
    HR.  Returns None if fewer than 10 valid readings are found.
    """
    vals: list[int] = []

    for w in workouts:
        # Top-level
        top = _extract_hr(w.get("heart_rate"))
        if top and is_valid_hr(top):
            vals.append(top)

        workout_data = w.get("workout") or {}

        # Per-split
        for sp in workout_data.get("splits") or []:
            v = _extract_hr(sp.get("heart_rate"))
            if v and is_valid_hr(v):
                vals.append(v)

        # Per-interval
        for iv in workout_data.get("intervals") or []:
            v = _extract_hr(iv.get("heart_rate"))
            if v and is_valid_hr(v):
                vals.append(v)

    if len(vals) < 10:
        return None

    vals.sort()
    idx = max(0, int(len(vals) * 0.98) - 1)
    return vals[idx]


def resolve_max_hr(
    profile: dict,
    workouts: list,
) -> tuple[Optional[int], bool]:
    """
    Return (max_hr, is_estimated).

    Explicit profile value wins; falls back to estimate_max_hr().
    is_estimated is False when the profile value is used.
    """
    explicit = profile.get("max_heart_rate")
    if explicit and is_valid_hr(explicit):
        return int(explicit), False
    return estimate_max_hr(workouts), True


# ---------------------------------------------------------------------------
# Zone classification
# ---------------------------------------------------------------------------


def hr_zone_idx(avg_hr: int, max_hr: int) -> int:
    """
    Map a valid average HR to a bin index 1–5.

    Assumes avg_hr has already been validated with is_valid_hr().

    Zones (% of HRmax):
        > 90 %  → 1  Z5 Max
        80–90 % → 2  Z4 Threshold
        70–80 % → 3  Z3 Tempo
        60–70 % → 4  Z2 Aerobic
        < 60 %  → 5  Z1 Recovery
    """
    pct = avg_hr / max_hr
    if pct > 0.90:
        return 1
    if pct > 0.80:
        return 2
    if pct > 0.70:
        return 3
    if pct > 0.60:
        return 4
    return 5


# ---------------------------------------------------------------------------
# Workout binning
# ---------------------------------------------------------------------------


def _empty_bins() -> list[float]:
    return [0.0] * len(HR_ZONE_NAMES)


def workout_hr_meters(workout: dict, max_hr: int) -> list[float]:
    """
    Return a 7-element HR-zone bin vector for a single workout.

    Shape matches volume_bins.workout_bin_meters() so it can be passed as
    bin_fn to aggregate_workouts().

    Resolution priority:

    1. Per-split HR (workout.splits[].heart_rate.average):
       Each split's meters are classified by that split's average HR.
       A split without valid HR contributes its meters to bin 6 (No HR).

    2. Per-interval HR (workout.intervals[].heart_rate.average):
       Each work-interval's meters are classified by its own average HR.
       Explicit rest intervals (type == "rest") → bin 0 (Rest).
       Intervals without valid HR → bin 6 (No HR).

    3. Top-level HR (workout.heart_rate.average):
       All work meters → one HR zone bin.

    4. No HR anywhere → all meters to bin 6 (No HR).

    Interval rest meters always go to bin 0 (Rest) regardless of HR data.
    """
    bins = _empty_bins()
    workout_data = workout.get("workout") or {}
    total_dist = workout.get("distance") or 0

    # ── Case 1: per-split HR ─────────────────────────────────────────────────
    splits = workout_data.get("splits") or []
    if splits and any(_extract_hr(s.get("heart_rate")) for s in splits):
        for sp in splits:
            dist = sp.get("distance") or 0
            if not dist:
                continue
            hr_val = _extract_hr(sp.get("heart_rate"))
            if hr_val and is_valid_hr(hr_val, max_hr):
                bins[hr_zone_idx(hr_val, max_hr)] += dist
            else:
                bins[6] += dist  # No HR
        return bins

    # ── Case 2: per-interval HR ──────────────────────────────────────────────
    intervals = workout_data.get("intervals") or []
    if intervals and any(_extract_hr(iv.get("heart_rate")) for iv in intervals):
        for iv in intervals:
            iv_type = (iv.get("type") or "").lower()
            dist = iv.get("distance") or 0
            if not dist:
                continue
            if iv_type == "rest":
                bins[0] += dist  # Rest
                continue
            hr_val = _extract_hr(iv.get("heart_rate"))
            if hr_val and is_valid_hr(hr_val, max_hr):
                bins[hr_zone_idx(hr_val, max_hr)] += dist
            else:
                bins[6] += dist  # No HR
        return bins

    # ── Case 3: top-level HR ─────────────────────────────────────────────────
    top_hr = _extract_hr(workout.get("heart_rate"))
    if top_hr and is_valid_hr(top_hr, max_hr):
        # For interval workouts, put rest meters in bin 0 and work in HR zone.
        # For steady-state, total_dist is already work-only (no rest).
        rest_dist = 0.0
        if intervals:
            rest_ivs = [
                iv for iv in intervals if (iv.get("type") or "").lower() == "rest"
            ]
            rest_dist = sum(iv.get("distance") or 0 for iv in rest_ivs)
            # Also capture rest_distance fields on work intervals
            rest_dist += sum(
                (iv.get("rest_distance") or 0)
                for iv in intervals
                if (iv.get("type") or "").lower() != "rest"
            )
        work_dist = max(0.0, total_dist - rest_dist)
        bins[0] += rest_dist
        bins[hr_zone_idx(top_hr, max_hr)] += work_dist
        return bins

    # ── Case 4: no HR data ───────────────────────────────────────────────────
    # Keep interval rest in bin 0; work meters → No HR (bin 6).
    rest_dist = 0.0
    if intervals:
        rest_ivs = [iv for iv in intervals if (iv.get("type") or "").lower() == "rest"]
        rest_dist = sum(iv.get("distance") or 0 for iv in rest_ivs)
        rest_dist += sum(
            (iv.get("rest_distance") or 0)
            for iv in intervals
            if (iv.get("type") or "").lower() != "rest"
        )
    bins[0] += rest_dist
    bins[6] += max(0.0, total_dist - rest_dist)
    return bins


# ---------------------------------------------------------------------------
# Intensity score + filter-threshold helpers
# ---------------------------------------------------------------------------


def hr_intensity_score(hr_bin_meters: Optional[list]) -> Optional[float]:
    """
    Return a 0–100 weighted-average HR-intensity score for a workout.

    Bins 0 (Rest) and 6 (No HR) are excluded from both the weights and the
    denominator so a workout with partial HR coverage is scored on the
    meters it could classify.  Returns None when no HR-classified meters
    exist — callers render that as "—" and sort it last.
    """
    if hr_bin_meters is None:
        return None
    classified = hr_bin_meters[1:6]  # bins 1–5 only
    total = sum(classified)
    if total <= 0:
        return None
    weights = HR_INTENSITY_WEIGHTS[1:6]
    return sum((m / total) * w for m, w in zip(classified, weights))


def hr_bin_passes(hr_bin_meters: Optional[list], bin_idx: int) -> bool:
    """
    Return True if a workout has enough HR-classified meters in ``bin_idx``
    for the Intervals-page HR filter to consider that zone "present".

    Thresholds mirror the hardness ordering of the pace thresholds.
    Bin 6 (No HR) is not filterable.
    """
    if hr_bin_meters is None:
        return False
    classified = sum(hr_bin_meters[1:6])
    if classified <= 0:
        return False
    if bin_idx == 1:
        return hr_bin_meters[1] / classified >= 0.05
    if bin_idx == 2:
        return hr_bin_meters[2] / classified >= 0.10
    if bin_idx == 3:
        return hr_bin_meters[3] / classified >= 0.20
    if bin_idx == 4:
        return hr_bin_meters[4] / classified >= 0.40
    if bin_idx == 5:
        return hr_bin_meters[5] / classified >= 0.40
    return False


# ---------------------------------------------------------------------------
# Coverage helper
# ---------------------------------------------------------------------------


def hr_coverage(workouts: list) -> tuple[int, int]:
    """
    Return (workouts_with_valid_hr, total_workouts).

    A workout "has HR" if its top-level heart_rate.average is valid (> 0 and
    within physiological range).  Per-split / per-interval HR is not checked
    here — top-level presence is the cheapest reliable signal.
    """
    total = len(workouts)
    with_hr = sum(1 for w in workouts if is_valid_hr(_extract_hr(w.get("heart_rate"))))
    return with_hr, total
