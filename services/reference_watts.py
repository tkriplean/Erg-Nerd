"""
Time-indexed fitness reference — watts at every ranked event, at any past date.

The app has historically classified every workout against a single snapshot of
the rower's current fitness (±365 days from today).  For rowers with multi-year
history that is anachronistic: a 2009 row gets graded against 2026 fitness.
This module replaces that snapshot with a **quarterly** index of reference
watts, built once per unique workout-set, interpolated at call time.

Pipeline
--------
1.  `_quality_efforts(all_workouts)` — the same stage-1 filter the Power Curve
    page uses: `is_rankable_noninterval` + `apply_quality_filters`.
2.  `_quarter_markers(first, today)` — every Jan 1 / Apr 1 / Jul 1 / Oct 1
    between the rower's first quality effort and today.
3.  For each marker, `_compute_marker_refs`:
        • Collect window PBs in [marker − 365d, marker].
        • Predictor cascade:
              ≥ 5 PBs with duration ratio ≥ 10 + R² ≥ 0.90 → CP fit (direct watts)
              ≥ 4 PBs → Paul's Law regression (`compute_pauls_constant`)
              ≥ 1 PB → Paul's Law default k = 5.0 anchored to that PB
              else    → skip marker
        • Where an actual PB exists in the window for an event, final watts =
          mean(predicted_watts, pb_watts).  A new PB is reflected without
          discarding the predictor's cross-event coherence.
    CP fits are cached by the sorted tuple of PB workout ids so adjacent
    markers sharing the same PB set reuse the same fit.

Public API
----------
`get_reference_watts(when, all_workouts) -> {cat_key: watts}`
    Interpolates the index at a date.  Builds synchronously on cache miss, so
    consumers don't need to know whether the loader has warmed the cache.
    Before the first marker: clamp to first marker's watts.
    Between markers m0/m1: linear watts interpolation per event.
    After the last marker: use last marker + merge-in any window-local PB using
    "better-of" (not average) — a fresh PB is hard evidence the prediction is
    stale.

Loader-facing API (used only by `components/reference_watts_loader.py`)
----------------------------------------------------------------------
`build_reference_watts_index(all_workouts, on_progress=None) -> Index`
    Runs the full quarterly build, calling on_progress(i, n, marker_label) per
    marker.  Idempotent; cached by input_hash.
`seed_reference_watts_index(index)`
    Inject a pre-built index (e.g. deserialized from localStorage) into the
    module cache.
`input_hash(all_workouts) -> str`
    Stable sha1 over workout identity tuples; callers use it to check index
    freshness against what's persisted.
`serialize_index(index) / deserialize_index(json_dict)`
    JSON round-trip helpers (tuple cat_keys ↔ "type:value" strings, date ↔ ISO).

Persistence is a loader concern; this module does no I/O.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from typing import Optional

from services.critical_power_model import (
    _CURVE_T_MAX,
    _CURVE_T_MIN,
    critical_power_model,
    fit_critical_power,
)
from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    apply_quality_filters,
    compute_pace,
    compute_pauls_constant,
    compute_watts,
    is_rankable_noninterval,
    parse_date,
    pauls_law_pace,
    PACE_MAX,
    PACE_MIN,
    workout_cat_key,
)

try:
    from scipy.optimize import brentq
except Exception:  # pragma: no cover — scipy is a first-order dep already
    brentq = None


# ---------------------------------------------------------------------------
# Event metadata — all 13 ranked events
# ---------------------------------------------------------------------------

# (cat_key, dist_m_or_None, duration_s_or_None)
#   Distance events: dist_m is the event distance; duration varies by performance.
#   Time events:     duration_s is the event definition; distance varies.
_DIST_EVENTS: list = [(("dist", d), d, None) for d, _ in RANKED_DISTANCES]
_TIME_EVENTS: list = [(("time", t), None, t / 10.0) for t, _ in RANKED_TIMES]
_ALL_EVENTS: list = _DIST_EVENTS + _TIME_EVENTS
_ALL_CAT_KEYS: list = [ck for ck, _, _ in _ALL_EVENTS]

WINDOW_DAYS = 365
INDEX_VERSION = 1
PAULS_DEFAULT_K = 5.0


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict = {}  # input_hash → Index dict


def input_hash(all_workouts: list) -> str:
    """Stable sha1 over the identity tuples of `all_workouts`.

    Two workout lists with the same sorted set of (date, type, distance, time)
    tuples hash identically, so the loader can detect when localStorage is
    still valid for the current workouts.
    """
    ids = sorted(
        (
            (w.get("date") or "")[:10],
            w.get("type") or "",
            w.get("distance") or 0,
            w.get("time") or 0,
        )
        for w in all_workouts
    )
    h = hashlib.sha1()
    for t in ids:
        h.update(repr(t).encode("utf-8"))
    return h.hexdigest()


def seed_reference_watts_index(index: dict) -> None:
    """Store a pre-built index in the cache under its own input_hash."""
    h = index.get("input_hash")
    if h:
        _INDEX_CACHE[h] = index


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_reference_watts(when: date, all_workouts: list) -> dict:
    """Return ``{cat_key: watts}`` for all 13 ranked events at ``when``.

    Builds the index synchronously on cache miss (loader normally warms it
    first for a better UX).
    """
    h = input_hash(all_workouts)
    index = _INDEX_CACHE.get(h)
    if index is None:
        index = build_reference_watts_index(all_workouts)
    watts = _interpolate(when, index)
    if index.get("markers") and when >= index["markers"][-1]["date"]:
        last_date = index["markers"][-1]["date"]
        watts = _merge_recent_tail(watts, last_date, when, all_workouts)
    return watts


def build_reference_watts_index(
    all_workouts: list,
    on_progress=None,
) -> dict:
    """Run the full quarterly build; return the index and cache it.

    ``on_progress(i, n, label)`` is invoked once per marker (0-indexed) and one
    final time with (n, n, "done").  Idempotent: repeated calls with the same
    ``all_workouts`` return the cached index immediately.
    """
    h = input_hash(all_workouts)
    cached = _INDEX_CACHE.get(h)
    if cached is not None:
        if on_progress:
            n = len(cached.get("markers", []))
            on_progress(n, n, "done")
        return cached

    quality = _quality_efforts(all_workouts)

    if not quality:
        index = {
            "version": INDEX_VERSION,
            "input_hash": h,
            "markers": [],
        }
        _INDEX_CACHE[h] = index
        if on_progress:
            on_progress(0, 0, "done")
        return index

    first_effort = min(parse_date(w.get("date", "")) for w in quality)
    markers = _quarter_markers(first_effort, date.today())

    cp_fit_cache: dict = {}
    result_markers: list = []
    n = len(markers)
    for i, m in enumerate(markers):
        label = m.isoformat()
        if on_progress:
            on_progress(i, n, label)
        refs = _compute_marker_refs(m, quality, cp_fit_cache)
        if refs is not None:
            result_markers.append(refs)

    if on_progress:
        on_progress(n, n, "done")

    index = {
        "version": INDEX_VERSION,
        "input_hash": h,
        "markers": result_markers,
    }
    _INDEX_CACHE[h] = index
    return index


# ---------------------------------------------------------------------------
# Quality-efforts + markers
# ---------------------------------------------------------------------------


def _quality_efforts(all_workouts: list) -> list:
    """Stage-1 quality filter — matches power_curve_workouts.build_workout_view."""
    rankable = [w for w in all_workouts if is_rankable_noninterval(w)]
    return apply_quality_filters(rankable)


def _quarter_markers(first: date, last: date) -> list:
    """Quarter boundaries (Jan 1, Apr 1, Jul 1, Oct 1) in [first, last]."""
    out: list = []
    y = first.year
    while True:
        for m in (1, 4, 7, 10):
            d = date(y, m, 1)
            if d > last:
                return out
            if d >= first:
                out.append(d)
        y += 1


# ---------------------------------------------------------------------------
# Per-marker computation
# ---------------------------------------------------------------------------


def _window_pbs(quality: list, start: date, end: date) -> dict:
    """Best performance per cat_key in [start, end].

    Returns ``{cat_key: {"pace": p, "duration_s": s, "watts": w, "id": id}}``.
    """
    best: dict = {}
    for w in quality:
        dt = parse_date(w.get("date", ""))
        if dt < start or dt > end:
            continue
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        ck = workout_cat_key(w)
        if ck is None:
            continue
        time_s = (w.get("time") or 0) / 10.0
        if time_s <= 0:
            continue
        watts = compute_watts(pace)
        cur = best.get(ck)
        if cur is None or pace < cur["pace"]:
            best[ck] = {
                "pace": pace,
                "duration_s": time_s,
                "watts": watts,
                "id": w.get("id"),
                "distance": w.get("distance"),
                "cat_key": ck,
            }
    return best


def _compute_marker_refs(marker: date, quality: list, cp_cache: dict) -> Optional[dict]:
    """Build ``MarkerRefs`` for one marker, or None if no PBs in window."""
    start = marker - timedelta(days=WINDOW_DAYS)
    pbs = _window_pbs(quality, start, marker)
    if not pbs:
        return None

    pb_list = list(pbs.values())

    # Predictor cascade.
    cp_params = None
    if len(pb_list) >= 5:
        cp_params = _cached_cp_fit(pb_list, cp_cache)

    predicted: dict = {}
    source_tag: dict = {}

    if cp_params is not None:
        for ck, dist_m, dur_s in _ALL_EVENTS:
            w = _cp_watts_at_event(cp_params, dist_m, dur_s)
            if w is not None:
                predicted[ck] = w
                source_tag[ck] = "cp"

    # Fall back through Paul's Law where CP didn't cover (or wasn't fit).
    if len(predicted) < len(_ALL_CAT_KEYS):
        # Build lifetime-best-style dicts for Paul's regression.
        lb = {ck: p["pace"] for ck, p in pbs.items()}
        lb_anchor: dict = {}
        for ck, p in pbs.items():
            if ck[0] == "dist":
                lb_anchor[ck] = ck[1]
            else:
                # Time event: anchor distance = pace × duration / 500
                lb_anchor[ck] = p["pace"] and (500.0 * p["duration_s"] / p["pace"])

        k = None
        if len(pbs) >= 4:
            k = compute_pauls_constant(lb, lb_anchor)
        if k is None:
            k = PAULS_DEFAULT_K

        # Anchor: fastest (pace-lowest) PB.
        anchor = min(pb_list, key=lambda p: p["pace"])
        anchor_pace = anchor["pace"]
        anchor_dist = lb_anchor.get(anchor["cat_key"])
        if not anchor_dist:
            anchor_dist = anchor.get("distance") or (
                500.0 * anchor["duration_s"] / anchor_pace
            )

        pauls_source = "pauls" if len(pbs) >= 4 and k != PAULS_DEFAULT_K else (
            "pauls" if len(pbs) >= 4 else "pauls_default"
        )
        for ck, dist_m, dur_s in _ALL_EVENTS:
            if ck in predicted:
                continue
            w = _pauls_watts_at_event(
                anchor_pace, anchor_dist, dist_m, dur_s, k
            )
            if w is not None:
                predicted[ck] = w
                source_tag[ck] = pauls_source

    # Merge in actual PB watts — mean of prediction and PB.  If there's only a
    # PB and no prediction, use the PB alone.
    final_watts: dict = {}
    final_source: dict = {}
    for ck in _ALL_CAT_KEYS:
        pb = pbs.get(ck)
        pred_w = predicted.get(ck)
        if pb is not None and pred_w is not None:
            final_watts[ck] = (pb["watts"] + pred_w) / 2.0
            final_source[ck] = "pb" if pb["watts"] >= pred_w else source_tag.get(ck, "pb")
        elif pb is not None:
            final_watts[ck] = pb["watts"]
            final_source[ck] = "pb"
        elif pred_w is not None:
            final_watts[ck] = pred_w
            final_source[ck] = source_tag.get(ck, "pauls")
        # else: leave unpopulated; interpolation handles gaps.

    if not final_watts:
        return None

    return {
        "date": marker,
        "watts": final_watts,
        "source": final_source,
        "n_pbs": len(pbs),
    }


def _cached_cp_fit(pb_list: list, cp_cache: dict) -> Optional[dict]:
    """CP fit with a caller-provided cache keyed by sorted PB ids."""
    key = frozenset((p.get("id") or (p["cat_key"], p["duration_s"])) for p in pb_list)
    if key in cp_cache:
        return cp_cache[key]
    params = fit_critical_power(pb_list)
    cp_cache[key] = params
    return params


# ---------------------------------------------------------------------------
# Predictor evaluation at each event
# ---------------------------------------------------------------------------


def _cp_watts_at_event(
    params: dict, dist_m: Optional[int], dur_s: Optional[float]
) -> Optional[float]:
    """Predicted watts at one event under the fitted CP model.

    Distance events: solve for duration t such that the model's speed × t = d.
    Time events: evaluate the model directly at the fixed duration.
    """
    Pow1 = params["Pow1"]
    tau1 = params["tau1"]
    Pow2 = params["Pow2"]
    tau2 = params["tau2"]

    if dur_s is not None:
        watts = critical_power_model(dur_s, Pow1, tau1, Pow2, tau2)
        return float(watts) if watts > 0 else None

    if dist_m is None or brentq is None:
        return None

    def _residual(t, _d=dist_m):
        P = critical_power_model(t, Pow1, tau1, Pow2, tau2)
        if P <= 0:
            return -_d
        return (P / 2.80) ** (1.0 / 3.0) * t - _d

    try:
        t_star = brentq(_residual, _CURVE_T_MIN, _CURVE_T_MAX, xtol=0.1)
    except Exception:
        return None
    watts = critical_power_model(t_star, Pow1, tau1, Pow2, tau2)
    return float(watts) if watts > 0 else None


def _pauls_watts_at_event(
    anchor_pace: float,
    anchor_dist: float,
    dist_m: Optional[int],
    dur_s: Optional[float],
    k: float,
) -> Optional[float]:
    """Predicted watts at one event under Paul's Law anchored to (anchor_pace,
    anchor_dist)."""
    if not anchor_pace or not anchor_dist:
        return None

    if dist_m is not None:
        p = pauls_law_pace(anchor_pace, anchor_dist, dist_m, k=k)
        if p < PACE_MIN or p > PACE_MAX:
            return None
        return compute_watts(p)

    if dur_s is None:
        return None

    # Time event: distance depends on pace; iterate.
    # d = 500 * dur_s / p; p = anchor_pace + k * log2(d / anchor_dist)
    p = anchor_pace
    for _ in range(30):
        d = 500.0 * dur_s / p
        if d <= 0:
            return None
        try:
            p_new = anchor_pace + k * math.log2(d / anchor_dist)
        except (ValueError, ZeroDivisionError):
            return None
        if abs(p_new - p) < 1e-4:
            p = p_new
            break
        p = p_new
    if p < PACE_MIN or p > PACE_MAX:
        return None
    return compute_watts(p)


# ---------------------------------------------------------------------------
# Interpolation + recent-tail merge
# ---------------------------------------------------------------------------


def _interpolate(when: date, index: dict) -> dict:
    """Per-event linear interpolation between bracketing markers."""
    markers = index.get("markers") or []
    if not markers:
        return {}
    if when <= markers[0]["date"]:
        return dict(markers[0]["watts"])
    if when >= markers[-1]["date"]:
        return dict(markers[-1]["watts"])

    # Find bracketing markers.
    lo_idx = 0
    for i in range(len(markers) - 1):
        if markers[i]["date"] <= when <= markers[i + 1]["date"]:
            lo_idx = i
            break
    m0 = markers[lo_idx]
    m1 = markers[lo_idx + 1]
    span = (m1["date"] - m0["date"]).days
    if span <= 0:
        return dict(m0["watts"])
    w = (when - m0["date"]).days / span

    out: dict = {}
    for ck in _ALL_CAT_KEYS:
        v0 = m0["watts"].get(ck)
        v1 = m1["watts"].get(ck)
        if v0 is not None and v1 is not None:
            out[ck] = v0 * (1.0 - w) + v1 * w
        elif v0 is not None:
            out[ck] = v0
        elif v1 is not None:
            out[ck] = v1
        else:
            # Scan outward for nearest populated marker at this event.
            for j in range(1, len(markers)):
                for idx in (lo_idx - j, lo_idx + 1 + j):
                    if 0 <= idx < len(markers):
                        v = markers[idx]["watts"].get(ck)
                        if v is not None:
                            out[ck] = v
                            break
                if ck in out:
                    break
    return out


def _merge_recent_tail(
    watts_dict: dict, last_marker_date: date, when: date, all_workouts: list
) -> dict:
    """Apply "better-of" PB merge for workouts between last marker and ``when``.

    A fresh PB after the last marker is hard evidence the prior prediction is
    stale — upgrade, don't average.
    """
    out = dict(watts_dict)
    for w in all_workouts:
        if not is_rankable_noninterval(w):
            continue
        dt = parse_date(w.get("date", ""))
        if dt <= last_marker_date or dt > when:
            continue
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        ck = workout_cat_key(w)
        if ck is None:
            continue
        watts = compute_watts(pace)
        cur = out.get(ck)
        if cur is None or watts > cur:
            out[ck] = watts
    return out


# ---------------------------------------------------------------------------
# JSON serialization (for localStorage round-trips)
# ---------------------------------------------------------------------------


def _ck_to_str(ck: tuple) -> str:
    return f"{ck[0]}:{ck[1]}"


def _ck_from_str(s: str) -> tuple:
    etype, evalue = s.split(":", 1)
    return (etype, int(evalue))


def serialize_index(index: dict) -> dict:
    """Return a JSON-ready dict (dates → ISO strings, cat_keys → "type:value")."""
    return {
        "version": index.get("version", INDEX_VERSION),
        "input_hash": index.get("input_hash", ""),
        "markers": [
            {
                "date": m["date"].isoformat(),
                "watts": {_ck_to_str(ck): round(v, 3) for ck, v in m["watts"].items()},
                "source": {_ck_to_str(ck): s for ck, s in m.get("source", {}).items()},
                "n_pbs": m.get("n_pbs", 0),
            }
            for m in index.get("markers", [])
        ],
    }


def deserialize_index(json_dict: dict) -> dict:
    """Inverse of ``serialize_index``."""
    return {
        "version": json_dict.get("version", INDEX_VERSION),
        "input_hash": json_dict.get("input_hash", ""),
        "markers": [
            {
                "date": date.fromisoformat(m["date"]),
                "watts": {_ck_from_str(k): float(v) for k, v in m.get("watts", {}).items()},
                "source": {_ck_from_str(k): s for k, s in m.get("source", {}).items()},
                "n_pbs": m.get("n_pbs", 0),
            }
            for m in json_dict.get("markers", [])
        ],
    }
