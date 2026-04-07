"""
Critical Power model — four-parameter two-component power-duration curve.

Model (veloclinic / rowsandall):
    P(t) = Pow1 / (1 + t/tau1)  +  Pow2 / (1 + t/tau2)

Where:
    Pow1  — peak fast-twitch (anaerobic/phosphagen) power [W]
    tau1  — fast-twitch depletion time constant [s]  (≈ 5–120 s)
    Pow2  — peak slow-twitch (aerobic) power [W]
    tau2  — slow-twitch depletion time constant [s]  (≈ 600–14400 s)

Fitting uses scipy.optimize.curve_fit in log-log space so that sprint and
endurance events are weighted equally.

Exported:
    critical_power_model()         — model function (also usable as a scipy callable)
    fit_critical_power()           — fit params from a list of PB dicts
    critical_power_curve_points()  — generate Chart.js {x, y} smooth curve points
    critical_power_event_points()  — generate Chart.js {x, y} marker points at ranked events
    crossover_point()              — fast/slow crossover as a Chart.js point
    stayer_sprinter_metrics()      — sprint/stayer index values
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy.optimize import brentq, curve_fit

from services.rowing_utils import (
    PACE_MIN,
    PACE_MAX,
    RANKED_DISTANCES,
    RANKED_TIMES,
    compute_watts,
    watts_to_pace,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum data requirements for a reliable fit.
_MIN_POINTS = 5
_MIN_DURATION_RATIO = 10.0  # longest / shortest duration must exceed this

# Duration range for curve generation (seconds).
_CURVE_T_MIN = 10.0
_CURVE_T_MAX = 10_800.0  # 3 hours
_CURVE_N_PTS = 200

# Minimum R² on (t, P) to accept a fit.
_MIN_R2 = 0.90

# Parameter bounds: [Pow1, tau1, Pow2, tau2]
_LOWER = [100.0, 5.0, 50.0, 600.0]
_UPPER = [5000.0, 240.0, 2000.0, 14400.0]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def critical_power_model(
    t: float | np.ndarray,
    Pow1: float,
    tau1: float,
    Pow2: float,
    tau2: float,
) -> float | np.ndarray:
    """
    Two-component power-duration model.

    P(t) = Pow1/(1 + t/tau1) + Pow2/(1 + t/tau2)
    """
    return Pow1 / (1.0 + t / tau1) + Pow2 / (1.0 + t / tau2)


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def fit_critical_power(pb_list: list[dict]) -> Optional[dict]:
    """
    Fit the critical power model to personal-best data.

    Parameters
    ----------
    pb_list:
        List of dicts with keys 'duration_s' (float, seconds) and 'watts' (float).
        Typically one entry per ranked event category (lifetime or season best).

    Returns
    -------
    dict with keys:
        Pow1, tau1, Pow2, tau2  — fitted parameters
        r_squared               — goodness of fit on (t, P)
    or None if:
        - fewer than _MIN_POINTS valid data points
        - duration range < _MIN_DURATION_RATIO
        - curve_fit raises (e.g. singular matrix, convergence failure)
        - R² < _MIN_R2
    """
    # Filter to valid numeric entries only.
    pts = [
        p
        for p in pb_list
        if p.get("duration_s")
        and p.get("watts")
        and p["duration_s"] > 0
        and p["watts"] > 0
    ]

    if len(pts) < _MIN_POINTS:
        return None

    durations = np.array([p["duration_s"] for p in pts], dtype=float)
    powers = np.array([p["watts"] for p in pts], dtype=float)

    # Require a meaningful duration spread.
    if durations.max() / durations.min() < _MIN_DURATION_RATIO:
        return None

    # Fit in log-log space so short and long events are weighted equally.
    log_t = np.log(durations)
    log_p = np.log(powers)

    def _log_model(log_t_arr, Pow1, tau1, Pow2, tau2):
        t_arr = np.exp(log_t_arr)
        p_arr = critical_power_model(t_arr, Pow1, tau1, Pow2, tau2)
        # Guard against non-positive predictions before taking log.
        p_arr = np.clip(p_arr, 1e-3, None)
        return np.log(p_arr)

    # Initial guesses derived from the data.
    p0 = [
        float(np.max(powers) - np.median(powers)),  # Pow1
        30.0,  # tau1
        float(np.median(powers)),  # Pow2
        3600.0,  # tau2
    ]
    # Clamp initial guesses within bounds.
    p0 = [max(_LOWER[i], min(_UPPER[i], p0[i])) for i in range(4)]

    try:
        popt, _ = curve_fit(
            _log_model,
            log_t,
            log_p,
            p0=p0,
            bounds=(_LOWER, _UPPER),
            maxfev=10_000,
        )
    except Exception:
        return None

    Pow1, tau1, Pow2, tau2 = popt

    # R² on the original (t, P) scale.
    p_pred = critical_power_model(durations, Pow1, tau1, Pow2, tau2)
    ss_res = float(np.sum((powers - p_pred) ** 2))
    ss_tot = float(np.sum((powers - np.mean(powers)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    if r_squared < _MIN_R2:
        return None

    return {
        "Pow1": float(Pow1),
        "tau1": float(tau1),
        "Pow2": float(Pow2),
        "tau2": float(tau2),
        "r_squared": round(r_squared, 4),
    }


# ---------------------------------------------------------------------------
# Curve generation
# ---------------------------------------------------------------------------


def _t_to_chart_point(
    t: float,
    Pow1: float,
    tau1: float,
    Pow2: float,
    tau2: float,
    show_watts: bool,
) -> Optional[dict]:
    """
    Convert a duration t (seconds) to a Chart.js {x, y} point.

    x = distance in meters  (parametric: d = speed × t)
    y = watts  or  pace in sec/500m

    Returns None if the resulting pace is outside PACE_MIN/PACE_MAX.
    """
    watts = critical_power_model(t, Pow1, tau1, Pow2, tau2)
    if watts <= 0:
        return None
    pace = watts_to_pace(watts)
    if pace < PACE_MIN or pace > PACE_MAX:
        return None
    # distance in meters: speed = 500/pace m/s → d = t × (500/pace)
    dist = t * (500.0 / pace)
    y = round(watts, 2) if show_watts else round(pace, 4)
    return {"x": round(dist, 1), "y": y}


def critical_power_curve_points(
    params: dict,
    x_min: float,
    x_max: float,
    show_watts: bool,
) -> list[dict]:
    """
    Generate Chart.js {x, y} points for the critical power prediction curve.

    x = distance in meters, y = watts or pace (sec/500m).
    Points outside [x_min, x_max] or outside PACE_MIN/PACE_MAX are dropped.
    """
    Pow1, tau1, Pow2, tau2 = (
        params["Pow1"],
        params["tau1"],
        params["Pow2"],
        params["tau2"],
    )
    ts = np.logspace(
        math.log10(_CURVE_T_MIN),
        math.log10(_CURVE_T_MAX),
        _CURVE_N_PTS,
    )
    pts = []
    for t in ts:
        pt = _t_to_chart_point(t, Pow1, tau1, Pow2, tau2, show_watts)
        if pt is not None and x_min <= pt["x"] <= x_max:
            pts.append(pt)
    return pts


# ---------------------------------------------------------------------------
# Event marker points (one per selected ranked distance and time)
# ---------------------------------------------------------------------------


def critical_power_event_points(
    params: dict,
    selected_dists: set,
    selected_times: set,
    show_watts: bool,
) -> list[dict]:
    """
    Generate one Chart.js {x, y, _event_label} point per selected ranked event,
    positioned at the Critical Power model's prediction for that event.

    Distance events: solve numerically for the duration t at which the model
    predicts the rower covers exactly dist_m meters.

    Time events: duration is the event definition itself; predicted distance and
    pace follow directly.
    """
    Pow1, tau1, Pow2, tau2 = (
        params["Pow1"],
        params["tau1"],
        params["Pow2"],
        params["tau2"],
    )
    pts = []

    # Distance events — solve: (P(t)/2.80)^(1/3) * t = dist_m
    for dist_m, label in RANKED_DISTANCES:
        if dist_m not in selected_dists:
            continue

        def _residual(t, _d=dist_m):
            P = critical_power_model(t, Pow1, tau1, Pow2, tau2)
            if P <= 0:
                return -_d
            return (P / 2.80) ** (1.0 / 3.0) * t - _d

        try:
            t_star = brentq(_residual, _CURVE_T_MIN, _CURVE_T_MAX, xtol=0.1)
        except Exception:
            continue

        pt = _t_to_chart_point(t_star, Pow1, tau1, Pow2, tau2, show_watts)
        if pt:
            pts.append({**pt, "_event_label": label})

    # Time events — duration is fixed by definition
    for time_tenths, label in RANKED_TIMES:
        if time_tenths not in selected_times:
            continue
        t = time_tenths / 10.0
        pt = _t_to_chart_point(t, Pow1, tau1, Pow2, tau2, show_watts)
        if pt:
            pts.append({**pt, "_event_label": label})

    return pts


# ---------------------------------------------------------------------------
# Crossover point
# ---------------------------------------------------------------------------


def crossover_point(params: dict, show_watts: bool) -> Optional[dict]:
    """
    Find the duration t* where fast-twitch and slow-twitch contributions are equal:
        Pow1 / (1 + t*/tau1)  =  Pow2 / (1 + t*/tau2)

    Returns a dict:
        {x, y, t_seconds, t_label}  — Chart.js coordinates + human-readable duration
    or None if no crossing exists in [_CURVE_T_MIN, _CURVE_T_MAX].
    """
    Pow1, tau1, Pow2, tau2 = (
        params["Pow1"],
        params["tau1"],
        params["Pow2"],
        params["tau2"],
    )

    def _diff(t):
        return Pow1 / (1.0 + t / tau1) - Pow2 / (1.0 + t / tau2)

    # Check that a sign change exists before trying brentq.
    f_min = _diff(_CURVE_T_MIN)
    f_max = _diff(_CURVE_T_MAX)
    if f_min * f_max > 0:
        return None  # No crossing in range

    try:
        t_star = brentq(_diff, _CURVE_T_MIN, _CURVE_T_MAX, xtol=0.1)
    except Exception:
        return None

    pt = _t_to_chart_point(t_star, Pow1, tau1, Pow2, tau2, show_watts)
    if pt is None:
        return None

    # Human-readable duration label.
    total_s = round(t_star)
    mins, secs = divmod(total_s, 60)
    if mins >= 60:
        hrs, mins = divmod(mins, 60)
        t_label = f"{hrs}h {mins}m {secs:02d}s"
    else:
        t_label = f"{mins}m {secs:02d}s" if mins else f"{secs}s"

    return {**pt, "t_seconds": round(t_star, 1), "t_label": t_label}


# ---------------------------------------------------------------------------
# Stayer/sprinter metrics
# ---------------------------------------------------------------------------


def stayer_sprinter_metrics(params: dict) -> dict:
    """
    Compute sprint and stayer indices from fitted parameters.

    Returns
    -------
    dict with keys:
        P10           — predicted power at 10 s (sprint)
        P240          — predicted power at 4 min (threshold)
        P3600         — predicted power at 60 min (endurance)
        sprint_index  — P10 / P240
        stayer_index  — P3600 / P240
    """
    Pow1, tau1, Pow2, tau2 = (
        params["Pow1"],
        params["tau1"],
        params["Pow2"],
        params["tau2"],
    )
    P10 = critical_power_model(10.0, Pow1, tau1, Pow2, tau2)
    P240 = critical_power_model(240.0, Pow1, tau1, Pow2, tau2)
    P3600 = critical_power_model(3600.0, Pow1, tau1, Pow2, tau2)
    return {
        "P10": round(P10, 1),
        "P240": round(P240, 1),
        "P3600": round(P3600, 1),
        "sprint_index": round(P10 / P240, 3) if P240 > 0 else None,
        "stayer_index": round(P3600 / P240, 3) if P240 > 0 else None,
    }
