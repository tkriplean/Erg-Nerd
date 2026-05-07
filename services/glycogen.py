"""
Glycogen Used — CHO depletion estimate as a session-level recovery-cost metric.

Direct sibling to W' Used (Skiba's anaerobic depletion).  Both report the
fraction of a finite physiological reservoir consumed by a session, for the
same downstream reason: telling the rower how much recovery debt they took
on.  W' Used captures the supra-CP work reservoir — drained by sprints and
2k-style efforts.  Glycogen Used captures the muscle + liver carbohydrate
reservoir — drained by long sustained efforts (HM, marathon, multi-hour
endurance) where W' barely moves but the rower is genuinely emptying out.

Together they cover the recovery-cost diagnosis:

    2k PB:    W' Used = 100 %, Glycogen Used =  5 %  → neuromuscular debt
    5k PB:    W' Used =  85 %, Glycogen Used = 20 %  → mixed
    HM PB:    W' Used =  25 %, Glycogen Used = 75 %  → fuel debt
    90' Z2:   W' Used =   5 %, Glycogen Used = 40 %  → mild fuel use
    4 h ultra-aerobic: Glycogen Used > 100 %         → bonk territory

Model
-----

The CHO oxidation rate as a fraction of total energy expenditure rises with
intensity (Brooks's "crossover concept"; Achten & Jeukendrup).  At rest /
low intensities CHO contributes ~30 % of fuel; at VO2max+ it contributes
~100 %.  Approximated as a linear ramp against the multi-band intensity
signal :math:`I(t)`:

    cho_fraction(I)        = clip(0.30 + 0.70 · I, 0.30, 1.00)
    metabolic_W            = P / GROSS_EFFICIENCY    (mech → metabolic)
    cho_burn_rate(t) [kJ/s] = (metabolic_W · cho_fraction(I)) / 1000
    session_kj             = ∫ cho_burn_rate(t) dt  over work-only seconds
    glycogen_used          = session_kj / (RESERVE_KJ_PER_KG · mass_kg)

Linear ramp is a deliberate v1 simplification; can upgrade to a power curve
(:math:`0.30 + 0.70 · I^{1.5}`) if real data reads off.

Constants
---------

``GROSS_EFFICIENCY = 0.25`` — mechanical / metabolic ratio for trained
rowers.  Lit range 0.22–0.27; fixed in v1.

``RESERVE_KJ_PER_KG = 80.0`` — *functional depletable* glycogen reserve
per kg body mass.  Trained athletes never deplete to zero (a non-zero
"floor" of glycogen remains even at the wall); the depletable fraction is
typically ~70–80 % of total stores.  Total stores ≈ 100–145 kJ/kg
(15–25 g/kg muscle × 17 kJ/g × ~30–40 % muscle fraction + 80–120 g liver
glycogen / mass), so depletable reserve ≈ 80 kJ/kg at the conservative
end.  Calibrated against real-world data points: a bonk-finishing
marathon PB reads ~95 %, the "wall" / bonk-onset feeling lands around
75 %, an HM PB finished hard but not bonked reads ~55 %, and a 4-hour
ultra-aerobic effort tips well over 100 % (bonk territory).  Affects all
readings linearly; the load-bearing tunable.

Severity contribution
---------------------

Glycogen depletion is itself a recovery-cost contributor — refilling
muscle glycogen takes 24–48 hr of carb intake, comparable wall-clock
recovery time to refilling W'.  The session-level severity formula in
:mod:`services.erg_stress` includes a ``+ 0.40 · glycogen_used`` term
alongside the existing ``+ 0.50 · anaerobic_strain``.  The 0.40 weight
is slightly lower than W's 0.50 because food refills glycogen faster
than rest refills W', and to dampen double-counting on workouts that
drain both.

No HyperDiv, no I/O.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mechanical / metabolic ratio for trained rowers.  Lit range 0.22–0.27.
GROSS_EFFICIENCY: float = 0.25

#: Functional depletable glycogen reserve per kg body mass (kJ/kg).
#: Trained athletes never deplete glycogen to zero — a non-zero floor
#: remains even at the wall.  Depletable fraction ≈ 70–80 % of total
#: stores; ~80 kJ/kg sits at the conservative end of that range.
#: Calibrated against real-world data: bonk-finishing marathon PB reads
#: ~95 %, bonk-onset (the "wall") ~75 %, HM PB hard-but-not-bonked
#: ~55 %, 4-hour ultra-aerobic > 100 %.  Affects all readings linearly.
RESERVE_KJ_PER_KG: float = 80.0

#: CHO fraction of energy expenditure at rest / low intensity (I → 0).
CHO_FRACTION_FLOOR: float = 0.30

#: Slope of the linear ramp from floor to ceiling against intensity I.
CHO_FRACTION_SLOPE: float = 0.70

#: Default body mass (kg) used when the rower's profile has no weight set.
DEFAULT_MASS_KG: float = 75.0


# ---------------------------------------------------------------------------
# Pure-function model
# ---------------------------------------------------------------------------


def cho_fraction(I_t):
    """Fraction of energy from CHO at intensity I.

    ``I_t`` may be a scalar or a numpy array.  Returns a value in
    ``[CHO_FRACTION_FLOOR, 1.0]``.  Uses a linear ramp ``0.30 + 0.70·I``
    clipped at the ceiling — a deliberate v1 simplification.
    """
    return np.clip(
        CHO_FRACTION_FLOOR + CHO_FRACTION_SLOPE * I_t,
        CHO_FRACTION_FLOOR,
        1.0,
    )


def cho_burn_rate_kj_s(P_t, I_t):
    """Per-second CHO oxidation rate (kJ/s) at instantaneous power P and
    intensity I.

    ``P_t`` is mechanical watts; ``I_t`` is the multi-band intensity signal
    from :mod:`services.erg_stress`.  Both may be scalars or numpy arrays.
    """
    metabolic_w = P_t / GROSS_EFFICIENCY
    return (metabolic_w * cho_fraction(I_t)) / 1000.0


def reserve_kj(mass_kg: float) -> float:
    """Estimated total glycogen reserve (kJ) for a rower of given mass."""
    return RESERVE_KJ_PER_KG * float(mass_kg)


def session_glycogen_used(
    P_arr: np.ndarray,
    I_arr: np.ndarray,
    work_mask: np.ndarray,
    mass_kg: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Integrate CHO burn rate over work-only seconds.

    Returns ``(glycogen_kj, glycogen_used_fraction)``.  ``glycogen_kj`` is
    the absolute kJ of CHO oxidised; ``glycogen_used_fraction`` is that
    value divided by the rower's estimated reserve (can exceed 1.0 — bonk
    territory).

    Returns ``(None, None)`` when ``mass_kg`` is None or ≤ 0 (no profile
    weight available).  Returns ``(0.0, 0.0)`` when there are no work
    seconds.

    ``P_arr`` and ``I_arr`` must be the same length (per-second timeline).
    ``work_mask`` is the boolean per-second mask of work-only seconds (rest
    seconds and gap seconds are excluded).
    """
    if mass_kg is None or mass_kg <= 0:
        return None, None
    if P_arr.size == 0 or not work_mask.any():
        return 0.0, 0.0

    cho_rate = cho_burn_rate_kj_s(P_arr, I_arr)
    cho_kj = float(cho_rate[work_mask].sum())
    used = cho_kj / reserve_kj(mass_kg)
    return cho_kj, used


def cumulative_glycogen_curve(
    P_arr: np.ndarray,
    I_arr: np.ndarray,
    work_mask: np.ndarray,
    mass_kg: Optional[float],
) -> Optional[np.ndarray]:
    """Per-second cumulative glycogen used as a fraction of reserve.

    Returns a length-N array where each element is the running total of
    fraction-of-reserve depleted by that second.  Monotonically
    non-decreasing — glycogen does not refill during a session.  Rest /
    gap seconds contribute zero to the running total but the timeline
    still has a value at each second (held flat).

    Returns ``None`` when ``mass_kg`` is None / ≤ 0 (no profile weight).

    Drives the Glycogen Used trace on the Effort & Stress chart.  Pass the
    *session-level* arrays to get a session-cumulative curve (a workout
    late in the session shows higher glycogen than one early — same
    convention as W'bal, which is also session-cumulative).
    """
    if mass_kg is None or mass_kg <= 0 or P_arr.size == 0:
        return None
    cho_rate = cho_burn_rate_kj_s(P_arr, I_arr)
    cho_rate_work_only = np.where(work_mask, cho_rate, 0.0)
    cumulative_kj = np.cumsum(cho_rate_work_only)
    return cumulative_kj / reserve_kj(mass_kg)


def workout_glycogen_used(
    P_w: np.ndarray,
    I_w: np.ndarray,
    kind_w: np.ndarray,
    mass_kg: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Per-workout CHO depletion estimate.

    Thin wrapper around :func:`session_glycogen_used` that builds the work
    mask from a per-workout ``kind_w`` array (values ``"work"`` / ``"rest"``).
    ``P_w`` and ``I_w`` are the workout-isolated per-second power and
    intensity arrays produced by the workout-isolated EMA pass in
    :mod:`services.erg_stress`.
    """
    if P_w.size == 0:
        if mass_kg is None or mass_kg <= 0:
            return None, None
        return 0.0, 0.0
    work_mask = kind_w == "work"
    return session_glycogen_used(P_w, I_w, work_mask, mass_kg)


def mass_kg_from_profile(profile: Optional[dict]) -> Optional[float]:
    """Extract body mass in kg from a rower's profile dict.

    Returns ``None`` when the profile is missing, has no weight, or weight
    is non-positive.  Handles ``weight_unit`` of ``"lbs"`` (converts to kg)
    or ``"kg"`` (default).  Mirrors the unit-handling pattern in
    :mod:`services.rowinglevel`.
    """
    if not profile:
        return None
    weight_raw = profile.get("weight")
    if not weight_raw or weight_raw <= 0:
        return None
    unit = (profile.get("weight_unit") or "kg").lower()
    if unit == "lbs":
        return float(weight_raw) * 0.453592
    return float(weight_raw)
