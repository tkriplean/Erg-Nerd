"""
Prediction table data builder for the ranked-events view.

Exported:
  build_prediction_table_data() — compute all four predictor columns for every
                                  ranked event, returning a list of row dicts.
                                  Rows are sorted by expected duration so that
                                  distance and timed events are interleaved in
                                  power-duration order (e.g. 1 min falls between
                                  100m and 500m).

Private helpers:
  _rl_interp_pace()   — log-log interpolation between RowingLevel distance→pace pairs
  _fmt_pred()         — format a predicted pace into (pace_str, result_str)
  _solve_timed_pace() — numerically find the pace that fills a timed event duration
"""

from __future__ import annotations

import math
from typing import Optional

from scipy.optimize import brentq

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    PACE_MIN,
    PACE_MAX,
    pauls_law_pace,
    loglog_fit,
    loglog_predict_pace,
    watts_to_pace,
)
from services.critical_power_model import critical_power_model
from services.formatters import fmt_split, fmt_result_duration


# ---------------------------------------------------------------------------
# Short display labels — derived from RANKED_DISTANCES so there is one source
# of truth for event names.
# ---------------------------------------------------------------------------

_DIST_LABELS: dict = {d: lbl for d, lbl in RANKED_DISTANCES}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-event predictor computation
# ---------------------------------------------------------------------------


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

    event_type == "dist": event_value is meters.
    event_type == "time": event_value is tenths-of-seconds; T = event_value / 10.
    cp_params: (Pow1, tau1, Pow2, tau2) tuple or None.
    pauls_k: personalised Paul's Law constant (sec/500m per doubling); default 5.0.
    """
    is_dist = event_type == "dist"
    dist_m = event_value if is_dist else None
    T = None if is_dist else event_value / 10.0

    # ── Critical Power ────────────────────────────────────────────────────────
    cp_raw = None
    if cp_params is not None:
        Pow1, tau1, Pow2, tau2 = cp_params
        if is_dist:

            def _cp_resid(t, _d=dist_m, P1=Pow1, t1=tau1, P2=Pow2, t2=tau2):
                P = critical_power_model(t, P1, t1, P2, t2)
                return (P / 2.80) ** (1.0 / 3.0) * t - _d if P > 0 else -_d

            try:
                t_star = brentq(_cp_resid, 10.0, 20_000.0, xtol=0.1)
                watts = critical_power_model(t_star, Pow1, tau1, Pow2, tau2)
                if watts > 0:
                    cp_raw = watts_to_pace(watts)
            except Exception:
                pass
        else:
            watts = critical_power_model(T, Pow1, tau1, Pow2, tau2)
            if watts > 0:
                cp_raw = watts_to_pace(watts)

    # ── Log-Log ───────────────────────────────────────────────────────────────
    ll_raw = None
    if ll_slope is not None:
        if is_dist:
            ll_raw = loglog_predict_pace(ll_slope, ll_intercept, dist_m)
        else:
            ll_raw = _solve_timed_pace(
                lambda d, s=ll_slope, i=ll_intercept: loglog_predict_pace(s, i, d), T
            )

    # ── Paul's Law ────────────────────────────────────────────────────────────
    pl_raw = None
    if lifetime_best:
        _pl_paces = []
        for cat, pb_pace in lifetime_best.items():
            anchor = lifetime_best_anchor.get(cat)
            if not anchor:
                continue
            if is_dist:
                predicted = pauls_law_pace(pb_pace, anchor, dist_m, k=pauls_k)
                if PACE_MIN <= predicted <= PACE_MAX:
                    _pl_paces.append(predicted)
            else:
                pace_star = _solve_timed_pace(
                    lambda d, p=pb_pace, a=anchor: pauls_law_pace(p, a, d, k=pauls_k), T
                )
                if pace_star is not None:
                    _pl_paces.append(pace_star)
        if _pl_paces:
            pl_raw = sum(_pl_paces) / len(_pl_paces)

    # ── RowingLevel — distance-weighted average ───────────────────────────────
    # rl_predictions keys are str(tuple) e.g. "('dist', 2000)"; normalise once.
    _str_lba = {str(k): v for k, v in lifetime_best_anchor.items()}
    rl_raw = None
    if rl_predictions:
        _rl_paces: list = []
        _rl_weights: list = []
        for cat_key, preds in rl_predictions.items():
            if is_dist:
                anchor_dist = _str_lba.get(cat_key)
                pace = preds.get(dist_m) or preds.get(str(dist_m))
                if pace is not None and PACE_MIN <= pace <= PACE_MAX:
                    w = (
                        1.0 / (abs(math.log2(dist_m / anchor_dist)) + 0.5)
                        if anchor_dist
                        else 1.0
                    )
                    _rl_paces.append(pace)
                    _rl_weights.append(w)
            else:
                pace_star = _solve_timed_pace(
                    lambda d, p=preds: _rl_interp_pace(p, d), T
                )
                if pace_star is not None:
                    _rl_paces.append(pace_star)
                    _rl_weights.append(1.0)
        if _rl_paces:
            total_w = sum(_rl_weights)
            rl_raw = sum(w * p for w, p in zip(_rl_weights, _rl_paces)) / total_w

    # print({"cp_raw": cp_raw, "ll_raw": ll_raw, "pl_raw": pl_raw, "rl_raw": rl_raw})
    return {"cp_raw": cp_raw, "ll_raw": ll_raw, "pl_raw": pl_raw, "rl_raw": rl_raw}


# ---------------------------------------------------------------------------
# Main table builder
# ---------------------------------------------------------------------------


def build_prediction_table_data(
    *,
    lifetime_best: dict,
    lifetime_best_anchor: dict,
    all_lifetime_best: dict,
    all_lifetime_best_anchor: dict,
    critical_power_params: Optional[dict] = None,
    rl_predictions: Optional[dict] = None,
    pauls_k: float = 5.0,
) -> list[dict]:
    """
    Compute prediction-table rows for all four predictors simultaneously.

    Always emits one row per canonical ranked event (all RANKED_DISTANCES then
    all RANKED_TIMES), regardless of the event-filter selection in the chart UI.

    ``lifetime_best`` / ``lifetime_best_anchor`` are the *filtered* bests (the
    same set used by the chart) and drive the prediction columns.
    ``all_lifetime_best`` / ``all_lifetime_best_anchor`` are unfiltered (only
    gated on sim_date and excluded seasons) and are used for the "Your PB"
    column so that PBs in events the user has hidden still appear.

    Row dict keys:
        label           — event display label, e.g. "2k", "30 min"
        event_type      — "dist" | "time"
        event_value     — meters (dist) or tenths-of-sec (time)
        avg_pace/result/raw
        cp_pace/result/raw
        loglog_pace/result/raw
        pl_pace/result/raw
        rl_pace/result/raw
        pb_pace/result/raw  (athlete's unfiltered best)
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

    # ── Distance events ───────────────────────────────────────────────────────
    for dist_m, label in RANKED_DISTANCES:
        raws = _compute_predictor_raws("dist", dist_m, **_predictor_kwargs())
        cp_raw, cp_pace, cp_result = _cell(raws["cp_raw"], "dist", dist_m)
        ll_raw, ll_pace, ll_result = _cell(raws["ll_raw"], "dist", dist_m)
        pl_raw, pl_pace, pl_result = _cell(raws["pl_raw"], "dist", dist_m)
        rl_raw, rl_pace, rl_result = _cell(raws["rl_raw"], "dist", dist_m)
        pb_raw = all_lifetime_best.get(("dist", dist_m))
        pb_raw, pb_pace, pb_result = _cell(pb_raw, "dist", dist_m)
        _avg_cands = [r for r in [cp_raw, ll_raw, pl_raw, rl_raw] if r is not None]
        _avg_r = sum(_avg_cands) / len(_avg_cands) if _avg_cands else None
        _avg_r, avg_pace, avg_result = _cell(_avg_r, "dist", dist_m)
        rows.append(
            {
                "label": _DIST_LABELS.get(dist_m, f"{dist_m:,}m"),
                "event_type": "dist",
                "event_value": dist_m,
                "avg_pace": avg_pace,
                "avg_result": avg_result,
                "avg_raw": _avg_r,
                "cp_pace": cp_pace,
                "cp_result": cp_result,
                "cp_raw": cp_raw,
                "loglog_pace": ll_pace,
                "loglog_result": ll_result,
                "loglog_raw": ll_raw,
                "pl_pace": pl_pace,
                "pl_result": pl_result,
                "pl_raw": pl_raw,
                "rl_pace": rl_pace,
                "rl_result": rl_result,
                "rl_raw": rl_raw,
                "pb_pace": pb_pace,
                "pb_result": pb_result,
                "pb_raw": pb_raw,
            }
        )

    # ── Timed events ──────────────────────────────────────────────────────────
    for time_tenths, label in RANKED_TIMES:
        raws = _compute_predictor_raws("time", time_tenths, **_predictor_kwargs())
        cp_raw, cp_pace, cp_result = _cell(raws["cp_raw"], "time", time_tenths)
        ll_raw, ll_pace, ll_result = _cell(raws["ll_raw"], "time", time_tenths)
        pl_raw, pl_pace, pl_result = _cell(raws["pl_raw"], "time", time_tenths)
        rl_raw, rl_pace, rl_result = _cell(raws["rl_raw"], "time", time_tenths)
        pb_raw = all_lifetime_best.get(("time", time_tenths))
        pb_raw, pb_pace, pb_result = _cell(pb_raw, "time", time_tenths)
        _avg_cands = [r for r in [cp_raw, ll_raw, pl_raw, rl_raw] if r is not None]
        _avg_r = sum(_avg_cands) / len(_avg_cands) if _avg_cands else None
        _avg_r, avg_pace, avg_result = _cell(_avg_r, "time", time_tenths)
        rows.append(
            {
                "label": label,
                "event_type": "time",
                "event_value": time_tenths,
                "avg_pace": avg_pace,
                "avg_result": avg_result,
                "avg_raw": _avg_r,
                "cp_pace": cp_pace,
                "cp_result": cp_result,
                "cp_raw": cp_raw,
                "loglog_pace": ll_pace,
                "loglog_result": ll_result,
                "loglog_raw": ll_raw,
                "pl_pace": pl_pace,
                "pl_result": pl_result,
                "pl_raw": pl_raw,
                "rl_pace": rl_pace,
                "rl_result": rl_result,
                "rl_raw": rl_raw,
                "pb_pace": pb_pace,
                "pb_result": pb_result,
                "pb_raw": pb_raw,
            }
        )

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
    return rows
