"""
Prediction models: catalog, per-model pace samplers, and prediction-table builder.

One stop for everything about prediction models used on the Power Curve page.

Three logical sections:

1. **Predictor registry** — `Predictor`, `PREDICTORS`, `PREDICTORS_BY_KEY`.
   Catalog of the five prediction models (CP, log-log, Paul's Law, RowingLevel,
   Average) plus the "none" sentinel.  UI-neutral: describes each model's
   name/description/component-behaviour; consumers decide how to render.

2. **Per-model pace samplers** — pure functions, one per model, each answering
   "at distance d meters, what pace (sec/500m) does this model predict?"
   Returns None when inputs are insufficient or the result falls outside
   [PACE_MIN, PACE_MAX].
       loglog_pace_at(slope, intercept, dist_m)
       pauls_law_pace_at(lifetime_best, lifetime_best_anchor, dist_m, k=5.0)
       cp_pace_at(cp_params, dist_m)         cp_params: dict or 4-tuple
       cp_pace_at_time(cp_params, T_seconds) direct watts(t) evaluation
       rowinglevel_pace_at(rl_predictions, lifetime_best_anchor, dist_m)

3. **Prediction-table builder** — `build_prediction_table_data` computes all
   four predictor columns for every ranked event, plus per-model RMSE/R²
   accuracy metrics vs actual PBs.  Returns
   ``{"rows": [...], "accuracy": {...}}``; rows are sorted by expected duration
   so that distance and timed events interleave in power-duration order (e.g.
   1 min falls between 100m and 500m).

Private helpers:
    _rl_interp_pace()   log-log interpolation between RowingLevel distance→pace
    _fmt_pred()         format a predicted pace → (pace_str, result_str)
    _solve_timed_pace() numerically find the pace that fills a timed event

All code here is pure — no I/O, no HyperDiv, no hidden state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from scipy.optimize import brentq

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    PACE_MIN,
    PACE_MAX,
    loglog_fit,
    loglog_predict_pace,
    pauls_law_pace,
    watts_to_pace,
)
from services.critical_power_model import critical_power_model
from services.formatters import fmt_split, fmt_result_duration


# ═══════════════════════════════════════════════════════════════════════════
# 1. Predictor registry
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Predictor:
    """Metadata describing one prediction model.

    Fields are UI-neutral strings describing the model; consumers (the chart
    settings dropdown, the prediction-table header, the components toggle)
    decide how to render them.
    """

    key: str  # "critical_power" | "loglog" | "pauls_law" | "rowinglevel" | "average" | "none"
    name: str  # Short display label (dropdown + table column header)
    extended_description: str  # Full description — dropdown option text + prediction-table tooltip
    computed_from_components: bool  # True when this model is computed by averaging per-anchor / per-model components
    component_label: str  # Checkbox label for "Show components" gear; "" when N/A
    component_desc: str  # Tooltip under the checkbox; "" when N/A


PREDICTORS: tuple = (
    Predictor(
        key="critical_power",
        name="Critical Power",
        extended_description=(
            "Two-component power-duration model (veloclinic). "
            "Requires 5 or more PBs spanning a 10:1 duration ratio. "
            "Method from rowsandall.com."
        ),
        computed_from_components=True,
        component_label="Show fast-twitch & slow-twitch components",
        component_desc="Shows the fast-twitch and slow-twitch power components separately.",
    ),
    Predictor(
        key="loglog",
        name="Log-Log Watts Fit",
        extended_description=(
            "Fits a power law (log watts vs log distance) across all scoped PBs. "
            "Similar to the Free Spirits Pace Predictor (freespiritsrowing.com) "
            "but uses all PBs, not just two."
        ),
        computed_from_components=False,
        component_label="",
        component_desc="",
    ),
    Predictor(
        key="pauls_law",
        name="Paul's Law (average)",
        extended_description=(
            "Predicts +5.0 s/500m for each doubling of distance "
            "(population default — needs 2 or more PBs to personalise), "
            "applied from each anchor PB and averaged."
        ),
        computed_from_components=True,
        component_label="Show one curve per anchor",
        component_desc="Shows one curve per PB anchor, before averaging.",
    ),
    Predictor(
        key="rowinglevel",
        name="RowingLevel (average)",
        extended_description=(
            "Predictions from rowinglevel.com based on your profile "
            "(gender, age, bodyweight). Distance-weighted average across all "
            "anchor PBs. Distance events only."
        ),
        computed_from_components=True,
        component_label="Show one RL curve per anchor",
        component_desc="Shows the RL curve from each PB anchor, before distance-weighted averaging.",
    ),
    Predictor(
        key="average",
        name="Average of all techniques",
        extended_description="Mean of all available predictions for this event.",
        computed_from_components=True,
        component_label="Show individual model curves",
        component_desc="Shows all individual model curves that were averaged.",
    ),
    Predictor(
        key="none",
        name="...actually, don't predict",
        extended_description="Hide the prediction curve.",
        computed_from_components=False,
        component_label="",
        component_desc="",
    ),
)

PREDICTORS_BY_KEY: dict = {p.key: p for p in PREDICTORS}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Per-model pace samplers
# ═══════════════════════════════════════════════════════════════════════════


def _pace_if_valid(p: Optional[float]) -> Optional[float]:
    """Return p when it's a realistic pace (sec/500m), else None."""
    return p if p is not None and PACE_MIN <= p <= PACE_MAX else None


def loglog_pace_at(
    slope: Optional[float],
    intercept: Optional[float],
    dist_m: float,
) -> Optional[float]:
    """Log-log power-law fit evaluated at dist_m."""
    if slope is None or intercept is None:
        return None
    return _pace_if_valid(loglog_predict_pace(slope, intercept, dist_m))


def pauls_law_pace_at(
    lifetime_best: dict,
    lifetime_best_anchor: dict,
    dist_m: float,
    k: float = 5.0,
) -> Optional[float]:
    """Average of Paul's Law predictions from each anchor PB."""
    if not lifetime_best:
        return None
    paces = []
    for cat, pb_pace in lifetime_best.items():
        anchor = lifetime_best_anchor.get(cat)
        if not anchor:
            continue
        p = pauls_law_pace(pb_pace, anchor, dist_m, k=k)
        if PACE_MIN <= p <= PACE_MAX:
            paces.append(p)
    return sum(paces) / len(paces) if paces else None


def _cp_unpack(cp_params):
    """Return (Pow1, tau1, Pow2, tau2) or None if cp_params is malformed."""
    if cp_params is None:
        return None
    if isinstance(cp_params, dict):
        if not all(k in cp_params for k in ("Pow1", "tau1", "Pow2", "tau2")):
            return None
        return (
            cp_params["Pow1"],
            cp_params["tau1"],
            cp_params["Pow2"],
            cp_params["tau2"],
        )
    return cp_params


def cp_pace_at(cp_params, dist_m: float) -> Optional[float]:
    """Critical Power pace at dist_m.

    cp_params accepts either a dict {Pow1, tau1, Pow2, tau2} or a 4-tuple
    in that order.  Requires numerical inversion to solve for the duration
    at which the rower covers exactly dist_m meters at the model's pace.
    """
    params = _cp_unpack(cp_params)
    if params is None:
        return None
    Pow1, tau1, Pow2, tau2 = params

    def _resid(t, _d=dist_m):
        P = critical_power_model(t, Pow1, tau1, Pow2, tau2)
        return (P / 2.80) ** (1.0 / 3.0) * t - _d if P > 0 else -_d

    try:
        t_star = brentq(_resid, 10.0, 20_000.0, xtol=0.5)
    except Exception:
        return None
    watts = critical_power_model(t_star, Pow1, tau1, Pow2, tau2)
    if watts <= 0:
        return None
    return _pace_if_valid(watts_to_pace(watts))


def cp_pace_at_time(cp_params, T_seconds: float) -> Optional[float]:
    """Critical Power pace at a fixed duration T_seconds.

    CP is formulated as watts(t), so for a timed event of T seconds the
    predicted watts are a direct evaluation — no inversion needed.  This is
    the correct sampler for timed events; ``_solve_timed_pace`` fails to
    bracket a root when composed with ``cp_pace_at``'s own inversion.
    """
    params = _cp_unpack(cp_params)
    if params is None:
        return None
    Pow1, tau1, Pow2, tau2 = params
    watts = critical_power_model(T_seconds, Pow1, tau1, Pow2, tau2)
    if watts <= 0:
        return None
    return _pace_if_valid(watts_to_pace(watts))


def _rl_interp_pace(preds: dict, target_dist: float) -> Optional[float]:
    """
    Log-log interpolate (or extrapolate) an RL pace prediction at ``target_dist``
    meters from the discrete distance→pace dict returned by the RL scraper.

    RL dicts may have integer or string keys; both are handled.
    Returns None if fewer than two valid data points are available.
    """
    known: list[tuple[float, float]] = []
    for k, v in preds.items():
        try:
            d = float(k)
        except (ValueError, TypeError):
            continue
        if d > 0 and isinstance(v, (int, float)) and PACE_MIN <= v <= PACE_MAX:
            known.append((d, v))
    if len(known) < 2:
        return None
    known.sort()

    dists = [d for d, _ in known]
    if target_dist <= dists[0]:
        lo, hi = known[0], known[1]
    elif target_dist >= dists[-1]:
        lo, hi = known[-2], known[-1]
    else:
        for i in range(len(known) - 1):
            if known[i][0] <= target_dist <= known[i + 1][0]:
                lo, hi = known[i], known[i + 1]
                break
        else:
            lo, hi = known[-2], known[-1]

    try:
        log_d_lo = math.log(lo[0])
        log_d_hi = math.log(hi[0])
        if log_d_hi == log_d_lo:
            return None
        t = (math.log(target_dist) - log_d_lo) / (log_d_hi - log_d_lo)
        log_pace = math.log(lo[1]) + t * (math.log(hi[1]) - math.log(lo[1]))
        return math.exp(log_pace)
    except (ValueError, ZeroDivisionError):
        return None


def _rl_pace_from_preds(preds: dict, dist_m: float) -> Optional[float]:
    """Single-anchor RL pace at dist_m: exact hit if RL published that
    distance, else log-log interpolation within this anchor's curve."""
    p = preds.get(int(dist_m)) or preds.get(str(int(dist_m)))
    if p is not None and PACE_MIN <= p <= PACE_MAX:
        return p
    return _pace_if_valid(_rl_interp_pace(preds, dist_m))


def rowinglevel_pace_at(
    rl_predictions: Optional[dict],
    lifetime_best_anchor: dict,
    dist_m: float,
) -> Optional[float]:
    """Distance-weighted average of RL predictions across all anchor PBs.

    Weight = 1 / (|log2(dist_m / anchor)| + 0.5), so anchors closer (in
    log-distance) to dist_m dominate the average.  Falls back to uniform
    weights when no anchor distance is available for a category.
    """
    if not rl_predictions:
        return None

    str_lba = {str(k): v for k, v in lifetime_best_anchor.items()}
    paces: list = []
    weights: list = []
    for cat_key, preds in rl_predictions.items():
        p = _rl_pace_from_preds(preds, dist_m)
        if p is None:
            continue
        anchor = str_lba.get(cat_key)
        w = (
            1.0 / (abs(math.log2(dist_m / anchor)) + 0.5)
            if anchor and dist_m > 0
            else 1.0
        )
        paces.append(p)
        weights.append(w)
    if not paces:
        return None
    total_w = sum(weights)
    return sum(w * p for w, p in zip(weights, paces)) / total_w


# ═══════════════════════════════════════════════════════════════════════════
# 3. Prediction-table builder
# ═══════════════════════════════════════════════════════════════════════════


# Short display labels — derived from RANKED_DISTANCES so there is one source
# of truth for event names.
_DIST_LABELS: dict = {d: lbl for d, lbl in RANKED_DISTANCES}


def _fmt_pred(
    pace: Optional[float], event_type: str, event_value: int
) -> Optional[tuple]:
    """
    Format a predicted pace into a (pace_str, result_str) tuple.

      distance event  → ("M:SS.t", result)   pace per 500m, predicted total time
                        result uses fmt_result_duration for readability:
                        sub-hour → "M:SS.t", ≥1 hour → "1hr 23m 03.7s"
      timed event     → ("M:SS.t", "N,NNNm")  pace per 500m, predicted meters

    Returns None if pace is unavailable or outside [PACE_MIN, PACE_MAX].
    """
    if pace is None or not (PACE_MIN <= pace <= PACE_MAX):
        return None
    pace_str = fmt_split(round(pace * 10))
    if event_type == "dist":
        time_tenths = round(pace * event_value / 500.0 * 10)
        return (pace_str, fmt_result_duration(time_tenths))
    else:
        T = event_value / 10.0
        dist_m = round(T * 500.0 / pace)
        return (pace_str, f"{dist_m:,}m")


def _solve_timed_pace(pace_fn, T_seconds: float) -> Optional[float]:
    """
    Numerically find the pace (sec/500m) at which a rower covers exactly
    ``T_seconds`` seconds at pace ``pace_fn(distance)``.

    Solves  pace_fn(d) × d / 500 = T  for d in [100m, 100km] using Brent's
    method, then returns pace_fn(d_star).  Returns None on failure or if the
    result is outside [PACE_MIN, PACE_MAX].

    ``pace_fn`` should accept a distance in meters and return a pace in
    sec/500m, or None if the pace cannot be computed at that distance.
    """

    def _residual(d):
        p = pace_fn(max(d, 1.0))
        if p is None:
            return -T_seconds
        return p * max(d, 1.0) / 500.0 - T_seconds

    try:
        d_star = brentq(_residual, 100.0, 100_000.0, xtol=1.0)
    except Exception:
        return None
    pace = pace_fn(d_star)
    return pace if pace is not None and PACE_MIN <= pace <= PACE_MAX else None


def _compute_predictor_raws(
    event_type: str,
    event_value: int,
    *,
    cp_params: Optional[tuple],
    ll_slope: Optional[float],
    ll_intercept: Optional[float],
    lifetime_best: dict,
    lifetime_best_anchor: dict,
    rl_predictions: Optional[dict],
    pauls_k: float = 5.0,
) -> dict:
    """
    Compute raw pace (sec/500m) for each predictor for one event.

    Returns a dict with keys: cp_raw, ll_raw, pl_raw, rl_raw.
    Any value may be None if unavailable or outside [PACE_MIN, PACE_MAX].

    event_type == "dist": event_value is meters — sampler called directly.
    event_type == "time": event_value is tenths-of-seconds; each sampler is
        inverted via _solve_timed_pace to find the distance a rower covers
        in exactly T seconds.
    cp_params: (Pow1, tau1, Pow2, tau2) tuple or None.
    pauls_k: personalised Paul's Law constant (sec/500m per doubling); default 5.0.
    """
    is_dist = event_type == "dist"
    dist_m = event_value if is_dist else None
    T = None if is_dist else event_value / 10.0

    def _at(sampler):
        """Evaluate sampler at the event distance (direct) or solve-for-T (timed)."""
        if is_dist:
            return sampler(dist_m)
        return _solve_timed_pace(sampler, T)

    # CP is formulated as watts(t), so for timed events evaluate directly.
    # Composing _solve_timed_pace with cp_pace_at's own inversion fails to
    # bracket a root in brentq.
    cp_raw = (
        cp_pace_at(cp_params, dist_m) if is_dist else cp_pace_at_time(cp_params, T)
    )

    return {
        "cp_raw": cp_raw,
        "ll_raw": _at(lambda d: loglog_pace_at(ll_slope, ll_intercept, d)),
        "pl_raw": _at(
            lambda d: pauls_law_pace_at(
                lifetime_best, lifetime_best_anchor, d, k=pauls_k
            )
        ),
        "rl_raw": _at(
            lambda d: rowinglevel_pace_at(rl_predictions, lifetime_best_anchor, d)
        ),
    }


def build_prediction_table_data(
    *,
    lifetime_best: dict,
    lifetime_best_anchor: dict,
    all_lifetime_best: dict,
    all_lifetime_best_anchor: dict,
    critical_power_params: Optional[dict] = None,
    rl_predictions: Optional[dict] = None,
    pauls_k: float = 5.0,
    selected_dist_set: Optional[set] = None,
    selected_time_set: Optional[set] = None,
) -> dict:
    """
    Compute prediction-table rows plus per-model accuracy.

    Always emits one row per canonical ranked event (all RANKED_DISTANCES then
    all RANKED_TIMES), regardless of the event-filter selection in the chart UI.

    ``lifetime_best`` / ``lifetime_best_anchor`` are the *filtered* bests (the
    same set used by the chart) and drive the prediction columns.
    ``all_lifetime_best`` / ``all_lifetime_best_anchor`` are unfiltered (only
    gated on timeline_date and excluded seasons) and are used for the "Your PB"
    column so that PBs in events the user has hidden still appear.

    ``selected_dist_set`` / ``selected_time_set`` restrict which rows are
    considered when computing the accuracy footer (RMSE / R² vs ``pb_raw``).
    When either is None, every event counts — useful when a caller doesn't
    have an enabled-event selection.

    Returns ``{"rows": [...], "accuracy": {...}}`` where:
      rows      — list of per-event row dicts.  Keys:
                    label, event_type, event_value,
                    average_pace/result/raw,
                    critical_power_pace/result/raw,
                    loglog_pace/result/raw,
                    pauls_law_pace/result/raw,
                    rowinglevel_pace/result/raw,
                    pb_pace/result/raw  (athlete's unfiltered best)
                  Prefixes match ``Predictor.key`` so callers can use
                  ``row[f"{p.key}_pace"]`` directly.
      accuracy  — dict[str, dict] keyed by Predictor.key
                  ("average", "critical_power", "loglog", "pauls_law",
                  "rowinglevel") → {"rmse": float|None, "r2": float|None,
                  "n": int}.  rmse/r2 are None when fewer than one (rmse) or
                  two (r2) matching (prediction, PB) pairs exist.
    """
    # ── Log-Log fit (single fit across all filtered lifetime PBs) ─────────────
    _ll_fit = loglog_fit(lifetime_best, lifetime_best_anchor) if lifetime_best else None
    _ll_slope, _ll_intercept = _ll_fit if _ll_fit is not None else (None, None)

    # ── CP params — convert dict to tuple once ────────────────────────────────
    _cp = critical_power_params
    _cp_tuple: Optional[tuple] = (
        (_cp["Pow1"], _cp["tau1"], _cp["Pow2"], _cp["tau2"])
        if _cp is not None
        else None
    )

    def _cell(raw_pace: Optional[float], event_type: str, event_value: int) -> tuple:
        """Return (raw, pace_str, result_str) — raw is None when pace is invalid."""
        t = _fmt_pred(raw_pace, event_type, event_value)
        if t is None:
            return (None, None, None)
        return (raw_pace, t[0], t[1])

    def _predictor_kwargs():
        return dict(
            cp_params=_cp_tuple,
            ll_slope=_ll_slope,
            ll_intercept=_ll_intercept,
            lifetime_best=lifetime_best,
            lifetime_best_anchor=lifetime_best_anchor,
            rl_predictions=rl_predictions,
            pauls_k=pauls_k,
        )

    rows: list[dict] = []

    def _build_row(event_type: str, event_value: int, label: str) -> dict:
        raws = _compute_predictor_raws(event_type, event_value, **_predictor_kwargs())
        cp_raw, cp_pace, cp_result = _cell(raws["cp_raw"], event_type, event_value)
        ll_raw, ll_pace, ll_result = _cell(raws["ll_raw"], event_type, event_value)
        pl_raw, pl_pace, pl_result = _cell(raws["pl_raw"], event_type, event_value)
        rl_raw, rl_pace, rl_result = _cell(raws["rl_raw"], event_type, event_value)
        pb_raw = all_lifetime_best.get((event_type, event_value))
        pb_raw, pb_pace, pb_result = _cell(pb_raw, event_type, event_value)
        _avg_cands = [r for r in [cp_raw, ll_raw, pl_raw, rl_raw] if r is not None]
        _avg_r = sum(_avg_cands) / len(_avg_cands) if _avg_cands else None
        _avg_r, avg_pace, avg_result = _cell(_avg_r, event_type, event_value)
        return {
            "label": label,
            "event_type": event_type,
            "event_value": event_value,
            "average_pace": avg_pace,
            "average_result": avg_result,
            "average_raw": _avg_r,
            "critical_power_pace": cp_pace,
            "critical_power_result": cp_result,
            "critical_power_raw": cp_raw,
            "loglog_pace": ll_pace,
            "loglog_result": ll_result,
            "loglog_raw": ll_raw,
            "pauls_law_pace": pl_pace,
            "pauls_law_result": pl_result,
            "pauls_law_raw": pl_raw,
            "rowinglevel_pace": rl_pace,
            "rowinglevel_result": rl_result,
            "rowinglevel_raw": rl_raw,
            "pb_pace": pb_pace,
            "pb_result": pb_result,
            "pb_raw": pb_raw,
        }

    # ── Distance events ───────────────────────────────────────────────────────
    for dist_m, label in RANKED_DISTANCES:
        rows.append(
            _build_row("dist", dist_m, _DIST_LABELS.get(dist_m, f"{dist_m:,}m"))
        )

    # ── Timed events ──────────────────────────────────────────────────────────
    for time_tenths, label in RANKED_TIMES:
        rows.append(_build_row("time", time_tenths, label))

    # ── Sort all rows by expected duration (mixes distance and timed events) ─────
    # Distance event duration: derived from log-log prediction if available, else
    # uses a typical default pace of 110 sec/500m as a fallback.
    # Timed event duration: event_value / 10 (tenths → seconds, direct).
    _FALLBACK_PACE = 110.0  # sec/500m — reasonable middle-of-road estimate

    def _expected_duration_s(row: dict) -> float:
        if row["event_type"] == "time":
            return row["event_value"] / 10.0
        # Distance event
        pace = row.get("loglog_raw") or _FALLBACK_PACE
        return row["event_value"] * pace / 500.0

    rows.sort(key=_expected_duration_s)

    # ── Accuracy: RMSE + R² per model vs actual PB, over enabled events ──
    # When selected_dist_set / selected_time_set are None, every event counts.
    accuracy: dict = {}
    _dist_ok = selected_dist_set is None
    _time_ok = selected_time_set is None
    for _ck in ("average", "critical_power", "loglog", "pauls_law", "rowinglevel"):
        _pairs = [
            (r[f"{_ck}_raw"], r["pb_raw"])
            for r in rows
            if r.get(f"{_ck}_raw") is not None
            and r.get("pb_raw") is not None
            and (
                (r["event_type"] == "dist"
                 and (_dist_ok or r["event_value"] in selected_dist_set))
                or (r["event_type"] == "time"
                    and (_time_ok or r["event_value"] in selected_time_set))
            )
        ]
        if _pairs:
            _actuals_v = [a for _, a in _pairs]
            _mean_actual = sum(_actuals_v) / len(_actuals_v)
            _ss_res = sum((p - a) ** 2 for p, a in _pairs)
            _ss_tot = sum((a - _mean_actual) ** 2 for a in _actuals_v)
            accuracy[_ck] = {
                "rmse": (_ss_res / len(_pairs)) ** 0.5,
                "r2": 1.0 - _ss_res / _ss_tot if _ss_tot > 0 else None,
                "n": len(_pairs),
            }
        else:
            accuracy[_ck] = {"rmse": None, "r2": None, "n": 0}

    return {"rows": rows, "accuracy": accuracy}
