"""
Training Load — Banister-style CTL/ATL/TSB rollup over per-session ESS.

A multi-day fitness/fatigue model originally formalised by Banister & Calvert
(1980) and popularised in cycling by TrainingPeaks's Performance Management
Chart (PMC).  The idea: model fitness as the slow-decay exponential of daily
training stress, fatigue as the fast-decay version of the same signal, and
"form" / readiness as the difference.

    CTL(t)  = exponentially-weighted mean ESS over ~42 days  ("chronic" / fitness)
    ATL(t)  = exponentially-weighted mean ESS over  ~7 days  ("acute"   / fatigue)
    TSB(t)  = CTL(t) − ATL(t)                                ("balance" / form)

Reading the chart:

    Rising CTL with positive TSB     fitness gain in a recovered state
    Rising CTL with deeply negative TSB  digging a fatigue hole
    Flat CTL with positive TSB       maintenance / taper
    Falling CTL                       detraining

The decay constants τ_CTL = 42, τ_ATL = 7 are TrainingPeaks-canonical and
chosen to match endurance-cycling adaptation and recovery timescales.  An
erg-only rower's recovery dynamics may differ slightly, but these are the
standard Banister values.

Public API
----------
``compute_ctl_atl_tsb(daily_loads, *, decay_ctl=42, decay_atl=7) -> dict``
    Daily roll-up.  ``daily_loads`` is an ordered list of ``(date, ess)``
    tuples; days with no workout pass ESS=0 (the EW averages decay
    naturally over rest days).  Returns ``{"dates": [...], "ctl": [...],
    "atl": [...], "tsb": [...]}`` aligned by index.

No HyperDiv, no I/O.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable

import numpy as np


# ---------------------------------------------------------------------------
# Constants — TrainingPeaks PMC defaults
# ---------------------------------------------------------------------------

#: Time constant (days) for the chronic / fitness EW average.
CTL_DECAY_DAYS: float = 42.0

#: Time constant (days) for the acute / fatigue EW average.
ATL_DECAY_DAYS: float = 7.0


# ---------------------------------------------------------------------------
# Pure-function rollup
# ---------------------------------------------------------------------------


def _ew_mean(
    loads: np.ndarray, tau_days: float, *, seed: float = 0.0
) -> np.ndarray:
    """Exponentially-weighted moving average with time-constant ``tau_days``.

    Implements the TrainingPeaks PMC recurrence:

        EW[t] = EW[t-1] + (load[t] − EW[t-1]) · α     where  α = 1 / τ

    Equivalent to the standard "first-order low-pass filter" formulation.
    ``seed`` is the starting value for ``EW[-1]`` — pass the mean of the
    first 7 days to avoid the ~42-day "very rested" artifact when a fresh
    workout history starts at zero.
    """
    n = loads.shape[0]
    out = np.empty(n, dtype=np.float64)
    if n == 0:
        return out
    alpha = 1.0 / tau_days
    prev = float(seed)
    for i in range(n):
        prev = prev + (float(loads[i]) - prev) * alpha
        out[i] = prev
    return out


#: Window (days) used to seed CTL / ATL at the start of the timeline.
#: Mean ESS over the first 7 days approximates the rower's loading state
#: at the start of the observation window.
SEED_WINDOW_DAYS: int = 7

#: Days at the start of the timeline annotated as "warm-up" in the chart
#: caption.  The seed leaves only ~14 days of EMA transient before CTL
#: tracks the true loading; rendering a band over that window keeps the
#: caveat visible.
WARMUP_DAYS: int = 14


def compute_ctl_atl_tsb(
    daily_loads: Iterable[tuple[date, float]],
    *,
    decay_ctl: float = CTL_DECAY_DAYS,
    decay_atl: float = ATL_DECAY_DAYS,
) -> dict:
    """Compute CTL / ATL / TSB curves from a day-indexed ESS series.

    Parameters
    ----------
    daily_loads:
        Ordered iterable of ``(date, ess)`` pairs.  Caller must include
        rest days as ``(date, 0.0)`` — this function does not infer
        gaps.  Multiple workouts on the same date should be pre-summed
        by the caller.
    decay_ctl, decay_atl:
        Time constants (days) for the chronic and acute EW averages.
        Defaults are TrainingPeaks-canonical (42 / 7).

    Returns
    -------
    dict with four parallel arrays:

        ``dates``  list of ``date`` — one entry per day passed in.
        ``ctl``    list[float] — chronic load curve.
        ``atl``    list[float] — acute load curve.
        ``tsb``    list[float] — ``ctl − atl``, the "balance" / form line.

    Empty input returns dict with empty lists.
    """
    pairs = list(daily_loads)
    if not pairs:
        return {"dates": [], "ctl": [], "atl": [], "tsb": []}

    dates = [p[0] for p in pairs]
    loads = np.asarray([float(p[1]) for p in pairs], dtype=np.float64)

    # Seed both averages with the mean of the first ``SEED_WINDOW_DAYS``
    # days of load.  Zero-seeding produces a spurious ~42-day "very rested"
    # ramp at the start of an established rower's history; seeding with the
    # observed loading state collapses it.
    seed_n = min(SEED_WINDOW_DAYS, loads.shape[0])
    seed_value = float(loads[:seed_n].mean()) if seed_n > 0 else 0.0

    ctl = _ew_mean(loads, float(decay_ctl), seed=seed_value)
    atl = _ew_mean(loads, float(decay_atl), seed=seed_value)
    tsb = ctl - atl

    return {
        "dates": dates,
        "ctl": ctl.tolist(),
        "atl": atl.tolist(),
        "tsb": tsb.tolist(),
    }


def daily_loads_from_workouts(
    workouts: Iterable[dict],
    *,
    fill_gaps: bool = True,
) -> list[tuple[date, float]]:
    """Aggregate per-workout ESS to a daily series suitable for
    :func:`compute_ctl_atl_tsb`.

    Each workout contributes its ``_ess`` (or ``_ess_session`` for session
    rows) value to its ``date`` (parsed as ``YYYY-MM-DD``).  Multiple
    workouts on the same day are summed.

    When ``fill_gaps=True`` (default), days between the earliest and
    latest workout dates with no recorded workout are filled with ESS=0
    so the EW averages decay correctly over rest days.

    Returns ``[]`` when no workouts have a parseable date + ESS.
    """
    by_day: dict[date, float] = {}
    for w in workouts:
        ds = w.get("date")
        if not ds:
            continue
        # Concept2 timestamps are full ISO datetimes; take the date part.
        day_str = ds[:10]
        try:
            d = date.fromisoformat(day_str)
        except (TypeError, ValueError):
            continue
        ess = w.get("_ess") or w.get("_ess_session") or 0.0
        if not isinstance(ess, (int, float)) or math.isnan(ess):
            continue
        by_day[d] = by_day.get(d, 0.0) + float(ess)

    if not by_day:
        return []

    if not fill_gaps:
        return sorted(by_day.items())

    start = min(by_day)
    end = max(by_day)
    out: list[tuple[date, float]] = []
    cur = start
    one_day = timedelta(days=1)
    while cur <= end:
        out.append((cur, by_day.get(cur, 0.0)))
        cur += one_day
    return out
