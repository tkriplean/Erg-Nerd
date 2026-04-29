"""
services/workout_metrics_cache.py

Process-wide cache for per-workout render-time metrics.

Several pages (Sessions, Volume, Workout, Intervals) ask for the same
heavy fields every render — power-bin meters, HR-bin meters, the SVG
bars derived from them, the spread scores, the quality category.  All
are pure functions of:

  - the workout content,
  - the reference-watts index for the workout's date,
  - (for HR-dependent metrics) the user's max heart rate.

Reference watts is keyed by ``input_hash(all_workouts)``: it changes only
when the workout corpus changes (sync, edit, delete).  So the cache key
is ``(metric, workout_id, input_hash[, max_hr])`` — any change to the
corpus or the profile naturally invalidates everything that depends on it.

Bounded; on overflow we drop everything (matches the previous
``_QUALITY_CACHE`` and intervals-page enrichment-cache patterns).

Public API
    get_or_compute(metric, workout_id, input_hash, compute_fn, **extra)
    cache_size()
    clear_cache()
"""

from __future__ import annotations

from typing import Any, Callable


_CACHE: dict[tuple, Any] = {}
_CACHE_MAX = 1_000_000

# Sentinel so legitimate ``None`` results are still cached (otherwise a
# None hit looks identical to a miss and we'd recompute every render).
_MISS = object()


def get_or_compute(
    metric: str,
    workout_id,
    input_hash: str,
    compute_fn: Callable[[], Any],
    **extra,
) -> Any:
    """Return the cached value for ``(metric, workout_id, input_hash, …extra)``,
    computing and storing it on miss.

    ``extra`` keyword arguments extend the cache key — pass ``max_hr=…``
    for HR-dependent metrics so a profile change invalidates only the HR
    family.  Keys are sorted by name to keep the tuple stable.
    """
    if workout_id is None:
        # Pre-enrichment / ad-hoc workouts shouldn't reach this layer; if
        # they do, fall through to compute every time.
        return compute_fn()

    key: tuple = (metric, workout_id, input_hash)
    if extra:
        key = key + tuple(sorted(extra.items()))

    hit = _CACHE.get(key, _MISS)
    if hit is not _MISS:
        return hit

    result = compute_fn()
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = result
    return result


def cache_size() -> int:
    """Current number of entries (test/debug helper)."""
    return len(_CACHE)


def clear_cache() -> None:
    """Drop all cached entries (test/debug helper)."""
    _CACHE.clear()
