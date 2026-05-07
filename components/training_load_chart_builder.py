"""
build_training_load_chart_config — Python config builder for the
TrainingLoadChart Chart.js plugin.

Turns CTL / ATL / TSB time-series produced by
:func:`services.training_load.compute_ctl_atl_tsb` into a JSON-shaped config
dict the JS plugin consumes.

Visual design
-------------
* **CTL / ATL** are rendered as smooth lines on the primary y-axis, without
  point markers (the daily granularity makes individual-day points visually
  noisy and meaningless — what the user reads is the curve shape).
* **TSB** is rendered as a filled area between the line and zero on a
  secondary y-axis, color-coded by sign:
    - positive (recovered / fresh):  green tint
    - negative (productive / fatigue): red tint
  The fill makes the form trend immediately legible without needing the
  user to read tick labels.
* **Background bands** mark the TSB interpretation zones from
  TrainingPeaks PMC convention (Fresh / Recovered / Neutral / Productive /
  Risk).  Drawn behind the lines as low-opacity horizontal bands.
* **Date x-axis** uses Chart.js's built-in time scale; ticks formatted as
  "Mon YY" or "Mon DD" depending on zoom level.
* **Navigator strip** beneath the main chart shows the entire history at
  reduced height with a draggable brush window — same pattern as the
  Workouts page chart.  Drag handles on the left/right edges resize the
  window; drag inside pans.
"""

from __future__ import annotations

from datetime import date
from typing import Optional


#: TSB zone boundaries (TrainingPeaks PMC convention).  Sorted ascending.
#: Tuple of (lower_bound, upper_bound, label, css_color_var).
TSB_ZONES: list[tuple[float, float, str, tuple[int, int, int, float]]] = [
    (float("-inf"), -30.0, "High-risk fatigue",   (220,  80,  80, 0.10)),
    (-30.0,        -10.0, "Productive training", (220, 140,  80, 0.08)),
    (-10.0,         5.0,  "Neutral",             (160, 160, 160, 0.04)),
    (5.0,          25.0,  "Recovered",           (140, 200, 140, 0.07)),
    (25.0,         float("inf"), "Fresh / peak", (90,  180, 200, 0.10)),
]


def _date_to_ms(d: date) -> int:
    """Convert a Python ``date`` to a JavaScript-compatible Unix-ms timestamp."""
    from datetime import datetime
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


def build_training_load_chart_config(
    pmc: dict,
    *,
    is_dark: bool = False,
    initial_window_days: int = 180,
) -> Optional[dict]:
    """Build the chart config from a CTL/ATL/TSB rollup dict.

    Parameters
    ----------
    pmc:
        Output from :func:`services.training_load.compute_ctl_atl_tsb` —
        a dict with parallel ``dates`` / ``ctl`` / ``atl`` / ``tsb`` lists.
    is_dark:
        Theme flag for color selection.
    initial_window_days:
        How many days the brush window initially covers (default 6 months).

    Returns
    -------
    Config dict for the JS plugin, or ``None`` when the input is empty.
    """
    dates = pmc.get("dates") or []
    if not dates:
        return None

    # Convert dates to ms timestamps so the JS time scale works directly.
    ts = [_date_to_ms(d) for d in dates]

    # Theme-aware accents.
    ctl_color = "rgba(40,140,210,0.95)" if not is_dark else "rgba(120,180,240,0.95)"
    atl_color = "rgba(220,80,80,0.85)" if not is_dark else "rgba(255,140,140,0.85)"
    tsb_pos_fill = "rgba(80,180,120,0.30)" if not is_dark else "rgba(120,210,150,0.25)"
    tsb_neg_fill = "rgba(220,100,100,0.30)" if not is_dark else "rgba(245,145,145,0.25)"
    tsb_line_color = "rgba(80,80,80,0.70)" if not is_dark else "rgba(200,200,200,0.70)"

    grid_color = "rgba(0,0,0,0.06)" if not is_dark else "rgba(255,255,255,0.06)"
    tick_color = "#6b7280" if not is_dark else "#9ca3af"

    zone_band_alpha_mul = 1.5 if is_dark else 1.0
    tsb_zones = [
        {
            "from": float(z[0]) if z[0] != float("-inf") else None,
            "to":   float(z[1]) if z[1] != float("inf")  else None,
            "label": z[2],
            "color": (
                f"rgba({z[3][0]},{z[3][1]},{z[3][2]},"
                f"{min(0.25, z[3][3] * zone_band_alpha_mul):.3f})"
            ),
        }
        for z in TSB_ZONES
    ]

    # Initial brush window: last `initial_window_days` of the available range.
    if len(ts) > initial_window_days:
        brush_start_ms = ts[-initial_window_days]
    else:
        brush_start_ms = ts[0]
    brush_end_ms = ts[-1]

    return {
        "ctl_points":  [{"x": ts[i], "y": float(pmc["ctl"][i])} for i in range(len(ts))],
        "atl_points":  [{"x": ts[i], "y": float(pmc["atl"][i])} for i in range(len(ts))],
        "tsb_points":  [{"x": ts[i], "y": float(pmc["tsb"][i])} for i in range(len(ts))],
        "ctl_color":   ctl_color,
        "atl_color":   atl_color,
        "tsb_pos_fill": tsb_pos_fill,
        "tsb_neg_fill": tsb_neg_fill,
        "tsb_line_color": tsb_line_color,
        "grid_color":  grid_color,
        "tick_color":  tick_color,
        "tsb_zones":   tsb_zones,
        "brush_start_ms": brush_start_ms,
        "brush_end_ms":   brush_end_ms,
        "x_min_ms":    ts[0],
        "x_max_ms":    ts[-1],
        "is_dark":     bool(is_dark),
    }
