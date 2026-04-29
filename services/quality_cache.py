"""
Process-wide cache for per-workout quality results.

Several pages (Volume, Sessions, Workout, Intervals) ask for the same workout's
``_quality`` field every render.  ``compute_workout_quality`` is pure given
``(workout content, ref_watts at workout date, thresholds at workout date)`` —
and both ref_watts and thresholds are themselves a function of the
reference-watts index, which is keyed by ``input_hash(all_workouts)``.

So the cache key is ``(workout_id, input_hash)``: a workout's quality only
changes when the underlying workout corpus changes (sync, delete, edit), and
the input_hash changes with it, automatically invalidating stale entries.

Bounded so a long session can't grow without limit; on overflow we drop
everything (matches the ``_workout_enrich_cache`` pattern in intervals_page).
"""

from __future__ import annotations

from typing import Optional

from services.workout_quality import compute_workout_quality

_QUALITY_CACHE: dict[tuple, Optional[dict]] = {}
_QUALITY_CACHE_MAX = 1000000

# Sentinel for "we tried and there's no result" so a None doesn't fall through
# to recompute on every call.
_NO_RESULT = object()


def get_or_compute_quality(
    workout: dict,
    ref_watts: Optional[dict],
    thresholds: Optional[dict],
    input_hash: str,
    reference_pbs: Optional[dict] = None,
) -> Optional[dict]:
    """Return the cached quality result for ``workout`` or compute and store it.

    Returns the same shape as :func:`compute_workout_quality`: a dict with
    ``score``/``category``/``per_category_energy``, or ``None``.
    """
    wid = workout["id"]
    cache_key = (wid, input_hash) if wid is not None else None

    if cache_key is not None:
        hit = _QUALITY_CACHE.get(cache_key, _NO_RESULT)
        if hit is not _NO_RESULT:
            return hit  # type: ignore[return-value]

    result = compute_workout_quality(workout, ref_watts, thresholds, reference_pbs)

    if cache_key is not None:
        if len(_QUALITY_CACHE) >= _QUALITY_CACHE_MAX:
            _QUALITY_CACHE.clear()
        _QUALITY_CACHE[cache_key] = result

    return result


def cache_size() -> int:
    """Current number of entries in the cache (test/debug helper)."""
    return len(_QUALITY_CACHE)


def clear_cache() -> None:
    """Drop all cached quality entries (test/debug helper)."""
    _QUALITY_CACHE.clear()
