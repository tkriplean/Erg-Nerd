"""
Per-date cache for reference watts and derived power-bin thresholds.

The Intervals, Volume, Workout, and Sessions pages all need to map a workout
to its date-appropriate ``ref_watts`` (for the Quality metric) and
``thresholds`` (for power-bin classification).  Both lookups are cheap when
cached but expensive when recomputed for every workout — and many workouts
share a date.

``make_thresholds_resolver(all_workouts)`` returns three callables backed
by a per-date cache (``thresholds_for``, ``ref_watts_for``,
``reference_pbs_for``).  The cache is local to the call site so each
page-render gets a fresh dict (HyperDiv re-runs the page function on every
state change; the cache lives only for the duration of one render).
"""

from __future__ import annotations

from typing import Callable, Optional

from services.reference_watts import _reference_watts_from_index, resolve_index
from services.volume_bins import compute_bin_thresholds
from services.workout_quality import _build_reference_pbs


def make_thresholds_resolver(
    all_workouts: list,
) -> tuple[
    Callable[[dict], Optional[dict]],
    Callable[[dict], Optional[dict]],
    Callable[[dict], Optional[dict]],
]:
    """Return ``(thresholds_for, ref_watts_for, reference_pbs_for)`` callables.

    All three take a single workout dict and return the cached result for that
    workout's date.  Workouts sharing a date hit the cache.

    ``reference_pbs_for`` is the per-category ``{cat: (watts, dist_m)}`` dict
    used by ``compute_workout_quality`` — caching it here means the quality
    metric doesn't rebuild it once per workout.
    """
    cache: dict = {}
    index = resolve_index(all_workouts)

    def _resolve(workout: dict):
        d = workout["date_dt"]
        if d not in cache:
            ref = _reference_watts_from_index(d, index, all_workouts)
            thresholds = compute_bin_thresholds(ref)
            ref_pbs = _build_reference_pbs(ref) if ref else None
            cache[d] = (ref, thresholds, ref_pbs)
        return cache[d]

    def thresholds_for(workout: dict) -> Optional[dict]:
        return _resolve(workout)[1]

    def ref_watts_for(workout: dict) -> Optional[dict]:
        return _resolve(workout)[0]

    def reference_pbs_for(workout: dict) -> Optional[dict]:
        return _resolve(workout)[2]

    return thresholds_for, ref_watts_for, reference_pbs_for
