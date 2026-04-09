"""
components/session_detail.py — Full-screen session detail overlay.

Renders when app_state.selected_session_id is set.  Displays:

  1. Header bar      — back button, date/machine/type title
  2. Summary stats   — compact multi-column metric grid
  3. Chart + splits  — pace/watts chart (left) beside splits/intervals table (right)
                       Chart has Pace/Watts toggle and Reset zoom button.
                       Clicking a split/interval row zooms the chart to that band.
  4. Similar sessions — result_table() of workouts with matching structure

Entry point::

    session_detail(
        workout,            # dict from _workouts_dict
        client,             # Concept2Client (for fetching strokes)
        user_id,            # str
        all_workouts,       # full sorted list
        on_session_click,   # callable(id) → open another session
    )
"""

from __future__ import annotations

import json
from typing import Optional

import hyperdiv as hd

from components.ranked_formatters import (
    _fmt_date,
    _fmt_distance,
    _pace_tenths,
    fmt_split,
    result_table,
)
from components.session_chart_builder import build_stroke_chart_config
from components.stroke_chart import StrokeChart
from services.interval_utils import interval_structure_key
from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    compute_watts,
    format_time,
)

from components.hyperdiv_extensions import radio_group
from components.workout_sync import workout_sync


# ---------------------------------------------------------------------------
# Summary stat grid
# ---------------------------------------------------------------------------


def _stat(label: str, value: str) -> None:
    """One stat cell: small muted label above bold value."""
    with hd.box(min_width=8, padding=(0.5, 1.25)):
        hd.text(
            label,
            font_size="x-small",
            font_color="neutral-500",
            font_weight="semibold",
        )
        hd.text(value, font_weight="bold", font_size="medium")


def _summary_section(workout: dict, strokes: Optional[list]) -> None:
    """Compact multi-column stat grid."""
    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES
    pace = _pace_tenths(workout)
    pace_sec = (pace / 10.0) if pace else None
    avg_watts = round(compute_watts(pace_sec)) if pace_sec else None

    # Stroke-derived metrics
    max_w = None
    if strokes:
        watts_list, hr_list = [], []
        for s in strokes:
            p = s.get("p")
            if p and p > 0:
                watts_list.append(compute_watts(p / 10.0))
            hr = s.get("hr")
            if hr:
                hr_list.append(hr)
        if watts_list:
            max_w = max(watts_list)

    hr_data = workout.get("heart_rate") or {}
    rest_dist = workout.get("rest_distance")
    rest_time = workout.get("rest_time")

    _theme = hd.theme()
    border_b = "1px solid neutral-200"

    with hd.box(border_bottom=border_b, padding_bottom=0.25, margin_bottom=0.5):
        with hd.hbox(wrap="wrap", gap=0):
            if workout.get("distance"):
                _stat("Distance", _fmt_distance(workout["distance"]))
            if workout.get("time"):
                _stat("Time", format_time(workout["time"]))
            if pace_sec:
                _stat("Pace", fmt_split(pace))
            if avg_watts:
                _stat("Avg. Watts", f"{avg_watts} W")
            if max_w is not None:
                _stat("Max Watts", f"{round(max_w)} W")
            if workout.get("stroke_rate"):
                _stat("SPM", str(workout["stroke_rate"]))
            if workout.get("stroke_count"):
                _stat("Strokes", str(workout["stroke_count"]))
            if workout.get("drag_factor"):
                _stat("Drag", str(workout["drag_factor"]))
            if is_interval:
                if rest_dist:
                    _stat("Rest Distance", _fmt_distance(rest_dist))
                if rest_time:
                    _stat("Rest Time", format_time(rest_time))
            if hr_data.get("average"):
                _stat("Avg. HR", f"{hr_data['average']} bpm")
            if hr_data.get("max"):
                _stat("Max HR", f"{hr_data['max']} bpm")


# ---------------------------------------------------------------------------
# Custom splits UI
# ---------------------------------------------------------------------------

_CUSTOM_SPLITS_LS_KEY = "custom_splits"


def _custom_splits_ui(
    workout_id: int,
    strokes: list,
    total_dist_m: int,
    on_splits_change,
) -> None:
    """Chip-row editor for custom split distances."""
    s = hd.state(
        loaded=False,
        store={},
        editing=False,
        inputs=[],
        error="",
    )

    if not s.loaded:
        ls = hd.local_storage.get_item(_CUSTOM_SPLITS_LS_KEY)
        if not ls.done:
            return
        raw = ls.result
        s.store = json.loads(raw) if raw else {}
        saved = s.store.get(str(workout_id))
        if saved:
            s.inputs = list(saved)
        else:
            n = total_dist_m // 500
            rem = total_dist_m % 500
            s.inputs = [500] * n + ([rem] if rem else [])
        s.loaded = True

    _theme = hd.theme()

    with hd.box(gap=0.5, padding_bottom=0.5):
        with hd.hbox(gap=1, align="center"):
            # hd.text("Custom splits", font_weight="semibold", font_size="small")
            toggle_btn = hd.button(
                "Edit" if not s.editing else "Cancel",
                variant="text",
                size="small",
            )
        if toggle_btn.clicked:
            s.editing = not s.editing

        if s.editing:
            with hd.box(gap=0.75):
                with hd.hbox(gap=0.5, wrap="wrap", align="center"):
                    for i, dist in enumerate(s.inputs):
                        with hd.scope(i):
                            ti = hd.text_input(value=str(dist), width=5, size="small")
                            if ti.changed:
                                try:
                                    new_val = int(ti.value)
                                    lst = list(s.inputs)
                                    lst[i] = max(1, new_val)
                                    s.inputs = lst
                                    s.error = ""
                                except ValueError:
                                    s.error = "Distances must be whole numbers."

                    add_btn = hd.icon_button(
                        "plus-circle", font_size="small", font_color="primary"
                    )
                    if add_btn.clicked:
                        s.inputs = list(s.inputs) + [500]

                    if len(s.inputs) > 1:
                        rem_btn = hd.icon_button(
                            "dash-circle", font_size="small", font_color="neutral-400"
                        )
                        if rem_btn.clicked:
                            s.inputs = list(s.inputs)[:-1]

                actual_sum = sum(s.inputs)
                diff = actual_sum - total_dist_m
                if s.error:
                    hd.text(s.error, font_color="danger", font_size="x-small")
                elif abs(diff) > 2:
                    hd.text(
                        f"Sum ({actual_sum:,}m) must equal workout distance "
                        f"({total_dist_m:,}m) — off by {diff:+,}m.",
                        font_color="warning-600",
                        font_size="x-small",
                    )
                else:
                    hd.text(
                        f"✓ Sum: {actual_sum:,}m",
                        font_color="success",
                        font_size="x-small",
                    )

                recalc_btn = hd.button(
                    "Recalculate",
                    variant="primary",
                    size="small",
                    disabled=(abs(actual_sum - total_dist_m) > 2 or bool(s.error)),
                )
                if recalc_btn.clicked:
                    splits_m = list(s.inputs)
                    s.store[str(workout_id)] = splits_m
                    hd.local_storage.set_item(
                        _CUSTOM_SPLITS_LS_KEY, json.dumps(s.store)
                    )
                    s.editing = False
                    on_splits_change(splits_m)


# ---------------------------------------------------------------------------
# Split recalculation from stroke data
# ---------------------------------------------------------------------------


def _recalculate_splits(strokes: list, split_distances_m: list) -> list:
    """
    Interpolate stroke data to compute split metrics at custom distance boundaries.

    Returns a list of dicts:
        {distance, time_tenths, pace_tenths, spm, hr_avg, hr_max, max_watts}

    Stroke d is in decimeters; t is in tenths of a second.
    """
    if not strokes or not split_distances_m:
        return []

    d_m = [s.get("d", 0) / 10.0 for s in strokes]
    t_t = [s.get("t", 0) for s in strokes]

    def interp_time(target_m: float) -> Optional[float]:
        if target_m <= 0:
            return 0.0
        if target_m >= d_m[-1]:
            return float(t_t[-1])
        lo, hi = 0, len(d_m) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if d_m[mid] < target_m:
                lo = mid
            else:
                hi = mid
        span = d_m[hi] - d_m[lo]
        if span <= 0:
            return float(t_t[lo])
        frac = (target_m - d_m[lo]) / span
        return t_t[lo] + frac * (t_t[hi] - t_t[lo])

    def strokes_in_range(lo_m: float, hi_m: float) -> list:
        return [s for s, dm in zip(strokes, d_m) if lo_m <= dm <= hi_m]

    result = []
    cumulative = 0.0
    for dist_m in split_distances_m:
        start_m = cumulative
        end_m = cumulative + dist_m
        t_start = interp_time(start_m) or 0.0
        t_end = interp_time(end_m) or 0.0
        dur_tenths = t_end - t_start

        window = strokes_in_range(start_m, end_m)
        spm_vals = [s.get("spm") for s in window if s.get("spm")]
        hr_vals = [s.get("hr") for s in window if s.get("hr")]
        pace_tenths = (dur_tenths * 500.0 / dist_m) if dist_m > 0 else None
        max_w = None
        if window:
            wl = [
                compute_watts(s["p"] / 10.0)
                for s in window
                if s.get("p") and s["p"] > 0
            ]
            if wl:
                max_w = max(wl)

        result.append(
            {
                "distance": dist_m,
                "time_tenths": dur_tenths,
                "pace_tenths": pace_tenths,
                "spm": (sum(spm_vals) / len(spm_vals)) if spm_vals else None,
                "hr_avg": (sum(hr_vals) / len(hr_vals)) if hr_vals else None,
                "hr_max": max(hr_vals) if hr_vals else None,
                "max_watts": max_w,
            }
        )
        cumulative = end_m

    return result


# ---------------------------------------------------------------------------
# Splits / intervals table
# ---------------------------------------------------------------------------


def _fmt_pace(pace_tenths) -> str:
    if not pace_tenths:
        return "—"
    return fmt_split(pace_tenths)


def _splits_table(
    workout: dict,
    strokes: Optional[list],
    custom_split_dists: Optional[list],
    focused_idx: int = -1,
    on_focus=None,
) -> None:
    """
    Render splits or intervals table.

    Clicking a row calls on_focus(i) to zoom the chart to that band.
    focused_idx highlights the currently zoomed row.
    """
    _theme = hd.theme()
    header_color = "neutral-500"
    ts = "small"
    wo = workout.get("workout") or {}
    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES

    if is_interval:
        _intervals_table(
            wo.get("intervals") or [],
            strokes,
            _theme,
            header_color,
            ts,
            focused_idx=focused_idx,
            on_focus=on_focus,
        )
        return

    # For split-based workouts
    splits_data = None
    if custom_split_dists and strokes:
        splits_data = _recalculate_splits(strokes, custom_split_dists)
    elif wo.get("splits"):
        splits_data = []
        for sp in wo["splits"]:
            t = sp.get("time") or 0
            d = sp.get("distance") or 0
            hr = sp.get("heart_rate") or {}
            splits_data.append(
                {
                    "distance": d,
                    "time_tenths": t,
                    "pace_tenths": (t * 500 / d) if d else None,
                    "spm": sp.get("stroke_rate"),
                    "hr_avg": hr.get("average"),
                    "hr_max": hr.get("max"),
                    "max_watts": None,
                }
            )

    if not splits_data:
        hd.text("No split data available.", font_color="neutral-500", font_size="small")
        return

    has_hr = any(sp.get("hr_avg") is not None for sp in splits_data)
    # "Watts" column shows "avg / max" when max is available from stroke data.
    # "HR" column shows "avg / max" when both values are present.
    col_w = [2.5, 6, 6, 6, 7, 3.5, 7]
    headers = ["#", "Dist", "Time", "Pace", "Watts", "SPM", "HR"]
    if not has_hr:
        col_w = col_w[:-1]
        headers = headers[:-1]

    _table_frame(
        splits_data,
        col_w,
        headers,
        _theme,
        header_color,
        ts,
        focused_idx=focused_idx,
        on_focus=on_focus,
        row_renderer=lambda i, sp, cw: _split_row(i, sp, cw, ts, has_hr),
    )


def _split_row(i, sp, col_w, ts, has_hr):
    pace_t = sp.get("pace_tenths")
    avg_w = round(compute_watts(pace_t / 10.0)) if pace_t else None
    max_w = sp.get("max_watts")
    hr_avg = sp.get("hr_avg")
    hr_max = sp.get("hr_max")
    spm = sp.get("spm")

    # Combined Watts: "avg" or "avg / max"
    if avg_w is None:
        watts_str = "—"
    elif max_w is not None:
        watts_str = f"{avg_w} / {round(max_w)}"
    else:
        watts_str = str(avg_w)

    # Combined HR: "avg" or "avg / max"
    if hr_avg is None:
        hr_str = "—"
    elif hr_max:
        hr_str = f"{hr_avg:.0f} / {hr_max:.0f}"
    else:
        hr_str = f"{hr_avg:.0f}"

    cells = [
        (str(i + 1), col_w[0], "neutral-500"),
        (_fmt_distance(sp.get("distance")), col_w[1], None),
        (
            format_time(round(sp.get("time_tenths", 0)))
            if sp.get("time_tenths")
            else "—",
            col_w[2],
            None,
        ),
        (_fmt_pace(pace_t), col_w[3], None),
        (watts_str, col_w[4], None),
        (f"{spm:.0f}" if spm else "—", col_w[5], None),
    ]
    if has_hr:
        cells.append((hr_str, col_w[6], None))

    for idx, (val, w, color) in enumerate(cells):
        with hd.scope(f"{idx}"):
            kwargs = {"font_size": ts, "width": w}
            if color:
                kwargs["font_color"] = color
            hd.text(val, **kwargs)


def _intervals_table(
    intervals: list,
    strokes: Optional[list],
    _theme,
    header_color: str,
    ts: str,
    focused_idx: int = -1,
    on_focus=None,
) -> None:
    """
    Render interval-workout intervals table.

    Includes both work and rest rows so row index i matches band index i from
    _bands_from_intervals() (both skip zero-duration intervals, same order).
    Rest rows are styled muted with "r" in the # column.
    """
    # Detect HR data across all work intervals
    has_hr = any((iv.get("heart_rate") or {}).get("average") for iv in intervals)

    # Build rows — one work row per interval, plus an optional rest row when
    # rest_time is set.  Mirrors _bands_from_intervals exactly so row index i
    # always corresponds to band index i for click-to-focus.
    rows = []
    for work_idx, iv in enumerate(intervals):
        t = iv.get("time") or 0
        if t <= 0:
            continue

        d = iv.get("distance") or 0
        pace_t = (t * 500 / d) if d else None
        hr = (iv.get("heart_rate") or {}).get("average")
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

        rest_t = iv.get("rest_time") or 0
        if rest_t > 0:
            rest_d = iv.get("rest_distance") or 0
            rest_pace_t = (rest_t * 500 / rest_d) if rest_d else None
            rows.append(
                {
                    "_is_rest": True,
                    "time": rest_t,
                    "distance": rest_d,
                    "pace_tenths": rest_pace_t,
                }
            )

    col_w = [2.5, 6, 6, 6, 5, 3.5]
    headers = ["#", "Dist", "Time", "Pace", "W", "SPM"]
    if has_hr:
        col_w.append(5.5)
        headers.append("HR")

    _table_frame(
        rows,
        col_w,
        headers,
        _theme,
        header_color,
        ts,
        focused_idx=focused_idx,
        on_focus=on_focus,
        row_renderer=lambda i, r, cw: _interval_row(i, r, cw, ts, has_hr),
    )


def _interval_row(i, r, col_w, ts, has_hr):
    is_rest = r.get("_is_rest", False)
    pace_t = r.get("pace_tenths")
    d = r.get("distance") or 0
    t = r.get("time") or 0
    spm = r.get("spm")
    hr = r.get("hr_avg")
    muted = "neutral-400" if is_rest else None

    num_str = "" if is_rest else str(r["_work_idx"] + 1)
    cells = [
        (num_str, col_w[0], "neutral-400" if is_rest else "neutral-500"),
        (_fmt_distance(d) if d else "—", col_w[1], muted),
        (format_time(t) if t else "—", col_w[2], muted),
        (_fmt_pace(pace_t), col_w[3], muted),
        (
            str(r["avg_watts"]) if r.get("avg_watts") is not None else "",
            col_w[4],
            muted,
        ),
        (str(spm) if spm else "", col_w[5], muted),
    ]
    if has_hr:
        cells.append((f"{hr:.0f}" if hr else "", col_w[6], muted))

    for idx, (val, w, color) in enumerate(cells):
        with hd.scope(f"{idx}"):
            kwargs = {"font_size": ts, "width": w}
            if color:
                kwargs["font_color"] = color
            hd.text(val, **kwargs)


def _table_frame(
    rows, col_w, headers, _theme, header_color, ts, focused_idx, on_focus, row_renderer
):
    """Shared table chrome: header + body rows with click-to-focus.

    Work rows (any row without _is_rest=True) are rendered as hd.link so the
    entire row is clickable; clicking toggles the zoom focus for that band.
    Rest rows are rendered as plain hboxes with no click target.
    """
    border = "1px solid neutral-200"
    row_border = "1px solid neutral-100"
    focus_bg = "primary-50"

    with hd.box(border=border, border_radius="medium"):
        # Header
        with hd.hbox(
            padding=(0.35, 0.75, 0.35, 0.75),
            background_color="neutral-50",
            border_bottom=border,
            gap=0.5,
        ):
            for h, w in zip(headers, col_w):
                with hd.scope(h):
                    hd.text(
                        h,
                        font_color=header_color,
                        font_size="x-small",
                        font_weight="semibold",
                        width=w,
                    )

        # Body rows
        for i, row in enumerate(rows):
            with hd.scope(i):
                is_focused = i == focused_idx
                is_rest = row.get("_is_rest", False)
                is_focusable = on_focus is not None and not is_rest

                row_kwargs = dict(
                    gap=0.5,
                    border_bottom=row_border,
                    background_color=focus_bg if is_focused else None,
                    align="center",
                    padding=(0.35, 0.75, 0.35, 0.75),
                )

                if is_focusable:
                    # hd.link renders as <a> and carries Interactive.clicked
                    # without causing navigation when href="#" (fragment only).
                    with hd.link(
                        href="#",
                        target="_self",
                        direction="horizontal",
                        font_color="neutral-700",
                        underline=False,
                        hover_background_color="neutral-50",
                        **row_kwargs,
                    ) as row_el:
                        row_renderer(i, row, col_w)
                    if row_el.clicked:
                        on_focus(None if is_focused else i, row)
                else:
                    with hd.hbox(**row_kwargs):
                        row_renderer(i, row, col_w)


# ---------------------------------------------------------------------------
# Similar workouts
# ---------------------------------------------------------------------------


def _find_similar(workout: dict, all_workouts: list, n: int = 8) -> list:
    wtype = workout.get("workout_type", "")
    wid = workout.get("id")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES

    if is_interval:
        key = interval_structure_key(workout)
        pool = [
            w
            for w in all_workouts
            if w.get("id") != wid and interval_structure_key(w) == key
        ]
        pool.sort(key=lambda w: w.get("date", ""), reverse=True)
    else:
        ref_dist = workout.get("distance", 0)
        ref_pace = _pace_tenths(workout)
        pool = []
        for w in all_workouts:
            if w.get("id") == wid or w.get("workout_type") != wtype:
                continue
            d = w.get("distance", 0)
            if ref_dist and d and abs(d - ref_dist) / ref_dist > 0.20:
                continue
            pool.append(w)
        if ref_pace:
            pool.sort(key=lambda w: abs((_pace_tenths(w) or 9999) - ref_pace))
        else:
            pool.sort(key=lambda w: w.get("date", ""), reverse=True)

    return pool[:n]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def session_detail(session_id: int, client, user_id: str) -> None:
    """Render the full-screen session detail overlay."""
    _theme = hd.theme()

    state = hd.state(
        metric="pace",  # "pace" | "watts"
        focused_interval=None,  # int | None
        focused_interval_excluding_rest=None,  # int | None
        custom_splits=None,  # list[int] | None
    )

    # ── Pre-fetch workout list (cached; zero cost on repeat renders) ───────
    sync_result = workout_sync(client)

    if sync_result is None:
        return

    _workouts_dict, all_workouts = sync_result
    workout = _workouts_dict.get(str(session_id))

    # ── Fetch stroke data ────────────────────────────────────────────────────

    has_strokes = bool(workout.get("stroke_data"))
    stroke_task = hd.task()

    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES

    def _fetch_detail():
        return client.get_strokes(int(user_id), workout["id"])

    if has_strokes:
        stroke_task.run(_fetch_detail)

    strokes: Optional[list] = None
    if has_strokes and stroke_task.done and not stroke_task.error:
        strokes = stroke_task.result if isinstance(stroke_task.result, list) else []

    if is_interval:
        ivs = (workout.get("workout") or {}).get("intervals") or []
        work_ivs = [iv for iv in ivs if (iv.get("type") or "").lower() != "rest"]
        reps = len(work_ivs) or len(ivs)

        title = f"{reps} x {interval_structure_key(workout, compact=True)}"
    else:
        title = _fmt_distance_label(workout)

    # ── Layout ───────────────────────────────────────────────────────────────

    with hd.box(padding=(1, 2), gap=1.5):
        # ── Header ───────────────────────────────────────────────────────────

        with hd.hbox(padding_top=1, gap=2, align="center"):
            hd.text(_fmt_date(workout.get("date", "")), font_color="neutral-500")

            # hd.text( "/", font_color="neutral-500")

            hd.text(title, font_weight="bold", font_size="x-large")

        if workout.get("comments"):
            with hd.hbox(gap=0.25):
                hd.icon("quote", font_color="neutral-500")
                hd.text(
                    workout["comments"], font_color="neutral-500", font_size="medium"
                )
                hd.text('"')

        # ── Summary stats ─────────────────────────────────────────────────────

        _summary_section(workout, strokes)

        # ── Chart + Splits side by side ───────────────────────────────────────

        is_interval = workout.get("workout_type", "") in INTERVAL_WORKOUT_TYPES
        total_dist = workout.get("distance") or 0
        show_custom = has_strokes and not is_interval and total_dist > 0

        def on_split_focus(idx, row):
            state.focused_interval = idx
            if idx is None:
                state.focused_interval_excluding_rest = None
            else:
                state.focused_interval_excluding_rest = row.get("_work_idx", 0) + 1

        with hd.hbox(gap=2, align="start"):
            # Left: chart
            with hd.box(gap=1, grow=True, min_width=0):
                if state.focused_interval is not None:
                    band_type = "Interval" if is_interval else "Split"
                    graph_title = f"Workout Graph: {band_type} {state.focused_interval_excluding_rest}"
                else:
                    graph_title = "Workout Graph"
                hd.text(
                    graph_title,
                    font_weight="semibold",
                    font_size="x-large",
                    font_color="neutral-800",
                )

                if not has_strokes:
                    with hd.box(
                        padding=2,
                        align="center",
                        border_radius="medium",
                        background_color="neutral-100"
                        if not _theme.is_dark
                        else "neutral-800",
                        height=18,
                    ):
                        hd.text(
                            "Stroke data not available for this session.",
                            font_color="neutral-500",
                        )
                elif stroke_task.running:
                    with hd.box(padding=2, align="center", height=18, justify="center"):
                        hd.spinner()
                        hd.text("Loading…", font_color="neutral-500", font_size="small")
                elif stroke_task.error:
                    hd.alert(
                        f"Could not load stroke data: {stroke_task.error}",
                        variant="warning",
                        opened=True,
                    )
                elif strokes:
                    with hd.scope("chart"):
                        cfg = build_stroke_chart_config(
                            strokes,
                            workout,
                            metric=state.metric,
                            focused_interval_idx=state.focused_interval,
                            is_dark=_theme.is_dark,
                        )
                        chart = StrokeChart(config=cfg, height=300)
                        if (
                            chart.clicked_band_idx >= 0
                            and chart.clicked_band_idx != state.focused_interval
                        ):
                            state.focused_interval = chart.clicked_band_idx

                    # ── Chart controls ────────────────────────────────────────────────────

                    with hd.hbox(gap=1.5, align="center", padding_bottom=0.25):
                        with radio_group(value=state.metric, size="small") as rg:
                            hd.radio_button("Pace", value="pace")
                            hd.radio_button("Watts", value="watts", size="small")
                        if rg.changed:
                            state.metric = rg.value

                        if state.focused_interval is not None:
                            reset_btn = hd.button(
                                "Reset zoom", variant="neutral", size="small"
                            )
                            if reset_btn.clicked:
                                state.focused_interval = None
                                state.focused_interval_excluding_rest = None

                else:
                    hd.text(
                        "No stroke data returned.",
                        font_color="neutral-500",
                        font_size="small",
                    )

            # Right: splits/intervals table + custom splits editor
            with hd.box(gap=0.75):
                with hd.hbox(gap=0.5):
                    hd.text(
                        "Intervals" if is_interval else "Splits",
                        font_weight="semibold",
                        font_size="x-large",
                        font_color="neutral-800",
                    )
                    if show_custom:
                        _custom_splits_ui(
                            workout_id=workout["id"],
                            strokes=strokes or [],
                            total_dist_m=total_dist,
                            on_splits_change=lambda dists: setattr(
                                state, "custom_splits", dists
                            ),
                        )
                _splits_table(
                    workout,
                    strokes,
                    state.custom_splits,
                    focused_idx=state.focused_interval
                    if state.focused_interval is not None
                    else -1,
                    on_focus=on_split_focus,
                )

        # ── Similar sessions ─────────────────────────────────────────────────

        similar = _find_similar(workout, all_workouts)
        if similar:
            hd.text(
                "Similar sessions",
                font_weight="semibold",
                font_size="small",
                font_color="neutral-500",
            )
            result_table(
                similar,
                extra_col=(
                    "Workout",
                    12,
                    lambda w: (
                        interval_structure_key(w, compact=True)
                        if w.get("workout_type", "") in INTERVAL_WORKOUT_TYPES
                        else ""
                    ),
                ),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_distance_label(workout: dict) -> str:
    d = workout.get("distance")
    if d:
        return _fmt_distance(d)
    t = workout.get("time")
    if t:
        return format_time(t)
    return ""
