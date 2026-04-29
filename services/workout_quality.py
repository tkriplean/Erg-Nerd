"""
Workout-quality metric.

Rates a workout (interval or steady-state) against a date-aware reference-watts
curve.  Each split/interval contributes "quality energy":

    energy = (split_watts / category_ref_watts) * split_watts * work_seconds

For interval workouts, energy is additionally discounted by the ratio of
preceding rest to work + rest — so the same hard reps rated against a generous
rest count for less than against a short rest.

Per-category energy is normalized by a reference target
(``category_ref_watts * category_ref_distance_m``) and summed; the resulting
scalar is bucketed into Low / Medium / High.

The algorithm is deliberately simple and pure-Python; it reuses the existing
watts-based power bin thresholds (see :mod:`services.volume_bins`), so quality
categories line up with the Power Spread zones shown elsewhere in the app.

Reference events per category:

    "Fast"          →  1k watts           · 1 km
    "2k"            →  2k watts           · 2 km
    "5k"            →  5k watts           · 5 km
    "Threshold"     →  60min watts        · 10 km
    "Fast Aerobic"  →  marathon watts     · 13 km
    "Slow Aerobic"  →  0.9 × marathon W   · 15 km

These are intentionally tunable — adjust ``QUALITY_REFERENCE_DISTANCES_M`` (and
the 0.5/0.75 bucket thresholds below) as real-world usage informs them.
"""

from __future__ import annotations

from typing import Optional

from services.rowing_utils import compute_watts
from services.volume_bins import BIN_NAMES, classify_watts


# Category → reference-watts cat_key.  Keys map 1:1 to BIN_NAMES[1:].
QUALITY_REFERENCE_EVENTS: dict[str, tuple] = {
    "Fast": ("dist", 1000),
    "2k": ("dist", 2000),
    "5k": ("dist", 5000),
    "Threshold": ("time", 36000),  # 1 hour, in tenths of a second
    "Fast Aerobic": ("dist", 42195),
    "Slow Aerobic": ("dist", 42195),  # 0.9× the marathon watts — see below
}

# Category → "expected quality distance" in meters.  Used as the denominator
# scaling when normalizing accumulated energy into a per-category quality
# contribution.
QUALITY_REFERENCE_DISTANCES_M: dict[str, float] = {
    "Fast": 1_000.0,
    "2k": 2_000.0,
    "5k": 5_000.0,
    "Threshold": 10_000.0,
    "Fast Aerobic": 13_000.0,
    "Slow Aerobic": 15_000.0,
}

# Slow Aerobic is anchored to 90% of marathon watts (the marathon watts the
# category shares is reduced by this factor when computing reference_pbs).
SLOW_AEROBIC_WATTS_FACTOR = 0.9

# Interval-only: the fraction of a split's energy that is rest-sensitive.
# A work interval with zero preceding rest keeps (1 - factor) of its energy;
# a rest-dominated interval pulls all the way down to (1 - factor) * energy
# plus an additional factor × (rest / (rest + work)) scaling.
WORK_REST_PENALTY_FACTOR = 0.25

# Category buckets.  Deliberately lenient — the intent is a spread that makes
# typical "junk miles" land Low, solid sessions Medium, and true PR-quality
# work High.  Reassess once real usage lands.
_LOW_MAX = 0.5
_MEDIUM_MAX = 0.75
_HIGH_MAX = 1

# Public mirror of the bucket boundaries above, so UI code can render the
# same numbers without duplicating literals.  Each entry: (label, upper bound
# inclusive — Ultra is open-ended).
QUALITY_THRESHOLDS: list[tuple[str, float | None]] = [
    ("Low", _LOW_MAX),
    ("Medium", _MEDIUM_MAX),
    ("High", _HIGH_MAX),
    ("Ultra", None),
]

QUALITY_ORDER: dict[str, int] = {"Low": 0, "Medium": 1, "High": 2, "Ultra": 3}


# Per-category visual style for table cells, legend chips, and chart coloring.
# bg is an (R, G, B, A) tuple; UI helpers convert to the appropriate CSS form.
QUALITY_STYLE: dict[str, dict] = {
    "Low": {
        "label": "Low",
        "bg": (215, 55, 55, 0.85),  # red — same family as BIN_COLORS[1]
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "Medium": {
        "label": "Medium",
        "bg": (225, 125, 35, 0.85),  # orange
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "High": {
        "label": "High",
        "bg": (25, 150, 50, 0.90),  # green
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "Ultra": {
        "label": "Ultra",
        "bg": (25, 150, 50, 1),  # green, full opacity
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
}


# One-line category descriptions and filter rules — used by legend chip tooltips.
QUALITY_DEFINITION_TEXT: dict[str, str] = {
    "Low": "Quality score below 0.50 — junk-mile / easy-day session.",
    "Medium": "Quality score 0.50–0.75 — solid moderate session.",
    "High": "Quality score 0.75–1.0 — sharp session with most splits at category-reference watts.",
    "Ultra": "Quality score > 1.0 — rare, top-end session at or beyond reference power.",
}

QUALITY_FILTER_TEXT: dict[str, str] = {
    "Low": "Selected: workouts whose Quality score is Low.",
    "Medium": "Selected: workouts whose Quality score is Medium.",
    "High": "Selected: workouts whose Quality score is High.",
    "Ultra": "Selected: workouts whose Quality score is Ultra.",
}


def quality_bg_css(category: str) -> str:
    """Return an ``rgba(...)`` CSS color string for the given quality bucket."""
    r, g, b, a = QUALITY_STYLE[category]["bg"]
    return f"rgba({r},{g},{b},{a})"


def _category_from_bin(bin_idx: int) -> Optional[str]:
    """Map a classify_watts() bin index (1–6) to a category name.

    Returns None for bin 0 (Rest) or any out-of-range index.
    """
    if 1 <= bin_idx < len(BIN_NAMES):
        return BIN_NAMES[bin_idx]
    return None


def _build_reference_pbs(ref_watts: dict) -> Optional[dict[str, tuple[float, float]]]:
    """Return ``{category: (ref_watts, ref_distance_m)}`` or None.

    Returns None if any required anchor event is missing from ``ref_watts`` —
    matches the fail-open policy in :func:`services.volume_bins.compute_bin_thresholds`.
    """
    result: dict[str, tuple[float, float]] = {}
    for cat, cat_key in QUALITY_REFERENCE_EVENTS.items():
        w = ref_watts.get(cat_key)
        if w is None:
            return None
        if cat == "Slow Aerobic":
            w = w * SLOW_AEROBIC_WATTS_FACTOR
        result[cat] = (w, QUALITY_REFERENCE_DISTANCES_M[cat])
    return result


def _bucket(score: float) -> str:
    if score < _LOW_MAX:
        return "Low"
    if score < _MEDIUM_MAX:
        return "Medium"
    if score < _HIGH_MAX:
        return "High"
    return "Ultra"


def _iter_quality_splits(workout: dict, is_interval: bool):
    """Yield (work_seconds, distance_m, preceding_rest_seconds) per scorable split.

    For interval workouts we walk ``workout["workout"]["intervals"]``:
      • Intervals with ``type == "rest"`` are skipped, but their ``time``
        accumulates into the next work interval's preceding rest.
      • The ``rest_time`` field on interval N is the rest that *follows* N
        (Concept2 API convention — see services/interval_utils.py); so we
        carry it forward onto interval N+1's preceding rest budget.
      • The first work interval has zero preceding rest.

    For steady workouts we walk ``workout["workout"]["splits"]``.  No rest.
    """
    nested = workout.get("workout") or {}

    if is_interval:
        ivs = nested.get("intervals") or []
        pending_rest_s = 0.0
        for iv in ivs:
            iv_type = (iv.get("type") or "").lower()
            iv_time_tenths = iv.get("time") or 0
            if iv_type == "rest":
                pending_rest_s += iv_time_tenths / 10.0
                continue
            dist_m = iv.get("distance") or 0
            time_s = iv_time_tenths / 10.0
            if dist_m > 0 and time_s > 0:
                yield time_s, dist_m, pending_rest_s
            # Whatever the work interval's rest_time field is rolls forward
            # as the next work interval's preceding rest.
            pending_rest_s = (iv.get("rest_time") or 0) / 10.0
        return

    splits = nested.get("splits") or []
    for sp in splits:
        dist_m = sp.get("distance") or 0
        time_s = (sp.get("time") or 0) / 10.0
        if dist_m > 0 and time_s > 0:
            yield time_s, dist_m, 0.0


def compute_workout_quality(
    workout: dict,
    ref_watts: Optional[dict],
    thresholds: Optional[dict],
    reference_pbs: Optional[dict] = None,
) -> Optional[dict]:
    """Return the quality result for a single workout, or None if insufficient data.

    Parameters
    ----------
    workout
        Concept2 workout dict (either an interval or steady-state session).
    ref_watts
        The rower's reference watts at the workout's date, keyed by cat_key —
        i.e. the output of :func:`services.reference_watts.get_reference_watts`.
    thresholds
        Watts-based power-bin thresholds at the workout's date, from
        :func:`services.volume_bins.compute_bin_thresholds`.
    reference_pbs
        Optional precomputed per-category ``{cat: (watts, dist_m)}`` dict.
        If omitted, built from ``ref_watts`` here.  Hot-path callers (e.g.
        ``attach_spread_and_quality``) supply the cached value from
        ``make_thresholds_resolver``'s ``reference_pbs_for``.

    Returns
    -------
    dict or None
        ``{"score": float, "category": "Low"|"Medium"|"High",
           "per_category_energy": {cat: joules}}`` — or None if ref_watts /
        thresholds are missing the required anchors, or if the workout has
        no scorable splits.
    """
    if not thresholds:
        return None

    if reference_pbs is None:
        if not ref_watts:
            return None
        reference_pbs = _build_reference_pbs(ref_watts)
    if reference_pbs is None:
        return None

    is_interval = workout["is_interval"]
    per_category_energy: dict[str, float] = {cat: 0.0 for cat in reference_pbs}
    any_scorable = False

    # print(ref_watts)

    for time_s, dist_m, preceding_rest_s in _iter_quality_splits(workout, is_interval):
        pace = time_s / (dist_m / 500.0)
        split_watts = compute_watts(pace)
        cat = _category_from_bin(classify_watts(split_watts, thresholds))

        if cat is None or cat not in reference_pbs:
            continue
        cat_watts, _ = reference_pbs[cat]
        energy = (split_watts / cat_watts) * split_watts * dist_m

        # print(
        #     workout.get("date"),
        #     pace,
        #     split_watts,
        #     (split_watts / cat_watts),
        #     split_watts * time_s,
        #     energy,
        # )

        if is_interval and preceding_rest_s > 0:
            total_s = time_s + preceding_rest_s
            rest_ratio = preceding_rest_s / total_s
            energy *= (1 - WORK_REST_PENALTY_FACTOR) + WORK_REST_PENALTY_FACTOR * (
                1 - rest_ratio
            )

        per_category_energy[cat] += energy
        any_scorable = True

    if not any_scorable:
        return None

    score = 0.0
    for cat, energy in per_category_energy.items():
        cat_watts, cat_dist = reference_pbs[cat]
        denom = cat_watts * cat_dist
        # print(cat, energy, denom, cat_watts, cat_dist)
        if denom > 0:
            score += energy / denom

    return {
        "score": score,
        "category": _bucket(score),
        "per_category_energy": per_category_energy,
    }
