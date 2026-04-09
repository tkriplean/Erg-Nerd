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


def _stitch_interval_times(
    strokes: list,
    intervals: Optional[list] = None,
) -> list:
    """
    Return a copy of strokes with t values made monotonically increasing.

    The Concept2 API resets t to 0 at the start of each work interval.
    t does NOT reset separately for rest periods — rest strokes (if any)
    continue counting up from where the work strokes left off, and the
    next reset happens only when the following work interval begins.
    This means there is exactly one backward jump per interval boundary.

    At each jump we know which interval just ended, so we advance the offset
    by the full canonical duration of that interval (work time + rest time)
    rather than by prev_t.  This is necessary because the last stroke before
    a boundary may arrive several tenths before the interval actually ends,
    and accumulating prev_t would compress the chart timeline by that gap on
    every boundary.

    Falls back to accumulating prev_t if interval metadata is absent or
    exhausted.
    """
    if not strokes:
        return strokes

    result = []
    offset = 0
    prev_t = 0
    interval_idx = 0  # index of the interval that just ended at each jump

    for i, s in enumerate(strokes):
        t = s.get("t", 0)
        if i > 0 and t < prev_t:
            if intervals and interval_idx < len(intervals):
                iv = intervals[interval_idx]
                offset += (iv.get("time") or 0) + (iv.get("rest_time") or 0)
            else:
                offset += prev_t  # fallback
            interval_idx += 1
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
    metric: str = "pace",  # "pace" | "watts"
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

    # Stitch t values so all strokes share a continuous timeline.
    # Pass the interval list so segment boundaries are snapped to exact
    # durations rather than inferred from the last observed stroke t.
    wo = workout.get("workout") or {}
    wtype = workout.get("workout_type", "")
    intervals = (
        wo.get("intervals")
        if wtype in INTERVAL_WORKOUT_TYPES
        else None
    )
    strokes = _stitch_interval_times(strokes, intervals=intervals)

    show_watts = metric == "watts"

    # ── Build datasets ───────────────────────────────────────────────────────

    pace_pts: list = []
    spm_pts: list = []
    hr_pts: list = []
    has_hr = False
    spm_max = 0

    for s in strokes:
        t_s = (s.get("t") or 0) / 10.0
        p_tenths = s.get("p")
        spm_val = s.get("spm")
        hr_val = s.get("hr")

        if p_tenths and p_tenths > 0:
            pace_sec = p_tenths / 10.0
            y_val = (
                round(compute_watts(pace_sec), 1) if show_watts else round(pace_sec, 2)
            )
            pace_pts.append({"x": round(t_s, 2), "y": y_val})

        if spm_val is not None:
            spm_pts.append({"x": round(t_s, 2), "y": spm_val})
            if spm_val > spm_max:
                spm_max = spm_val

        if hr_val:
            has_hr = True
            hr_pts.append({"x": round(t_s, 2), "y": hr_val})

    # Colour palette — all solid lines
    pace_color = "#60a5fa"  # light blue (pace/watts, thicker)
    spm_color = "#1e40af"  # dark blue  (stroke rate)
    hr_color = "#ef4444"  # red        (heart rate)

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
        datasets.append(
            {
                "label": "SPM",
                "data": spm_pts,
                "yAxisID": "yspm",
                "borderColor": spm_color,
                "backgroundColor": "transparent",
                "borderWidth": 1,
                "pointRadius": 0,
                "tension": 0.1,
                "order": 2,
            }
        )

    if has_hr and hr_pts:
        datasets.append(
            {
                "label": "HR",
                "data": hr_pts,
                "yAxisID": "yhr",
                "borderColor": hr_color,
                "backgroundColor": "transparent",
                "borderWidth": 1,
                "pointRadius": 0,
                "tension": 0.1,
                "order": 3,
            }
        )

    # ── Bands ────────────────────────────────────────────────────────────────

    bands = _build_bands(workout, strokes)

    # ── x-axis zoom ──────────────────────────────────────────────────────────

    x_min = None
    x_max = None
    if focused_interval_idx is not None and 0 <= focused_interval_idx < len(bands):
        b = bands[focused_interval_idx]
        x_min = b["xMin"]
        x_max = b["xMax"]
    else:
        # For non-interval workouts cap the x-axis at the recorded session
        # duration so trailing noise/GPS drift beyond the finish doesn't expand
        # the chart domain uselessly.
        wtype = workout.get("workout_type", "")
        if wtype not in INTERVAL_WORKOUT_TYPES:
            session_time_s = (workout.get("time") or 0) / 10.0
            if session_time_s > 0:
                x_max = round(session_time_s, 2)

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

    Each entry in the intervals list is a work interval.  A rest band is
    synthesised immediately after whenever the work interval carries a
    non-zero rest_time field.

    Mirrors the row-building logic in _intervals_table exactly so that
    band index i always corresponds to table row i (required for
    click-to-focus to zoom the correct chart region).
    """
    bands = []
    elapsed_s = 0.0
    for work_idx, iv in enumerate(intervals):
        dur_s = (iv.get("time") or 0) / 10.0
        if dur_s <= 0:
            continue

        # Work band
        bands.append(
            {
                "idx": len(bands),
                "xMin": round(elapsed_s, 2),
                "xMax": round(elapsed_s + dur_s, 2),
                "label": f"#{work_idx + 1}",
                "work": True,
            }
        )
        elapsed_s += dur_s

        # Rest band (optional — only present when rest_time is set)
        rest_dur_s = (iv.get("rest_time") or 0) / 10.0
        if rest_dur_s > 0:
            bands.append(
                {
                    "idx": len(bands),
                    "xMin": round(elapsed_s, 2),
                    "xMax": round(elapsed_s + rest_dur_s, 2),
                    "label": "",
                    "work": False,
                }
            )
            elapsed_s += rest_dur_s
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
        bands.append(
            {
                "idx": i,
                "xMin": round(elapsed_s, 2),
                "xMax": round(elapsed_s + dur_s, 2),
                "label": label,
                "work": True,
            }
        )
        elapsed_s += dur_s
    return bands
