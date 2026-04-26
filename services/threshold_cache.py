"""
Per-date cache for reference watts and derived power-bin thresholds.

The Intervals, Volume, Workout, and Sessions pages all need to map a workout
to its date-appropriate ``ref_watts`` (for the Quality metric) and
``thresholds`` (for power-bin classification).  Both lookups are cheap when
cached but expensive when recomputed for every workout — and many workouts
share a date.

``make_thresholds_resolver(all_workouts)`` returns a pair of callables backed
by a per-date cache.  The cache is local to the call site so each page-render
gets a fresh dict (HyperDiv re-runs the page function on every state change;
the cache lives only for the duration of one render).
"""

from __future__ import annotations

from typing import Callable, Optional

from services.reference_watts import get_reference_watts
from services.rowing_utils import parse_date
from services.volume_bins import compute_bin_thresholds


def make_thresholds_resolver(
    all_workouts: list,
) -> tuple[Callable[[dict], Optional[dict]], Callable[[dict], Optional[dict]]]:
    """Return ``(thresholds_for, ref_watts_for)`` callables.

    Both take a single workout dict and return the cached result for that
    workout's date.  Workouts sharing a date hit the cache.
    """
    cache: dict = {}

    def _resolve(workout: dict):
        d = parse_date(workout.get("date", ""))
        if d not in cache:
            ref = get_reference_watts(d, all_workouts)
            cache[d] = (ref, compute_bin_thresholds(ref))
        return cache[d]

    def thresholds_for(workout: dict) -> Optional[dict]:
        return _resolve(workout)[1]

    def ref_watts_for(workout: dict) -> Optional[dict]:
        return _resolve(workout)[0]

    return thresholds_for, ref_watts_for
