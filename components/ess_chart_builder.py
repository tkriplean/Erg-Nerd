"""
build_effort_stress_chart_config — Python config builder for EffortStressChart.

Turns a workout's session-aware ``_ess_session_summary`` + ``_ess_timeline``
fields (attached by :func:`components.add_metrics.add_metrics`) into a
JSON-shaped config dict that the Chart.js plugin in
``components/chart_assets/ess_chart_plugin.js`` consumes.

Under the v2 multi-band PDC-saturation model the chart has two y-axes:

* **Left (0 – ~1.0):** ``I(t)`` — the multi-band intensity signal, painted
  thick.  Severity bands sit beneath as low-opacity background fills.
* **Right (0 – ~1.5):** six thin colored lines, one per duration band,
  showing ``EMA_d(P)/RW(d)`` as a percentage.  Color-coded to match the
  app's six-zone power palette (1 Fast → red, 6 Slow Aerobic → light
  blue) so the band saturating most aligns with the rower's mental
  zone-by-color model.

The config dict has:

* ``timeline``  list of ``{t, intensity, w_bal_pct, zones}`` points,
                sub-sampled at the stride :func:`compute_session_metrics`
                already applied.  ``zones`` is keyed by the band's
                τ-in-seconds (matches ``services.erg_stress.ZONE_BANDS_S``).
* ``zone_specs`` list of ``{band_s, label, color, color_dark}`` — one per
                band, in the chart's render order.  Lets the JS lay out the
                six zone traces without hard-coding labels or colors.
* ``severity_bands``
                a list of horizontal bands ``{from, to, label, color}``
                painted as backgrounds on the I(t) axis to convey the
                Severity bucket cutoffs.
* ``workout_window`` ``{xMin, xMax}`` highlighting where the current
                workout sits within the session timeline.
* axis bounds and theme-aware colors.
"""

from __future__ import annotations

from typing import Optional

from services.erg_stress import (
    SEVERITY_STYLE,
    SEVERITY_THRESHOLDS,
    ZONE_BANDS_S,
)
from services.volume_bins import BAND_TO_BIN, BIN_COLORS, BIN_NAMES


# ---------------------------------------------------------------------------
# Zone-band → power-bin color mapping
# ---------------------------------------------------------------------------
# The six duration bands map naturally to the six rowing power bins
# (1 Fast → 6 Slow Aerobic) in inverse-duration order: shortest band = most
# anaerobic = matches the "Fast" bin.  This palette is shared with the
# Workouts / Volume page so a user already trained to read the colors finds
# them familiar here.
_BAND_TO_BIN: dict[int, int] = {
    20: 1,  # Fast       (red)
    90: 2,  # 2k         (orange)
    300: 3,  # 5k         (yellow)
    1200: 4,  # Threshold  (green)
    3600: 5,  # Fast Aero  (blue)
    7200: 6,  # Slow Aero  (light blue)
}


def _band_color(band_s: int, is_dark: bool) -> str:
    """Return an ``rgba(...)`` color for the given duration band."""
    bin_idx = _BAND_TO_BIN.get(band_s)
    if bin_idx is None:
        return "rgba(160,160,160,0.85)"
    dark, light = BIN_COLORS[bin_idx]
    return dark if is_dark else light


# Subtle severity band fills (lower opacity than the chip backgrounds, so they
# sit beneath the I(t) line without stealing attention).
def _severity_band_color(label: str) -> str:
    """Lower-opacity ``rgba(...)`` for a severity bucket's chart background."""
    style = SEVERITY_STYLE.get(label)
    if not style:
        return "rgba(160,160,160,0.10)"
    r, g, b, _ = style["bg"]
    return f"rgba({r},{g},{b},0.10)"


def _severity_bands(intensity_y_max: float) -> list[dict]:
    """Build the Severity background bands keyed off SEVERITY_THRESHOLDS.

    The top open-ended bucket is capped at ``intensity_y_max`` so the band
    always paints visibly without forcing the y-axis higher than needed.
    """
    bands: list[dict] = []
    prev = 0.0
    for label, upper in SEVERITY_THRESHOLDS:
        top = upper if upper != float("inf") else max(intensity_y_max, 1.2)
        bands.append(
            {
                "from": prev,
                "to": top,
                "label": label,
                "color": _severity_band_color(label),
            }
        )
        prev = upper if upper != float("inf") else prev
        if upper == float("inf"):
            break
    return bands


def build_effort_stress_chart_config(
    workout: dict,
    *,
    is_dark: bool = False,
) -> Optional[dict]:
    """Return the chart config or ``None`` if the workout lacks ESS data."""
    timeline = workout.get("_ess_timeline") or []
    summary = workout.get("_ess_session_summary") or {}
    if not timeline:
        return None

    # Workout window inside the session timeline.
    workout_window = None
    per_workout = summary.get("per_workout") or []
    wid = workout.get("id")
    for pw in per_workout:
        if pw.get("workout_id") == wid:
            workout_window = {
                "xMin": float(pw.get("t_start_s") or 0.0),
                "xMax": float(pw.get("t_end_s") or 0.0),
            }
            break

    # Per-workout markers for multi-workout sessions: a vertical line at
    # each workout's start, labelled "W1", "W2", etc., so the rower can
    # locate where each workout begins in the session timeline.  The
    # current workout is flagged so the JS can render it more boldly.
    workout_markers: list[dict] = []
    if len(per_workout) > 1:
        for i, pw in enumerate(per_workout):
            workout_markers.append(
                {
                    "t_start_s": float(pw.get("t_start_s") or 0.0),
                    "t_end_s": float(pw.get("t_end_s") or 0.0),
                    "label": f"W{i + 1}",
                    "is_current": pw.get("workout_id") == wid,
                }
            )

    # I(t) axis bound.  In practice peak I sits in [0.4, 1.0]; pad a bit.
    intensity_max = (
        max((p.get("intensity") or 0.0) for p in timeline) if timeline else 1.0
    )
    intensity_y_max = max(1.0, intensity_max + 0.15)

    # Zone-ratio axis bound.  Ratios > 1.0 happen for PB efforts (athlete
    # exceeded their existing reference); cap at the observed max + headroom.
    # Glycogen Used can also exceed 1.0 (bonk territory) and shares this
    # axis in percentage form, so include it in the cap calculation.
    zone_max = 0.0
    for p in timeline:
        zones = p.get("zones") or {}
        for v in zones.values():
            if v is not None and v > zone_max:
                zone_max = v
        gly = p.get("glycogen_pct")
        if gly is not None and (gly / 100.0) > zone_max:
            zone_max = gly / 100.0
    zone_y_max = max(1.2, zone_max + 0.15)

    # Theme-aware accents.
    intensity_color = (
        "rgba(220,80,80,0.95)" if not is_dark else "rgba(255,140,140,0.95)"
    )
    workout_band_color = (
        "rgba(120,120,120,0.10)" if not is_dark else "rgba(200,200,200,0.08)"
    )

    # Per-band specs in render order (shortest → longest band).
    # Labels use the rower-facing stimulus bin name (Sprint, Anaerobic,
    # VO2max, Threshold, Tempo, Endurance) rather than the duration so
    # the legend reads as a physiological story.
    zone_specs = [
        {
            "band_s": d,
            "label": BIN_NAMES[BAND_TO_BIN[d]],
            "color": _band_color(d, is_dark=is_dark),
        }
        for d in ZONE_BANDS_S
    ]

    # Glycogen Used curve: present only when the rower's profile has
    # weight set (otherwise the cumulative curve was never computed).
    has_glycogen = any(p.get("glycogen_pct") is not None for p in timeline)
    glycogen_color = (
        "rgba(225,125,35,0.95)" if not is_dark else "rgba(245,165,75,0.95)"
    )  # orange — same family as the Anaerobic bin to flag "fuel store"

    return {
        "has_w_bal": True,
        "has_glycogen": has_glycogen,
        "timeline": [
            {
                "t": float(p.get("t") or 0),
                "intensity": float(p.get("intensity") or 0),
                "w_bal_pct": (
                    float(p["w_bal_pct"]) * 100.0
                    if p.get("w_bal_pct") is not None
                    else None
                ),
                # Cumulative glycogen used as a percentage (0 → 100+).
                # Plotted alongside W'bal — both are session-cumulative
                # reservoir-state lines, but inverse: W'bal trends down
                # with depletion, glycogen up.
                "glycogen_pct": (
                    float(p["glycogen_pct"]) * 100.0
                    if p.get("glycogen_pct") is not None
                    else None
                ),
                # Pass through the per-band ratios as a flat dict keyed by
                # the band-seconds string (JS-safe key).
                "zones": {
                    str(d): float((p.get("zones") or {}).get(d, 0.0))
                    for d in ZONE_BANDS_S
                },
            }
            for p in timeline
        ],
        "zone_specs": zone_specs,
        "severity_bands": _severity_bands(intensity_y_max),
        "workout_window": workout_window,
        "workout_markers": workout_markers,
        "workout_band_color": workout_band_color,
        "intensity_y_max": intensity_y_max,
        "zone_y_max": zone_y_max,
        "intensity_color": intensity_color,
        "glycogen_color": glycogen_color,
        "is_dark": bool(is_dark),
        "session_duration_s": float(summary.get("duration_s") or 0.0),
    }
