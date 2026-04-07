"""
Prediction table data builder for the ranked-events view.

Exported:
  build_prediction_table_data() — compute all four predictor columns for every
                                  ranked event, returning a list of row dicts.

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
from components.ranked_formatters import fmt_split


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

      distance event  → ("M:SS.t", "M:SS.t")   pace per 500m, predicted total time
      timed event     → ("M:SS.t", "N,NNNm")    pace per 500m, predicted meters

    Returns None if pace is unavailable or outside [PACE_MIN, PACE_MAX].
    """
    if pace is None or not (PACE_MIN <= pace <= PACE_MAX):
        return None
    pace_str = fmt_split(round(pace * 10))
    if event_type == "dist":
        time_tenths = round(pace * event_value / 500.0 * 10)
        return (pace_str, fmt_split(time_tenths))
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
        rl_pace/result/raw  (dist events only; None for timed)
        pb_pace/result/raw  (athlete's unfiltered best)
    """
    # ── Log-Log fit (single fit across all filtered lifetime PBs) ─────────────
    _ll_fit = loglog_fit(lifetime_best, lifetime_best_anchor) if lifetime_best else None
    _ll_slope, _ll_intercept = _ll_fit if _ll_fit is not None else (None, None)

    # ── CP params (destructured once) ─────────────────────────────────────────
    _cp = critical_power_params
    if _cp is not None:
        _cp_Pow1, _cp_tau1, _cp_Pow2, _cp_tau2 = (
            _cp["Pow1"], _cp["tau1"], _cp["Pow2"], _cp["tau2"]
        )
    else:
        _cp_Pow1 = _cp_tau1 = _cp_Pow2 = _cp_tau2 = None

    def _cell(raw_pace: Optional[float], event_type: str, event_value: int) -> tuple:
        """Return (raw, pace_str, result_str) — raw is None when pace is invalid."""
        t = _fmt_pred(raw_pace, event_type, event_value)
        if t is None:
            return (None, None, None)
        return (raw_pace, t[0], t[1])

    rows: list[dict] = []

    # ── Distance events ───────────────────────────────────────────────────────
    for dist_m, label in RANKED_DISTANCES:

        # Critical Power — solve numerically for the duration at which the CP
        # model predicts the rower covers exactly dist_m meters.
        cp_raw = None
        if _cp is not None:
            def _cp_resid(t, _d=dist_m, P1=_cp_Pow1, t1=_cp_tau1, P2=_cp_Pow2, t2=_cp_tau2):
                P = critical_power_model(t, P1, t1, P2, t2)
                return (P / 2.80) ** (1.0 / 3.0) * t - _d if P > 0 else -_d

            try:
                t_star = brentq(_cp_resid, 10.0, 20_000.0, xtol=0.1)
                watts = critical_power_model(t_star, _cp_Pow1, _cp_tau1, _cp_Pow2, _cp_tau2)
                if watts > 0:
                    cp_raw = watts_to_pace(watts)
            except Exception:
                pass
        cp_raw, cp_pace, cp_result = _cell(cp_raw, "dist", dist_m)

        # Log-Log Watts
        ll_raw = (
            loglog_predict_pace(_ll_slope, _ll_intercept, dist_m)
            if _ll_slope is not None
            else None
        )
        ll_raw, ll_pace, ll_result = _cell(ll_raw, "dist", dist_m)

        # Paul's Law — averaged across all anchor PBs
        pl_raw = None
        if lifetime_best:
            _pl_paces = [
                predicted
                for cat, pb_pace in lifetime_best.items()
                if (anchor := lifetime_best_anchor.get(cat))
                and PACE_MIN <= (predicted := pauls_law_pace(pb_pace, anchor, dist_m)) <= PACE_MAX
            ]
            if _pl_paces:
                pl_raw = sum(_pl_paces) / len(_pl_paces)
        pl_raw, pl_pace, pl_result = _cell(pl_raw, "dist", dist_m)

        # RowingLevel — averaged across all anchor curves
        rl_raw = None
        if rl_predictions:
            _rl_paces = [
                pace
                for preds in rl_predictions.values()
                if (pace := preds.get(dist_m) or preds.get(str(dist_m))) is not None
                and PACE_MIN <= pace <= PACE_MAX
            ]
            if _rl_paces:
                rl_raw = sum(_rl_paces) / len(_rl_paces)
        rl_raw, rl_pace, rl_result = _cell(rl_raw, "dist", dist_m)

        # Actual PB — from the *unfiltered* bests so hidden events still show
        pb_raw = all_lifetime_best.get(("dist", dist_m))
        pb_raw, pb_pace, pb_result = _cell(pb_raw, "dist", dist_m)

        # Average of available predictor raws
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
        T = time_tenths / 10.0  # seconds

        # Critical Power — duration is fixed; read watts directly from the model
        cp_raw = None
        if _cp is not None:
            watts = critical_power_model(T, _cp_Pow1, _cp_tau1, _cp_Pow2, _cp_tau2)
            if watts > 0:
                cp_raw = watts_to_pace(watts)
        cp_raw, cp_pace, cp_result = _cell(cp_raw, "time", time_tenths)

        # Log-Log — solve pace_fn(d) * d / 500 = T
        ll_raw = (
            _solve_timed_pace(
                lambda d, s=_ll_slope, i=_ll_intercept: loglog_predict_pace(s, i, d),
                T,
            )
            if _ll_slope is not None
            else None
        )
        ll_raw, ll_pace, ll_result = _cell(ll_raw, "time", time_tenths)

        # Paul's Law — solve per anchor, average results
        pl_raw = None
        if lifetime_best:
            _pl_paces = []
            for cat, pb_pace in lifetime_best.items():
                anchor = lifetime_best_anchor.get(cat)
                if not anchor:
                    continue
                pace_star = _solve_timed_pace(
                    lambda d, p=pb_pace, a=anchor: pauls_law_pace(p, a, d), T
                )
                if pace_star is not None:
                    _pl_paces.append(pace_star)
            if _pl_paces:
                pl_raw = sum(_pl_paces) / len(_pl_paces)
        pl_raw, pl_pace, pl_result = _cell(pl_raw, "time", time_tenths)

        # RowingLevel — solve per anchor using log-log interpolation, average
        rl_raw = None
        if rl_predictions:
            _rl_paces = []
            for preds in rl_predictions.values():
                pace_star = _solve_timed_pace(
                    lambda d, p=preds: _rl_interp_pace(p, d), T
                )
                if pace_star is not None:
                    _rl_paces.append(pace_star)
            if _rl_paces:
                rl_raw = sum(_rl_paces) / len(_rl_paces)
        rl_raw, rl_pace, rl_result = _cell(rl_raw, "time", time_tenths)

        # Actual PB
        pb_raw = all_lifetime_best.get(("time", time_tenths))
        pb_raw, pb_pace, pb_result = _cell(pb_raw, "time", time_tenths)

        # Average of available predictor raws
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

    return rows
