"""
Pure per-model pace samplers.

Each sampler answers a single question: "at distance d meters, what is the
predicted pace (sec/500m) from this model?"  Returns None when the input
data is insufficient or the result falls outside [PACE_MIN, PACE_MAX].

Exported:
    loglog_pace_at(slope, intercept, dist_m)
    pauls_law_pace_at(lifetime_best, lifetime_best_anchor, dist_m, k=5.0)
    cp_pace_at(cp_params, dist_m)         cp_params: dict or 4-tuple
    rowinglevel_pace_at(rl_predictions, lifetime_best_anchor, dist_m)

Used by:
    components/power_curve_chart_builder.py::_average_datasets
        (sweeps d over a log-spaced range; averages whatever is non-None)
    services/ranked_predictions.py::_compute_predictor_raws
        (picks paces at each ranked event's anchor distance; timed events
        invert via _solve_timed_pace using a sampler-bound lambda)

All samplers are pure — no I/O, no HyperDiv, no hidden state.
"""

from __future__ import annotations

import math
from typing import Optional

from scipy.optimize import brentq

from services.rowing_utils import (
    PACE_MIN,
    PACE_MAX,
    pauls_law_pace,
    loglog_predict_pace,
    watts_to_pace,
)
from services.critical_power_model import critical_power_model


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


def _rl_pace_from_preds(preds: dict, dist_m: float) -> Optional[float]:
    """Single-anchor RL pace at dist_m: exact hit if RL published that
    distance, else log-log interpolation within this anchor's curve."""
    p = preds.get(int(dist_m)) or preds.get(str(int(dist_m)))
    if p is not None and PACE_MIN <= p <= PACE_MAX:
        return p
    # Defer to the existing interp helper to avoid duplicating log-log math.
    from services.ranked_predictions import _rl_interp_pace

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
