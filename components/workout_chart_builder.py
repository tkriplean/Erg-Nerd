"""
workout_chart_builder.py — Chart.js config builder for stroke-by-stroke data.

Exported:
    build_stroke_chart_config(strokes, workout, *, metric, focused_interval_idx,
                               is_dark, stack, show_pace, show_spm, show_hr) -> dict
    build_interval_rows_and_bands(intervals) -> (rows, bands)

The config dict is passed directly to StrokeChart(config=...).

Stroke data format (from Concept2 API /users/{u}/results/{id}/strokes):
    t   — elapsed time in tenths of a second (resets at each interval)
    d   — elapsed distance in decimeters
    p   — pace in tenths-of-a-second per 500m  (divide by 10 for sec/500m)
    spm — strokes per minute (integer)
    hr  — heart rate bpm (0 when no HR monitor worn)

For interval workouts the API resets t to 0 at the start of each interval.
_stitch_interval_times() detects backwards jumps and accumulates an offset so
the resulting t values are monotonically increasing across the whole workout.
"""

from __future__ import annotations

import math
from typing import Optional

from services.rowing_utils import compute_watts, INTERVAL_WORKOUT_TYPES
from services.stroke_utils import ensure_raw_stroke_origin
from services.formatters import fmt_distance


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

    Anomalous device-emitted strokes with small backward-t values are removed
    upstream by concept2.get_strokes(), so every backward jump seen here is a
    genuine section reset.
    """
    if not strokes:
        return strokes

    # Ensure the first sample sits at (t=0, d=0) so the chart x-axis starts
    # at the catch rather than at the first recorded stroke (which the PM5
    # sometimes emits 1-2 seconds into a short piece).
    strokes = ensure_raw_stroke_origin(strokes)

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
# Shared interval utilities
# ---------------------------------------------------------------------------


def _iter_valid_intervals(intervals: list):
    """Yield (work_idx, iv) for each interval with positive duration."""
    for work_idx, iv in enumerate(intervals):
        if (iv.get("time") or 0) > 0:
            yield work_idx, iv


def _time_weighted_hr(
    stroke_t_hr: list[tuple[float, float]], xMin: float, xMax: float
) -> Optional[float]:
    """Time-weighted HR average across strokes whose elapsed t falls in window.

    Each stroke contributes its HR weighted by the gap to the next stroke
    inside the window (the trailing stroke uses the gap to xMax).  Returns
    None unless at least 3 stroke samples land in the window.
    """
    in_win = [(t, hr) for t, hr in stroke_t_hr if xMin <= t <= xMax]
    if len(in_win) < 3:
        return None
    weighted_sum = 0.0
    weight_total = 0.0
    for i, (t, hr) in enumerate(in_win):
        if i + 1 < len(in_win):
            w = in_win[i + 1][0] - t
        else:
            w = max(0.0, xMax - t)
        if w <= 0:
            w = 1.0  # avoid zero-weight collapse on coincident samples
        weighted_sum += hr * w
        weight_total += w
    if weight_total <= 0:
        return None
    return weighted_sum / weight_total


def build_interval_rows_and_bands(
    intervals: list, strokes: Optional[list] = None
) -> tuple:
    """
    Single source of truth for the interval → (rows, bands) transformation.

    Both the intervals table (workout_page.py) and band generation must
    iterate intervals in exactly the same order so that band index i always
    matches table row i for click-to-focus.  This function guarantees that by
    computing both simultaneously from the same iteration.

    When ``strokes`` is provided, per-work-interval ``hr_avg`` is recomputed
    from the strokes whose elapsed time falls within that interval's
    [xMin, xMax] window using a time-weighted average (each stroke weighted
    by its inter-stroke duration).  This corrects cases where the C2 API's
    ``heart_rate.average`` field is unreliable.  Falls back to the API value
    when fewer than 3 stroke samples land in the window.

    Returns
    -------
    rows : list of dicts for _table_frame() / _interval_row() in workout_page
        Keys: _is_rest, _work_idx, time, distance, pace_tenths, avg_watts,
              spm, hr_avg  (work rows only);  _is_rest, time, distance,
              pace_tenths  (rest rows).
    bands : list of band dicts for chart annotations
        Keys: idx, xMin, xMax, label, work (bool).
    """
    rows: list = []
    bands: list = []
    elapsed_s = 0.0

    # Pre-extract (t_seconds, hr) pairs for time-weighted HR aggregation.
    stroke_t_hr: list[tuple[float, float]] = []
    if strokes:
        for s in strokes:
            t_s = (s.get("t") or 0) / 10.0
            hr = s.get("hr")
            if hr:
                stroke_t_hr.append((t_s, hr))
        stroke_t_hr.sort(key=lambda x: x[0])

    for work_idx, iv in _iter_valid_intervals(intervals):
        t = iv.get("time") or 0
        dur_s = t / 10.0
        d = iv.get("distance") or 0
        pace_t = (t * 500 / d) if d else None
        hr = (iv.get("heart_rate") or {}).get("average")

        if stroke_t_hr:
            xMin = elapsed_s
            xMax = elapsed_s + dur_s
            recomputed = _time_weighted_hr(stroke_t_hr, xMin, xMax)
            if recomputed is not None:
                hr = recomputed

        # ── Work row ────────────────────────────────────────────────────────
        rows.append(
            {
                "_is_rest": False,
                "_work_idx": work_idx,
                "time": t,
                "distance": d,
                "pace_tenths": pace_t,
                "avg_watts": round(compute_watts(pace_t / 10.0)) if pace_t else None,
                "spm": iv.get("stroke_rate"),
                "hr_avg": hr,
            }
        )

        # ── Work band ───────────────────────────────────────────────────────
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

        # ── Rest row + band (optional) ──────────────────────────────────────
        rest_t = iv.get("rest_time") or 0
        if rest_t > 0:
            rest_d = iv.get("rest_distance") or 0
            rest_pace_t = (rest_t * 500 / rest_d) if rest_d else None
            rest_dur_s = rest_t / 10.0

            rows.append(
                {
                    "_is_rest": True,
                    "time": rest_t,
                    "distance": rest_d,
                    "pace_tenths": rest_pace_t,
                }
            )
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

    return rows, bands


# ---------------------------------------------------------------------------
# Chart utilities
# ---------------------------------------------------------------------------


def _interval_colors(n: int) -> list:
    """Generate n visually distinct HSL colors spanning blue → orange."""
    if n == 0:
        return []
    if n == 1:
        return ["hsl(220, 75%, 55%)"]
    return [f"hsl({round(220 - i * 190 / (n - 1))}, 75%, 55%)" for i in range(n)]


def _slow_end_cap(
    points: list,
    bands: list,
    *,
    show_watts: bool,
    workout: dict,
    compare_series: Optional[list] = None,
) -> Optional[float]:
    """Return the y-value that caps the slow end of the pace/watts axis.

    Slow end = the side of the axis representing slower rowing —
    higher seconds/500m for pace, lower watts for power.  Without the cap,
    a single slow catch stroke can stretch the axis so far that the actual
    race line looks compressed into a 15% band.

    Cap = (slowest band/workout average) ± 10%.  The caller uses this as
    a ceiling vs. the observed slow-end value and picks the *faster* of
    the two (lower pace, higher watts), i.e. the tighter range.

    "Band/workout average" is derived in this order:
      1. Mean of ``points`` inside each work band, taking the max-pace /
         min-watts band (the slowest work interval).
      2. If there are no bands, the workout-wide pace average from
         ``workout['time'] / workout['distance']``.
      3. Each compare series contributes its own full-series mean.

    Returns None when no usable average can be computed.
    """
    work_bands = [b for b in (bands or []) if b.get("work")]
    avgs: list = []

    if work_bands:
        for b in work_bands:
            lo, hi = b["xMin"], b["xMax"]
            in_band = [
                p["y"] for p in points if p["y"] is not None and lo <= p["x"] <= hi
            ]
            if in_band:
                avgs.append(sum(in_band) / len(in_band))
    else:
        t = (workout.get("time") or 0) / 10.0
        d = workout.get("distance") or 0
        if t > 0 and d > 0:
            pace_s = t * 500 / d
            avgs.append(compute_watts(pace_s) if show_watts else pace_s)

    for cs in compare_series or []:
        ys = [p["y"] for p in (cs.get("pace_points") or []) if p["y"] is not None]
        if ys:
            avgs.append(sum(ys) / len(ys))

    if not avgs:
        return None
    slowest = min(avgs) if show_watts else max(avgs)
    return slowest * (0.90 if show_watts else 1.10)


def _pad(lo, hi, frac=0.05, min_pad=0, lo_floor=None, round_to_int=False):
    """Expand [lo, hi] by frac of the span on each side.

    lo_floor     — if set, clamps the lower bound to at least this value.
    round_to_int — if True, floors lo and ceils hi to the nearest integer.
    """
    if lo is None or hi is None:
        return lo, hi
    pad = max((hi - lo) * frac, min_pad)
    lo_out, hi_out = lo - pad, hi + pad
    if lo_floor is not None:
        lo_out = max(lo_out, lo_floor)
    if round_to_int:
        lo_out = math.floor(lo_out)
        hi_out = math.ceil(hi_out)
    return lo_out, hi_out


# ---------------------------------------------------------------------------
# Band generation
# ---------------------------------------------------------------------------


def _build_bands(
    wo: dict,
    wtype: str,
    *,
    custom_splits: Optional[dict] = None,
    strokes: Optional[list] = None,
    workout: Optional[dict] = None,
) -> list:
    """Return annotation band dicts. Each band: {idx, xMin, xMax, label, work}.

    When `custom_splits` is provided for a non-interval workout, bands are
    derived by interpolating the stroke stream at each cumulative boundary.
    For distance-unit splits (meters), boundaries map to elapsed time via
    `interp_time`.  For time-unit splits (seconds), boundaries are the
    cumulative time values themselves.
    """
    intervals = wo.get("intervals")
    splits = wo.get("splits")

    if intervals and wtype in INTERVAL_WORKOUT_TYPES:
        _, bands = build_interval_rows_and_bands(intervals, strokes)
        return bands

    if custom_splits and strokes and workout is not None:
        return _bands_from_custom_splits(custom_splits, strokes, workout)

    if splits:
        return _bands_from_splits(splits)
    return []


def _bands_from_custom_splits(
    custom_splits: dict, strokes: list, workout: dict
) -> list:
    """Convert custom split boundaries to elapsed-time bands."""
    # Local import to avoid a circular dependency with components.workout_page.
    from components.workout_page import _build_interp, _format_mmss

    values = custom_splits.get("values") or []
    if not values:
        return []
    unit = custom_splits.get("unit", "m")

    interp_time, _ = _build_interp(strokes, workout)
    if interp_time is None:
        return []

    bands: list = []
    if unit == "s":
        cumulative_tenths = 0.0
        for i, dur_s in enumerate(values):
            start_s = cumulative_tenths / 10.0
            end_s = (cumulative_tenths + dur_s * 10) / 10.0
            bands.append(
                {
                    "idx": i,
                    "xMin": round(start_s, 2),
                    "xMax": round(end_s, 2),
                    "label": _format_mmss(dur_s),
                    "work": True,
                }
            )
            cumulative_tenths += dur_s * 10
        return bands

    # unit == "m"
    cumulative = 0.0
    for i, dist_m in enumerate(values):
        start_t = interp_time(cumulative) or 0.0
        end_t = interp_time(cumulative + dist_m) or 0.0
        bands.append(
            {
                "idx": i,
                "xMin": round(start_t / 10.0, 2),
                "xMax": round(end_t / 10.0, 2),
                "label": f"{dist_m}m",
                "work": True,
            }
        )
        cumulative += dist_m
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


# ---------------------------------------------------------------------------
# Stacked mode builder
# ---------------------------------------------------------------------------


def _build_stacked_config(
    strokes: list,
    work_bands: list,
    *,
    show_watts: bool,
    show_pace: bool,
    show_spm: bool,
    show_hr: bool,
    has_hr: bool,
    pace_y_min,
    pace_y_max,
    pace_y_min_full,
    pace_y_max_full,
    spm_y_min,
    spm_y_max,
    pace_color: str,
    spm_color: str,
    hr_color: str,
    is_dark: bool,
    smoothen: bool,
) -> dict:
    """
    Build the stacked-intervals config dict.

    Each work band is overlaid on a shared x-axis starting at t=0.
    Returns a config dict with stack=True and a stackedIntervals list,
    one entry per work band.
    """
    colors = _interval_colors(len(work_bands))
    stacked_intervals = []

    for idx, band in enumerate(work_bands):
        x_min_s, x_max_s = band["xMin"], band["xMax"]
        pace_p: list = []
        spm_p: list = []
        hr_p: list = []

        for s in strokes:
            t_s = (s.get("t") or 0) / 10.0
            if t_s < x_min_s or t_s > x_max_s:
                continue
            x = round(t_s - x_min_s, 2)

            p = s.get("p")
            if p and p > 0:
                pace_sec = p / 10.0
                y_val = (
                    round(compute_watts(pace_sec), 1)
                    if show_watts
                    else round(pace_sec, 2)
                )
                pace_p.append({"x": x, "y": y_val})

            spm_v = s.get("spm")
            if spm_v is not None:
                spm_p.append({"x": x, "y": spm_v})

            hr_v = s.get("hr")
            if hr_v:
                hr_p.append({"x": x, "y": hr_v})

        stacked_intervals.append(
            {
                "label": band.get("label", f"#{idx + 1}"),
                "color": colors[idx],
                "pacePoints": pace_p,
                "spmPoints": spm_p,
                "hrPoints": hr_p,
            }
        )

    return {
        "stack": True,
        "stackedIntervals": stacked_intervals,
        "showWatts": show_watts,
        "showPace": show_pace,
        "showSpm": show_spm,
        "showHr": show_hr and has_hr,
        "hasHr": has_hr,
        "paceYMin": pace_y_min,
        "paceYMax": pace_y_max,
        "paceYMinFull": pace_y_min_full,
        "paceYMaxFull": pace_y_max_full,
        "spmYMin": spm_y_min,
        "spmYMax": spm_y_max,
        "paceColor": pace_color,
        "spmColor": spm_color,
        "hrColor": hr_color,
        "isDark": is_dark,
        "smoothen": smoothen,
    }


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def _points_from_strokes(strokes: list, *, show_watts: bool) -> tuple:
    """Convert already-stitched strokes into (pace_pts, spm_pts, hr_pts, has_hr).

    Pace points carry watts when `show_watts` is True; otherwise pace in
    sec/500m.  Used for both the primary series and compare overlays.
    """
    pace_pts: list = []
    spm_pts: list = []
    hr_pts: list = []
    has_hr = False
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

        if hr_val:
            has_hr = True
            hr_pts.append({"x": round(t_s, 2), "y": hr_val})
    return pace_pts, spm_pts, hr_pts, has_hr


def _rest_spans_from_intervals(intervals: Optional[list]) -> list:
    """Return [(xMin, xMax), ...] for rest periods given an interval list.

    Mirrors the elapsed-time accounting in build_interval_rows_and_bands so
    spans line up with the rest bands annotated on the chart.
    """
    if not intervals:
        return []
    spans: list = []
    elapsed_s = 0.0
    for iv in intervals:
        dur_t = iv.get("time") or 0
        if dur_t <= 0:
            continue
        elapsed_s += dur_t / 10.0
        rest_t = iv.get("rest_time") or 0
        if rest_t > 0:
            rest_dur_s = rest_t / 10.0
            spans.append((round(elapsed_s, 2), round(elapsed_s + rest_dur_s, 2)))
            elapsed_s += rest_dur_s
    return spans


def _apply_rest_gaps(points: list, rest_spans: list) -> list:
    """Drop points falling inside rest spans and insert ``y: None`` gap
    markers at each rest boundary so Chart.js (with ``spanGaps: false``)
    breaks the line through the rest interval rather than connecting the
    last work stroke to the first stroke after.
    """
    if not rest_spans or not points:
        return points
    kept = [p for p in points if not any(lo <= p["x"] <= hi for lo, hi in rest_spans)]
    nulls = [{"x": lo, "y": None} for lo, _ in rest_spans]
    return sorted(kept + nulls, key=lambda p: p["x"])


def build_compare_series(
    compared_ids: tuple,
    compare_results: dict,
    workouts_dict: dict,
    *,
    show_watts: bool,
    colors: Optional[list] = None,
    labels: Optional[dict] = None,
) -> list:
    """Turn per-id stroke result dicts into the compare_series list consumed
    by build_stroke_chart_config.

    Parameters
    ----------
    compared_ids : sequence of workout IDs to overlay.
    compare_results : {id: raw_strokes_list}.  Missing or empty entries are
        skipped (still-loading or error).
    workouts_dict : {str(id): workout_dict} used to derive labels + interval
        metadata for time stitching.
    show_watts : whether pace points should carry watts instead of pace.
    colors : optional per-position color override list.  Defaults to a
        blue→orange palette via _interval_colors.
    labels : optional {id: label_str} override.  Defaults to
        "<yyyy-mm-dd> · <structure>".
    """
    if not compared_ids:
        return []
    palette = colors if colors is not None else _interval_colors(len(compared_ids))
    out = []
    for i, cid in enumerate(compared_ids):
        raw = compare_results.get(cid)
        if not raw:
            continue
        cw = workouts_dict.get(str(cid)) or {}
        intervals = (
            (cw.get("workout") or {}).get("intervals") if cw["is_interval"] else None
        )
        stitched = _stitch_interval_times(raw, intervals=intervals)
        pace_pts, spm_pts, hr_pts, has_hr = _points_from_strokes(
            stitched, show_watts=show_watts
        )
        rest_spans = _rest_spans_from_intervals(intervals)
        if rest_spans:
            pace_pts = _apply_rest_gaps(pace_pts, rest_spans)
            spm_pts = _apply_rest_gaps(spm_pts, rest_spans)
        if labels is not None and cid in labels:
            label = labels[cid]
        else:
            date_str = cw["day"]
            dist = cw.get("distance") or 0
            if cw["is_interval"]:
                suffix = cw["structure_key"]
            else:
                suffix = fmt_distance(dist) if dist else ""
            label = f"{date_str} · {suffix}".strip(" ·") or f"Workout {cid}"
        if intervals:
            # Wall-clock end including rest between intervals (workout["time"]
            # is work-only for interval workouts).
            total_tenths = sum(
                (iv.get("time") or 0) + (iv.get("rest_time") or 0) for iv in intervals
            )
            total_t_s = total_tenths / 10.0
        else:
            total_t_s = (cw.get("time") or 0) / 10.0
        out.append(
            {
                "id": cid,
                "label": label,
                "color": palette[i] if i < len(palette) else "#999",
                "pace_points": pace_pts,
                "spm_points": spm_pts,
                "hr_points": hr_pts,
                "has_hr": has_hr,
                "total_time_s": total_t_s,
            }
        )
    return out


def build_stroke_chart_config(
    strokes: list,
    workout: dict,
    *,
    metric: str = "pace",  # "pace" | "watts"
    focused_interval_idx: Optional[int] = None,
    is_dark: bool = False,
    stack: bool = False,
    show_pace: bool = True,
    show_spm: bool = True,
    show_hr: bool = True,
    custom_splits: Optional[dict] = None,
    compare_series: Optional[list] = None,
    primary_color: Optional[str] = None,
    primary_label: Optional[str] = None,
    smoothen: bool = True,
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
    stack   : overlay all work intervals starting from t=0
    show_pace/show_spm/show_hr : series visibility (stacked mode and compare mode)
    custom_splits : {"unit":"m"|"s","values":[int,...]} | None — when present on
        a non-interval workout, drives both the band layout (so stacked mode
        uses custom boundaries) and the chart labels.
    compare_series : list of compare dicts | None.  Each dict has id, label,
        color, pace_points, spm_points, hr_points, has_hr, total_time_s.
        When provided and stack=False, each entry is drawn as an additional
        dashed line on the primary chart.  x-axis expands to fit the longest
        compared workout.
    smoothen : should the series be smoothed based on point to pixel density.
    """
    if not strokes:
        return {}

    # Stitch t values so all strokes share a continuous timeline.
    wo = workout.get("workout") or {}
    wtype = workout.get("workout_type", "")
    intervals = wo.get("intervals") if workout["is_interval"] else None
    strokes = _stitch_interval_times(strokes, intervals=intervals)

    show_watts = metric == "watts"

    # ── Series colors ───────────────────────────────────────────────────────
    # Passed through the config dict so JS reads them in one place — no
    # hardcoded literals in JS segment callbacks.
    pace_color = primary_color or "#60a5fa"  # light blue  (pace/watts, thicker)
    spm_color = "#1e40af"  # dark blue   (stroke rate)
    hr_color = "#ef4444"  # red         (heart rate)
    pace_faded_color = "rgba(96,165,250,0.25)"  # pace at rest / onset
    spm_faded_color = "rgba(30,64,175,0.0)"  # spm at rest (invisible)

    # ── Build point arrays ───────────────────────────────────────────────────

    pace_pts, spm_pts, hr_pts, has_hr = _points_from_strokes(
        strokes, show_watts=show_watts
    )

    # Gap pace/SPM lines through rest periods so the chart doesn't drop a
    # dramatic line during inactive rests.  HR is left intact since recovery
    # HR carries useful information.
    rest_spans = _rest_spans_from_intervals(intervals)
    if rest_spans:
        pace_pts = _apply_rest_gaps(pace_pts, rest_spans)
        spm_pts = _apply_rest_gaps(spm_pts, rest_spans)

    has_compares = bool(compare_series)
    if primary_label is not None:
        primary_series_label = primary_label
    else:
        primary_series_label = "Watts" if show_watts else "Pace"
        if has_compares:
            # Use a meaningful legend label for the primary series when compares
            # are overlaid.  Falls back to a generic label if no date is present.
            date_str = workout["day"]
            primary_series_label = date_str or ("Watts" if show_watts else "Pace")
    primary_label = primary_series_label

    # ── Datasets ─────────────────────────────────────────────────────────────
    #
    # Without compares, SPM and HR are always shown (existing behaviour).
    # With compares, all three series groups are gated on show_pace/show_spm/
    # show_hr so the per-series toggle row can hide them uniformly.

    datasets: list = []

    if (not has_compares) or show_pace:
        datasets.append(
            {
                "label": primary_label,
                "data": pace_pts,
                "yAxisID": "y",
                "borderColor": pace_color,
                "backgroundColor": "transparent",
                "borderWidth": 1.5,
                "pointRadius": 0,
                "tension": 0.15,
                "order": 1,
                "spanGaps": False,
            }
        )

    if spm_pts and ((not has_compares) or show_spm):
        datasets.append(
            {
                "label": "_SPM" if has_compares else "SPM",
                "data": spm_pts,
                "yAxisID": "yspm",
                "borderColor": spm_color,
                "backgroundColor": "transparent",
                "borderWidth": 1,
                "pointRadius": 0,
                "tension": 0.1,
                "order": 2,
                "spanGaps": False,
            }
        )

    if has_hr and hr_pts and ((not has_compares) or show_hr):
        datasets.append(
            {
                "label": "_HR" if has_compares else "HR",
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

    # ── Compare overlays ─────────────────────────────────────────────────────

    if has_compares:
        for cs in compare_series:
            label = cs.get("label", "")
            color = cs.get("color", "#999")
            if show_pace and cs.get("pace_points"):
                datasets.append(
                    {
                        "label": label,
                        "data": cs["pace_points"],
                        "yAxisID": "y",
                        "borderColor": color,
                        "backgroundColor": "transparent",
                        "borderWidth": 1.5,
                        "pointRadius": 0,
                        "tension": 0.15,
                        "order": 1,
                        "isCompare": True,
                        "spanGaps": False,
                    }
                )
            if show_spm and cs.get("spm_points"):
                datasets.append(
                    {
                        "label": "_" + label + " spm",
                        "data": cs["spm_points"],
                        "yAxisID": "yspm",
                        "borderColor": color,
                        "backgroundColor": "transparent",
                        "borderWidth": 1,
                        "pointRadius": 0,
                        "tension": 0.1,
                        "order": 2,
                        "isCompare": True,
                        "spanGaps": False,
                    }
                )
            if show_hr and cs.get("has_hr") and cs.get("hr_points"):
                datasets.append(
                    {
                        "label": "_" + label + " hr",
                        "data": cs["hr_points"],
                        "yAxisID": "yhr",
                        "borderColor": color,
                        "backgroundColor": "transparent",
                        "borderWidth": 1,
                        "pointRadius": 0,
                        "tension": 0.1,
                        "order": 3,
                        "isCompare": True,
                    }
                )

    # ── Bands ────────────────────────────────────────────────────────────────

    bands = _build_bands(
        wo,
        wtype,
        custom_splits=custom_splits,
        strokes=strokes,
        workout=workout,
    )

    # ── Y-axis bounds ────────────────────────────────────────────────────────
    #
    # For interval workouts: derive bounds from work-interval points only so
    # that rest-period droop doesn't compress the scale.
    # Bounds are computed globally (not per-zoom) so the scale stays fixed
    # when zoomed into a single interval, enabling easy split comparison.
    # HR axis uses the same data-driven approach as SPM (lo_floor=40).

    if wtype in INTERVAL_WORKOUT_TYPES and bands:
        work_ranges = [(b["xMin"], b["xMax"]) for b in bands if b.get("work")]
        y_pace = [
            p["y"]
            for p in pace_pts
            if p["y"] is not None and any(lo <= p["x"] <= hi for lo, hi in work_ranges)
        ]
        y_spm = [
            p["y"]
            for p in spm_pts
            if p["y"] is not None and any(lo <= p["x"] <= hi for lo, hi in work_ranges)
        ]
        y_hr = [
            p["y"]
            for p in hr_pts
            if p["y"] is not None and any(lo <= p["x"] <= hi for lo, hi in work_ranges)
        ]
    else:
        y_pace = [p["y"] for p in pace_pts if p["y"] is not None]
        y_spm = [p["y"] for p in spm_pts if p["y"] is not None]
        y_hr = [p["y"] for p in hr_pts if p["y"] is not None]

    # Extend y ranges so compared-workout lines aren't clipped.
    if has_compares:
        for cs in compare_series:
            y_pace.extend(
                p["y"] for p in (cs.get("pace_points") or []) if p["y"] is not None
            )
            y_spm.extend(
                p["y"] for p in (cs.get("spm_points") or []) if p["y"] is not None
            )
            if cs.get("has_hr"):
                y_hr.extend(
                    p["y"] for p in (cs.get("hr_points") or []) if p["y"] is not None
                )

    pace_y_min_full, pace_y_max_full = _pad(
        *((min(y_pace), max(y_pace)) if y_pace else (None, None))
    )
    # Cap the slow end (top for pace, bottom for watts) so a single slow
    # catch stroke can't stretch the axis past the useful range.  The JS
    # plugin exposes a "full range" toggle button that swaps back to the
    # uncapped bounds.
    pace_y_min, pace_y_max = pace_y_min_full, pace_y_max_full
    slow_cap = _slow_end_cap(
        pace_pts,
        bands,
        show_watts=show_watts,
        workout=workout,
        compare_series=compare_series or [],
    )
    if slow_cap is not None and y_pace:
        if show_watts:
            if pace_y_min_full is not None and slow_cap > pace_y_min_full:
                pace_y_min = slow_cap
        else:
            if pace_y_max_full is not None and slow_cap < pace_y_max_full:
                pace_y_max = slow_cap

    spm_y_min, spm_y_max = _pad(
        *((min(y_spm), max(y_spm)) if y_spm else (None, None)),
        min_pad=2,
        lo_floor=0,
        round_to_int=True,
    )
    hr_y_min, hr_y_max = _pad(
        *((min(y_hr), max(y_hr)) if y_hr else (None, None)),
        min_pad=5,
        lo_floor=40,
        round_to_int=True,
    )

    # ── Stacked mode ─────────────────────────────────────────────────────────

    if stack:
        work_bands = [b for b in bands if b.get("work")]
        return _build_stacked_config(
            strokes,
            work_bands,
            show_watts=show_watts,
            show_pace=show_pace,
            show_spm=show_spm,
            show_hr=show_hr,
            has_hr=has_hr,
            pace_y_min=pace_y_min,
            pace_y_max=pace_y_max,
            pace_y_min_full=pace_y_min_full,
            pace_y_max_full=pace_y_max_full,
            spm_y_min=spm_y_min,
            spm_y_max=spm_y_max,
            pace_color=pace_color,
            spm_color=spm_color,
            hr_color=hr_color,
            is_dark=is_dark,
            smoothen=smoothen,
        )

    # ── x-axis zoom ──────────────────────────────────────────────────────────

    x_min = None
    x_max = None
    if focused_interval_idx is not None and 0 <= focused_interval_idx < len(bands):
        b = bands[focused_interval_idx]
        x_min = b["xMin"]
        x_max = b["xMax"]
    else:
        # Cap the x-axis at the recorded workout duration so trailing noise
        # beyond the finish doesn't expand the domain.  For interval workouts
        # the workout's `time` is work-only, so derive the wall-clock end from
        # the last band's xMax instead.
        if wtype in INTERVAL_WORKOUT_TYPES and bands:
            primary_total_s = bands[-1]["xMax"]
        else:
            primary_total_s = (workout.get("time") or 0) / 10.0
        if primary_total_s > 0:
            x_max = round(primary_total_s, 2)
        # With compares, expand x-axis to cover the longest compared workout.
        if has_compares:
            compare_max = max((cs.get("total_time_s") or 0) for cs in compare_series)
            if compare_max > 0:
                x_max = round(max(x_max or 0, compare_max), 2)

    # Extend the shaded band area to cover the full x-axis when a compared
    # workout stretches the domain past the primary workout's last band.
    # Without this the plot area beyond the primary's final split renders
    # un-shaded, which looks like a chart bug.
    if has_compares and bands and x_max is not None:
        last = bands[-1]
        if x_max > last["xMax"]:
            last["xMax"] = round(x_max, 2)

    return {
        "datasets": datasets,
        "bands": bands,
        "showWatts": show_watts,
        "showSpm": show_spm,
        "hasHr": has_hr,
        "paceYMin": pace_y_min,
        "paceYMax": pace_y_max,
        "paceYMinFull": pace_y_min_full,
        "paceYMaxFull": pace_y_max_full,
        "spmYMin": spm_y_min,
        "spmYMax": spm_y_max,
        "hrYMin": hr_y_min,
        "hrYMax": hr_y_max,
        "xMin": x_min,
        "xMax": x_max,
        "paceColor": pace_color,
        "paceFadedColor": pace_faded_color,
        "spmColor": spm_color,
        "spmFadedColor": spm_faded_color,
        "hrColor": hr_color,
        "isDark": is_dark,
        "showLegend": has_compares,
        "smoothen": smoothen,
    }
