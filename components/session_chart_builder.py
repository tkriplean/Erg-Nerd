"""
session_chart_builder.py — Chart.js config builder for stroke-by-stroke data.

Exported:
    build_stroke_chart_config(strokes, workout, *, metric, focused_interval_idx,
                               is_dark) -> dict

The returned dict is passed directly to StrokeChart(config=...).

Stroke data format (from Concept2 API /users/{u}/results/{id}/strokes):
    t   — elapsed time in tenths of a second (resets at each interval)
    d   — elapsed distance in decimeters
    p   — pace in tenths-of-a-second per 500m  (divide by 10 for sec/500m)
    spm — strokes per minute (integer)
    hr  — heart rate bpm (0 when no HR monitor worn)

For interval workouts the API resets t to 0 at the start of each interval.
_stitch_interval_times() detects backwards jumps and accumulates an offset so
the resulting t values are monotonically increasing across the whole session.
"""

from __future__ import annotations

from typing import Optional

from services.rowing_utils import compute_watts, INTERVAL_WORKOUT_TYPES


# ---------------------------------------------------------------------------
# Time stitching
# ---------------------------------------------------------------------------


def _stitch_interval_times(strokes: list) -> list:
    """
    Return a copy of strokes with t values made monotonically increasing.

    When the Concept2 API returns interval workouts, t resets to 0 at the
    start of each interval (work and rest).  This function detects the reset
    by checking whether t drops significantly from the previous stroke, then
    accumulates an offset so all strokes share a single continuous timeline.
    """
    if not strokes:
        return strokes
    result = []
    offset = 0
    prev_t = 0
    for i, s in enumerate(strokes):
        t = s.get("t", 0)
        if i > 0 and t < prev_t:
            offset += prev_t
        prev_t = t
        stitched = dict(s)
        stitched["t"] = t + offset
        result.append(stitched)
    return result


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_stroke_chart_config(
    strokes: list,
    workout: dict,
    *,
    metric: str = "pace",       # "pace" | "watts"
    focused_interval_idx: Optional[int] = None,
    is_dark: bool = False,
) -> dict:
    """
    Return a Chart.js config dict for the stroke time-series.

    Parameters
    ----------
    strokes : list of stroke dicts (t, d, p, spm, hr)
    workout : top-level workout dict (used for interval band generation)
    metric  : "pace" to show pace on primary Y, "watts" for power
    focused_interval_idx : when set, x-axis is clamped to that band's range
    is_dark : apply dark-mode palette
    """
    if not strokes:
        return {}

    # Stitch t values for interval workouts (each interval resets t to 0).
    strokes = _stitch_interval_times(strokes)

    show_watts = metric == "watts"

    # ── Build datasets ───────────────────────────────────────────────────────

    pace_pts: list = []
    spm_pts:  list = []
    hr_pts:   list = []
    has_hr = False
    spm_max = 0

    for s in strokes:
        t_s = (s.get("t") or 0) / 10.0
        p_tenths = s.get("p")
        spm_val  = s.get("spm")
        hr_val   = s.get("hr")

        if p_tenths and p_tenths > 0:
            pace_sec = p_tenths / 10.0
            y_val = round(compute_watts(pace_sec), 1) if show_watts else round(pace_sec, 2)
            pace_pts.append({"x": round(t_s, 2), "y": y_val})

        if spm_val is not None:
            spm_pts.append({"x": round(t_s, 2), "y": spm_val})
            if spm_val > spm_max:
                spm_max = spm_val

        if hr_val:
            has_hr = True
            hr_pts.append({"x": round(t_s, 2), "y": hr_val})

    # Colour palette
    pace_color = "#f59e0b"
    spm_color  = "#d97706" if is_dark else "#b45309"
    hr_color   = "#f87171" if is_dark else "#dc2626"

    datasets = [
        {
            "label": "Watts" if show_watts else "Pace",
            "data": pace_pts,
            "yAxisID": "y",
            "borderColor": pace_color,
            "backgroundColor": "transparent",
            "borderWidth": 1.5,
            "pointRadius": 0,
            "tension": 0.15,
            "order": 1,
        },
    ]

    if spm_pts:
        datasets.append({
            "label": "SPM",
            "data": spm_pts,
            "yAxisID": "yspm",
            "borderColor": spm_color,
            "backgroundColor": "transparent",
            "borderWidth": 1,
            "borderDash": [4, 3],
            "pointRadius": 0,
            "tension": 0.1,
            "order": 2,
        })

    if has_hr and hr_pts:
        datasets.append({
            "label": "HR",
            "data": hr_pts,
            "yAxisID": "yhr",
            "borderColor": hr_color,
            "backgroundColor": "transparent",
            "borderWidth": 1,
            "borderDash": [2, 4],
            "pointRadius": 0,
            "tension": 0.1,
            "order": 3,
        })

    # ── Bands ────────────────────────────────────────────────────────────────

    bands = _build_bands(workout, strokes)

    # ── x-axis zoom ──────────────────────────────────────────────────────────

    x_min = None
    x_max = None
    if focused_interval_idx is not None and 0 <= focused_interval_idx < len(bands):
        b = bands[focused_interval_idx]
        x_min = b["xMin"]
        x_max = b["xMax"]

    return {
        "datasets": datasets,
        "bands": bands,
        "showWatts": show_watts,
        "hasHr": has_hr,
        "spmMax": spm_max,
        "xMin": x_min,
        "xMax": x_max,
        "isDark": is_dark,
    }


# ---------------------------------------------------------------------------
# Band generation
# ---------------------------------------------------------------------------


def _build_bands(workout: dict, strokes: list) -> list:
    """Return annotation band dicts. Each band: {idx, xMin, xMax, label, work}"""
    wo = workout.get("workout") or {}
    intervals = wo.get("intervals")
    splits = wo.get("splits")
    wtype = workout.get("workout_type", "")

    if intervals and wtype in INTERVAL_WORKOUT_TYPES:
        return _bands_from_intervals(intervals)
    elif splits:
        return _bands_from_splits(splits)
    return []


def _bands_from_intervals(intervals: list) -> list:
    """
    Build bands from the workout's interval list using cumulative durations.

    Each interval (work and rest) with a non-zero time becomes a band.  The
    x positions are accumulated from interval time fields (tenths → seconds),
    which directly matches the stitched stroke timeline produced by
    _stitch_interval_times() since both accumulate the same durations.
    """
    bands = []
    elapsed_s = 0.0
    work_idx = 0
    for iv in intervals:
        dur_s = (iv.get("time") or 0) / 10.0
        if dur_s <= 0:
            continue
        is_work = (iv.get("type") or "work") != "rest"
        label = f"#{work_idx + 1}" if is_work else ""
        bands.append({
            "idx": len(bands),
            "xMin": round(elapsed_s, 2),
            "xMax": round(elapsed_s + dur_s, 2),
            "label": label,
            "work": is_work,
        })
        elapsed_s += dur_s
        if is_work:
            work_idx += 1
    return bands


def _bands_from_splits(splits: list) -> list:
    """Build bands from splits (500m splits for steady-state rows)."""
    bands = []
    elapsed_s = 0.0
    for i, sp in enumerate(splits):
        dur_s = (sp.get("time") or 0) / 10.0
        if dur_s <= 0:
            continue
        dist_m = sp.get("distance") or 0
        label = f"{dist_m}m" if dist_m else f"Split {i + 1}"
        bands.append({
            "idx": i,
            "xMin": round(elapsed_s, 2),
            "xMax": round(elapsed_s + dur_s, 2),
            "label": label,
            "work": True,
        })
        elapsed_s += dur_s
    return bands
