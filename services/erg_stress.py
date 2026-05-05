"""
Erg Stress Score — multi-band power-duration-saturation training-load metric.

This module is the home of three per-workout / per-session metrics that sit
alongside (not replacing) the existing Quality metric in
:mod:`services.workout_quality`:

* **ESS** — time integral of ``I(t)²`` over the session.  Calibrated so a
  60' continuous effort at 60-min reference watts yields ESS ≈ 100.  Strictly
  additive: a session's ESS is the sum of its workouts' ESS, and a workout's
  ESS is the sum of its segments'.
* **Severity** — peak rolling ``I(t)`` (5-min, 60-s, 15-s windows) plus a
  contribution from anaerobic depletion.  Bucketed Low / Moderate / High /
  Maximal.  Captures recovery demand independently of total volume.
* **Anaerobic Strain** — Skiba (2012) W'bal model.  Reports
  ``1 − min(W'bal)/W'`` over the session as a 0–1 fraction (display as
  percent).

The core idea is a **multi-band PDC-saturation intensity** instead of v1's
single-anchor IF with a recency multiplier.  At each second:

    EMA_d(t)        = causal EMA of P with τ = d, seeded at 0       d ∈ ZONE_BANDS_S
    zone_ratio_d(t) = EMA_d(t) / RW_d                                RW_d = ref watts at d
    I(t)            = INTENSITY_SCALE · sqrt( Σ_d zone_ratio_d(t)² )

The L2 norm gives every duration band a vote in the intensity signal — there
is no single anchor duration.  Squaring naturally emphasises the dominant
band.  The ``INTENSITY_SCALE = 0.5`` factor calibrates I so sustained at-zone
effort approaches ≈ 1.0 at steady state across the duration spectrum.

Compositionality
----------------
Because the metric is a strict time integral of a continuous signal that's a
pure function of the session timeline, a workout chopped into N pieces and
logged as N separate Concept2 entries — provided their timestamps reconstruct
the same continuous timeline — yields the same session ESS as one combined
entry.  EMA state and W'bal are integrated across the *session timeline*,
not per workout.

A "session" is the maximal run of workouts on the same date with gaps less
than ``SESSION_GAP_S`` (30 min) between consecutive workouts.  Concept2
logs the workout's *end* time as ``date``; we recover the start as
``end − duration``.

Literature precedent
--------------------
* Coggan & Allen — Normalized Power (single 30-s rectangular MA → 4th power).
* Skiba (2008) — xPower (single 25-s EMA → 4th power).  Already uses an EMA.
* Allen-Coggan-McGregor / Pinot & Grappe (2011) — Mean-Maximal Power /
  Power Profile.  Multi-band PDC envelope, but as a per-workout descriptor.
* Skiba (2012, 2014) — W'bal and dynamic CP.  Multi-time-scale state, but
  mechanistic rather than PDC-saturation.
* Banister (1975), Busso (2003) — TRIMP fitness/fatigue with multiple time
  constants, but at across-day chronic scope.

The continuous-time multi-band EMA formulation here appears to be a novel
synthesis of NP/xPower (multi-time-scale rolling-MA + power-mean) and the
Power Profile (multi-band PDC framing).

W' / W'bal
----------
We approximate ``W'`` as ``Pow1 × tau1`` from
:func:`services.critical_power_model.fit_critical_power` when a CP fit is
available, else fall back to a population default (28 kJ men, 22 kJ women).
``CP`` is the rower's date-aware 60-min reference watts.  Skiba's recovery
time constant ``τ_W' = 546 × exp(−0.01 × DCP) + 316`` where DCP is the
session-mean watts below CP; clamped to ``[200, 1200]`` seconds for short
workouts where DCP is undefined.

Public API
----------
``compute_session_metrics(session_workouts, ref_watts_at_duration_fn,
cp, w_prime)`` — the workhorse.
``build_segments(workout)`` — split a workout into work/rest segments at
the highest available resolution (intervals → splits → whole workout).
``compute_w_prime_estimate(cp_params, gender)`` — pick W' for a rower.
``severity_bucket(score)`` — bucket label from a numeric severity score.
``SEVERITY_STYLE`` — color metadata for the UI chip (parallel to the
``QUALITY_STYLE`` dict in :mod:`services.workout_quality`).
``ZONE_BANDS_S`` — the six band time-constants (= τ_d), in seconds.
``ZONE_BAND_LABELS`` — short human labels matching the bands.

This module is pure Python (no HyperDiv, no I/O).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Callable, Optional

from services.rowing_utils import (
    PACE_MAX,
    PACE_MIN,
    compute_watts,
)
from services.heartrate_utils import _extract_hr


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The power to which to raise, and ultimately lower the sum, of each zone_ratio
SIGNAL_AMPLIFIER = 3

#: Duration bands (seconds) for the multi-band intensity signal.  Spans
#: 20 s sprint → 2 h ultra-aerobic, matching the rower's six-zone
#: power-duration model.
ZONE_BANDS_S: tuple[int, ...] = (20, 90, 300, 1200, 3600, 7200)

#: Short labels matching the bands (for chart legends / tooltips).
ZONE_BAND_LABELS: dict[int, str] = {
    20: "20s",
    90: "1:30",
    300: "5:00",
    1200: "20:00",
    3600: "60:00",
    7200: "2h",
}

#: Per-band EMA τ factors — *knobs* you can dial to tune how fast each
#: band fills and decays.  ``τ_d = d * EMA_TAU_FACTORS[d]``.
#:
#: Why this is here: a vanilla EMA with ``τ = d`` has "characteristic-time"
#: memory — it reaches only 1 − 1/e ≈ 63 % saturation by t = d, and after
#: a rest of length d still retains 1/e ≈ 37 % of its prior value.  That
#: under-saturates the visible signal at sustained at-zone efforts and
#: over-retains contributions from work that should have decayed away.
#:
#: Setting the factor to 1/3 makes each band fill ~95 % by t = d and
#: decay to ~5 % after a rest of length d — much closer to the
#: physiological "rolling-window over the last d seconds" intuition.
#:
#: Defaults are physiologically motivated:
#:
#:   20s   factor 0.30  — phosphocreatine kinetics: ~30 s recovery τ
#:   90s   factor 0.30  — fast-glycolysis lactate kinetics
#:   5min  factor 0.33  — VO₂ on-/off-kinetics (~2 min)
#:   20min factor 0.33  — threshold/MLSS dynamics
#:   60min factor 0.40  — substrate / hydration / glycogen drift
#:   2h    factor 0.40  — long-aerobic durability
#:
#: All values default to a uniform 1/3 under the current setting; tweak
#: per-band if a particular zone reads off in your data.
EMA_TAU_FACTORS: dict[int, float] = {
    20: 0.30,
    90: 0.30,
    300: 0.33,
    1200: 0.33,
    3600: 0.40,
    7200: 0.40,
}


def _tau(d: int) -> float:
    """Effective EMA time constant for band ``d`` (seconds)."""
    return float(d) * EMA_TAU_FACTORS.get(d, 1.0)


#: The scale factor in
#: ``I(t) = INTENSITY_SCALE · pow(Σ pow(zone_ratio, SIGNAL_AMPLIFIER), 1/SIGNAL_AMPLIFIER)``.
#: Calibration knob for fixing at-zone steady-state effort reads ≈ 1.0
#: across the spectrum.
INTENSITY_SCALE = 1

#: Maximum gap (seconds) between consecutive workouts on the same date that
#: still count as one session.  30 minutes.
SESSION_GAP_S = 1800.0 * 2

#: Population-default anaerobic capacity (joules) when no CP fit is
#: available.  Cycling-derived; rowing W' tends slightly higher.
W_PRIME_DEFAULT_M = 28_000.0
W_PRIME_DEFAULT_F = 22_000.0

#: Bounds on Skiba τ_W' to keep DCP-driven formula sensible for short workouts.
TAU_W_MIN = 200.0
TAU_W_MAX = 1200.0

#: Severity bucket cutoffs on the new I(t) scale.  Tuned against the
#: synthetic calibration suite under the current ``EMA_TAU_FACTORS`` and
#: ``SIGNAL_AMPLIFIER`` settings — typical max-effort peak I ranges
#: 0.85–1.75 once the strain bonus is applied.  Cut-offs are first-cut
#: guesses; revisit after real-data exposure.  See
#: :data:`SEVERITY_DEFINITION_TEXT` for matching prose.
SEVERITY_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("Low", 0.70),
    ("Moderate", 1.00),
    ("High", 1.40),
    ("Maximal", float("inf")),
)

SEVERITY_ORDER: dict[str, int] = {"Low": 0, "Moderate": 1, "High": 2, "Maximal": 3}

#: Per-bucket visual style for table cells / chips.  Parallel shape to
#: :data:`services.workout_quality.QUALITY_STYLE`.
SEVERITY_STYLE: dict[str, dict] = {
    "Low": {
        "label": "Low",
        "bg": (115, 170, 230, 0.85),  # light blue — same family as Z1 power bin
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "Moderate": {
        "label": "Moderate",
        "bg": (205, 190, 50, 0.85),  # yellow-green
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "High": {
        "label": "High",
        "bg": (225, 125, 35, 0.90),  # orange
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "Maximal": {
        "label": "Maximal",
        "bg": (215, 55, 55, 0.95),  # red
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
}

SEVERITY_DEFINITION_TEXT: dict[str, str] = {
    "Low": "Severity below 0.70 — easy / recovery / base session.",
    "Moderate": "Severity 0.70–1.00 — solid moderate / aerobic session.",
    "High": "Severity 1.00–1.40 — sharp threshold / VO2 / hard intervals.",
    "Maximal": "Severity ≥ 1.40 — race-pace or PB territory; high recovery demand.",
}


def severity_bg_css(category: str) -> str:
    """Return an ``rgba(...)`` CSS color for the given severity bucket."""
    style = SEVERITY_STYLE.get(category)
    if style is None:
        return "rgba(160,160,160,0.6)"
    r, g, b, a = style["bg"]
    return f"rgba({r},{g},{b},{a})"


def severity_bucket(score: Optional[float]) -> Optional[str]:
    """Bucket a numeric severity score; None passes through."""
    if score is None:
        return None
    for label, upper in SEVERITY_THRESHOLDS:
        if score < upper:
            return label
    return SEVERITY_THRESHOLDS[-1][0]


# ---------------------------------------------------------------------------
# Date-time parsing
# ---------------------------------------------------------------------------


def _parse_workout_datetime(date_str: Optional[str]) -> Optional[datetime]:
    """Parse a Concept2 ``date`` field into a naive ``datetime``.

    Concept2 logs the workout's *end* timestamp.  The string is typically
    ``"YYYY-MM-DD HH:MM:SS"`` (space separator).  Returns ``None`` for
    unparseable input.
    """
    if not date_str:
        return None
    s = date_str.strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _workout_total_duration_s(w: dict) -> float:
    """Total elapsed seconds for a workout, including interval rest_time."""
    base = (w.get("time") or 0) / 10.0
    if w.get("is_interval"):
        base += (w.get("rest_time") or 0) / 10.0
    return float(base)


# ---------------------------------------------------------------------------
# Segment builder
# ---------------------------------------------------------------------------


def _segment(t_offset_s: float, duration_s: float, watts: float, hr, kind: str) -> dict:
    return {
        "t_offset_s": float(t_offset_s),
        "duration_s": float(duration_s),
        "watts": float(watts),
        "hr": hr,
        "kind": kind,
    }


def build_segments(workout: dict) -> list[dict]:
    """Return ordered segments for a workout at the highest resolution available.

    Each segment dict: ``{t_offset_s, duration_s, watts, hr, kind}`` with
    ``kind ∈ {"work", "rest"}``.

    Resolution priority:

    1. **Intervals** (when ``is_interval``) — work and rest sub-segments.
       A rest interval (``type == "rest"``) emits one rest segment.
       A work interval emits a work segment plus a trailing rest segment
       if ``rest_time > 0``.
    2. **Splits** — when the nested ``splits`` list is populated.
    3. **Whole workout** — single segment.

    Per-stroke segments are not built here; the chart layer can extend the
    timeline using cached stroke data.
    """
    nested = workout.get("workout") or {}
    is_interval = bool(workout.get("is_interval"))
    segments: list[dict] = []
    t = 0.0

    if is_interval:
        intervals = nested.get("intervals") or []
        for iv in intervals:
            iv_type = (iv.get("type") or "").lower()
            iv_time_s = (iv.get("time") or 0) / 10.0
            if iv_time_s <= 0:
                continue
            iv_dist = iv.get("distance") or 0
            iv_hr = _extract_hr(iv.get("heart_rate"))

            if iv_type == "rest":
                segments.append(_segment(t, iv_time_s, 0.0, iv_hr, "rest"))
                t += iv_time_s
                continue

            if iv_dist > 0:
                pace = iv_time_s / (iv_dist / 500.0)
                watts = compute_watts(pace) if PACE_MIN <= pace <= PACE_MAX else 0.0
            else:
                watts = 0.0
            segments.append(_segment(t, iv_time_s, watts, iv_hr, "work"))
            t += iv_time_s

            rest_time_s = (iv.get("rest_time") or 0) / 10.0
            if rest_time_s > 0:
                segments.append(_segment(t, rest_time_s, 0.0, None, "rest"))
                t += rest_time_s

        if segments:
            return segments

    splits = nested.get("splits") or []
    if splits:
        for sp in splits:
            sp_time_s = (sp.get("time") or 0) / 10.0
            if sp_time_s <= 0:
                continue
            sp_dist = sp.get("distance") or 0
            sp_hr = _extract_hr(sp.get("heart_rate"))
            if sp_dist > 0:
                pace = sp_time_s / (sp_dist / 500.0)
                watts = compute_watts(pace) if PACE_MIN <= pace <= PACE_MAX else 0.0
            else:
                watts = 0.0
            segments.append(_segment(t, sp_time_s, watts, sp_hr, "work"))
            t += sp_time_s
        if segments:
            return segments

    # Whole-workout fallback.
    total_time_s = (workout.get("time") or 0) / 10.0
    if total_time_s > 0:
        watts = workout.get("watts") or 0.0
        if not watts:
            pace = workout.get("pace")
            if pace and PACE_MIN <= pace <= PACE_MAX:
                watts = compute_watts(pace)
        top_hr = _extract_hr(workout.get("heart_rate"))
        segments.append(_segment(0.0, total_time_s, float(watts), top_hr, "work"))
    return segments


# ---------------------------------------------------------------------------
# W' estimation
# ---------------------------------------------------------------------------


def compute_w_prime_estimate(
    cp_params: Optional[dict],
    gender: Optional[str],
) -> float:
    """Return an anaerobic-capacity estimate (joules).

    Uses ``Pow1 × tau1`` from a 4-parameter CP fit when available — a rough
    proxy for the rower's anaerobic work capacity.  Falls back to a
    population default keyed by ``gender`` (``"Male"`` / ``"Female"``).
    """
    if cp_params:
        p1 = cp_params.get("Pow1")
        t1 = cp_params.get("tau1")
        if p1 and t1 and p1 > 0 and t1 > 0:
            return float(p1) * float(t1)
    g = (gender or "").strip().lower()
    if g.startswith("f"):
        return W_PRIME_DEFAULT_F
    return W_PRIME_DEFAULT_M


# ---------------------------------------------------------------------------
# C_ESS calibration
# ---------------------------------------------------------------------------


def _calibrate_c_ess() -> float:
    """Empirically calibrate ``C_ESS`` so a 60' @ FTP workout integrates to ESS ≈ 100.

    Uses a synthetic adult-endurance profile with the canonical
    multi-duration ratios:

        RW(20s)   = 5.00 · FTP    (peak 20-s power)
        RW(90s)   = 2.50 · FTP    (~ 90-s "lactate-tolerance" power)
        RW(5min)  = 1.40 · FTP    (~ VO2max power)
        RW(20min) = 1.05 · FTP    (~ "FTP test" anchor)
        RW(60min) = 1.00 · FTP    (definitional FTP)
        RW(120min)= 0.95 · FTP    (long-aerobic ceiling)

    The exact FTP value is irrelevant — ``zone_ratio`` is dimensionless, and
    so is the integral of ``I(t)²``.  The profile shape is what matters; it
    matches typical adult-endurance rowers and gives a stable calibration
    point.  C_ESS is then the constant that scales ∫ I² over the synthetic
    hour to land exactly at 100.
    """
    FTP = 250.0
    rw = {
        20: 5.00 * FTP,
        90: 2.50 * FTP,
        300: 1.40 * FTP,
        1200: 1.05 * FTP,
        3600: 1.00 * FTP,
        7200: 0.95 * FTP,
    }
    duration_s = 3600
    P = FTP
    ema = {d: 0.0 for d in ZONE_BANDS_S}
    taus = {d: _tau(d) for d in ZONE_BANDS_S}
    integral = 0.0
    for _ in range(duration_s):
        sum_sq = 0.0
        for d in ZONE_BANDS_S:
            ema[d] += (P - ema[d]) / taus[d]
            r = ema[d] / rw[d]
            sum_sq += math.pow(r, SIGNAL_AMPLIFIER)
        I = INTENSITY_SCALE * math.pow(sum_sq, 1.0 / SIGNAL_AMPLIFIER)
        integral += I * I
    if integral <= 0.0:
        return 1.0
    return 100.0 / integral


#: Calibration constant scaling ∫ I(t)² dt → ESS.  Computed at module import
#: from a synthetic 60'@FTP simulation; see :func:`_calibrate_c_ess`.
C_ESS: float = _calibrate_c_ess()


# ---------------------------------------------------------------------------
# Session integration
# ---------------------------------------------------------------------------


def _empty_metrics() -> dict:
    return {
        "ess": 0.0,
        "intensity_session": 0.0,
        "peak_intensity_5min": 0.0,
        "peak_intensity_60s": 0.0,
        "peak_intensity_15s": 0.0,
        "severity_score": 0.0,
        "severity_bucket": "Low",
        "anaerobic_strain": 0.0,
        "w_bal_trough": None,
        "duration_s": 0.0,
        "per_workout": [],
        "per_segment": [],
        "timeline": [],
    }


def _peak_rolling_mean(arr: list[float], window: int) -> float:
    """Maximum rolling-mean over ``window`` of a numeric list.

    For ``window > len(arr)``, returns the mean of the whole array (so a
    short session reports its overall mean as its 5-min peak).
    """
    if not arr:
        return 0.0
    w = min(window, len(arr))
    if w <= 0:
        return 0.0
    s = sum(arr[:w])
    best = s / w
    for i in range(w, len(arr)):
        s += arr[i] - arr[i - w]
        avg = s / w
        if avg > best:
            best = avg
    return best


def _attach_per_workout_records(
    total_session_s,
    workout_windows,
    w_bal_curve,
    w_prime,
    P_arr,
    inv_taus,
    inv_rws,
    inv_amp,
    kind_arr,
    wid_arr,
    ess_per_second,
):
    # ----- Per-workout records -----
    # Per-workout severity / intensity use a *workout-isolated* EMA
    # simulation — the bands are reset to zero at each workout's start
    # and the EMAs are run forward only over that workout's seconds.
    # This is what the column should report:
    #
    #   * Cooldown after a max-effort race: the session-level I(t) is
    #     still high (inherited from the race), but the cooldown's own
    #     low watts barely fill any band.  Workout-isolated I stays low.
    #     ⇒ Severity = Low, as the user's intuition demands.
    #   * 5k race after a warmup: identical to a standalone 5k race.
    #     The session priming effect is captured in ESS attribution
    #     (which uses the session-state I(t)), not in this column.
    #
    # ESS attribution (above) still uses the time-slice integral of the
    # session-state I(t), so ``Σ ESS_workout = ESS_session`` exactly.
    n_bands = len(ZONE_BANDS_S)

    # Per-workout ESS attribution
    ess_by_workout: dict = {wid: 0.0 for wid in workout_windows}
    for t in range(total_session_s):
        wid = wid_arr[t]
        if wid is not None:
            ess_by_workout[wid] += ess_per_second[t]

    per_workout_records: list = []
    for wid, (t_start, t_end) in workout_windows.items():
        n = max(1, t_end - t_start)

        # Workout-isolated EMA simulation — uses the same flat-list /
        # ``r*r*r`` hot-path as the session-level loop above.  Reuses the
        # already-computed ``inv_taus`` and ``inv_rws``.
        ema_w = [0.0] * n_bands
        I_w_arr: list[float] = []
        if SIGNAL_AMPLIFIER == 3:
            for t in range(t_start, t_end):
                P = P_arr[t]
                sum_amp_w = 0.0
                for i in range(n_bands):
                    e = ema_w[i]
                    e += (P - e) * inv_taus[i]
                    ema_w[i] = e
                    r = e * inv_rws[i]
                    sum_amp_w += r * r * r
                I_w_arr.append(INTENSITY_SCALE * (sum_amp_w**inv_amp))
        else:
            amp = SIGNAL_AMPLIFIER
            for t in range(t_start, t_end):
                P = P_arr[t]
                sum_amp_w = 0.0
                for i in range(n_bands):
                    e = ema_w[i]
                    e += (P - e) * inv_taus[i]
                    ema_w[i] = e
                    r = e * inv_rws[i]
                    sum_amp_w += r**amp
                I_w_arr.append(INTENSITY_SCALE * (sum_amp_w**inv_amp))

        # Workout intensity is the arithmetic mean of the workout-isolated
        # I(t) across this workout's *work-only* seconds.  Excluding rest
        # seconds keeps the workout-level number aligned with per-segment
        # values shown in the splits/intervals table.
        work_intensity_vals = [
            I_w_arr[t - t_start] for t in range(t_start, t_end) if kind_arr[t] == "work"
        ]
        intensity_w = (
            sum(work_intensity_vals) / len(work_intensity_vals)
            if work_intensity_vals
            else 0.0
        )
        slc = I_w_arr or [0.0]
        pk5 = _peak_rolling_mean(slc, 300)
        pk60 = _peak_rolling_mean(slc, 60)
        pk15 = _peak_rolling_mean(slc, 15)

        # Anaerobic strain per workout: depletion *caused* by this workout
        # = (W'bal at workout start − min W'bal during workout) / W'.
        # A cooldown that starts on an empty reserve and only recovers reads
        # 0% even though the absolute W'bal level stays low; a race that
        # drains a fresh reserve to 0 reads 100%.
        if w_bal_curve is not None and w_prime:
            wb_slc = w_bal_curve[t_start:t_end]
            if wb_slc:
                wb_start = w_bal_curve[t_start - 1] if t_start > 0 else w_prime
                strain_w = max(0.0, min(1.0, (wb_start - min(wb_slc)) / w_prime))
            else:
                strain_w = 0.0
        else:
            strain_w = 0.0
        sev_w = max(pk5, 0.90 * pk60, 0.75 * pk15) + 0.50 * strain_w
        per_workout_records.append(
            {
                "workout_id": wid,
                "t_start_s": t_start,
                "t_end_s": t_end,
                "duration_s": n,
                "ess": ess_by_workout.get(wid, 0.0),
                "intensity_avg": intensity_w,
                "peak_intensity_5min": pk5,
                "peak_intensity_60s": pk60,
                "peak_intensity_15s": pk15,
                "severity_score": sev_w,
                "severity_bucket": severity_bucket(sev_w) or "Low",
                "anaerobic_strain": strain_w,
            }
        )
    return per_workout_records


def _build_session_timeline(
    total_session_s,
    zone_ratio_arr,
    w_bal_curve,
    w_prime,
    I_arr,
    P_arr,
):
    # ----- Timeline (sub-sampled for chart payload) -----
    # Keep ≤ 1800 points to bound payload size; for workouts ≤ 30 min, full
    # 1-Hz resolution (1800 pts).  Longer: stride out evenly.  Each kept
    # timepoint includes the per-zone ratios so the chart can paint the six
    # band traces as percentages on the right axis.
    target_points = 1800
    stride = max(1, total_session_s // target_points)
    timeline = []
    for t in range(0, total_session_s, stride):
        timeline.append(
            {
                "t": t,
                "intensity": I_arr[t],
                "P": P_arr[t],
                "w_bal_pct": (
                    (w_bal_curve[t] / w_prime) if (w_bal_curve and w_prime) else None
                ),
                "zones": {d: zone_ratio_arr[d][t] for d in ZONE_BANDS_S},
            }
        )
    return timeline


def _calculate_intensity(
    annotated, total_session_s, session_start, start_dt, ref_watts_at_duration_fn
):
    # Build per-second arrays.  Initialised to zero so untouched seconds
    # (gaps within the 30-minute session window where no workout is logged)
    # contribute neither power nor intensity.
    P_arr = [0.0] * total_session_s
    wid_arr: list = [None] * total_session_s
    kind_arr: list[str] = ["gap"] * total_session_s

    workout_windows: dict = {}  # workout_id → (sess_t_start, sess_t_end)
    per_segment_records: list = []

    for start_dt, end_dt, w in annotated:
        wkt_offset_s = int((start_dt - session_start).total_seconds() + 0.5)
        wkt_id = w.get("id")
        wkt_segments = build_segments(w)
        if not wkt_segments:
            continue
        wkt_t_end = wkt_offset_s
        for seg in wkt_segments:
            seg_t = wkt_offset_s + int(seg["t_offset_s"] + 0.5)
            seg_d = max(1, int(seg["duration_s"] + 0.5))
            seg_P = seg["watts"]
            end_t = min(seg_t + seg_d, total_session_s)
            for i in range(seg_t, end_t):
                P_arr[i] = seg_P
                wid_arr[i] = wkt_id
                kind_arr[i] = seg["kind"]
            wkt_t_end = max(wkt_t_end, end_t)
            per_segment_records.append(
                {
                    "workout_id": wkt_id,
                    "t_session_s": float(seg_t),
                    "duration_s": float(seg["duration_s"]),
                    "kind": seg["kind"],
                    "watts": float(seg_P),
                    "intensity_avg": 0.0,  # filled in below
                    "ess": 0.0,  # filled in below
                }
            )
        workout_windows[wkt_id] = (wkt_offset_s, wkt_t_end)

    # ----- Resolve RW_d for each band against the session's representative date -----
    # Concept2 logs end-time, so the last workout's date is the latest; using
    # one date for the whole session makes the metric stable across the
    # session (no tiny per-workout discontinuities).  reference_watts is
    # assumed robust at any duration in ZONE_BANDS_S — if we ever discover
    # edge cases, the fix lives in services/reference_watts.py.
    session_when = annotated[-1][2].get("date_dt")
    rw_d: dict[int, float] = {}
    for d in ZONE_BANDS_S:
        rw = ref_watts_at_duration_fn(session_when, float(d))
        rw_d[d] = float(rw) if (rw and rw > 0) else 0.0

    # ----- Run 6 EMAs forward in lock-step; build I(t) and per-zone traces -----
    # The inner loop runs ~once per second of session × six bands; in pure
    # Python that's the dominant cost on the Workouts page (every visible
    # session triggers it on cache miss).  We flatten band-keyed dicts
    # into parallel lists indexed by band index so the inner loop sees no
    # dict lookups, and we specialise for the common SIGNAL_AMPLIFIER=3
    # case (cube via ``r*r*r`` is ~3× faster than ``math.pow(r, 3)`` since
    # the latter pays C-FFI + log/exp cost).
    n_bands = len(ZONE_BANDS_S)
    ema = [0.0] * n_bands
    taus_list = [_tau(d) for d in ZONE_BANDS_S]
    rws_list = [rw_d[d] for d in ZONE_BANDS_S]
    inv_rws = [(1.0 / rw if rw > 0 else 0.0) for rw in rws_list]
    inv_taus = [1.0 / tau for tau in taus_list]
    I_arr = [0.0] * total_session_s
    # Per-band zone-ratio time-series.  Kept as a list-of-lists so the
    # inner loop can index without hashing.  We re-key by band-seconds
    # at the end of the function for the existing return shape.
    zone_arrs = [[0.0] * total_session_s for _ in ZONE_BANDS_S]
    inv_amp = 1.0 / SIGNAL_AMPLIFIER

    if SIGNAL_AMPLIFIER == 3:
        # Hot-path specialisation: cube via plain multiplication.
        for t in range(total_session_s):
            P = P_arr[t]
            sum_amp = 0.0
            for i in range(n_bands):
                e = ema[i]
                e += (P - e) * inv_taus[i]
                ema[i] = e
                r = e * inv_rws[i]
                zone_arrs[i][t] = r
                sum_amp += r * r * r
            I_arr[t] = INTENSITY_SCALE * (sum_amp**inv_amp)
    else:
        # General-case fallback for any SIGNAL_AMPLIFIER value.
        amp = SIGNAL_AMPLIFIER
        for t in range(total_session_s):
            P = P_arr[t]
            sum_amp = 0.0
            for i in range(n_bands):
                e = ema[i]
                e += (P - e) * inv_taus[i]
                ema[i] = e
                r = e * inv_rws[i]
                zone_arrs[i][t] = r
                sum_amp += r**amp
            I_arr[t] = INTENSITY_SCALE * (sum_amp**inv_amp)

    return (
        I_arr,
        workout_windows,
        inv_rws,
        inv_taus,
        inv_amp,
        zone_arrs,
        P_arr,
        kind_arr,
        wid_arr,
        per_segment_records,
    )


def _compute_wbal(cp, w_prime, total_session_s, P_arr, kind_arr):
    # ----- W'bal -----
    w_bal_curve: Optional[list] = None
    w_bal_trough: Optional[float] = None
    anaerobic_strain = 0.0
    if cp and w_prime and cp > 0 and w_prime > 0:
        # DCP: mean of P below CP.  When the rower is rarely below CP we
        # don't have good DCP — use mid-range τ_W'.
        below_cp_s = 0.0
        below_cp_p_sum = 0.0
        for t in range(total_session_s):
            if kind_arr[t] == "gap":
                continue
            P = P_arr[t]
            if P < cp:
                below_cp_s += 1
                below_cp_p_sum += P
        DCP = (below_cp_p_sum / below_cp_s) if below_cp_s > 0 else cp / 2.0
        tau_w = 546.0 * math.exp(-0.01 * DCP) + 316.0
        tau_w = max(TAU_W_MIN, min(TAU_W_MAX, tau_w))

        w_bal = w_prime
        w_bal_trough = w_prime
        w_bal_curve = [w_prime] * total_session_s
        for t in range(total_session_s):
            if kind_arr[t] == "gap":
                # No rowing during the gap — pure recovery toward W'.
                dW = (w_prime - w_bal) / tau_w
            else:
                P = P_arr[t]
                if P > cp:
                    dW = -(P - cp)
                else:
                    dW = (w_prime - w_bal) / tau_w
            w_bal = max(0.0, min(w_prime, w_bal + dW))
            w_bal_curve[t] = w_bal
            if w_bal < w_bal_trough:
                w_bal_trough = w_bal
        anaerobic_strain = max(0.0, min(1.0, 1.0 - w_bal_trough / w_prime))
    return anaerobic_strain, w_bal_curve, w_bal_trough


def compute_session_metrics(
    session_workouts: list[dict],
    ref_watts_at_duration_fn: Callable,
    cp: Optional[float],
    w_prime: Optional[float],
) -> dict:
    """Compute ESS / Severity / Anaerobic Strain across a session timeline.

    Parameters
    ----------
    session_workouts:
        The list of workouts that make up this session.  Order doesn't
        matter — we re-sort by start datetime internally.
    ref_watts_at_duration_fn:
        Callable ``(when: date, duration_s: float) -> Optional[float]``
        returning the rower's reference watts at a given duration on a
        given date.  Typically a partial of
        :func:`services.reference_watts.reference_watts_at_duration`.  Called
        once per band against the session's representative date (the last
        workout's date — Concept2 logs end-time).
    cp:
        Rower's critical power (watts).  Typically the date-aware 60-min
        reference watts.  ``None`` disables the W'bal track.
    w_prime:
        Rower's anaerobic capacity (joules).  ``None`` disables the
        W'bal track.

    Returns
    -------
    dict matching :func:`_empty_metrics`'s shape.  ``timeline`` is a list of
    sub-sampled per-second records ``{t, intensity, P, w_bal_pct, zones}``
    where ``zones`` is a dict mapping band-seconds → ratio.  ``per_workout``
    attributes session ESS to each constituent workout so summing the
    column yields the session value exactly.
    """
    # Annotate (start_dt, end_dt, w) per workout.
    annotated: list = []
    for w in session_workouts:
        end_dt = _parse_workout_datetime(w.get("date"))
        if end_dt is None:
            continue
        duration_s = _workout_total_duration_s(w)
        start_dt = end_dt - timedelta(seconds=duration_s)
        annotated.append((start_dt, end_dt, w))

    if not annotated:
        return _empty_metrics()
    annotated.sort(key=lambda x: x[0])

    session_start = annotated[0][0]
    session_end = max(end for _, end, _ in annotated)
    total_session_s = max(0, int((session_end - session_start).total_seconds() + 0.5))
    if total_session_s == 0:
        return _empty_metrics()

    (
        I_arr,
        workout_windows,
        inv_rws,
        inv_taus,
        inv_amp,
        zone_arrs,
        P_arr,
        kind_arr,
        wid_arr,
        per_segment_records,
    ) = _calculate_intensity(
        annotated, total_session_s, session_start, start_dt, ref_watts_at_duration_fn
    )

    # Re-key zone arrays back to ``{band_seconds: [...]}`` for existing
    # callers (timeline payload, per-segment records).  This is cheap —
    # we're transferring six list references, not data.
    zone_ratio_arr = {d: zone_arrs[i] for i, d in enumerate(ZONE_BANDS_S)}

    # ----- ESS -----
    ess_per_second = [(I_arr[t] * I_arr[t]) * C_ESS for t in range(total_session_s)]
    ess_total = sum(ess_per_second)

    # Fill per-segment intensity_avg / ESS now that the I_arr exists.
    for rec in per_segment_records:
        s = int(rec["t_session_s"])
        d = max(1, int(rec["duration_s"] + 0.5))
        e = min(s + d, total_session_s)
        if e > s:
            slc = I_arr[s:e]
            rec["intensity_avg"] = sum(slc) / len(slc) if slc else 0.0
            rec["ess"] = sum(ess_per_second[s:e])

    # ----- Peak rolling I(t) (session-level) -----
    peak_5min = _peak_rolling_mean(I_arr, 300)
    peak_60s = _peak_rolling_mean(I_arr, 60)
    peak_15s = _peak_rolling_mean(I_arr, 15)

    anaerobic_strain, w_bal_curve, w_bal_trough = _compute_wbal(
        cp, w_prime, total_session_s, P_arr, kind_arr
    )

    # ----- Severity (session-level) -----
    # Severity = peak rolling I(t) over short windows + a strain bonus.
    # The strain term rescues short max-efforts that don't sustain long
    # enough for the long-band EMAs to climb (e.g. a 2k PB peaks ~1.1 in I
    # but fully drains W'bal — the +0.5·strain pushes it past 1.4 → Maximal).
    severity_score = (
        max(peak_5min, 0.90 * peak_60s, 0.75 * peak_15s) + 0.50 * anaerobic_strain
    )
    sev_bucket = severity_bucket(severity_score) or "Low"

    per_workout_records = _attach_per_workout_records(
        total_session_s,
        workout_windows,
        w_bal_curve,
        w_prime,
        P_arr,
        inv_taus,
        inv_rws,
        inv_amp,
        kind_arr,
        wid_arr,
        ess_per_second,
    )

    # ----- Session-level intensity -----
    # Same convention as per-workout intensity: arithmetic mean over
    # work-only seconds in the session.  Includes carryover state across
    # workouts.
    work_intensity_session = [
        I_arr[t] for t in range(total_session_s) if kind_arr[t] == "work"
    ]
    intensity_session = (
        sum(work_intensity_session) / len(work_intensity_session)
        if work_intensity_session
        else 0.0
    )

    timeline = _build_session_timeline(
        total_session_s,
        zone_ratio_arr,
        w_bal_curve,
        w_prime,
        I_arr,
        P_arr,
    )

    return {
        "ess": ess_total,
        "intensity_session": intensity_session,
        "peak_intensity_5min": peak_5min,
        "peak_intensity_60s": peak_60s,
        "peak_intensity_15s": peak_15s,
        "severity_score": severity_score,
        "severity_bucket": sev_bucket,
        "anaerobic_strain": anaerobic_strain,
        "w_bal_trough": w_bal_trough,
        "duration_s": float(total_session_s),
        "per_workout": per_workout_records,
        "per_segment": per_segment_records,
        "timeline": timeline,
    }
