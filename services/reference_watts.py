"""
Time-indexed fitness reference — watts at every ranked event, at any past date.

The app has historically classified every workout against a single snapshot of
the rower's current fitness (±365 days from today).  For rowers with multi-year
history that is anachronistic: a 2009 row gets graded against 2026 fitness.
This module replaces that snapshot with a **quarterly** index of reference
watts, built once per unique workout-set, interpolated at call time.

Indices are **per machine**.  Callers must pass a single-machine workout list
to ``build_reference_watts_index`` / ``resolve_index`` / ``get_reference_watts``;
the upstream global filter (``gstate.machine`` via ``get_all_workouts``) does
this filtering.  The machine is captured on each index dict so interpolation
knows which ranked-event grid to scan.

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
                 — but only used at events whose implied duration falls
                 within ±1 octave of the actual PB duration range.  Outside
                 that span the 4-parameter fit is unconstrained and produces
                 above-WR watts at e.g. 100m when no sub-1min PB anchors it.
              ≥ 4 PBs → Paul's Law regression (`compute_pauls_constant`)
              ≥ 1 PB → Paul's Law default k = 5.0 anchored to that PB
              else    → skip marker
          For Paul's Law, predictions are *averaged across all anchors* (not
          projected only from the fastest PB).  Single-anchor extrapolation
          carries a sprint-bias when projecting from a longer PB to a shorter
          event; averaging dilutes that bias and matches the predictor used
          in :mod:`services.predictions`.
        • Where an actual PB exists in the window for an event, final watts =
          PB if PB > prediction (a real performance above the curve is
          adopted outright — you can't accidentally overperform), else
          mean(predicted_watts, pb_watts) (a sub-prediction PB may be a
          sub-maximal day; blend to keep cross-event coherence).
        • Final monotonicity pass: walk events in duration order and cap each
          longer event at the running minimum.  Blending a sub-prediction PB
          can pull a merged value below adjacent-event predictions; this
          ensures the reference curve respects power-duration ordering.
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
`reference_watts_at_duration(when, duration_s, all_workouts) -> watts | None`
    Continuous power-duration curve evaluator: looks up the date's discrete
    cat_key anchors, converts each to (duration, watts), and interpolates
    piecewise-linearly in log-log space.  Outside the anchor range, extrapolates
    on the nearest segment's slope.  Used by ESS computation to grade segments
    of arbitrary duration against the rower's duration-appropriate ability.

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

from services.power_duration_model import (
    _CURVE_T_MAX,
    _CURVE_T_MIN,
    power_duration_model,
    fit_power_duration,
)
from services.rowing_utils import (
    ranked_distances,
    ranked_times,
    apply_quality_filters,
    compute_pauls_constant,
    compute_watts,
    is_rankable_noninterval,
    pauls_law_pace,
    workout_machine,
    PACE_MAX,
    PACE_MIN,
)

try:
    from scipy.optimize import brentq
except Exception:  # pragma: no cover — scipy is a first-order dep already
    brentq = None


# ---------------------------------------------------------------------------
# Event metadata — resolved per machine
# ---------------------------------------------------------------------------
# (cat_key, dist_m_or_None, duration_s_or_None)
#   Distance events: dist_m is the event distance; duration varies by performance.
#   Time events:     duration_s is the event definition; distance varies.


def _all_events(machine: str) -> list:
    return [(("dist", d), d, None) for d, _ in ranked_distances(machine)] + [
        (("time", t), None, t / 10.0) for t, _ in ranked_times(machine)
    ]


def _all_cat_keys(machine: str) -> list:
    return [ck for ck, _, _ in _all_events(machine)]


WINDOW_DAYS = 365
INDEX_VERSION = 4
PAULS_DEFAULT_K = 5.0


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict = {}  # input_hash → Index dict

# Identity-keyed memo: callers within a single render reuse the same workouts
# list object, so `id` + `len` + `id(first)` is a safe O(1) cache key. A false
# hit would require list-object reuse with identical length and identical first
# element after the previous list was gc'd — see `input_hash` for details.
_HASH_MEMO: dict = {"id": None, "len": -1, "first_id": None, "hash": ""}


def input_hash(all_workouts: list) -> str:
    """Stable sha1 over the identity tuples of `all_workouts`, scoped by
    INDEX_VERSION so a code-driven version bump also invalidates the
    module-level ``_INDEX_CACHE`` (not only localStorage).

    Two workout lists with the same sorted set of (date, type, distance, time)
    tuples hash identically, so the loader can detect when localStorage is
    still valid for the current workouts.
    """
    lid = id(all_workouts)
    n = len(all_workouts)
    first_id = id(all_workouts[0]) if n else None
    if (
        _HASH_MEMO["id"] == lid
        and _HASH_MEMO["len"] == n
        and _HASH_MEMO["first_id"] == first_id
    ):
        return _HASH_MEMO["hash"]

    ids = sorted(
        (
            w["day"],
            w["machine"],
            w.get("distance") or 0,
            w.get("time") or 0,
        )
        for w in all_workouts
    )
    h = hashlib.sha1()
    h.update(f"v{INDEX_VERSION}|".encode("utf-8"))
    for t in ids:
        h.update(repr(t).encode("utf-8"))
    digest = h.hexdigest()
    _HASH_MEMO["id"] = lid
    _HASH_MEMO["len"] = n
    _HASH_MEMO["first_id"] = first_id
    _HASH_MEMO["hash"] = digest
    return digest


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
    index = resolve_index(all_workouts)
    return _reference_watts_from_index(when, index, all_workouts)


def reference_watts_at_duration(
    when: date, duration_s: float, all_workouts: list
) -> Optional[float]:
    """Return reference watts at an arbitrary duration on a date.

    Builds the rower's continuous power-duration curve at ``when`` by querying
    :func:`get_reference_watts` to obtain watts at the ranked-event anchors,
    converting each anchor to a ``(duration_s, watts)`` pair, then
    interpolating piecewise-linearly in ``log(duration)`` vs ``log(watts)``
    space.

    Distance anchors are converted to duration via the Concept2 pace formula:
    ``pace = 500 / (W/2.80)^(1/3)``; ``duration = pace × dist_m / 500``.
    Time anchors carry their duration directly (``tenths/10``).

    Outside the ``[min_anchor, max_anchor]`` duration range, extrapolates on
    the nearest segment's log-log slope.  Returns ``None`` if the date has
    fewer than two populated anchors.
    """
    refs = get_reference_watts(when, all_workouts)
    return _interp_watts_at_duration(refs, duration_s)


def _interp_watts_at_duration(refs: dict, duration_s: float) -> Optional[float]:
    """Pure helper: log-log interpolation of (duration, watts) anchors.

    Split out from :func:`reference_watts_at_duration` so hot-loop callers can
    fetch a date's full ``refs`` dict once and resolve many durations against
    it without rehitting the index.
    """
    if not refs or duration_s <= 0:
        return None
    pts: list = []
    for ck, w in refs.items():
        if w is None or w <= 0:
            continue
        if ck[0] == "time":
            d = ck[1] / 10.0  # tenths → seconds
        else:
            # Distance event: duration = pace × dist / 500
            pace = 500.0 * (2.80 / w) ** (1.0 / 3.0)
            d = pace * ck[1] / 500.0
        if d > 0:
            pts.append((d, w))
    if len(pts) < 2:
        return None
    pts.sort()
    # Inside the anchor range: piecewise linear in log-log.
    if pts[0][0] <= duration_s <= pts[-1][0]:
        for (d0, w0), (d1, w1) in zip(pts, pts[1:]):
            if d0 <= duration_s <= d1:
                if d0 == d1:
                    return w0
                ld0, ld1 = math.log(d0), math.log(d1)
                f = (math.log(duration_s) - ld0) / (ld1 - ld0)
                return math.exp(math.log(w0) * (1 - f) + math.log(w1) * f)
        return pts[-1][1]
    # Outside the range: extrapolate on the nearest segment's log-log slope.
    if duration_s < pts[0][0]:
        (d0, w0), (d1, w1) = pts[0], pts[1]
    else:
        (d0, w0), (d1, w1) = pts[-2], pts[-1]
    if d0 == d1:
        return w0
    slope = (math.log(w1) - math.log(w0)) / (math.log(d1) - math.log(d0))
    return math.exp(math.log(w0) + slope * (math.log(duration_s) - math.log(d0)))


def resolve_index(all_workouts: list) -> dict:
    """Return the cached index for ``all_workouts``, building on miss.

    Hot-path callers that look up many dates against the same workouts list
    (e.g. ``make_thresholds_resolver``) should call this once and pass the
    result to ``_reference_watts_from_index`` per date — avoiding even the
    memoized ``input_hash`` lookup in the inner loop.
    """
    h = input_hash(all_workouts)
    index = _INDEX_CACHE.get(h)
    if index is None:
        index = build_reference_watts_index(all_workouts, _known_hash=h)
    return index


def _reference_watts_from_index(when: date, index: dict, all_workouts: list) -> dict:
    """Index-aware core of ``get_reference_watts`` — no hashing, no cache lookup."""
    watts = _interpolate(when, index)
    if index.get("markers") and when >= index["markers"][-1]["date"]:
        last_date = index["markers"][-1]["date"]
        recent = _get_recent_tail(index, last_date, all_workouts)
        watts = _merge_recent_tail(watts, when, recent)
    return watts


def _get_recent_tail(index: dict, last_marker_date: date, all_workouts: list) -> list:
    """Return cached list of ``(dt, cat_key, watts)`` tuples for rankable
    workouts after ``last_marker_date``.

    Stored on the index so it survives across renders for the same input_hash.
    Lazy — built on first access, including for indexes seeded from
    localStorage (which arrive without ``_recent_tail``).
    """
    cached = index.get("_recent_tail")
    if cached is not None:
        return cached
    tail: list = []
    for w in all_workouts:
        if not is_rankable_noninterval(w):
            continue
        dt = w["date_dt"]
        if dt <= last_marker_date:
            continue
        pace = w["pace"]
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        ck = w["cat_key"]
        if ck is None:
            continue
        tail.append((dt, ck, w["watts"]))
    index["_recent_tail"] = tail
    return tail


def build_reference_watts_index(
    all_workouts: list,
    on_progress=None,
    _known_hash: Optional[str] = None,
) -> dict:
    """Run the full quarterly build; return the index and cache it.

    Invariant: ``all_workouts`` must be from a single machine. Callers filter
    by ``gstate.machine`` upstream; the cache key (``input_hash``) includes
    the per-workout ``machine`` field as a safety check, naturally separating
    indices when this contract is honored.

    ``on_progress(i, n, label)`` is invoked once per marker (0-indexed) and one
    final time with (n, n, "done").  Idempotent: repeated calls with the same
    ``all_workouts`` return the cached index immediately.

    ``_known_hash`` is an internal optimization: callers that already computed
    the input hash can pass it through to avoid recomputing it here.
    """
    h = _known_hash if _known_hash is not None else input_hash(all_workouts)
    cached = _INDEX_CACHE.get(h)
    if cached is not None:
        if on_progress:
            n = len(cached.get("markers", []))
            on_progress(n, n, "done")
        return cached

    machine = workout_machine(all_workouts[0]) if all_workouts else "rower"
    quality = _quality_efforts(all_workouts)

    if not quality:
        index = {
            "version": INDEX_VERSION,
            "input_hash": h,
            "machine": machine,
            "markers": [],
        }
        _INDEX_CACHE[h] = index
        if on_progress:
            on_progress(0, 0, "done")
        return index

    first_effort = min(w["date_dt"] for w in quality)
    markers = _quarter_markers(first_effort, date.today())

    pd_fit_cache: dict = {}
    result_markers: list = []
    n = len(markers)
    for i, m in enumerate(markers):
        label = m.isoformat()
        if on_progress:
            on_progress(i, n, label)
        refs = _compute_marker_refs(m, quality, pd_fit_cache, machine)
        if refs is not None:
            result_markers.append(refs)

    if on_progress:
        on_progress(n, n, "done")

    index = {
        "version": INDEX_VERSION,
        "input_hash": h,
        "machine": machine,
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
        dt = w["date_dt"]
        if dt < start or dt > end:
            continue
        pace = w["pace"]
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        ck = w["cat_key"]
        if ck is None:
            continue
        time_s = (w.get("time") or 0) / 10.0
        if time_s <= 0:
            continue
        watts = w["watts"]
        cur = best.get(ck)
        if cur is None or pace < cur["pace"]:
            best[ck] = {
                "pace": pace,
                "duration_s": time_s,
                "watts": watts,
                "id": w["id"],
                "distance": w.get("distance"),
                "cat_key": ck,
            }
    return best


def _compute_marker_refs(
    marker: date, quality: list, pd_cache: dict, machine: str
) -> Optional[dict]:
    """Build ``MarkerRefs`` for one marker, or None if no PBs in window."""
    start = marker - timedelta(days=WINDOW_DAYS)
    pbs = _window_pbs(quality, start, marker)
    if not pbs:
        return None

    pb_list = list(pbs.values())
    all_events = _all_events(machine)
    all_cat_keys = _all_cat_keys(machine)

    # Predictor cascade.
    pd_params = None
    if len(pb_list) >= 5:
        pd_params = _cached_pd_fit(pb_list, pd_cache)

    predicted: dict = {}
    source_tag: dict = {}

    if pd_params is not None:
        # Bound CP extrapolation by the duration range of the actual PBs.
        # The 4-parameter CP model is poorly constrained outside its anchor
        # data: with no sub-1min PB, the fast-twitch component runs free and
        # produces above-WR watts at 100m.  Allow ±1 octave of slack so the
        # smooth model still covers events just outside the PB span, but no
        # further — Paul's Law fills in the extremes via the fallback below.
        pb_durations = [p["duration_s"] for p in pb_list if p.get("duration_s")]
        if pb_durations:
            t_lo = min(pb_durations) / 2.0
            t_hi = max(pb_durations) * 2.0
        else:
            t_lo, t_hi = None, None

        for ck, dist_m, dur_s in all_events:
            w = _pd_watts_at_event(pd_params, dist_m, dur_s)
            if w is None:
                continue
            # Derive the event's implied duration so we can gate the CP
            # prediction against the PB-range bound.
            if dur_s is not None:
                event_dur = dur_s
            elif w > 0:
                pace = 500.0 * (2.80 / w) ** (1.0 / 3.0)
                event_dur = pace * dist_m / 500.0
            else:
                continue
            if t_lo is not None and not (t_lo <= event_dur <= t_hi):
                continue  # outside PB range — let Paul's Law fill in
            predicted[ck] = w
            source_tag[ck] = "cp"

    # Fall back through Paul's Law where CP didn't cover (or wasn't fit).
    if len(predicted) < len(all_cat_keys):
        # Build lifetime-best-style dicts for Paul's regression.
        lb = {ck: p["pace"] for ck, p in pbs.items()}
        lb_anchor: dict = {}
        for ck, p in pbs.items():
            if ck[0] == "dist":
                lb_anchor[ck] = ck[1]
            else:
                # Time event: anchor distance = pace × duration / 500
                lb_anchor[ck] = p["pace"] and (500.0 * p["duration_s"] / p["pace"])

        k = compute_pauls_constant(lb, lb_anchor) if len(pbs) >= 4 else None
        if k is None:
            k = PAULS_DEFAULT_K
        pauls_source = "pauls" if len(pbs) >= 4 else "pauls_default"

        # Build (anchor_pace, anchor_dist) for every PB so we can average
        # Paul's-Law projections across all anchors rather than projecting
        # only from the fastest one.  Single-anchor extrapolation from the
        # fastest PB systematically over-predicts at events shorter than
        # that anchor (the sprint bias of e.g. projecting 500m → 100m).
        # Averaging across anchors dilutes that bias and tracks actual
        # short-distance performance better.  Mirrors the "Paul's Law
        # (average)" predictor in :mod:`services.predictions`.
        anchors: list[tuple[float, float]] = []
        for p in pb_list:
            a_pace = p["pace"]
            a_dist = lb_anchor.get(p["cat_key"])
            if not a_dist:
                a_dist = p.get("distance") or (
                    500.0 * p["duration_s"] / a_pace if a_pace else None
                )
            if a_pace and a_dist:
                anchors.append((a_pace, a_dist))

        for ck, dist_m, dur_s in all_events:
            if ck in predicted:
                continue
            watts_list = [
                w
                for w in (
                    _pauls_watts_at_event(a_pace, a_dist, dist_m, dur_s, k)
                    for a_pace, a_dist in anchors
                )
                if w is not None
            ]
            if watts_list:
                predicted[ck] = sum(watts_list) / len(watts_list)
                source_tag[ck] = pauls_source

    # Merge in actual PB watts.  Performance is asymmetric evidence: a real PB
    # *above* the prediction means the rower beat the model — adopt the PB
    # outright.  A PB *below* the prediction may reflect a sub-maximal day
    # (didn't go all-out, just trained at race pace, etc.), so blend with the
    # prediction to keep cross-event coherence.
    final_watts: dict = {}
    final_source: dict = {}
    for ck in all_cat_keys:
        pb = pbs.get(ck)
        pred_w = predicted.get(ck)
        if pb is not None and pred_w is not None:
            if pb["watts"] > pred_w:
                final_watts[ck] = pb["watts"]
                final_source[ck] = "pb"
            else:
                final_watts[ck] = (pb["watts"] + pred_w) / 2.0
                final_source[ck] = source_tag.get(ck, "pb")
        elif pb is not None:
            final_watts[ck] = pb["watts"]
            final_source[ck] = "pb"
        elif pred_w is not None:
            final_watts[ck] = pred_w
            final_source[ck] = source_tag.get(ck, "pauls")
        # else: leave unpopulated; interpolation handles gaps.

    if not final_watts:
        return None

    # Enforce monotonic non-increase of watts vs duration.  Predictions are
    # monotonic by construction, but blending a sub-prediction PB with the
    # prediction can pull a merged value below adjacent-event predictions
    # (e.g. a slow 5k blended to ~250W when the neighbouring 6k prediction
    # is 270W).  Walk events in duration order and cap each longer event at
    # the running minimum so the curve respects the power-duration ordering.
    def _event_duration_s(ck: tuple, watts: float) -> float:
        if ck[0] == "time":
            return ck[1] / 10.0
        if watts <= 0:
            return float("inf")
        pace = 500.0 * (2.80 / watts) ** (1.0 / 3.0)
        return pace * ck[1] / 500.0

    cks_in_dur_order = sorted(
        final_watts.keys(),
        key=lambda c: _event_duration_s(c, final_watts[c]),
    )
    prev_w: Optional[float] = None
    for ck in cks_in_dur_order:
        if prev_w is not None and final_watts[ck] > prev_w:
            final_watts[ck] = prev_w
        prev_w = final_watts[ck]

    return {
        "date": marker,
        "watts": final_watts,
        "source": final_source,
        "n_pbs": len(pbs),
    }


def _cached_pd_fit(pb_list: list, pd_cache: dict) -> Optional[dict]:
    """CP fit with a caller-provided cache keyed by sorted PB ids."""
    key = frozenset((p["id"] or (p["cat_key"], p["duration_s"])) for p in pb_list)
    if key in pd_cache:
        return pd_cache[key]
    params = fit_power_duration(pb_list)
    pd_cache[key] = params
    return params


# ---------------------------------------------------------------------------
# Predictor evaluation at each event
# ---------------------------------------------------------------------------


def _pd_watts_at_event(
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
        watts = power_duration_model(dur_s, Pow1, tau1, Pow2, tau2)
        return float(watts) if watts > 0 else None

    if dist_m is None or brentq is None:
        return None

    def _residual(t, _d=dist_m):
        P = power_duration_model(t, Pow1, tau1, Pow2, tau2)
        if P <= 0:
            return -_d
        return (P / 2.80) ** (1.0 / 3.0) * t - _d

    try:
        t_star = brentq(_residual, _CURVE_T_MIN, _CURVE_T_MAX, xtol=0.1)
    except Exception:
        return None
    watts = power_duration_model(t_star, Pow1, tau1, Pow2, tau2)
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

    machine = index.get("machine") or "rower"
    out: dict = {}
    for ck in _all_cat_keys(machine):
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


def _merge_recent_tail(watts_dict: dict, when: date, recent_tail: list) -> dict:
    """Apply "better-of" PB merge using the precomputed recent-tail list.

    A fresh PB after the last marker is hard evidence the prior prediction is
    stale — upgrade, don't average.

    ``recent_tail`` is a list of ``(dt, cat_key, watts)`` tuples already
    filtered to rankable non-interval workouts after the last marker — see
    ``_get_recent_tail``.
    """
    out = dict(watts_dict)
    for dt, ck, watts in recent_tail:
        if dt > when:
            continue
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
        "machine": index.get("machine", "rower"),
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
        "machine": json_dict.get("machine", "rower"),
        "markers": [
            {
                "date": date.fromisoformat(m["date"]),
                "watts": {
                    _ck_from_str(k): float(v) for k, v in m.get("watts", {}).items()
                },
                "source": {_ck_from_str(k): s for k, s in m.get("source", {}).items()},
                "n_pbs": m.get("n_pbs", 0),
            }
            for m in json_dict.get("markers", [])
        ],
    }
