"""
Synthetic workout data generator for UI development.

Exported:
    augment_with_synthetic(real_workouts_dict) → (augmented_dict, sorted_list)

Adds ~28k synthetic workout entries (25 seasons × multiple machine types) to
the real dict returned by concept2_sync, without modifying any real entry.

Synthetic IDs are negative integers — they cannot collide with real Concept2
IDs (which are always positive).  Generation is deterministically seeded
(seed=20240101) so the UI is stable across reloads.

Volume targets
--------------
  Rower:   ~1 000 workouts / synthetic season (seasons not in real data)
  SkiErg:  ~350  workouts / season (all 25 seasons)
  BikeErg: ~150  workouts / season (all 25 seasons)
  ≈ 17 rower seasons × 1 000 + 25 × (350+150) ≈ 29 500 synthetic
  + real (~2 000) ≈ 31 500 total

Pace model
----------
  Base pace comes from the real data's best 2k.  Other distances are
  derived via Paul's Law (K=5).  Older seasons are slowed by 0.5 %/yr
  to simulate a plausible historical improvement arc.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Optional

from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    RANKED_DIST_SET,
    get_season,
    compute_pace,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEED = 20240101

# Machine pace multipliers relative to rower.
_MACHINE_PACE_FACTOR: dict[str, float] = {
    "rower": 1.0,
    "skierg": 1.08,  # ~8% slower
    "bike": 0.72,  # ~28% faster (BikeErg is quicker per 500m)
}

_ROWER_PER_SEASON = 1000
_SKIERG_PER_SEASON = 350
_BIKE_PER_SEASON = 150

# Pace degrades 0.5 % per year going back in time.
_PACE_DEGRADE_PER_YEAR = 0.005

# Paul's Law constant for deriving paces at other distances.
_K = 5.0

# Default 2k pace (sec/500m) when real data has no ranked events.
_DEFAULT_BASE_PACE = 117.0  # ≈ 1:57/500m

# ---------------------------------------------------------------------------
# Workout type distribution
# ---------------------------------------------------------------------------

_WORKOUT_TYPE_WEIGHTS = [
    ("JustRow", 0.45),
    ("FixedDistanceSplits", 0.15),
    ("FixedTimeSplits", 0.10),
    ("FixedDistanceInterval", 0.20),
    ("FixedTimeInterval", 0.10),
]
_WORKOUT_TYPES = [t for t, _ in _WORKOUT_TYPE_WEIGHTS]
_WORKOUT_WEIGHTS = [w for _, w in _WORKOUT_TYPE_WEIGHTS]

# ---------------------------------------------------------------------------
# Distance / time pools
# ---------------------------------------------------------------------------

# Common training distances — ranked distances are included naturally.
_TRAINING_DIST_WEIGHTS = [
    (100, 0.015),
    (500, 0.050),
    (1000, 0.080),
    (1500, 0.040),
    (2000, 0.150),
    (3000, 0.070),
    (4000, 0.040),
    (5000, 0.120),
    (6000, 0.040),
    (7000, 0.030),
    (8000, 0.040),
    (10000, 0.100),
    (12000, 0.020),
    (15000, 0.020),
    (20000, 0.020),
    (21097, 0.020),
    (42195, 0.005),
]
_DIST_VALUES = [d for d, _ in _TRAINING_DIST_WEIGHTS]
_DIST_WEIGHTS = [w for _, w in _TRAINING_DIST_WEIGHTS]

# Timed events (tenths of a second).
_TIMED_EVENT_WEIGHTS = [
    (600, 0.10),  # 1 min
    (2400, 0.20),  # 4 min
    (18000, 0.50),  # 30 min
    (36000, 0.20),  # 60 min
]
_TIMED_VALUES = [t for t, _ in _TIMED_EVENT_WEIGHTS]
_TIMED_WEIGHTS = [w for _, w in _TIMED_EVENT_WEIGHTS]

# ---------------------------------------------------------------------------
# Interval patterns: (work_type, work_value, rest_tenths, default_reps)
# ---------------------------------------------------------------------------

_INTERVAL_PATTERNS = [
    ("distance", 250, 600, 10),  # 10 × 250m / 1 min
    ("distance", 500, 1200, 8),  # 8 × 500m / 2 min
    ("distance", 500, 600, 10),  # 10 × 500m / 1 min
    ("distance", 1000, 1800, 5),  # 5 × 1k / 3 min
    ("distance", 1000, 1200, 4),  # 4 × 1k / 2 min
    ("distance", 2000, 2400, 3),  # 3 × 2k / 4 min
    ("distance", 2000, 3000, 4),  # 4 × 2k / 5 min
    ("time", 600, 600, 8),  # 8 × 1 min / 1 min
    ("time", 600, 600, 10),  # 10 × 1 min / 1 min
    ("time", 1200, 900, 6),  # 6 × 2 min / 1.5 min
    ("time", 2400, 1800, 4),  # 4 × 4 min / 3 min
    ("time", 2400, 1500, 3),  # 3 × 4 min / 2.5 min
]

# ---------------------------------------------------------------------------
# Pace helpers
# ---------------------------------------------------------------------------


def _extract_base_pace(real_workouts_dict: dict) -> float:
    """
    Return the user's best 2k pace (sec/500m) from real data.

    Falls back to Paul's Law projection from the nearest ranked distance
    when no 2k exists, and to _DEFAULT_BASE_PACE when no ranked data exists.
    """
    best: dict[int, float] = {}
    for w in real_workouts_dict.values():
        d = w.get("distance", 0)
        if d not in RANKED_DIST_SET:
            continue
        p = compute_pace(w)
        if p is None:
            continue
        if d not in best or p < best[d]:
            best[d] = p

    if 2000 in best:
        return best[2000]

    if best:
        # Project from nearest ranked distance to 2k via Paul's Law.
        ref_d, ref_p = min(best.items(), key=lambda kv: abs(math.log2(kv[0] / 2000)))
        return ref_p - _K * math.log2(2000 / ref_d)

    return _DEFAULT_BASE_PACE


def _pace_at_dist(base_2k: float, dist_m: float) -> float:
    """Paul's Law: pace at dist_m given a 2k base pace."""
    if dist_m <= 0:
        return base_2k
    return base_2k + _K * math.log2(dist_m / 2000.0)


# ---------------------------------------------------------------------------
# Season / date helpers
# ---------------------------------------------------------------------------


def _season_year(season_str: str) -> int:
    """'2024-25' → 2024"""
    try:
        return int(season_str[:4])
    except Exception:
        return date.today().year


def _season_bounds(season_str: str) -> tuple[date, date]:
    """Return (May 1 start, Apr 30 end) for a season string."""
    y1 = _season_year(season_str)
    return date(y1, 5, 1), date(y1 + 1, 4, 30)


def _current_season() -> str:
    return get_season(date.today().strftime("%Y-%m-%d"))


def _compute_target_seasons(n: int = 25) -> list[str]:
    """Return the n most recent seasons as strings, newest-first."""
    y1 = _season_year(_current_season())
    return [f"{y1 - i}-{str(y1 - i + 1)[2:]}" for i in range(n)]


def _years_back(season_str: str, current_season_str: str) -> int:
    return max(0, _season_year(current_season_str) - _season_year(season_str))


def _generate_dates(
    rng: random.Random, season_start: date, season_end: date, count: int
) -> list[date]:
    """
    Generate `count` dates within [season_start, season_end].
    Oct–Mar (peak) gets ~75 % of the volume; Apr–Sep gets ~25 %.
    """
    peak, off = [], []
    d = season_start
    while d <= season_end:
        (peak if d.month in (10, 11, 12, 1, 2, 3) else off).append(d)
        d += timedelta(days=1)

    n_peak = round(count * 0.75)
    n_off = count - n_peak
    return (rng.choices(peak, k=n_peak) if peak else []) + (
        rng.choices(off, k=n_off) if off else []
    )


# ---------------------------------------------------------------------------
# Per-workout generators
# ---------------------------------------------------------------------------


def _hr(rng: random.Random, pace: float, machine: str) -> Optional[dict]:
    """Generate a heart-rate dict for ~60 % of workouts."""
    if rng.random() > 0.60:
        return None
    # Faster pace → higher HR; BikeErg typically a touch lower.
    base = max(120.0, min(185.0, 155.0 + (120.0 - pace) * 0.5))
    if machine == "bike":
        base *= 0.93
    avg = round(base + rng.uniform(-5, 5))
    return {
        "average": avg,
        "min": avg - round(rng.uniform(15, 30)),
        "max": avg + round(rng.uniform(10, 20)),
    }


def _spm(rng: random.Random, workout_type: str) -> int:
    if workout_type in INTERVAL_WORKOUT_TYPES:
        return round(rng.uniform(26, 34))
    if workout_type in ("FixedDistanceSplits", "FixedTimeSplits"):
        return round(rng.uniform(22, 28))
    return round(rng.uniform(18, 24))


def _distance_workout(
    rng: random.Random,
    synth_id: int,
    date_str: str,
    machine: str,
    workout_type: str,
    pace_2k: float,
) -> tuple[dict, int]:
    dist = rng.choices(_DIST_VALUES, weights=_DIST_WEIGHTS, k=1)[0]
    pace = max(60.0, _pace_at_dist(pace_2k, dist) * rng.gauss(1.0, 0.03))
    time_t = round(pace * dist / 500.0 * 10)
    w = {
        "id": synth_id,
        "date": date_str,
        "distance": dist,
        "time": time_t,
        "type": machine,
        "workout_type": workout_type,
        "stroke_data": False,
        "verified": True,
        "ranked": False,
        "stroke_rate": _spm(rng, workout_type),
    }
    hr = _hr(rng, pace, machine)
    if hr:
        w["heart_rate"] = hr
    return w, synth_id - 1


def _timed_workout(
    rng: random.Random,
    synth_id: int,
    date_str: str,
    machine: str,
    workout_type: str,
    pace_2k: float,
) -> tuple[dict, int]:
    time_t = rng.choices(_TIMED_VALUES, weights=_TIMED_WEIGHTS, k=1)[0]
    # Estimate distance from approximate pace at that duration.
    approx_dist = (time_t / 10.0) * 500.0 / pace_2k
    pace = max(60.0, _pace_at_dist(pace_2k, approx_dist) * rng.gauss(1.0, 0.03))
    dist = round(time_t / 10.0 * 500.0 / pace)
    w = {
        "id": synth_id,
        "date": date_str,
        "distance": dist,
        "time": time_t,
        "type": machine,
        "workout_type": workout_type,
        "stroke_data": False,
        "verified": True,
        "ranked": False,
        "stroke_rate": _spm(rng, workout_type),
    }
    hr = _hr(rng, pace, machine)
    if hr:
        w["heart_rate"] = hr
    return w, synth_id - 1


def _interval_workout(
    rng: random.Random,
    synth_id: int,
    date_str: str,
    machine: str,
    workout_type: str,
    pace_2k: float,
) -> tuple[dict, int]:
    work_type, work_val, rest_tenths, default_reps = rng.choice(_INTERVAL_PATTERNS)
    reps = max(2, default_reps + rng.randint(-1, 1))

    intervals = []
    total_dist = total_time = 0

    for _ in range(reps):
        if work_type == "distance":
            dist = work_val
            pace = max(60.0, _pace_at_dist(pace_2k, dist) * rng.gauss(1.0, 0.025))
            time_t = round(pace * dist / 500.0 * 10)
        else:
            time_t = work_val
            approx = work_val / 10.0 * 500.0 / pace_2k
            pace = max(60.0, _pace_at_dist(pace_2k, approx) * rng.gauss(1.0, 0.025))
            dist = round(work_val / 10.0 * 500.0 / pace)

        iv: dict = {
            "type": work_type,
            "distance": dist,
            "time": time_t,
            "rest_time": rest_tenths,
            "stroke_rate": round(rng.uniform(26, 34)),
        }
        iv_hr = _hr(rng, pace, machine)
        if iv_hr:
            iv["heart_rate"] = iv_hr
        intervals.append(iv)
        total_dist += dist
        total_time += time_t

    w = {
        "id": synth_id,
        "date": date_str,
        "distance": total_dist,
        "time": total_time,
        "type": machine,
        "workout_type": workout_type,
        "stroke_data": False,
        "verified": True,
        "ranked": False,
        "stroke_rate": round(rng.uniform(26, 34)),
        "workout": {"intervals": intervals},
    }
    hr = _hr(rng, pace_2k, machine)
    if hr:
        w["heart_rate"] = hr
    return w, synth_id - 1


# ---------------------------------------------------------------------------
# Season batch generator
# ---------------------------------------------------------------------------


def _gen_season(
    rng: random.Random,
    season_start: date,
    season_end: date,
    machine: str,
    pace_2k: float,
    count: int,
    synth_id: int,
) -> tuple[dict, int]:
    """Generate `count` workouts for one machine × one season."""
    today = date.today()
    cap = min(season_end, today)
    dates = _generate_dates(rng, season_start, cap, count)

    result: dict = {}
    for d in dates:
        if d > cap:
            continue
        date_str = f"{d.strftime('%Y-%m-%d')} {rng.randint(5, 20):02d}:{rng.randint(0, 59):02d}:00"
        workout_type = rng.choices(_WORKOUT_TYPES, weights=_WORKOUT_WEIGHTS, k=1)[0]

        if workout_type == "FixedTimeSplits":
            w, synth_id = _timed_workout(
                rng, synth_id, date_str, machine, workout_type, pace_2k
            )
        elif workout_type in INTERVAL_WORKOUT_TYPES:
            w, synth_id = _interval_workout(
                rng, synth_id, date_str, machine, workout_type, pace_2k
            )
        else:
            w, synth_id = _distance_workout(
                rng, synth_id, date_str, machine, workout_type, pace_2k
            )

        result[str(w["id"])] = w

    return result, synth_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def augment_with_synthetic(real_workouts_dict: dict) -> tuple[dict, list]:
    """
    Return (augmented_dict, sorted_list) with ~28k synthetic workouts added.

    Real entries are never modified.  Synthetic IDs are all negative.
    Deterministically seeded — same output every run for stable UI iteration.
    localStorage is written BEFORE this is called, so real data is safe.
    """

    print("Augmenting with synthetic data")

    rng = random.Random(_SEED)
    base_pace = _extract_base_pace(real_workouts_dict)
    cur_season = _current_season()

    real_seasons: set[str] = {
        get_season(w.get("date", "")) for w in real_workouts_dict.values()
    } - {"Unknown"}

    target_seasons = _compute_target_seasons(25)
    synthetic: dict = {}
    synth_id = -1

    for season in target_seasons:
        s_start, s_end = _season_bounds(season)
        yrs_back = _years_back(season, cur_season)
        pace_factor = 1.0 + _PACE_DEGRADE_PER_YEAR * yrs_back

        # Rower: only for seasons not already in real data.
        if season not in real_seasons:
            batch, synth_id = _gen_season(
                rng,
                s_start,
                s_end,
                "rower",
                base_pace * pace_factor,
                _ROWER_PER_SEASON,
                synth_id,
            )
            synthetic.update(batch)

        # SkiErg + BikeErg: added for every season.
        for machine, count in [
            ("skierg", _SKIERG_PER_SEASON),
            ("bike", _BIKE_PER_SEASON),
        ]:
            mfactor = _MACHINE_PACE_FACTOR[machine]
            batch, synth_id = _gen_season(
                rng,
                s_start,
                s_end,
                machine,
                base_pace * pace_factor * mfactor,
                count,
                synth_id,
            )
            synthetic.update(batch)

    merged = {**real_workouts_dict, **synthetic}
    sorted_list = sorted(merged.values(), key=lambda w: w.get("date", ""), reverse=True)
    return merged, sorted_list
