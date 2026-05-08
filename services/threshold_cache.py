"""
Per-date cache for reference watts.

The Intervals, Volume, Workout, and Workouts pages all need to map a workout
to its date-appropriate ``ref_watts``.  The lookup is cheap when cached but
expensive when recomputed for every workout — and many workouts share a
date.

``make_thresholds_resolver(all_workouts)`` returns a single ``ref_watts_for``
callable backed by a per-date cache.  The cache is local to the call site
so each page-render gets a fresh dict (HyperDiv re-runs the page function
on every state change; the cache lives only for the duration of one
render).
"""

from __future__ import annotations

from typing import Callable, Optional

from services.reference_watts import _reference_watts_from_index, resolve_index


def make_thresholds_resolver(
    all_workouts: list,
) -> Callable[[dict], Optional[dict]]:
    """Return a ``ref_watts_for`` callable.

    The callable takes a single workout dict and returns the cached
    reference-watts dict for that workout's date.  Workouts sharing a
    date hit the cache.
    """
    cache: dict = {}
    index = resolve_index(all_workouts)

    def ref_watts_for(workout: dict) -> Optional[dict]:
        d = workout["date_dt"]
        if d not in cache:
            cache[d] = _reference_watts_from_index(d, index, all_workouts)
        return cache[d]

    return ref_watts_for
