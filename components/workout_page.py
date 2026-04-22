"""
components/workout_page.py — Full-screen workout detail overlay.

Activated by URL routing: app.py renders this component when
loc.path starts with "/session/".  The view icon in every result table
navigates to /session/{id}; the Back link returns to the previous tab.

Displays:

  1. Header bar      — date/machine/type title (with workout comment if present)
  2. Summary stats   — compact multi-column metric grid
  3. Chart + splits  — pace/watts chart (left) beside splits/intervals table (right)
                       Chart has Pace/Watts toggle, Stack mode, and Reset zoom button.
                       Clicking a split/interval row zooms the chart to that band.
  4. Similar sessions — WorkoutTable() of workouts with matching structure

Entry point::

    workout_page(session_id, client, user_id)

    session_id  int   — extracted from loc.path ("/session/<id>")
    client      Concept2Client
    user_id     str

Workout data and the full list are fetched via concept2_sync(), which is
task-cached so repeated calls within a render cycle are free.
"""

from __future__ import annotations

import json
from typing import Optional

import hyperdiv as hd

from services.formatters import (
    fmt_date,
    fmt_distance,
    pace_tenths,
    fmt_split,
    fmt_distance_label,
    format_time,
)

from components.workout_table import (
    WorkoutTable,
    ColumnDef,
    COL_DATE,
    COL_DISTANCE,
    COL_TIME,
    COL_PACE,
    COL_WATTS,
    COL_DRAG,
    COL_SPM,
    COL_HR,
    COL_LINK,
)

from components.workout_chart_builder import (
    build_interval_rows_and_bands,
    build_stroke_chart_config,
    _interval_colors,
    _points_from_strokes,
    _stitch_interval_times,
)
from components.workout_chart_plugin import StrokeChart
from services.interval_utils import interval_structure_key
from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    compute_watts,
)

from components.hyperdiv_extensions import radio_group
from components.concept2_sync import sync_from_context, strokes_for


# ---------------------------------------------------------------------------
# Summary stat grid
# ---------------------------------------------------------------------------


def _stat(label: str, value: str) -> None:
    """One stat cell: small muted label above bold value."""
    with hd.box(padding=(0.5, 1.25, 0.5, 1.25)):
        hd.text(
            label,
            font_size="small",
            font_color="neutral-500",
            font_weight="semibold",
        )
        hd.text(value, font_weight="bold", font_size="large")


def _summary_section(workout: dict, strokes: Optional[list]) -> None:
    """Compact multi-column stat grid."""
    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES
    pace = pace_tenths(workout)
    pace_sec = (pace / 10.0) if pace else None
    avg_watts = round(compute_watts(pace_sec)) if pace_sec else None

    # Stroke-derived metrics
    max_w = None
    if strokes:
        watts_list = []
        hr_list = []
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

    with hd.box(grow=True):
        with hd.hbox(wrap="wrap", gap=0):
            if workout.get("distance"):
                _stat("Distance", fmt_distance(workout["distance"]))
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
                    _stat("Rest Distance", fmt_distance(rest_dist))
                if rest_time:
                    _stat("Rest Time", format_time(rest_time))
            if hr_data.get("average"):
                _stat("Avg. HR", f"{hr_data['average']} bpm")
            if hr_data.get("max"):
                _stat("Max HR", f"{hr_data['max']} bpm")


# ---------------------------------------------------------------------------
# Custom splits UI
# ---------------------------------------------------------------------------
#
# Persisted shape (localStorage key "custom_splits"):
#     {str(workout_id): {"unit": "m" | "s", "values": [int, ...]}}
#
# Legacy values (bare list of meters) are auto-migrated in memory on load; the
# migrated shape is written back only when the user next clicks Recalculate on
# that workout.

_CUSTOM_SPLITS_LS_KEY = "custom_splits"

_TIME_BASED_WORKOUT_TYPES = {"FixedTimeSplits"}


def _format_mmss(seconds: int) -> str:
    """Integer seconds → 'M:SS' (e.g. 90 → '1:30', 30 → '0:30')."""
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _parse_time_input(text: str):
    """Parse chip text into integer seconds.

    Accepts bare integer seconds ("90") or M:SS ("1:30").  M:SS requires a
    2-digit seconds side so that ambiguous inputs like "1:5" are rejected
    (could mean 65s or 105s).

    Returns (seconds: int, None) on success, (None, error_message: str) on
    failure.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Enter a duration."
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None, 'Use "M:SS" format.'
        m_str, s_str = parts
        if len(s_str) != 2:
            return (
                None,
                'Use seconds ("90") or M:SS ("1:30"). '
                f'"{raw}" is ambiguous — write "{m_str}:{s_str.zfill(2)}".',
            )
        try:
            m_val = int(m_str)
            s_val = int(s_str)
        except ValueError:
            return None, f'Could not parse "{raw}" as M:SS.'
        if m_val < 0 or s_val < 0 or s_val >= 60:
            return None, f'"{raw}" is out of range.'
        total = m_val * 60 + s_val
        if total <= 0:
            return None, "Duration must be positive."
        return total, None
    # Bare integer seconds.
    try:
        v = int(raw)
    except ValueError:
        return None, 'Use seconds ("90") or M:SS ("1:30").'
    if v <= 0:
        return None, "Duration must be positive."
    return v, None


_DEFAULT_SPLIT_COUNT = 5
_SPLIT_COUNT_OPTIONS = (2, 3, 4, 5, 6, 8, 10)


def _even_splits(total: int, n: int) -> list:
    """Divide `total` into `n` integer splits as evenly as possible.

    Distributes the remainder onto the trailing splits (each gets +1) so the
    list sums exactly to `total`.  Example: _even_splits(5001, 5) →
    [1000, 1000, 1000, 1000, 1001].
    """
    if n <= 0 or total <= 0:
        return []
    base = total // n
    rem = total - base * n
    return [base + (1 if i >= n - rem else 0) for i in range(n)]


def _normalize_saved_entry(saved):
    """Coerce a localStorage value (legacy list or new dict) to the new shape.

    Returns {"unit": "m"|"s", "values": [int,...]} or None.
    """
    if isinstance(saved, list):
        return {"unit": "m", "values": [int(v) for v in saved]}
    if isinstance(saved, dict) and "unit" in saved and "values" in saved:
        return {
            "unit": saved["unit"],
            "values": [int(v) for v in saved["values"]],
        }
    return None


def _custom_splits_ui(workout: dict, strokes: list, on_splits_change) -> None:
    """Chip-row editor for custom split distances or durations.

    For distance-based workouts, chips are meters.  For FixedTimeSplits,
    chips are integer seconds displayed as M:SS; the text input accepts
    either bare seconds or M:SS.
    """
    workout_id = workout.get("id")
    wtype = workout.get("workout_type", "")
    is_time_based = wtype in _TIME_BASED_WORKOUT_TYPES
    total_dist_m = workout.get("distance") or 0
    total_time_s = (workout.get("time") or 0) // 10

    target = total_time_s if is_time_based else total_dist_m
    target_unit = "s" if is_time_based else "m"

    s = hd.state(
        loaded=False,
        store={},
        editing=False,
        inputs=[],
        unit=target_unit,
        error="",
    )

    if not s.loaded:
        ls = hd.local_storage.get_item(_CUSTOM_SPLITS_LS_KEY)
        if not ls.done:
            return
        raw = ls.result
        parsed = json.loads(raw) if raw else {}
        # Migrate legacy list-valued entries in memory so reads are uniform.
        store = {}
        for k, v in parsed.items():
            normalized = _normalize_saved_entry(v)
            if normalized is not None:
                store[k] = normalized
        s.store = store
        saved = store.get(str(workout_id))
        if saved and saved["unit"] == target_unit:
            s.inputs = list(saved["values"])
            s.unit = saved["unit"]
        else:
            # Smart default: divide the workout into 5 as-even-as-possible
            # splits (e.g. 5k → 5×1000m, 30min → 5×6:00).  Beats the old
            # fixed 500m / 60s defaults on longer sessions.
            s.inputs = _even_splits(target, _DEFAULT_SPLIT_COUNT)
            s.unit = target_unit
        s.loaded = True

    with hd.box(gap=0.5, padding_bottom=0.5):
        with hd.hbox(gap=1, align="center"):
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
                    for i, v in enumerate(s.inputs):
                        with hd.scope(i):
                            display = _format_mmss(v) if s.unit == "s" else str(v)
                            ti = hd.text_input(value=display, width=5, size="small")
                            if ti.changed:
                                if s.unit == "s":
                                    val, err = _parse_time_input(ti.value)
                                else:
                                    try:
                                        val = max(1, int(ti.value))
                                        err = None
                                    except ValueError:
                                        val = None
                                        err = "Distances must be whole numbers."
                                if val is None:
                                    s.error = err or "Invalid input."
                                else:
                                    lst = list(s.inputs)
                                    lst[i] = val
                                    s.inputs = lst
                                    s.error = ""

                    add_btn = hd.icon_button(
                        "plus-circle", font_size="small", font_color="primary"
                    )
                    if add_btn.clicked:
                        default = 60 if s.unit == "s" else 500
                        s.inputs = list(s.inputs) + [default]

                    if len(s.inputs) > 1:
                        rem_btn = hd.icon_button(
                            "dash-circle", font_size="small", font_color="neutral-400"
                        )
                        if rem_btn.clicked:
                            s.inputs = list(s.inputs)[:-1]

                # Even-split helper: regenerate chips as N as-equal-as-
                # possible splits covering the whole workout.
                with hd.hbox(gap=0.5, align="center"):
                    hd.text(
                        "Divide into",
                        font_size="x-small",
                        font_color="neutral-500",
                    )
                    with hd.dropdown(f"{len(s.inputs)} splits") as _n_dd:
                        with hd.box(
                            background_color="neutral-0",
                            align="start",
                            padding=0.25,
                        ):
                            for n in _SPLIT_COUNT_OPTIONS:
                                with hd.scope(f"n_{n}"):
                                    n_btn = hd.button(
                                        f"{n} splits",
                                        variant="text",
                                        size="small",
                                        font_weight="bold"
                                        if n == len(s.inputs)
                                        else "normal",
                                    )
                                    if n_btn.clicked:
                                        new_vals = _even_splits(target, n)
                                        if new_vals:
                                            s.inputs = new_vals
                                            s.error = ""
                                        _n_dd.opened = False

                actual_sum = sum(s.inputs)
                diff = actual_sum - target
                unit_noun = "s" if s.unit == "s" else "m"
                target_display = (
                    _format_mmss(target) if s.unit == "s" else f"{target:,}m"
                )
                sum_display = (
                    _format_mmss(actual_sum) if s.unit == "s" else f"{actual_sum:,}m"
                )
                if s.error:
                    hd.text(s.error, font_color="danger", font_size="x-small")
                elif abs(diff) > 2:
                    hd.text(
                        f"Sum ({sum_display}) must equal workout "
                        f"{'time' if s.unit == 's' else 'distance'} "
                        f"({target_display}) — off by {diff:+,}{unit_noun}.",
                        font_color="warning-600",
                        font_size="x-small",
                    )
                else:
                    hd.text(
                        f"✓ Sum: {sum_display}",
                        font_color="success",
                        font_size="x-small",
                    )

                recalc_btn = hd.button(
                    "Recalculate",
                    variant="primary",
                    size="small",
                    disabled=(abs(diff) > 2 or bool(s.error)),
                )
                if recalc_btn.clicked:
                    obj = {"unit": s.unit, "values": list(s.inputs)}
                    s.store[str(workout_id)] = obj
                    hd.local_storage.set_item(
                        _CUSTOM_SPLITS_LS_KEY, json.dumps(s.store)
                    )
                    s.editing = False
                    on_splits_change(obj)


# ---------------------------------------------------------------------------
# Split recalculation from stroke data
# ---------------------------------------------------------------------------


def _build_interp(strokes: list, workout: dict):
    """Build (interp_time, interp_distance) helpers over a synthetic-extended
    stroke stream.

    A synthetic final entry is appended at (total_distance, total_time) when
    the last real stroke tails short of either total.  This guarantees that
    interp_time(total_distance) == total_time and interp_distance(total_time)
    == total_distance, so split sums reconcile to the workout totals.

    The synthetic sentinel is used for interpolation only; callers that need
    per-stroke aggregations (SPM, HR, watts averages) should iterate the
    original `strokes` list, not these helpers' backing arrays.
    """
    if not strokes:
        return None, None

    total_d_dm = (workout.get("distance") or 0) * 10  # decimeters
    total_t_tenths = workout.get("time") or 0  # tenths of seconds

    last = strokes[-1]
    last_d_dm = last.get("d", 0)
    last_t = last.get("t", 0)
    need_synth = (total_d_dm > 0 and last_d_dm < total_d_dm - 5) or (
        total_t_tenths > 0 and last_t < total_t_tenths - 1
    )

    extended = list(strokes)
    if need_synth:
        synth_d = max(total_d_dm, last_d_dm)
        synth_t = max(total_t_tenths, last_t)
        extended.append({"d": synth_d, "t": synth_t})

    d_m = [s.get("d", 0) / 10.0 for s in extended]
    t_t = [s.get("t", 0) for s in extended]

    def interp_time(target_m: float):
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

    def interp_distance(target_t: float):
        if target_t <= 0:
            return 0.0
        if target_t >= t_t[-1]:
            return float(d_m[-1])
        lo, hi = 0, len(t_t) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if t_t[mid] < target_t:
                lo = mid
            else:
                hi = mid
        span = t_t[hi] - t_t[lo]
        if span <= 0:
            return float(d_m[lo])
        frac = (target_t - t_t[lo]) / span
        return d_m[lo] + frac * (d_m[hi] - d_m[lo])

    return interp_time, interp_distance


def _recalculate_splits(strokes: list, workout: dict, custom_splits) -> list:
    """
    Interpolate stroke data to compute split metrics at custom boundaries.

    `custom_splits` is {"unit": "m"|"s", "values": [int,...]}.  For "m" the
    values are meter-lengths of successive splits; for "s" they are integer
    seconds.  A synthetic final stroke is appended at the workout's reported
    totals so that sum(distance) == total_distance and sum(time) ==
    total_time within ±1 (see _build_interp).

    Returns a list of dicts:
        {distance, time_tenths, pace_tenths, spm, hr_avg, hr_max, max_watts}

    Stroke d is in decimeters; t is in tenths of a second.
    """
    if not strokes or not custom_splits:
        return []
    values = custom_splits.get("values") or []
    if not values:
        return []
    unit = custom_splits.get("unit", "m")

    interp_time, interp_distance = _build_interp(strokes, workout)
    if interp_time is None:
        return []

    # Aggregation windows use the original strokes only so the synthetic
    # sentinel never skews SPM/HR/watts averages.
    d_m_real = [s.get("d", 0) / 10.0 for s in strokes]
    t_t_real = [s.get("t", 0) for s in strokes]

    def strokes_in_d_range(lo_m: float, hi_m: float) -> list:
        return [s for s, dm in zip(strokes, d_m_real) if lo_m <= dm <= hi_m]

    def strokes_in_t_range(lo_t: float, hi_t: float) -> list:
        return [s for s, tt in zip(strokes, t_t_real) if lo_t <= tt <= hi_t]

    result = []
    if unit == "s":
        cumulative_tenths = 0.0
        for dur_s in values:
            t_start_tenths = cumulative_tenths
            t_end_tenths = cumulative_tenths + dur_s * 10
            d_start = interp_distance(t_start_tenths)
            d_end = interp_distance(t_end_tenths)
            dist_m = max(0.0, d_end - d_start)
            dur_tenths = dur_s * 10

            window = strokes_in_t_range(t_start_tenths, t_end_tenths)
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
            cumulative_tenths = t_end_tenths
        return result

    # unit == "m"
    cumulative = 0.0
    for dist_m in values:
        start_m = cumulative
        end_m = cumulative + dist_m
        t_start = interp_time(start_m) or 0.0
        t_end = interp_time(end_m) or 0.0
        dur_tenths = t_end - t_start

        window = strokes_in_d_range(start_m, end_m)
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


def _splits_table(
    workout: dict,
    strokes: Optional[list],
    custom_splits: Optional[dict],
    focused_idx: int = -1,
    on_focus=None,
) -> None:
    """
    Render splits or intervals table.

    Clicking a row calls on_focus(i, row) to zoom the chart to that band.
    focused_idx highlights the currently zoomed row.
    """
    header_color = "neutral-500"
    ts = "small"
    wo = workout.get("workout") or {}
    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES

    if is_interval:
        _intervals_table(
            wo.get("intervals") or [],
            header_color,
            ts,
            focused_idx=focused_idx,
            on_focus=on_focus,
        )
        return

    # For split-based workouts
    splits_data = None
    if custom_splits and strokes:
        splits_data = _recalculate_splits(strokes, workout, custom_splits)
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
    col_w = [2.5, 6, 6, 6, 7, 3.5, 7]
    headers = ["#", "Dist", "Time", "Pace", "Watts", "SPM", "HR"]
    if not has_hr:
        col_w = col_w[:-1]
        headers = headers[:-1]

    _table_frame(
        splits_data,
        col_w,
        headers,
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
        (fmt_distance(round(sp.get("distance"), 1)), col_w[1], None),
        (
            format_time(round(sp.get("time_tenths", 0)))
            if sp.get("time_tenths")
            else "—",
            col_w[2],
            None,
        ),
        (fmt_split(pace_t), col_w[3], None),
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
    header_color: str,
    ts: str,
    focused_idx: int = -1,
    on_focus=None,
) -> None:
    """
    Render interval-workout intervals table.

    Rows and their indices are produced by build_interval_rows_and_bands(),
    which is the single source of truth shared with _build_bands() in
    workout_chart_builder.py.  This guarantees row index i always corresponds
    to band index i for click-to-focus zoom.
    """
    rows, _ = build_interval_rows_and_bands(intervals)

    # Detect HR data across work rows only
    has_hr = any(r.get("hr_avg") for r in rows if not r.get("_is_rest"))

    col_w = [2.5, 6, 6, 6, 5, 3.5]
    headers = ["#", "Dist", "Time", "Pace", "W", "SPM"]
    if has_hr:
        col_w.append(5.5)
        headers.append("HR")

    _table_frame(
        rows,
        col_w,
        headers,
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
    if is_rest and d == 0:
        return hd.text(height=0, border=None)

    cells = [
        (num_str, col_w[0], "neutral-400" if is_rest else "neutral-500"),
        (fmt_distance(d) if d else "—", col_w[1], muted),
        (format_time(t) if t else "—", col_w[2], muted),
        (fmt_split(pace_t), col_w[3], muted),
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
    rows, col_w, headers, header_color, ts, focused_idx, on_focus, row_renderer
):
    """Shared table chrome: header + body rows with click-to-focus.

    Work rows (any row without _is_rest=True) are rendered as hd.link so the
    entire row is clickable; clicking toggles the zoom focus for that band.
    Rest rows are rendered as plain hboxes with no click target.
    """
    border = "1px solid neutral-200"
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
                    background_color=focus_bg if is_focused else None,
                    align="center",
                    padding=(0.35, 0.75, 0.35, 0.75),
                )

                if is_focusable:
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


def _compare_cell(w: dict, state) -> None:
    """Render the Compare checkbox for one similar-workout row.

    Muted "—" when the row has no stroke data (can't draw a line).  The
    checkbox is disabled while Stack mode is active so the two modes stay
    mutually exclusive.
    """
    if not w.get("stroke_data"):
        hd.text("—", font_color="neutral-300", font_size="small")
        return
    wid = w.get("id")
    if wid is None:
        hd.text("—", font_color="neutral-300", font_size="small")
        return
    checked = wid in state.compared_workouts
    cb = hd.checkbox(checked=checked, disabled=state.stack, size="small")
    if cb.changed:
        current = set(state.compared_workouts)
        if cb.checked:
            current.add(wid)
            state.stack = False
        else:
            current.discard(wid)
        state.compared_workouts = tuple(sorted(current))


def _build_compare_series(
    compared_ids: tuple,
    compare_results: dict,
    workouts_dict: dict,
    *,
    show_watts: bool,
) -> list:
    """Turn per-id stroke result dicts into the compare_series list consumed
    by build_stroke_chart_config.  Skips entries that errored or haven't
    resolved yet.  ``compare_results`` maps cid → list[stroke] (possibly
    empty) for resolved entries, or is missing the key while loading.
    """
    if not compared_ids:
        return []
    colors = _interval_colors(len(compared_ids))
    out = []
    for i, cid in enumerate(compared_ids):
        raw = compare_results.get(cid)
        if not raw:
            continue
        cw = workouts_dict.get(str(cid)) or {}
        wtype = cw.get("workout_type", "")
        intervals = (
            (cw.get("workout") or {}).get("intervals")
            if wtype in INTERVAL_WORKOUT_TYPES
            else None
        )
        stitched = _stitch_interval_times(raw, intervals=intervals)
        pace_pts, spm_pts, hr_pts, has_hr = _points_from_strokes(
            stitched, show_watts=show_watts
        )
        date_str = (cw.get("date") or "")[:10]
        dist = cw.get("distance") or 0
        if wtype in INTERVAL_WORKOUT_TYPES:
            suffix = interval_structure_key(cw, compact=True)
        else:
            suffix = fmt_distance(dist) if dist else ""
        label = f"{date_str} · {suffix}".strip(" ·") or f"Workout {cid}"
        total_t_s = (cw.get("time") or 0) / 10.0
        out.append(
            {
                "id": cid,
                "label": label,
                "color": colors[i],
                "pace_points": pace_pts,
                "spm_points": spm_pts,
                "hr_points": hr_pts,
                "has_hr": has_hr,
                "total_time_s": total_t_s,
            }
        )
    return out


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
        ref_pace = pace_tenths(workout)
        pool = []
        for w in all_workouts:
            if w.get("id") == wid or w.get("workout_type") != wtype:
                continue
            d = w.get("distance", 0)
            if ref_dist and d and abs(d - ref_dist) / ref_dist > 0.20:
                continue
            pool.append(w)
        if ref_pace:
            pool.sort(key=lambda w: abs((pace_tenths(w) or 9999) - ref_pace))
        else:
            pool.sort(key=lambda w: w.get("date", ""), reverse=True)

    return pool[:n]


# ---------------------------------------------------------------------------
# Chart controls
# ---------------------------------------------------------------------------


def _chart_controls(
    state,
    can_stack: bool,
    has_hr: bool,
    is_interval: bool,
    has_compares: bool,
) -> None:
    """
    Render the two-row chart control bar and mutate state in place.

    Row 1: Pace/Watts radio · Stack switch (if multi-band; disabled while
           any compares are active) · Reset zoom button
    Row 2: (stacked or compare mode) per-series visibility switches
    """
    with hd.box(gap=0.75, padding_bottom=0.25):
        # Row 1: metric toggle · stack switch · reset zoom
        with hd.hbox(gap=1.5, align="center"):
            with radio_group(value=state.metric, size="small") as rg:
                hd.radio_button("Pace", value="pace")
                hd.radio_button("Watts", value="watts", size="small")
            if rg.changed:
                state.metric = rg.value

            if can_stack:
                if is_interval:
                    stack_lbl = "Stack intervals"
                else:
                    stack_lbl = "Stack splits"
                stack_sw = hd.switch(
                    stack_lbl,
                    checked=state.stack,
                    size="small",
                    disabled=has_compares,
                )
                if stack_sw.changed:
                    state.stack = stack_sw.checked
                    if stack_sw.checked:
                        state.focused_interval = None
                        state.focused_interval_excluding_rest = None

            if not state.stack and state.focused_interval is not None:
                reset_btn = hd.button("Reset zoom", variant="neutral", size="small")
                if reset_btn.clicked:
                    state.focused_interval = None
                    state.focused_interval_excluding_rest = None

        # Row 2: per-series visibility toggles (stacked mode or compare mode)
        if state.stack or has_compares:
            with hd.hbox(gap=1.5, align="center"):
                metric_label = "Watts" if state.metric == "watts" else "Pace"
                pace_sw = hd.switch(metric_label, checked=state.show_pace, size="small")
                if pace_sw.changed:
                    state.show_pace = pace_sw.checked

                spm_sw = hd.switch("SPM", checked=state.show_spm, size="small")
                if spm_sw.changed:
                    state.show_spm = spm_sw.checked

                if has_hr:
                    hr_sw = hd.switch("HR", checked=state.show_hr, size="small")
                    if hr_sw.changed:
                        state.show_hr = hr_sw.checked


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def workout_page(session_id: int, ctx, global_state) -> None:
    """Render the full-screen workout detail overlay."""
    _theme = hd.theme()

    state = hd.state(
        metric="pace",  # "pace" | "watts"
        focused_interval=None,  # int | None  (raw band index)
        focused_interval_excluding_rest=None,  # int | None  (1-based work interval #)
        custom_splits=None,  # {"unit": "m"|"s", "values": [int,...]} | None
        stack=False,  # stacked-intervals overlay mode
        show_pace=True,  # show pace/watts in stacked / compare mode
        show_spm=False,  # show SPM in stacked / compare mode
        show_hr=False,  # show HR in stacked / compare mode
        compared_workouts=(),  # tuple[int,...] of other workout ids to overlay
        last_click_seq=0,  # last chart.click_seq we've processed
    )

    # ── Pre-fetch workout list (task-cached; free on repeat renders) ────────
    sync_result = sync_from_context(ctx)
    if sync_result is None:
        hd.box(padding=2, min_height="80vh")
        return

    _workouts_dict, all_workouts = sync_result
    workout = _workouts_dict.get(str(session_id))

    # ── Fetch stroke data (unified via concept2_sync.strokes_for) ────────────

    has_strokes = bool(workout.get("stroke_data"))
    wtype = workout.get("workout_type", "")
    is_interval = wtype in INTERVAL_WORKOUT_TYPES

    stroke_result = strokes_for(ctx, workout)
    stroke_status = stroke_result["status"]
    stroke_error = stroke_result["error"]
    strokes = stroke_result["strokes"]

    # ── Fetch strokes for each compared workout ───────────────────────────────

    compare_results: dict = {}
    compare_loading = False
    for cid in state.compared_workouts:
        cw = _workouts_dict.get(str(cid)) or {"id": cid, "stroke_data": True}
        cr = strokes_for(ctx, cw)
        if cr["status"] == "loaded":
            compare_results[cid] = cr["strokes"] or []
        elif cr["status"] == "loading":
            compare_loading = True

    # ── Title ────────────────────────────────────────────────────────────────

    if is_interval:
        ivs = (workout.get("workout") or {}).get("intervals") or []
        work_ivs = [iv for iv in ivs if (iv.get("type") or "").lower() != "rest"]
        reps = len(work_ivs) or len(ivs)
        title = interval_structure_key(workout, compact=True)
        if not workout.get("workout_type", "") == "VariableInterval":
            title = f"{reps} x {title}"
    else:
        title = fmt_distance_label(workout)

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on_split_focus(idx, row):
        state.focused_interval = idx
        if idx is None:
            state.focused_interval_excluding_rest = None
        else:
            state.focused_interval_excluding_rest = row.get("_work_idx", 0) + 1

    # ── Layout ───────────────────────────────────────────────────────────────

    total_dist = workout.get("distance") or 0
    total_time_tenths = workout.get("time") or 0
    is_time_based = wtype in _TIME_BASED_WORKOUT_TYPES
    show_custom = (
        has_strokes
        and not is_interval
        and (total_dist > 0 or (is_time_based and total_time_tenths > 0))
    )

    with hd.box(padding=(1, 2, 0, 4), gap=3, align="center", min_height="80vh"):
        with hd.hbox(gap=4, align="center", justify="end"):
            # ── Header ───────────────────────────────────────────────────────

            with hd.box(padding_top=1, gap=0, align="start"):
                hd.text(fmt_date(workout.get("date", "")), font_color="neutral-500")
                hd.text(title, font_weight="bold", font_size="2x-large")

                if workout.get("comments"):
                    with hd.hbox(gap=0.25):
                        hd.icon("quote", font_color="neutral-500")
                        hd.text(
                            workout["comments"],
                            font_color="neutral-500",
                            font_size="medium",
                        )
                        hd.text('"')

            # ── Summary stats ─────────────────────────────────────────────────

            _summary_section(workout, strokes)

        # ── Chart + Splits side by side ───────────────────────────────────

        with hd.hbox(gap=2, align="start", grow=True, width="100%"):
            # Left: chart
            with hd.box(gap=1, grow=True, min_width=0):
                if state.focused_interval is not None:
                    band_type = "Interval" if is_interval else "Split"
                    graph_title = f"Workout Graph: {band_type} {state.focused_interval_excluding_rest}"
                else:
                    graph_title = "Workout Graph"
                hd.h2(
                    graph_title,
                    font_weight="semibold",
                    font_size="x-large",
                    font_color="neutral-800",
                )

                if stroke_status == "no_strokes":
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
                elif stroke_status == "uncached":
                    with hd.box(
                        padding=2,
                        align="center",
                        border_radius="medium",
                        background_color="neutral-100"
                        if not _theme.is_dark
                        else "neutral-800",
                        height=18,
                        justify="center",
                        gap=0.5,
                    ):
                        hd.text(
                            "Stroke-level data for this session is not yet available.",
                            font_color="neutral-500",
                            text_align="center",
                        )
                        hd.text(
                            "It appears after the owner opens this session.",
                            font_color="neutral-400",
                            font_size="small",
                            text_align="center",
                        )
                elif stroke_status == "loading":
                    with hd.box(padding=2, align="center", height=18, justify="center"):
                        hd.spinner()
                        hd.text("Loading…", font_color="neutral-500", font_size="small")
                elif stroke_status == "error":
                    hd.alert(
                        f"Could not load stroke data: {stroke_error}",
                        variant="warning",
                        opened=True,
                    )
                elif strokes:
                    has_hr = any(s.get("hr") for s in strokes)
                    can_stack = is_interval or bool(
                        (workout.get("workout") or {}).get("splits")
                        or state.custom_splits
                    )
                    has_compares = bool(state.compared_workouts)
                    compare_series = (
                        _build_compare_series(
                            state.compared_workouts,
                            compare_results,
                            _workouts_dict,
                            show_watts=(state.metric == "watts"),
                        )
                        if has_compares and not state.stack
                        else None
                    )

                    with hd.scope("chart"):
                        cfg = build_stroke_chart_config(
                            strokes,
                            workout,
                            metric=state.metric,
                            focused_interval_idx=(
                                None if state.stack else state.focused_interval
                            ),
                            is_dark=_theme.is_dark,
                            stack=state.stack,
                            show_pace=state.show_pace,
                            show_spm=state.show_spm,
                            show_hr=state.show_hr,
                            custom_splits=state.custom_splits,
                            compare_series=compare_series,
                        )
                        chart = StrokeChart(config=cfg, height="50vh")
                        # Fire only on *new* clicks — the plugin's
                        # clicked_band_idx prop keeps its last value across
                        # renders, so we key off a monotonic seq counter
                        # instead.  Without this, Reset zoom would re-focus
                        # the stale band on the next render.
                        if (
                            not state.stack
                            and chart.click_seq > state.last_click_seq
                            and chart.clicked_band_idx >= 0
                        ):
                            state.focused_interval = chart.clicked_band_idx
                            state.last_click_seq = chart.click_seq

                    if compare_loading:
                        with hd.hbox(gap=0.5, align="center"):
                            hd.spinner()
                            hd.text(
                                "Loading compare data…",
                                font_color="neutral-500",
                                font_size="x-small",
                            )

                    _chart_controls(state, can_stack, has_hr, is_interval, has_compares)

                else:
                    hd.text(
                        "No stroke data returned.",
                        font_color="neutral-500",
                        font_size="small",
                    )

            # Right: splits/intervals table + custom splits editor
            with hd.box(gap=0.75):
                with hd.hbox(gap=0.5):
                    hd.h2(
                        "Intervals" if is_interval else "Splits",
                        font_weight="semibold",
                        font_size="x-large",
                        font_color="neutral-800",
                    )
                    if show_custom:
                        _custom_splits_ui(
                            workout=workout,
                            strokes=strokes or [],
                            on_splits_change=lambda obj: setattr(
                                state, "custom_splits", obj
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

        # ── Similar sessions ─────────────────────────────────────────────

        similar = _find_similar(workout, all_workouts)
        if similar:
            with hd.box(align="center"):
                hd.h2(
                    "Similar sessions",
                    font_weight="semibold",
                    font_size="x-large",
                    font_color="neutral-800",
                )
                is_interval_workout = (
                    workout.get("workout_type", "") in INTERVAL_WORKOUT_TYPES
                )
                compare_col = ColumnDef(
                    key="compare",
                    header="Compare",
                    width="5.5rem",
                    render_value=lambda w: "",
                    render_cell=lambda w: _compare_cell(w, state),
                    sortable=False,
                )
                if is_interval_workout:
                    # Similar sessions are mostly intervals — show structure column
                    workout_col = ColumnDef(
                        "workout_structure",
                        "Workout",
                        "minmax(8rem,1fr)",
                        render_value=lambda w: (
                            interval_structure_key(w, compact=True)
                            if w.get("workout_type", "") in INTERVAL_WORKOUT_TYPES
                            else ""
                        ),
                    )
                    cols = [
                        COL_DATE,
                        workout_col,
                        COL_DISTANCE,
                        COL_TIME,
                        COL_PACE,
                        COL_WATTS,
                        COL_SPM,
                        COL_HR,
                        compare_col,
                        COL_LINK,
                    ]
                else:
                    # Non-interval: show standard performance columns
                    cols = [
                        COL_DATE,
                        COL_DISTANCE,
                        COL_TIME,
                        COL_PACE,
                        COL_WATTS,
                        COL_DRAG,
                        COL_SPM,
                        COL_HR,
                        compare_col,
                        COL_LINK,
                    ]
                WorkoutTable(similar, cols)
