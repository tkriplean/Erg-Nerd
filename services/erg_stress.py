"""
Erg Stress Score — multi-band power-duration-saturation training-load metric.

This module is the home of three per-workout / per-session metrics:

* **ESS** — time integral of ``I(t)²`` over the session.  Calibrated so a
  60' continuous effort at 60-min reference watts yields ESS ≈ 100.  Strictly
  additive: a session's ESS is the sum of its workouts' ESS, and a workout's
  ESS is the sum of its segments'.
* **Severity** — peak rolling ``I(t)`` (5-min, 60-s, 20-s windows) plus a
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
``SEVERITY_STYLE`` — color metadata for the UI chip.
``ZONE_BANDS_S`` — the six band time-constants (= τ_d), in seconds.
``ZONE_BAND_LABELS`` — short human labels matching the bands.

Implementation notes
--------------------
The hot per-second loops vectorise via numpy and ``scipy.signal.lfilter``:
the six-band EMA in :func:`_calculate_intensity` and the per-workout EMA
in :func:`_attach_per_workout_records` both run as ``lfilter`` recurrences
against ``P_arr`` (zero-init), and the cube-and-sum intensity fold is one
elementwise pass.  ESS attribution to overlapping workouts is preserved
via ``np.bincount`` over a ``wid_idx_arr`` painted by per-segment slice
assignment.  Only :func:`_compute_wbal` keeps a Python integration loop
(the recursive clamp/branch doesn't map to lfilter); it operates on a
preallocated ``np.ndarray``.

:func:`compute_session_metrics` accepts ``with_timeline=False`` to skip
the per-second sub-sampled timeline build — the Workouts list view
strips ``_ess_timeline`` from the JS payload, so building it there is
~1.8 M wasted dict allocations.  The Workout detail page uses the
default and renders the chart from that scope.

No HyperDiv, no I/O.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np
from scipy.signal import lfilter

from services.rowing_utils import (
    PACE_MAX,
    PACE_MIN,
    compute_watts,
)
from services.heartrate_utils import _extract_hr
from services.glycogen import cumulative_glycogen_curve, session_glycogen_used


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
    300: 0.30,
    1200: 0.30,
    3600: 0.30,
    7200: 0.30,
}


def _tau(d: int) -> float:
    """Effective EMA time constant for band ``d`` (seconds)."""
    return float(d) * EMA_TAU_FACTORS.get(d, 1.0)


#: Per-band saturation threshold for *full* stimulus.  When peak
#: ``zone_ratio_d`` reaches this value, the dose is 1.0 (full stimulus).
#: Higher peaks scale linearly toward 2.0 (peak = 1.0) up to a 3.0 cap
#: for super-PB efforts.  Below threshold, dose ramps quadratically from
#: 0 to ~0.95 as a function of peak, so a near-threshold effort still
#: reads "almost full."
#:
#: Reference watts are PB-anchored, so any non-PB workout has
#: ``zone_ratio < 1.0``.  Each ``S_thresh`` is chosen so the canonical
#: stimulus prescription for that system lands at peak ≥ ``S_thresh``:
#:
#:   - **Sprint (0.60):** lit prescription is 4–12 efforts of 5–30 s at
#:     near-max with full recovery (Allen-Coggan).  A 30 s sprint at
#:     near-PB pace generates ``zone_ratio_20s ≈ 0.85+``; the PB-anchored
#:     RW often pulls peak down (a strong 1-min PB lifts RW_20s above
#:     pure-sprint pace), so 0.60 accommodates training-effort sprints.
#:   - **Anaerobic (0.65):** 3–8 efforts of 30–180 s at supra-threshold
#:     (Glaister).  90 s @ supra-CP brings ``zone_ratio_90s`` to ~0.85+.
#:   - **VO2max (0.70):** 4–15 min cumulative at 90–100 % VO2max
#:     (Buchheit-Laursen, Billat).  4 × 4 min @ VO2max produces peak
#:     ``zone_ratio_5min ≈ 0.93``; a training-pace 5k at 85 % of PB
#:     reaches ~0.81.
#:   - **Threshold (0.80):** 30–60 min cumulative at LT2 / MLSS
#:     (Seiler-Tønnessen).  2 × 20 min @ threshold yields peak ≈ 0.96–0.98.
#:   - **Tempo (0.85):** 40+ min sustained at sweet-spot.  60 min @ FTP
#:     yields peak ``zone_ratio_60min ≈ 0.96``.
#:   - **Endurance (0.75):** 60+ min LIT.  τ for the 2-hour band is
#:     ≈ 36 min, so even a 90–120 min workout climbs to ≤ 0.93 peak;
#:     0.75 is required to accommodate this.
STIMULUS_S_THRESH: dict[int, float] = {
    20: 0.60,    # Sprint
    90: 0.65,    # Anaerobic
    300: 0.75,   # VO2max     — 0.70 leaks 5 min @ FTP into "full"; 0.75 excludes
    1200: 0.80,  # Threshold
    3600: 0.85,  # Tempo
    7200: 0.75,  # Endurance
}

#: Per-band minimum total work duration (seconds) to register stimulus.
#: Workouts whose work-only duration is below this get ``dose = 0``
#: regardless of peak — filters out brief warmups / sample efforts that
#: incidentally cross a band's saturation threshold.
#:
#: Calibrated to the *shortest* effort in lit prescriptions for each
#: system, not the typical session length:
#:
#:   - **Sprint 10 s** — 100 m PBs (~15 s), 200 m efforts (~30 s) all
#:     count.  Single explosive sprint registers; sustained 1-second
#:     samples don't.
#:   - **Anaerobic 45 s** — 250 m / 500 m PBs (~60–85 s), 1 min PB
#:     (60 s) all count.  Below 45 s the EMA hasn't filled enough
#:     for the band to mean anything.
#:   - **VO2max 180 s** — 3-min reps and beyond.  Single 4 min rep
#:     counts; 1 min "spike" doesn't.
#:   - **Threshold 600 s** — 10 min sustained or more.
#:   - **Tempo 1500 s** — 25 min sustained or more.
#:   - **Endurance 1800 s** — 30 min LIT or more.
STIMULUS_MIN_WORK_S: dict[int, float] = {
    20: 10.0,
    90: 45.0,
    300: 180.0,
    1200: 600.0,
    3600: 1500.0,
    7200: 1800.0,
}

# Backwards-compat aliases.  Earlier versions exposed STIMULUS_PEAK_GATE
# and STIMULUS_T_TARGET; keep them as read-only mirrors while downstream
# callers migrate.
STIMULUS_PEAK_GATE: dict[int, float] = STIMULUS_S_THRESH
STIMULUS_T_THRESH: dict[int, float] = STIMULUS_MIN_WORK_S
STIMULUS_T_TARGET: dict[int, float] = STIMULUS_MIN_WORK_S

#: Per-band stimulus filter chip definitions for the Workouts page.
#: Keyed by band-seconds; values describe the physiological system the
#: band targets, used in chip-tooltip body text.
STIMULUS_BAND_DEFINITIONS: dict[int, str] = {
    20: (
        "Sprint band — neuromuscular / phosphocreatine power.  "
        "Stimulated by repeated near-maximal sprints (e.g. 4 × 30 s)."
    ),
    90: (
        "Anaerobic band — supra-threshold work, fast glycolysis.  "
        "Stimulated by repeated 60–120 s efforts (e.g. 6 × 90 s)."
    ),
    300: (
        "VO2max band — maximal aerobic power.  "
        "Stimulated by 3–8 min reps near VO2max (e.g. 4 × 4 min)."
    ),
    1200: (
        "Threshold band — lactate / MLSS work.  "
        "Stimulated by 10–30 min sustained efforts (e.g. 2 × 20 min)."
    ),
    3600: (
        "Tempo band — sustained sub-threshold work.  "
        "Stimulated by 40+ min continuous at ~FTP (e.g. 60 min @ FTP)."
    ),
    7200: (
        "Endurance band — long aerobic / Z2 base.  "
        "Stimulated by 60+ min low-intensity volume (LIT)."
    ),
}

#: Filter-rule text for the stimulus chip tooltip.  Uniform "≥ 1.0× dose"
#: rule across all bands.
STIMULUS_FILTER_TEXT: dict[int, str] = {
    d: (
        "Selected: workouts that delivered a full adaptation-grade "
        "stimulus to this system (dose ≥ 1.0×)."
    )
    for d in (20, 90, 300, 1200, 3600, 7200)
}


#: Dose buckets for the stimulus-strip renderer.  Boundaries on the
#: continuous ``dose`` value: < 0.5 = none, < 1.0 = partial, < 2.0 = full,
#: ≥ 2.0 = overdose.  Exposed as a constant so the JS plugin can mirror
#: the cutoffs without hardcoding them.
STIMULUS_DOSE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("none", 0.5),
    ("partial", 1.0),
    ("full", 2.0),
    ("overdose", float("inf")),
)


def _compute_stimulus_doses(
    Z_w: np.ndarray,
    work_mask: np.ndarray,
    zone_time_fractions: Optional[dict] = None,
) -> dict[int, float]:
    """Per-band stimulus dose from the workout-isolated band-saturation matrix.

    ``Z_w`` is the 6×N matrix ``zone_ratio_d(t) = EMA_d(P_w)(t) / RW_d``
    already computed for the Intensity signal.  ``work_mask`` is the
    boolean per-second mask of work-only seconds.  ``zone_time_fractions``
    is the closest-RW band classification for the same workout (per-band
    fraction of work-seconds where each band's reference watts was the
    nearest of the six).  When provided, it gates the dose values via the
    confirmation rule below.

    Peak-driven dose model:

      * ``dose = 0`` when total work time < ``STIMULUS_MIN_WORK_S[d]``
        (workout too short to credit this band).
      * else when peak < ``STIMULUS_S_THRESH[d]``: partial credit
        ``dose = (peak / S_thresh)²`` (quadratic ramp, capped just below
        1.0).  Reaches ~0.6 at peak = 0.78·S, ~0.9 at peak = 0.95·S.
      * else (peak ≥ S_thresh): full or beyond.
        ``dose = 1.0 + (peak − S_thresh) / (1 − S_thresh)``, capped at 3.0.
        Lands at 1.0 right at threshold; 2.0 at peak = 1.0; 3.0 cap for
        super-PB efforts where zone_ratio exceeds 1.0.

    Closest-RW confirmation gate (when ``zone_time_fractions`` is passed):
        For each candidate band ``d``, the dose is kept *only if* the
        rower spent some work-time at watts classified to band ``d`` OR a
        more-intense (shorter-duration) band.  Otherwise the dose is
        zeroed out.

    The gate solves a class of false-positive readings.  Without it, the
    peak-driven dose can register partial stimulus across all bands for
    a sustained low-intensity workout — e.g., easy Z2 at 180 W elevates
    every band's EMA peak above 0.36 (zone_ratio_20s = 0.36, etc.), and
    the quadratic partial-credit branch produces small but non-zero doses
    everywhere.  Physiologically, no Sprint / Anaerobic / VO2max stimulus
    was actually delivered — the rower never produced watts in those
    bands' classification range.  The closest-RW classification is the
    immediate-power fingerprint; using it to confirm the duration-aware
    EMA dose strips the leakage cleanly.

    Sub-band leakage (e.g., 1-hour FTP triggering Sprint stimulus because
    sustained 240 W elevates short-EMA seconds) is bounded by the
    per-band ``S_thresh`` plus the closest-RW gate.  Short-EMA bands need
    their *peak* to reach threshold AND the rower to have spent some time
    at watts classified to that band or higher.

    Workout-isolated convention (bands seed at zero per workout) — same
    as Severity / Intensity / Zone Spread.  A cooldown after a race
    reports its own profile, not inherited session state.
    """
    if Z_w.size == 0 or not work_mask.any():
        return {int(d): 0.0 for d in ZONE_BANDS_S}

    Z_work = Z_w[:, work_mask]   # (6, n_work)
    work_s = int(Z_work.shape[1])
    peaks = Z_work.max(axis=1)   # (6,)

    s_thresh_arr = np.array([STIMULUS_S_THRESH[d] for d in ZONE_BANDS_S])
    min_work_arr = np.array([STIMULUS_MIN_WORK_S[d] for d in ZONE_BANDS_S])

    # Default to zero for every band; selectively populate.
    dose = np.zeros(len(ZONE_BANDS_S), dtype=np.float64)

    # Mask out bands where total work duration is insufficient.
    duration_pass = work_s >= min_work_arr   # (6,) boolean
    # Above-threshold branch.
    above_thresh = (peaks >= s_thresh_arr) & duration_pass
    # Below-threshold-but-passing-duration branch.
    below_thresh = (~above_thresh) & duration_pass & (peaks > 0)

    # Above threshold: 1.0 + overshoot/(1-S), capped at 3.0
    if above_thresh.any():
        denom = np.maximum(0.01, 1.0 - s_thresh_arr)
        dose[above_thresh] = np.minimum(
            3.0,
            1.0 + (peaks[above_thresh] - s_thresh_arr[above_thresh])
            / denom[above_thresh],
        )
    # Below threshold: quadratic partial credit
    if below_thresh.any():
        ratio = peaks[below_thresh] / s_thresh_arr[below_thresh]
        dose[below_thresh] = np.minimum(0.99, ratio * ratio)

    # Closest-RW confirmation gate.  ``ZONE_BANDS_S`` runs shortest →
    # longest = highest-intensity → lowest-intensity.  For each band d,
    # cumulative_fraction[d] = Σ fraction[d'] for d' ≤ d = fraction of
    # work-time spent at watts classified to band d or any more-intense
    # band.  A band is confirmed when this cumulative fraction is > 0.
    if zone_time_fractions is not None:
        cumulative = 0.0
        for i, d in enumerate(ZONE_BANDS_S):
            cumulative += float(zone_time_fractions.get(int(d), 0.0))
            if cumulative <= 0.0:
                dose[i] = 0.0

    return {
        int(ZONE_BANDS_S[i]): float(dose[i])
        for i in range(len(ZONE_BANDS_S))
    }


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

#: Per-bucket visual style for table cells / chips.
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

SEVERITY_FILTER_TEXT: dict[str, str] = {
    "Low": "Selected: workouts in the Low severity bucket.",
    "Moderate": "Selected: workouts in the Moderate severity bucket.",
    "High": "Selected: workouts in the High severity bucket.",
    "Maximal": "Selected: workouts in the Maximal severity bucket.",
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
# Single-workout zone summary (Zone Spread)
# ---------------------------------------------------------------------------


def compute_workout_zone_summary(
    workout: dict,
    ref_watts_at_duration_fn: Callable,
) -> Optional[dict]:
    """Workout-isolated zone time fractions by closest-reference-watts band.

    Classifies each *work*-second of the workout to the duration band whose
    reference watts (``RW_d``) is closest in absolute distance to the
    second's power.  Returns:

        {"zone_time_fractions": {band_seconds: fraction, ...}}

    The dict has one entry per :data:`ZONE_BANDS_S` and sums to 1.0 over
    work-only seconds (or 0 if the workout has no work seconds).

    Returns ``None`` when the workout cannot be summarised — no segments,
    zero duration, or missing reference watts at any band.

    Semantics — this is a **power-level distribution** (Power Spread): a
    sample's classification depends only on its instantaneous watts vs. the
    rower's six reference points, not on duration-band activation history.
    Distinct from the multi-band EMA *intensity* signal :math:`I(t)`, which
    is duration-aware (a 30-s sprint vs. a 30-min steady ride at the same
    average watts produce different intensity profiles but the same power-
    level distribution).
    """
    segments = build_segments(workout)
    if not segments:
        return None

    duration_s = int(round(_workout_total_duration_s(workout)))
    if duration_s <= 0:
        return None

    when = workout.get("date_dt")
    rws_list: list[float] = []
    for d in ZONE_BANDS_S:
        rw = ref_watts_at_duration_fn(when, d)
        if rw is None or rw <= 0:
            return None
        rws_list.append(float(rw))

    P_arr = np.zeros(duration_s, dtype=np.float64)
    kind_arr = np.empty(duration_s, dtype=object)
    kind_arr[:] = "rest"
    for seg in segments:
        s = int(seg["t_offset_s"])
        e = min(s + int(round(seg["duration_s"])), duration_s)
        if e > s:
            P_arr[s:e] = float(seg["watts"])
            kind_arr[s:e] = seg["kind"]

    n_bands = len(ZONE_BANDS_S)
    work_mask = kind_arr == "work"
    if not work_mask.any():
        zeros = {int(d): 0.0 for d in ZONE_BANDS_S}
        return {"zone_time_fractions": dict(zeros)}

    rws_arr = np.asarray(rws_list, dtype=np.float64)
    P_work = P_arr[work_mask]
    # Classify each work-second to argmin_d |P − RW_d|.  For piecewise-
    # constant P (segment-based source data) this is exact per-second; the
    # broadcast cost is trivial (n_bands × n_work floats).
    band_per_second = np.abs(P_work[None, :] - rws_arr[:, None]).argmin(axis=0)
    counts = np.bincount(band_per_second, minlength=n_bands)
    total_count = int(counts.sum())
    fractions_arr = (
        counts / total_count if total_count else np.zeros(n_bands, dtype=np.float64)
    )

    return {
        "zone_time_fractions": {
            int(ZONE_BANDS_S[i]): float(fractions_arr[i]) for i in range(n_bands)
        },
    }


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
        "peak_intensity_20s": 0.0,
        "severity_score": 0.0,
        "severity_bucket": "Low",
        "anaerobic_strain": 0.0,
        "glycogen_used": None,
        "glycogen_kj": None,
        "w_bal_trough": None,
        "duration_s": 0.0,
        "per_workout": [],
        "per_segment": [],
        "timeline": [],
    }


def _peak_rolling_mean(arr, window: int) -> float:
    """Maximum rolling-mean over ``window`` of a numeric sequence.

    Accepts a Python list or a 1-D numpy array.  For ``window >= len(arr)``,
    returns the mean of the whole array (so a short session reports its
    overall mean as its 5-min peak).  Implemented via cumulative-sum so the
    cost is O(N) numpy elementwise, not O(N) Python.
    """
    a = np.asarray(arr, dtype=np.float64)
    n = a.size
    if n == 0:
        return 0.0
    w = min(window, n)
    if w <= 0:
        return 0.0
    if w >= n:
        return float(a.mean())
    cs = np.concatenate(([0.0], np.cumsum(a)))
    return float(((cs[w:] - cs[:-w]) / w).max())


def _attach_per_workout_records(
    total_session_s,
    workout_windows,
    w_bal_curve,
    w_prime,
    P_arr,
    inv_taus,
    inv_rws,
    rws_list,
    inv_amp,
    kind_arr,
    wid_idx_arr,
    ess_per_second,
    mass_kg,
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
    rws_arr = np.asarray(rws_list, dtype=np.float64)

    # Per-workout ESS attribution.  ``wid_idx_arr`` (built in
    # :func:`_calculate_intensity` via vectorised slice assignment) maps
    # each second to a workout index in iteration order of
    # ``workout_windows`` (-1 for gap seconds).  Last-painter-wins for
    # overlapping segments falls out of the slice-assignment order.
    n_w = len(workout_windows)
    if n_w and total_session_s:
        mask = wid_idx_arr >= 0
        if mask.any():
            sums = np.bincount(
                wid_idx_arr[mask], weights=ess_per_second[mask], minlength=n_w
            )
        else:
            sums = np.zeros(n_w, dtype=np.float64)
        ess_by_workout = {wid: float(sums[i]) for i, wid in enumerate(workout_windows)}
    else:
        ess_by_workout = {wid: 0.0 for wid in workout_windows}

    per_workout_records: list = []
    for wid, (t_start, t_end) in workout_windows.items():
        n = max(1, t_end - t_start)

        # Workout-isolated EMA: lfilter zero-inits, so each per-workout
        # filter run starts from rest — semantically equivalent to the
        # original ``ema_w = [0.0] * n_bands`` reset.
        P_w = P_arr[t_start:t_end]
        kind_w = kind_arr[t_start:t_end]
        if P_w.size:
            Z_w = np.empty((n_bands, P_w.size), dtype=np.float64)
            for i in range(n_bands):
                a = inv_taus[i]
                Z_w[i] = lfilter([a], [1.0, a - 1.0], P_w) * inv_rws[i]
            if SIGNAL_AMPLIFIER == 3:
                sum_amp_w = (Z_w * Z_w * Z_w).sum(axis=0)
            else:
                sum_amp_w = np.power(Z_w, SIGNAL_AMPLIFIER).sum(axis=0)
            I_w_arr = INTENSITY_SCALE * np.power(sum_amp_w, inv_amp)
        else:
            I_w_arr = np.zeros(0, dtype=np.float64)

        # Workout intensity: mean over work-only seconds in this window.
        # Zone Spread (power-level distribution): classify each work-second
        # to argmin_d |P − RW_d|, the duration band whose reference watts
        # is closest to the instantaneous power.  Independent of the EMA
        # state above — a duration-aware view lives in I(t).
        # Training Stimulus: per-band cumulative-time-at-saturation against
        # the same Z_w matrix.  EMA-based and duration-aware (a 30-min
        # FTP and a 30-s sprint at the same average watts produce different
        # stimulus signatures).
        if I_w_arr.size:
            work_w_mask = kind_w == "work"
            intensity_w = (
                float(I_w_arr[work_w_mask].mean()) if work_w_mask.any() else 0.0
            )
            if work_w_mask.any():
                P_work = P_w[work_w_mask]
                band_per_second = np.abs(P_work[None, :] - rws_arr[:, None]).argmin(
                    axis=0
                )
                counts = np.bincount(band_per_second, minlength=n_bands)
                total_count = int(counts.sum())
                fractions_arr = (
                    counts / total_count
                    if total_count
                    else np.zeros(n_bands, dtype=np.float64)
                )
                # Build zone_time_fractions first so the stimulus call can
                # use it as the closest-RW confirmation gate.
                zone_time_fractions = {
                    int(ZONE_BANDS_S[i]): float(fractions_arr[i])
                    for i in range(n_bands)
                }
                stimulus_doses = _compute_stimulus_doses(
                    Z_w, work_w_mask, zone_time_fractions
                )
            else:
                fractions_arr = np.zeros(n_bands, dtype=np.float64)
                zone_time_fractions = {int(d): 0.0 for d in ZONE_BANDS_S}
                stimulus_doses = {int(d): 0.0 for d in ZONE_BANDS_S}
        else:
            intensity_w = 0.0
            fractions_arr = np.zeros(n_bands, dtype=np.float64)
            zone_time_fractions = {int(d): 0.0 for d in ZONE_BANDS_S}
            stimulus_doses = {int(d): 0.0 for d in ZONE_BANDS_S}

        stimulus_systems = sorted(d for d, dose in stimulus_doses.items() if dose >= 1.0)

        slc = I_w_arr if I_w_arr.size else np.zeros(1, dtype=np.float64)
        pk5 = _peak_rolling_mean(slc, 300)
        pk60 = _peak_rolling_mean(slc, 60)
        pk20 = _peak_rolling_mean(slc, 20)

        # Anaerobic strain per workout: depletion *caused* by this workout
        # = (W'bal at workout start − min W'bal during workout) / W'.
        # A cooldown that starts on an empty reserve and only recovers reads
        # 0% even though the absolute W'bal level stays low; a race that
        # drains a fresh reserve to 0 reads 100%.
        if w_bal_curve is not None and w_prime:
            wb_slc = w_bal_curve[t_start:t_end]
            if wb_slc.size:
                wb_start = float(w_bal_curve[t_start - 1]) if t_start > 0 else w_prime
                strain_w = max(
                    0.0, min(1.0, (wb_start - float(wb_slc.min())) / w_prime)
                )
            else:
                strain_w = 0.0
        else:
            strain_w = 0.0

        # Per-workout glycogen used: integrate CHO burn rate over the
        # workout-isolated work-only seconds.  Uses the workout-isolated
        # I_w_arr (not the session-state I) so a cooldown after a race
        # reports its own fueling, not the inherited session state.
        if I_w_arr.size and P_w.size:
            glycogen_kj_w, glycogen_used_w = session_glycogen_used(
                P_w, I_w_arr, kind_w == "work", mass_kg
            )
        else:
            glycogen_kj_w, glycogen_used_w = (None, None) if mass_kg else (None, None)

        sev_w = _calc_severity(pk5, pk60, pk20, strain_w, glycogen_used_w)
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
                "peak_intensity_20s": pk20,
                "severity_score": sev_w,
                "severity_bucket": severity_bucket(sev_w) or "Low",
                "anaerobic_strain": strain_w,
                "glycogen_used": glycogen_used_w,
                "glycogen_kj": glycogen_kj_w,
                "zone_time_fractions": zone_time_fractions,
                "stimulus_doses": stimulus_doses,
                "stimulus_systems": stimulus_systems,
            }
        )
    return per_workout_records


#: Severity-formula weight on Skiba W' depletion (anaerobic strain).
SEVERITY_W_STRAIN_WEIGHT: float = 0.50

#: Severity-formula weight on glycogen depletion.  Lower than the W'-strain
#: weight because food refills glycogen faster than rest refills W', and
#: to dampen double-counting on workouts that drain both reservoirs.  This
#: is the term that elevates HM/marathon-distance efforts (low W' use,
#: high glycogen use) into the Maximal severity bucket they deserve.
SEVERITY_GLYCOGEN_WEIGHT: float = 0.40


def _calc_severity(pk5, pk60, pk20, anaerobic_strain, glycogen_used=0.0):
    """Severity score: peak rolling I(t) + recovery-debt contributions.

    Combines three orthogonal contributions:
      * ``max(pk5, pk60, pk20)`` — peak rolling intensity over short
        windows; captures neuromuscular / VO2max-domain stress.
      * ``0.50 · anaerobic_strain`` — Skiba W' depletion fraction;
        captures supra-CP recovery debt.
      * ``0.40 · glycogen_used`` — CHO depletion fraction; captures
        long-effort fueling debt (the term that correctly elevates
        HM/marathon efforts into Maximal severity).

    ``glycogen_used`` may be ``None`` (no profile mass available); treated
    as 0.0.  All weights live in module-level constants.
    """
    return (
        max(pk5, pk60, pk20)
        + SEVERITY_W_STRAIN_WEIGHT * anaerobic_strain
        + SEVERITY_GLYCOGEN_WEIGHT * (glycogen_used or 0.0)
    )


def _build_session_timeline(
    total_session_s,
    zone_ratio_arr,
    w_bal_curve,
    w_prime,
    I_arr,
    P_arr,
    glycogen_curve=None,
):
    # ----- Timeline (sub-sampled for chart payload) -----
    # Keep ≤ 1800 points to bound payload size; for workouts ≤ 30 min, full
    # 1-Hz resolution (1800 pts).  Longer: stride out evenly.  Each kept
    # timepoint includes the per-zone ratios so the chart can paint the six
    # band traces as percentages on the right axis.
    target_points = 1800
    stride = max(1, total_session_s // target_points)
    ts = np.arange(0, total_session_s, stride)
    I_s = I_arr[ts]
    P_s = P_arr[ts]
    has_wbal = (w_bal_curve is not None) and bool(w_prime)
    if has_wbal:
        wb_s = w_bal_curve[ts] / w_prime
    has_glycogen = glycogen_curve is not None
    if has_glycogen:
        gly_s = glycogen_curve[ts]
    bands = list(ZONE_BANDS_S)
    Z_s = {d: zone_ratio_arr[d][ts] for d in bands}
    timeline = []
    for k, t in enumerate(ts):
        timeline.append(
            {
                "t": int(t),
                "intensity": float(I_s[k]),
                "P": float(P_s[k]),
                "w_bal_pct": float(wb_s[k]) if has_wbal else None,
                "glycogen_pct": float(gly_s[k]) if has_glycogen else None,
                "zones": {d: float(Z_s[d][k]) for d in bands},
            }
        )
    return timeline


def _calculate_intensity(
    annotated, total_session_s, session_start, start_dt, ref_watts_at_duration_fn
):
    # Build per-second arrays.  Initialised to zero so untouched seconds
    # (gaps within the 30-minute session window where no workout is logged)
    # contribute neither power nor intensity.  ``wid_idx_arr`` maps each
    # second to a workout index into ``workout_windows.keys()`` (-1 for
    # gaps); used by ``_attach_per_workout_records`` for vectorised
    # last-painter-wins ESS attribution via ``np.bincount``.
    P_arr = np.zeros(total_session_s, dtype=np.float64)
    wid_idx_arr = np.full(total_session_s, -1, dtype=np.int64)
    kind_arr = np.full(total_session_s, "gap", dtype="<U4")

    workout_windows: dict = {}  # workout_id → (sess_t_start, sess_t_end)
    per_segment_records: list = []

    for start_dt, end_dt, w in annotated:
        wkt_offset_s = int((start_dt - session_start).total_seconds() + 0.5)
        wkt_id = w.get("id")
        wkt_segments = build_segments(w)
        if not wkt_segments:
            continue
        wkt_idx = len(workout_windows)
        wkt_t_end = wkt_offset_s
        for seg in wkt_segments:
            seg_t = wkt_offset_s + int(seg["t_offset_s"] + 0.5)
            seg_d = max(1, int(seg["duration_s"] + 0.5))
            seg_P = seg["watts"]
            end_t = min(seg_t + seg_d, total_session_s)
            if end_t > seg_t:
                P_arr[seg_t:end_t] = seg_P
                kind_arr[seg_t:end_t] = seg["kind"]
                wid_idx_arr[seg_t:end_t] = wkt_idx
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
    # The recursion ``e[t] = e[t-1] + (P[t] - e[t-1]) * α`` is algebraically
    # ``e[t] = α·P[t] + (1-α)·e[t-1]``, which maps to
    # ``lfilter([α], [1, α-1], P)`` (zero-init).  scipy runs this in C, so
    # the dominant per-second cost collapses to six BLAS-y kernel launches
    # plus one elementwise cube + sum + power for the intensity fold.
    n_bands = len(ZONE_BANDS_S)
    rws_list = [rw_d[d] for d in ZONE_BANDS_S]
    inv_rws = [(1.0 / rw if rw > 0 else 0.0) for rw in rws_list]
    inv_taus = [1.0 / _tau(d) for d in ZONE_BANDS_S]
    inv_amp = 1.0 / SIGNAL_AMPLIFIER

    Z = np.empty((n_bands, total_session_s), dtype=np.float64)
    for i in range(n_bands):
        a = inv_taus[i]
        Z[i] = lfilter([a], [1.0, a - 1.0], P_arr) * inv_rws[i]

    if SIGNAL_AMPLIFIER == 3:
        sum_amp = (Z * Z * Z).sum(axis=0)
    else:
        sum_amp = np.power(Z, SIGNAL_AMPLIFIER).sum(axis=0)
    # ``sum_amp`` is non-negative (cube of a non-negative ratio is
    # non-negative; for general amplifier zone_ratio is always ≥ 0).
    I_arr = INTENSITY_SCALE * np.power(sum_amp, inv_amp)
    zone_arrs = list(Z)  # per-band views, in ZONE_BANDS_S order

    return (
        I_arr,
        workout_windows,
        inv_rws,
        rws_list,
        inv_taus,
        inv_amp,
        zone_arrs,
        P_arr,
        kind_arr,
        wid_idx_arr,
        per_segment_records,
    )


def _compute_wbal(cp, w_prime, total_session_s, P_arr, kind_arr):
    # ----- W'bal -----
    w_bal_curve: Optional[np.ndarray] = None
    w_bal_trough: Optional[float] = None
    anaerobic_strain = 0.0
    if cp and w_prime and cp > 0 and w_prime > 0:
        # DCP: mean of P (work seconds only) below CP.  When the rower is
        # rarely below CP we don't have good DCP — use mid-range τ_W'.
        non_gap = kind_arr != "gap"
        below = non_gap & (P_arr < cp)
        DCP = float(P_arr[below].mean()) if below.any() else cp / 2.0
        tau_w = 546.0 * math.exp(-0.01 * DCP) + 316.0
        tau_w = max(TAU_W_MIN, min(TAU_W_MAX, tau_w))

        # Pre-decompose into per-second deltas for the recursive update:
        #   gap second:   dW = (w_prime - w_bal) / tau_w     (recovery)
        #   work, P>cp:   dW = -(P - cp)                      (depletion)
        #   work, P≤cp:   dW = (w_prime - w_bal) / tau_w     (recovery)
        # The recovery term depends on the running ``w_bal``, so we keep
        # the loop in Python — but on a preallocated numpy array.
        is_gap = kind_arr == "gap"
        is_above = (~is_gap) & (P_arr > cp)
        # Depletion magnitude is constant per-second: store it once.
        depletion = np.where(is_above, P_arr - cp, 0.0)
        # ``recover_mask[t]`` is True iff this second is a recovery
        # (i.e. either a gap, or a work second with P ≤ cp).
        recover_mask = ~is_above
        inv_tau_w = 1.0 / tau_w

        w_bal = w_prime
        w_bal_trough = w_prime
        w_bal_curve = np.empty(total_session_s, dtype=np.float64)
        for t in range(total_session_s):
            if recover_mask[t]:
                dW = (w_prime - w_bal) * inv_tau_w
            else:
                dW = -depletion[t]
            new_w = w_bal + dW
            if new_w < 0.0:
                new_w = 0.0
            elif new_w > w_prime:
                new_w = w_prime
            w_bal = new_w
            w_bal_curve[t] = new_w
            if new_w < w_bal_trough:
                w_bal_trough = new_w
        anaerobic_strain = max(0.0, min(1.0, 1.0 - w_bal_trough / w_prime))
    return anaerobic_strain, w_bal_curve, w_bal_trough


def compute_session_metrics(
    session_workouts: list[dict],
    ref_watts_at_duration_fn: Callable,
    cp: Optional[float],
    w_prime: Optional[float],
    *,
    mass_kg: Optional[float] = None,
    with_timeline: bool = True,
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
    mass_kg:
        Rower's body mass in kilograms.  ``None`` disables the Glycogen
        Used track (per-workout / session-level ``glycogen_used`` and
        ``glycogen_kj`` are returned as ``None``).
    with_timeline:
        When False, ``timeline`` is returned as ``None`` and the per-second
        sub-sampling step is skipped entirely.  The Workouts page list view
        never reads ``_ess_timeline`` (table renderer drops it via
        ``_TABLE_IRRELEVANT_KEYS``); skipping the build there saves ~1.8 M
        Python dict allocations per page render.

    Returns
    -------
    dict matching :func:`_empty_metrics`'s shape.  ``timeline`` is a list of
    sub-sampled per-second records ``{t, intensity, P, w_bal_pct, zones}``
    where ``zones`` is a dict mapping band-seconds → ratio (or ``None``
    when ``with_timeline=False``).  ``per_workout`` attributes session
    ESS to each constituent workout so summing the column yields the
    session value exactly.
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
        rws_list,
        inv_taus,
        inv_amp,
        zone_arrs,
        P_arr,
        kind_arr,
        wid_idx_arr,
        per_segment_records,
    ) = _calculate_intensity(
        annotated, total_session_s, session_start, start_dt, ref_watts_at_duration_fn
    )

    # Re-key zone arrays back to ``{band_seconds: [...]}`` for existing
    # callers (timeline payload, per-segment records).  This is cheap —
    # we're transferring six list references, not data.
    zone_ratio_arr = {d: zone_arrs[i] for i, d in enumerate(ZONE_BANDS_S)}

    # ----- ESS -----
    ess_per_second = (I_arr * I_arr) * C_ESS
    ess_total = float(ess_per_second.sum())

    # Fill per-segment intensity_avg / ESS now that the I_arr exists.
    for rec in per_segment_records:
        s = int(rec["t_session_s"])
        d = max(1, int(rec["duration_s"] + 0.5))
        e = min(s + d, total_session_s)
        if e > s:
            rec["intensity_avg"] = float(I_arr[s:e].mean())
            rec["ess"] = float(ess_per_second[s:e].sum())

    # ----- Peak rolling I(t) (session-level) -----
    peak_5min = _peak_rolling_mean(I_arr, 300)
    peak_60s = _peak_rolling_mean(I_arr, 60)
    peak_20s = _peak_rolling_mean(I_arr, 20)

    anaerobic_strain, w_bal_curve, w_bal_trough = _compute_wbal(
        cp, w_prime, total_session_s, P_arr, kind_arr
    )

    # ----- Glycogen Used (session-level) -----
    # CHO depletion estimate over work-only seconds.  Computed once at
    # session scope; per-workout values are computed in
    # :func:`_attach_per_workout_records` from the workout-isolated slices.
    session_work_mask = kind_arr == "work"
    glycogen_kj_session, glycogen_used_session = session_glycogen_used(
        P_arr, I_arr, session_work_mask, mass_kg
    )

    # ----- Severity (session-level) -----
    # Severity = peak rolling I(t) over short windows + recovery-debt
    # contributions from both anaerobic strain (W' depletion) and glycogen
    # use.  The strain term rescues short max-efforts that don't sustain
    # long enough for the long-band EMAs to climb (e.g. a 2k PB peaks
    # ~1.1 in I but fully drains W'bal).  The glycogen term elevates
    # HM/marathon-distance efforts (low W' use, high CHO use) into the
    # Maximal bucket they deserve.
    severity_score = _calc_severity(
        peak_5min, peak_60s, peak_20s, anaerobic_strain, glycogen_used_session
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
        rws_list,
        inv_amp,
        kind_arr,
        wid_idx_arr,
        ess_per_second,
        mass_kg,
    )

    # ----- Session-level intensity -----
    # Same convention as per-workout intensity: arithmetic mean over
    # work-only seconds in the session.  Includes carryover state across
    # workouts.
    work_mask = kind_arr == "work"
    intensity_session = float(I_arr[work_mask].mean()) if work_mask.any() else 0.0

    if with_timeline:
        # Cumulative glycogen-used curve, sub-sampled inside
        # ``_build_session_timeline`` at the same stride as I and W'bal.
        # ``None`` when no profile mass — the chart line won't render.
        glycogen_curve = cumulative_glycogen_curve(
            P_arr, I_arr, session_work_mask, mass_kg
        )
        timeline = _build_session_timeline(
            total_session_s,
            zone_ratio_arr,
            w_bal_curve,
            w_prime,
            I_arr,
            P_arr,
            glycogen_curve=glycogen_curve,
        )
    else:
        timeline = None

    return {
        "ess": ess_total,
        "intensity_session": intensity_session,
        "peak_intensity_5min": peak_5min,
        "peak_intensity_60s": peak_60s,
        "peak_intensity_20s": peak_20s,
        "severity_score": severity_score,
        "severity_bucket": sev_bucket,
        "anaerobic_strain": anaerobic_strain,
        "glycogen_used": glycogen_used_session,
        "glycogen_kj": glycogen_kj_session,
        "w_bal_trough": w_bal_trough,
        "duration_s": float(total_session_s),
        "per_workout": per_workout_records,
        "per_segment": per_segment_records,
        "timeline": timeline,
    }
