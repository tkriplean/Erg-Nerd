"""
services/data_integrity.py

Pure-Python normalization pass applied to workout dicts at fetch/store time.

Concept2 occasionally returns workouts with corrupted data: heart-rate monitor
dropouts that produce misleading aggregates, electrical artifacts above
physiological maxima, empty stub splits, and individual splits whose
distance/pace/watts are clearly wrong (e.g. a 4:00 / 1,193 m row whose 2nd
split reports 1:00 / 600 m / 2800 W).  Downstream consumers should be able to
trust whatever sits in localStorage, so every workout passes through this
normalizer before being persisted.

Public API
    normalize_workout(workout, max_hr=None)        -> dict
    normalize_workouts_dict(workouts_dict, max_hr=None) -> dict

Rules
    1. Drop HR data when coverage is partial — any split (or any work
       interval) has valid HR while at least one peer doesn't.
    2. Drop HR data when the max observed HR exceeds 1.05 × profile max_hr.
    3. Drop empty splits / work intervals (distance == 0 and time == 0) —
       device stubs with no real data; len(splits) is the rep count
       consumers use.  Rest intervals with zero time AND zero rest_time AND
       zero distance are also dropped as fully-empty stubs.
    4. Repair corrupt splits and work intervals, gated on either a sum
       discrepancy against the workout totals or a stroke-rate anomaly
       (one unit reports SPM == 0 / missing while peers report SPM > 0).
       The discrepancy gate keeps legitimately fast sprint splits in
       interval workouts from being falsely "repaired".  Outliers are
       units whose implied distance is >25 % off the time-proportional
       expected distance, whose pace is faster than 1:00/500m, or whose
       SPM is zero amid valid peers.

The pass is idempotent — running it twice produces the same result, so it is
safe to apply over the entire workouts dict on every sync.
"""

from __future__ import annotations

from services.heartrate_utils import _extract_hr


# ---------------------------------------------------------------------------
# Splits / intervals
# ---------------------------------------------------------------------------

# Tolerance used by the discrepancy gate.  Below this, sum(units) is
# considered to line up with the workout totals and no repair runs.
_DIST_TOL_FRAC = 0.05
_DIST_TOL_ABS_M = 50
_TIME_TOL_FRAC = 0.05
_TIME_TOL_ABS_TENTHS = 30

# Outlier criteria once the gate has tripped.
_OUTLIER_DIST_FRAC = 0.25
_IMPLAUSIBLE_PACE_TENTHS = 60  # 1:00 / 500 m — well below world record


def _spm_of(unit: dict) -> int:
    """Stroke-rate accessor that handles both ``spm`` and ``stroke_rate``
    keys (Concept2 uses both depending on the endpoint)."""
    return unit.get("spm") or unit.get("stroke_rate") or 0


def _replace_workout_field(workout: dict, key: str, new_value: list) -> dict:
    out = dict(workout)
    new_wo = dict(workout.get("workout") or {})
    new_wo[key] = new_value
    out["workout"] = new_wo
    return out


def _detect_and_repair_units(
    units: list[dict], total_dist: float, total_time: float
) -> tuple[list[dict], bool]:
    """
    Apply the discrepancy / SPM gate and per-unit outlier repair to a list
    of work units (splits or work intervals).  Returns ``(new_units,
    repaired_any)``.  Always returns shallow copies so callers can swap
    them in without mutating their input.
    """
    new_units = [dict(u) for u in units]
    if not new_units or total_dist <= 0 or total_time <= 0:
        return new_units, False

    sum_dist = sum(u.get("distance") or 0 for u in new_units)
    sum_time = sum(u.get("time") or 0 for u in new_units)
    dist_tol = max(_DIST_TOL_ABS_M, int(total_dist * _DIST_TOL_FRAC))
    time_tol = max(_TIME_TOL_ABS_TENTHS, int(total_time * _TIME_TOL_FRAC))
    sum_discrepancy = (
        abs(sum_dist - total_dist) > dist_tol
        or abs(sum_time - total_time) > time_tol
    )

    peer_spm_present = any(_spm_of(u) > 0 for u in new_units)
    spm_anomaly_present = peer_spm_present and any(
        not _spm_of(u) for u in new_units
    )

    if not sum_discrepancy and not spm_anomaly_present:
        return new_units, False

    repaired_any = False
    for u in new_units:
        d = u.get("distance") or 0
        t = u.get("time") or 0
        if t <= 0:
            continue
        expected = t * total_dist / total_time
        if expected <= 0:
            continue
        sp_pace = (t * 500 / d) if d > 0 else 0
        spm_zero = peer_spm_present and not _spm_of(u)
        is_outlier = (
            d <= 0
            or abs(d - expected) / expected > _OUTLIER_DIST_FRAC
            or (sp_pace and sp_pace < _IMPLAUSIBLE_PACE_TENTHS)
            or spm_zero
        )
        if not is_outlier:
            continue
        new_d = max(1, round(expected))
        u["distance"] = new_d
        u["pace"] = round(t * 500 / new_d)
        u.pop("watts", None)
        u.pop("calories_hour", None)
        u.pop("spm", None)
        u.pop("stroke_rate", None)
        repaired_any = True

    return new_units, repaired_any


def _repair_splits(workout: dict) -> dict:
    wo = workout.get("workout") or {}
    splits = wo.get("splits") or []
    if not splits:
        return workout

    # Rule 3 — drop empty stubs first so they don't skew the gate math.
    nonempty = [
        s for s in splits if (s.get("distance") or 0) > 0 or (s.get("time") or 0) > 0
    ]
    dropped_any = len(nonempty) < len(splits)

    total_dist = workout.get("distance") or 0
    total_time = workout.get("time") or 0

    new_splits, repaired_any = _detect_and_repair_units(
        nonempty, total_dist, total_time
    )

    if repaired_any or dropped_any:
        return _replace_workout_field(workout, "splits", new_splits)
    return workout


def _repair_intervals(workout: dict) -> dict:
    """
    Apply Rules 3 + 4 to ``workout.intervals``.  Rest intervals are
    preserved at their original positions (their ``distance`` is naturally
    zero and not subject to repair); only work intervals contribute to the
    sum-discrepancy gate and only they can be flagged as outliers.
    """
    wo = workout.get("workout") or {}
    intervals = wo.get("intervals") or []
    if not intervals:
        return workout

    # Drop fully-empty stubs.  For work intervals: no distance and no time.
    # For rest intervals: no distance, no time, and no rest_time either —
    # a normal rest carries time and/or rest_time even when distance is 0.
    cleaned = []
    dropped_any = False
    for iv in intervals:
        is_rest = (iv.get("type") or "").lower() == "rest"
        d = iv.get("distance") or 0
        t = iv.get("time") or 0
        if is_rest:
            rt = iv.get("rest_time") or 0
            if d <= 0 and t <= 0 and rt <= 0:
                dropped_any = True
                continue
        else:
            if d <= 0 and t <= 0:
                dropped_any = True
                continue
        cleaned.append(iv)

    work_indices = [
        i for i, iv in enumerate(cleaned)
        if (iv.get("type") or "").lower() != "rest"
    ]
    work_ivs = [cleaned[i] for i in work_indices]

    total_dist = workout.get("distance") or 0
    total_time = workout.get("time") or 0

    repaired_work, repaired_any = _detect_and_repair_units(
        work_ivs, total_dist, total_time
    )

    if not (repaired_any or dropped_any):
        return workout

    new_intervals = [dict(iv) for iv in cleaned]
    for idx, repaired in zip(work_indices, repaired_work):
        new_intervals[idx] = repaired
    return _replace_workout_field(workout, "intervals", new_intervals)


# ---------------------------------------------------------------------------
# Heart rate
# ---------------------------------------------------------------------------

_HR_OVER_MAX_FACTOR = 1.05


def _max_observed_hr(workout: dict) -> int | None:
    vals: list[int] = []

    def _collect(hr: dict | None) -> None:
        if not isinstance(hr, dict):
            return
        for key in ("max", "average"):
            v = hr.get(key)
            if isinstance(v, (int, float)) and v > 0:
                vals.append(int(v))

    _collect(workout.get("heart_rate"))
    wo = workout.get("workout") or {}
    for s in wo.get("splits") or []:
        _collect(s.get("heart_rate"))
    for iv in wo.get("intervals") or []:
        _collect(iv.get("heart_rate"))
    return max(vals) if vals else None


def _strip_hr(workout: dict) -> dict:
    out = {k: v for k, v in workout.items() if k != "heart_rate"}
    wo = out.get("workout")
    if isinstance(wo, dict):
        new_wo = dict(wo)
        if "splits" in new_wo:
            new_wo["splits"] = [
                {k: v for k, v in s.items() if k != "heart_rate"}
                for s in new_wo["splits"]
            ]
        if "intervals" in new_wo:
            new_wo["intervals"] = [
                {k: v for k, v in iv.items() if k != "heart_rate"}
                for iv in new_wo["intervals"]
            ]
        out["workout"] = new_wo
    return out


def _has_partial_hr_coverage(workout: dict) -> bool:
    wo = workout.get("workout") or {}
    splits = wo.get("splits") or []
    if splits:
        with_hr = [s for s in splits if _extract_hr(s.get("heart_rate"))]
        if with_hr and len(with_hr) < len(splits):
            return True
        # If splits exist and any carry HR, that signal is authoritative —
        # do not also evaluate intervals (avoids double-counting in the rare
        # case both lists are present).
        if with_hr:
            return False

    intervals = wo.get("intervals") or []
    if intervals:
        work_ivs = [iv for iv in intervals if (iv.get("type") or "").lower() != "rest"]
        if work_ivs:
            with_hr = [iv for iv in work_ivs if _extract_hr(iv.get("heart_rate"))]
            if with_hr and len(with_hr) < len(work_ivs):
                return True
    return False


def _normalize_hr(workout: dict, max_hr: int | None) -> dict:
    if _has_partial_hr_coverage(workout):
        return _strip_hr(workout)

    if max_hr and max_hr > 0:
        observed = _max_observed_hr(workout)
        if observed is not None and observed > max_hr * _HR_OVER_MAX_FACTOR:
            return _strip_hr(workout)

    return workout


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_workout(workout: dict, max_hr: int | None = None) -> dict:
    """Apply all integrity rules to a single workout dict.  Idempotent."""
    if not isinstance(workout, dict):
        return workout
    w = _repair_splits(workout)
    w = _repair_intervals(w)
    w = _normalize_hr(w, max_hr)
    return w


def normalize_workouts_dict(workouts_dict: dict, max_hr: int | None = None) -> dict:
    """Return a new ``{str(id): workout}`` dict with every value normalized."""
    return {k: normalize_workout(v, max_hr=max_hr) for k, v in workouts_dict.items()}
