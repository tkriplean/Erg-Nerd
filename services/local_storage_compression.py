from __future__ import annotations

import base64
import json
import zlib

from services.formatters import format_time


# ---------------------------------------------------------------------------
# Workout localStorage compression
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
        # Stage-3 render-time fields (``_bin_meters``, ``_bar_uri``,
        # ``_quality``, …) are attached by services.workout_enrichment when
        # a page renders.  They are derived from cached metrics and must
        # never be persisted.
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
# Versioned envelope around the workouts payload
# ---------------------------------------------------------------------------
#
# The workouts that sit in localStorage have already passed through
# ``services.data_integrity.normalize_workout``.  When the rules in that
# module change we need to invalidate the cache and force a full re-fetch
# from Concept2.  We do that with a thin envelope that records the integrity
# version alongside the compressed payload:
#
#   {"v": <int>, "w": "<base64-compressed-workouts>"}
#
# Pre-versioning caches stored the raw compressed string at the same key.
# ``decompress_workouts_envelope`` detects that legacy shape, returns the
# inner dict, and reports ``None`` for the version so the caller knows to
# discard.


def compress_workouts_envelope(workouts_dict: dict, integrity_version: int) -> str:
    """Wrap the compressed workouts payload in a versioned envelope.

    The envelope is itself a JSON string suitable for direct
    ``localStorage.setItem``.  Use ``decompress_workouts_envelope`` to read
    it back.
    """
    return json.dumps(
        {"v": int(integrity_version), "w": compress_workouts(workouts_dict)}
    )


def decompress_workouts_envelope(stored: str) -> tuple[dict, int | None]:
    """Reverse of ``compress_workouts_envelope``.

    Returns ``(workouts_dict, integrity_version)``:

    - ``({}, None)`` — empty / invalid blob.  Caller should treat the
      cache as missing.
    - ``(<dict>, None)`` — pre-versioning legacy blob: the inner payload
      decoded but the envelope was absent.  Caller should treat as
      version-mismatched and re-fetch.
    - ``(<dict>, <int>)`` — versioned envelope; caller compares the int
      to ``INTEGRITY_VERSION``.
    """
    if not stored:
        return {}, None
    try:
        outer = json.loads(stored)
    except (TypeError, ValueError):
        outer = None
    if isinstance(outer, dict) and "v" in outer and "w" in outer:
        try:
            return decompress_workouts(outer["w"]), int(outer["v"])
        except Exception:
            return {}, None
    # Legacy: the value at the key was the raw compressed string from
    # before the envelope existed.  Decode it but flag as un-versioned.
    legacy = decompress_workouts(stored)
    return legacy, None


# ---------------------------------------------------------------------------
# Stroke cache localStorage compression
# ---------------------------------------------------------------------------


def compress_strokes_cache(strokes_dict: dict) -> str:
    """
    Compress the stroke cache dict for browser localStorage.

    The stroke cache maps str(workout_id) → [{t: float, d: float}, …].
    The dict is JSON-serialised, zlib-compressed (level 9), and base64-encoded.

    Typical reduction: ~5–8× vs raw JSON for a set of 2k stroke arrays.
    """
    raw = json.dumps(strokes_dict).encode()
    return base64.b64encode(zlib.compress(raw, level=9)).decode()


def decompress_strokes_cache(stored: str) -> dict:
    """
    Reverse of compress_strokes_cache(). Returns the dict, or {} on error.
    """
    try:
        return json.loads(zlib.decompress(base64.b64decode(stored)))
    except Exception:
        return {}
