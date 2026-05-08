"""
services/local_storage_compression.py

Compression helpers for the workout dict.  Originally written to keep
the per-browser workouts blob inside the ~5–10 MB ``localStorage`` cap;
the client-side cache now lives in IndexedDB (see
``components.indexed_db``) so the localStorage consumer is gone.  These
helpers remain because :mod:`services.public_profiles` reuses the same
encoding for the server-side public-profile JSON files.
"""

from __future__ import annotations

import base64
import json
import zlib

from services.formatters import format_time


# ---------------------------------------------------------------------------
# Workout dict compression — used by services.public_profiles for the
# server-side ``.public_profiles/{uid}/workouts.json`` payload.
# ---------------------------------------------------------------------------

# Fields that are always redundant and never used by the app.
_WORKOUT_PERM_DROP = frozenset(
    {
        "real_time",
        "calories_total",  # dropped from top-level summary …
        "timezone",
        "date_utc",
        "user_id",
        "privacy",
        # time_formatted is NOT permanently dropped — for interval workouts it
        # carries work-only time (total minus rest) which differs from format_time(time).
        # It is dropped conditionally in _compress_one_workout() when redundant.
        # Stage-2 fetch-time enrichment fields (services.workout_enrichment) —
        # derived from other workout fields and re-applied on every load, so
        # they are stripped here.  Includes ``date_dt`` which is a Python
        # ``date`` object that JSON cannot serialise.
        "pace",
        "watts",
        "date_dt",
        "date_ms",
        "day",
        "season",
        "cat_key",
        "machine",
        "is_interval",
        "reps",
        "structure_key",
        "work_pace",
        "work_spm",
    }
)

# Fields whose default value is implied; omitted on compress, restored on
# decompress.  Saves space without losing any information.
_WORKOUT_DEFAULTS = {
    "verified": True,
    "type": "rower",
    "comments": None,
    "ranked": False,
}


def _hr_empty(hr) -> bool:
    """True when a heart_rate value carries no real data."""
    if hr is None or hr == {}:
        return True
    if isinstance(hr, dict):
        return all(v == 0 for v in hr.values())
    return False


def _compress_one_workout(w: dict) -> dict:
    out = {}
    for k, v in w.items():
        if k in _WORKOUT_PERM_DROP:
            continue
        # Stage-3 render-time fields (``_bin_meters``, ``_severity``, …) are
        # attached by services.workout_enrichment when a page renders.  They
        # are derived from cached metrics and must never be persisted.
        if isinstance(k, str) and k.startswith("_"):
            continue
        if k in _WORKOUT_DEFAULTS and v == _WORKOUT_DEFAULTS[k]:
            continue
        # Drop time_formatted when it's identical to what format_time() produces
        # (true for JustRow, FixedDistanceSplits, FixedTimeSplits). For interval
        # workouts it carries work-only time and must be kept.
        if k == "time_formatted" and v == format_time(w.get("time", 0)):
            continue
        if k == "heart_rate" and _hr_empty(v):
            continue
        if k == "workout" and isinstance(v, dict):
            # Strip targets, calories_total from splits, empty heart_rate,
            # always-constant split type, and always-zero wattminutes_total.
            splits = v.get("splits") or []
            new_splits = []
            for s in splits:
                ns = {}
                for sk, sv in s.items():
                    if sk in ("calories_total", "type", "wattminutes_total"):
                        continue
                    if sk == "heart_rate" and _hr_empty(sv):
                        continue
                    ns[sk] = sv
                new_splits.append(ns)
            new_wo = {wk: wv for wk, wv in v.items() if wk not in ("targets", "splits")}
            if new_splits:
                new_wo["splits"] = new_splits
            out[k] = new_wo
            continue
        out[k] = v
    return out


def _decompress_one_workout(w: dict) -> dict:
    """Restore default-value fields stripped during compression."""
    out = dict(w)
    for k, default in _WORKOUT_DEFAULTS.items():
        if k not in out:
            out[k] = default
    return out


def compress_workouts(workouts_dict: dict) -> str:
    """
    Serialize and compress a workout dict for browser localStorage storage.

    Before compression, redundant and always-default fields are stripped from
    each workout (and from split sub-dicts).  Default-value fields are
    restored transparently by decompress_workouts().

    The pruned dict is then JSON-serialized, compressed with zlib (level=9),
    and base64-encoded to a plain ASCII string for localStorage.setItem().

    Typical end-to-end reduction vs. raw JSON: ~8–10×.
    """
    pruned = {k: _compress_one_workout(v) for k, v in workouts_dict.items()}
    raw = json.dumps(pruned).encode()
    return base64.b64encode(zlib.compress(raw, level=9)).decode()


def decompress_workouts(stored: str) -> dict:
    """
    Reverse of compress_workouts(). Returns the workout dict, or {} on error.
    Restores default-value fields (verified, type, comments, ranked) that were
    omitted during compression.
    """
    try:
        raw = json.loads(zlib.decompress(base64.b64decode(stored)))
        return {k: _decompress_one_workout(v) for k, v in raw.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Generic dict compression — used for session records on the public profile.
# No per-field stripping; the workout-specific helpers above mangle session
# records because fields like ``machine`` and ``day`` overlap with Stage-2
# enrichment names that get dropped on workout compression.
# ---------------------------------------------------------------------------


def compress_dict(payload: dict) -> str:
    """JSON + zlib(9) + base64 for an arbitrary dict.  No per-field filtering."""
    raw = json.dumps(payload or {}).encode()
    return base64.b64encode(zlib.compress(raw, level=9)).decode()


def decompress_dict(stored: str) -> dict:
    """Reverse of :func:`compress_dict`.  Returns ``{}`` on error."""
    try:
        return json.loads(zlib.decompress(base64.b64decode(stored)))
    except Exception:
        return {}


