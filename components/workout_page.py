"""
components/workout_page.py — Full-screen workout detail overlay.

Activated by URL routing: app.py renders this component when
loc.path starts with "/workout/".  The view icon in every result table
navigates to /workout/{id}; the Back link returns to the previous tab.

Displays:

  1. Header bar      — date/machine/type title (with workout comment if present)
  2. Summary stats   — compact multi-column metric grid
  3. Chart + splits  — pace/watts chart (left) beside splits/intervals table (right)
                       Chart has Pace/Watts toggle, Stack mode, and Reset zoom button.
                       Clicking a split/interval row zooms the chart to that band.
  4. Similar workouts — WorkoutTable() of workouts with matching structure

Entry point::

    workout_page(workout_id, client, user_id)

    workout_id  int   — extracted from loc.path ("/workout/<id>")
    client      Concept2Client
    user_id     str

Workout data and the full list are fetched via concept2_sync(), which is
task-cached so repeated calls within a render cycle are free.
"""

from __future__ import annotations

import json
import os
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
    render_spread_cell,
)
from components.app_context import AppContext, get_profile
from services.heartrate_utils import (
    HR_ZONE_COLORS,
    HR_ZONE_NAMES,
    resolve_max_hr,
)
from services.volume_bins import BAND_TO_BIN, BIN_COLORS, BIN_NAMES
from services.erg_stress import (
    SEVERITY_DEFINITION_TEXT,
    SEVERITY_FILTER_TEXT,
    SEVERITY_ORDER,
    SEVERITY_STYLE,
    SEVERITY_THRESHOLDS,
    ZONE_BANDS_S,
    stimulus_category_label,
)
from components.spread_quality_legends import legend_chip
from components.add_metrics import add_metrics

from components.workout_chart_builder import (
    build_interval_rows_and_bands,
    build_stroke_chart_config,
    build_compare_series,
    _interval_colors,
    _points_from_strokes,
    _stitch_interval_times,
)
from components.workout_chart_plugin import StrokeChart
from components.ess_chart_plugin import EffortStressChart
from components.ess_chart_builder import build_effort_stress_chart_config
from services.rowing_utils import compute_watts
from services.splits import (
    TIME_BASED_WORKOUT_TYPES,
    recalculate_interval_sub_splits,
    recalculate_splits,
)
from components.workout_splits_modal import (
    normalize_entry,
    render_ranked_events_modal,
    render_splits_modal,
)

from components.hyperdiv_extensions import radio_group, blockquote
from components.concept2_sync import sync_workouts, strokes_for
from components import indexed_db
from components.indexed_db import SESSIONS_STORE, WORKOUTS_STORE
from services import time_overrides
from services.sessions import assign_sessions_incremental
from services.workout_enrichment import enrich_for_storage


# ---------------------------------------------------------------------------
# Summary stat grid
# ---------------------------------------------------------------------------


def _stat(label: str, value: str, tooltip: str | None = None) -> None:
    """One stat cell: small muted label above bold value, optional tooltip."""
    if tooltip:
        with hd.tooltip(tooltip):
            with hd.box(padding=(0.5, 1.25, 0.5, 1.25)):
                hd.text(
                    label,
                    font_size="small",
                    font_color="neutral-500",
                    font_weight="semibold",
                )
                hd.text(value, font_weight="bold", font_size="large")
        return
    with hd.box(padding=(0.5, 1.25, 0.5, 1.25)):
        hd.text(
            label,
            font_size="small",
            font_color="neutral-500",
            font_weight="semibold",
        )
        hd.text(value, font_weight="bold", font_size="large")


def _spread_stat(label: str, render_inner) -> None:
    """One spread-style stat cell — small label above a render-cell payload."""
    with hd.box(padding=(0.5, 1.25, 0.5, 1.25), gap=0.25):
        hd.text(
            label,
            font_size="small",
            font_color="neutral-500",
            font_weight="semibold",
        )
        render_inner()


def _safe_for_json(v):
    """Recursively coerce a workout dict's value into JSON-serialisable form.

    Drops keys starting with ``_`` (render-time enrichments — SVG bar URIs,
    full timeline arrays — that bloat the file without adding diagnostic
    value).  Anything else (including ``date_dt``) is preserved; falls back
    to ``str(v)`` for non-serialisable leaves.
    """
    if isinstance(v, dict):
        return {
            k: _safe_for_json(x) for k, x in v.items() if not str(k).startswith("_")
        }
    if isinstance(v, (list, tuple)):
        return [_safe_for_json(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _dump_session_to_tmp(session_workouts: list, current_id) -> str:
    """Write every workout in a same-day session to ``tmp/session-<id>/``.

    Filenames encode the workout's local time-of-day, distance, watts, and
    id so they sort chronologically and tell you what they are at a glance:
    ``HH-MM-SS_<distance>m_<watts>W_<id>.json``.

    The directory is named after the *current* workout's id (the page the
    user is on when they click the button) so multiple workouts can sit
    side-by-side without colliding.

    Returns the directory path so callers can surface it in the UI.
    """
    sid = current_id if current_id is not None else "unknown"
    dirpath = os.path.join("tmp", f"session-{sid}")
    os.makedirs(dirpath, exist_ok=True)

    for w in session_workouts:
        date_str = w.get("date") or ""
        # ``date`` is "YYYY-MM-DD HH:MM:SS"; pull the time portion as HH-MM-SS.
        time_part = (
            date_str[11:19].replace(":", "-") if len(date_str) >= 19 else "unknown"
        )
        dist_m = int(w.get("distance") or 0)
        watts = int(w.get("watts") or 0)
        wid = w.get("id") or "unknown"
        filename = f"{time_part}_{dist_m}m_{watts}W_{wid}.json"
        path = os.path.join(dirpath, filename)
        payload = {
            k: _safe_for_json(v) for k, v in w.items() if not str(k).startswith("_")
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)

    return dirpath


def _summary_section(workout: dict, strokes: Optional[list]) -> None:
    """Compact multi-column stat grid."""
    is_interval = workout["is_interval"]
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

    is_dark = hd.theme().is_dark
    has_intensity = (
        workout.get("_if_eff") is not None
        and workout.get("_zone_bin_fractions") is not None
    )
    has_hr_spread = workout.get("_hr_spread_score") is not None
    has_ess = workout.get("_ess") is not None

    with hd.box(grow=True, gap=0.25):
        # ── Top row: Intensity (with Zone Spread bar), HR Spread ─────────

        # ── ESS row: Severity + Reservoirs (W' Used, Glycogen Used) ────
        # The two reservoirs sit adjacent so the limiter story is legible
        # at a glance: W' Used dominates short max efforts (2k, sprints),
        # Glycogen Used dominates long endurance efforts (HM, marathon).
        # Training Stimulus follows: which physiological systems received
        # an adaptation-grade dose from this workout.
        with hd.hbox(wrap="wrap", gap=0):
            # if has_intensity:
            #     _spread_stat(
            #         "Intensity",
            #         lambda: render_spread_cell(
            #             score=round((workout.get("_if_eff") or 0.0) * 100),
            #             _bin_meters=workout.get("_zone_bin_fractions"),
            #             zone_names=BIN_NAMES,
            #             zone_colors=BIN_COLORS,
            #             is_dark=is_dark,
            #             skip_indices=(0,),
            #             show_meters=False,
            #         ),
            #     )
            if has_hr_spread:
                _spread_stat(
                    "HR Spread",
                    lambda: render_spread_cell(
                        score=workout.get("_hr_spread_score"),
                        _bin_meters=workout.get("_hr_bin_meters"),
                        zone_names=HR_ZONE_NAMES,
                        zone_colors=HR_ZONE_COLORS,
                        is_dark=is_dark,
                        skip_indices=(0, 6),
                    ),
                )
            if avg_watts:
                _spread_stat(
                    "Avg. Watts",
                    lambda: render_spread_cell(
                        score=avg_watts,
                        _bin_meters=workout.get("_zone_bin_fractions"),
                        zone_names=BIN_NAMES,
                        zone_colors=BIN_COLORS,
                        is_dark=is_dark,
                        skip_indices=(0,),
                        show_meters=False,
                    ),
                ),

            if max_w is not None:
                _stat("Max Watts", f"{round(max_w)} W")

            if has_ess:
                if workout.get("_severity"):
                    _stat(
                        "Severity",
                        workout["_severity"],
                        tooltip=(
                            "How hard this workout was on your body, "
                            "combining peak intensity, anaerobic strain, "
                            "and fuel cost. The Low / Moderate / High / "
                            "Maximal buckets are rough guides — calibrate "
                            "them against how your legs feel the day after."
                        ),
                    )
                # if workout.get("_anaerobic_strain") is not None:
                #     _stat(
                #         "W' Used",
                #         f"{round(workout['_anaerobic_strain'] * 100)}%",
                #     )
                # if workout.get("_glycogen_used") is not None:
                #     gly = workout["_glycogen_used"]
                #     gly_warn = " ⚠" if gly > 1 else ""
                #     _stat("Glycogen Used", f"{round(gly * 100)}%{gly_warn}")
                stim_doses = workout.get("_stimulus_doses")
                if stim_doses is not None:
                    parts: list[str] = []
                    for d in ZONE_BANDS_S:
                        dose = float(
                            stim_doses.get(d, stim_doses.get(int(d), 0.0)) or 0.0
                        )
                        label = stimulus_category_label(dose)
                        if label is not None:
                            parts.append(f"{BIN_NAMES[BAND_TO_BIN[d]]}")
                    _stat(
                        "Stimulated",
                        ", ".join(parts) if parts else "—",
                        tooltip=(
                            "Estimates whether you spent enough time near "
                            "your reference power for this duration to "
                            "count as a real training stimulus for that "
                            "system. The threshold for each band is set "
                            "where a sustained effort at that target "
                            "power would saturate the curve."
                        ),
                    )

        with hd.hbox(wrap="wrap", gap=0):
            if workout.get("distance"):
                _stat("Distance", fmt_distance(workout["distance"]))
            if workout.get("time"):
                _stat("Time", format_time(workout["time"]))
            if pace_sec:
                _stat("Pace", fmt_split(pace))
            if workout.get("stroke_rate"):
                _stat("SPM", str(workout["stroke_rate"]))
            if workout.get("stroke_count"):
                _stat("Strokes", str(workout["stroke_count"]))
            if workout.get("drag_factor"):
                _stat("Drag", str(workout["drag_factor"]))
            if is_interval:
                if rest_dist:
                    _stat("Rest Distance", fmt_distance(rest_dist))
                # if rest_time:
                #     _stat("Rest Time", format_time(rest_time))
            if hr_data.get("average"):
                _stat("Avg. HR", f"{hr_data['average']} bpm")
            if hr_data.get("max"):
                _stat("Max HR", f"{hr_data['max']} bpm")


# ---------------------------------------------------------------------------
# Custom splits — localStorage key
# ---------------------------------------------------------------------------
#
# Persisted shape (per workout_id), managed by the splits modal:
#     {
#       "<id>": {
#         "splits": {"values": [{"u": "m"|"s", "v": int}, ...]},
#         "interval_sub": {...}    # Phase 3
#       }
#     }

_CUSTOM_SPLITS_LS_KEY = "custom_splits"


# ---------------------------------------------------------------------------
# Splits / intervals table
# ---------------------------------------------------------------------------


_PACE_DELTA_KIND_COLORS = {
    "faster": "success-600",
    "slower": "danger-600",
    "same": "neutral-400",
}


def _pace_delta_inline(curr_t, prior_t):
    """Return ``(delta_text, color)`` for pace cells, or ``None`` when no
    prior pace is available."""
    from services.formatters import fmt_pace_delta_tenths

    text, kind = fmt_pace_delta_tenths(curr_t, prior_t)
    if not text:
        return None
    return text, _PACE_DELTA_KIND_COLORS.get(kind, "neutral-500")


def _render_cells(cells, ts):
    """Render a row's cells.

    Each cell is a 4-tuple ``(val, width, color, inline_extra)``.  When
    ``inline_extra`` is non-None it's a ``(text, color)`` pair rendered
    next to ``val`` inside an ``hd.hbox`` of the cell's width — used for
    the pace-vs-prior delta annotation.  Otherwise the cell renders as a
    plain ``hd.text``.
    """
    for idx, cell in enumerate(cells):
        val, w, color, extra = cell
        with hd.scope(f"{idx}"):
            if extra is None:
                kwargs = {"font_size": ts, "width": w}
                if color:
                    kwargs["font_color"] = color
                hd.text(val, **kwargs)
            else:
                extra_text, extra_color = extra
                with hd.hbox(width=w, gap=0.25):
                    txt_kwargs = {"font_size": ts}
                    if color:
                        txt_kwargs["font_color"] = color
                    hd.text(val, **txt_kwargs)
                    hd.text(
                        extra_text,
                        font_size="x-small",
                        font_color=extra_color,
                    )


def _splits_table(
    workout: dict,
    strokes: Optional[list],
    custom_splits: Optional[list],
    focused_idx: int = -1,
    on_focus=None,
    interval_sub: Optional[dict] = None,
    expanded_intervals: Optional[tuple] = None,
    on_interval_expand=None,
    prior_pace_by_idx: Optional[list] = None,
) -> None:
    """
    Render splits or intervals table.

    ``custom_splits`` is the mixed-unit chip list ``[{u,v},...]`` from
    ``state.custom_splits`` (or ``None`` when no custom splits are applied).

    For interval workouts, ``interval_sub`` carries the sub-split spec
    (e.g. ``{"mode": "n_pieces", "n": 4}``) and ``expanded_intervals`` is the
    set of band indices currently expanded.  ``on_interval_expand(i, opening)``
    toggles the expansion of band index ``i``.

    Clicking a row calls ``on_focus(i, row)`` to zoom the chart to that
    band; ``focused_idx`` highlights the currently zoomed row.
    """
    header_color = "neutral-500"
    ts = "small"
    wo = workout.get("workout") or {}
    is_interval = workout["is_interval"]

    if is_interval:
        _intervals_table(
            wo.get("intervals") or [],
            header_color,
            ts,
            focused_idx=focused_idx,
            on_focus=on_focus,
            strokes=strokes,
            ess_segments=workout.get("_ess_segments"),
            workout=workout,
            interval_sub=interval_sub,
            expanded=expanded_intervals,
            on_expand=on_interval_expand,
            prior_pace_by_work_idx=prior_pace_by_idx,
        )
        return

    # For split-based workouts
    splits_data = None
    if custom_splits and strokes:
        splits_data = recalculate_splits(strokes, workout, custom_splits)
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
    ess_segments = workout.get("_ess_segments") or []
    has_if = bool(ess_segments)
    col_w = [2.5, 6, 6, 6, 7, 3.5, 7]
    headers = ["#", "Dist", "Time", "Pace", "Watts", "SPM", "HR"]
    if not has_hr:
        col_w = col_w[:-1]
        headers = headers[:-1]

    def _prior_at(i):
        if not prior_pace_by_idx or i >= len(prior_pace_by_idx):
            return None
        return prior_pace_by_idx[i]

    _table_frame(
        splits_data,
        col_w,
        headers,
        header_color,
        ts,
        focused_idx=focused_idx,
        on_focus=on_focus,
        row_renderer=lambda i, sp, cw: _split_row(
            i,
            sp,
            cw,
            ts,
            has_hr,
            ess_segments if has_if else None,
            prior_pace_t=_prior_at(i),
        ),
    )


def _split_row(i, sp, col_w, ts, has_hr, ess_segments=None, *, prior_pace_t=None):
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

    pace_delta = _pace_delta_inline(pace_t, prior_pace_t)
    cells = [
        (str(i + 1), col_w[0], "neutral-500", None),
        (fmt_distance(int(round(sp.get("distance") or 0))), col_w[1], None, None),
        (
            format_time(round(sp.get("time_tenths", 0)))
            if sp.get("time_tenths")
            else "—",
            col_w[2],
            None,
            None,
        ),
        (fmt_split(pace_t), col_w[3], None, pace_delta),
        (watts_str, col_w[4], None, None),
        (f"{spm:.0f}" if spm else "—", col_w[5], None, None),
    ]
    if has_hr:
        cells.append((hr_str, col_w[6], None, None))

    _render_cells(cells, ts)


def _intervals_table(
    intervals: list,
    header_color: str,
    ts: str,
    focused_idx: int = -1,
    on_focus=None,
    strokes: Optional[list] = None,
    ess_segments: Optional[list] = None,
    workout: Optional[dict] = None,
    interval_sub: Optional[dict] = None,
    expanded: Optional[tuple] = None,
    on_expand=None,
    prior_pace_by_work_idx: Optional[list] = None,
) -> None:
    """
    Render interval-workout intervals table.

    Rows and their indices are produced by build_interval_rows_and_bands(),
    which is the single source of truth shared with _build_bands() in
    workout_chart_builder.py.  This guarantees row index i always corresponds
    to band index i for click-to-focus zoom.

    When ``interval_sub`` is provided, each work row gets a chevron and
    expands inline to show its sub-splits (computed via
    ``recalculate_interval_sub_splits``).  Sub-rows are non-focusable and
    do not shift the parent row index.
    """
    rows, _ = build_interval_rows_and_bands(
        intervals, strokes, workout_id=(workout or {}).get("id")
    )

    # Detect HR data across work rows only
    has_hr = any(r.get("hr_avg") for r in rows if not r.get("_is_rest"))
    has_if = bool(ess_segments)

    col_w = [2.5, 6, 6, 6, 5, 3.5]
    headers = ["#", "Dist", "Time", "Pace", "W", "SPM"]
    if has_hr:
        col_w.append(5.5)
        headers.append("HR")

    # Compute sub-splits per band when a spec is supplied.  The returned
    # list is aligned with ``rows`` (work entries carry sub-rows, rest
    # entries carry []).
    children = None
    if interval_sub and strokes and workout is not None:
        sub_lists = recalculate_interval_sub_splits(
            strokes, workout, intervals, interval_sub
        )
        # Pad to len(rows) defensively (rest-segment indices match []).
        children = list(sub_lists) + [[]] * max(0, len(rows) - len(sub_lists))

    def _prior_for_row(r):
        if not prior_pace_by_work_idx or r.get("_is_rest"):
            return None
        wi = r.get("_work_idx")
        if wi is None or wi >= len(prior_pace_by_work_idx):
            return None
        return prior_pace_by_work_idx[wi]

    _table_frame(
        rows,
        col_w,
        headers,
        header_color,
        ts,
        focused_idx=focused_idx,
        on_focus=on_focus,
        row_renderer=lambda i, r, cw: _interval_row(
            i,
            r,
            cw,
            ts,
            has_hr,
            ess_segments if False and has_if else None,
            prior_pace_t=_prior_for_row(r),
        ),
        children=children,
        child_renderer=(
            (lambda parent_i, sub, cw: _interval_sub_row(parent_i, sub, cw, ts, has_hr))
            if children
            else None
        ),
        expanded=expanded,
        on_expand=on_expand,
    )


def _interval_sub_row(parent_i, sub, col_w, ts, has_hr):
    """Render one sub-split row indented under its parent interval row.

    Uses the same column widths as the parent so cells line up under the
    parent's columns.  No focus indicator, no chevron, smaller / muted font.
    """
    pace_t = sub.get("pace_tenths")
    avg_w = round(compute_watts(pace_t / 10.0)) if pace_t else None
    hr_avg = sub.get("hr_avg")
    spm = sub.get("spm")
    dist = sub.get("distance") or 0
    t = sub.get("time_tenths") or 0

    cells = [
        ("·", col_w[0], "neutral-400"),
        (fmt_distance(int(round(dist))) if dist else "—", col_w[1], "neutral-500"),
        (format_time(int(round(t))) if t else "—", col_w[2], "neutral-500"),
        (fmt_split(pace_t), col_w[3], "neutral-500"),
        (str(avg_w) if avg_w is not None else "—", col_w[4], "neutral-500"),
        (f"{spm:.0f}" if spm else "—", col_w[5], "neutral-500"),
    ]
    if has_hr:
        cells.append((f"{hr_avg:.0f}" if hr_avg else "—", col_w[6], "neutral-500"))

    for idx, (val, w, color) in enumerate(cells):
        with hd.scope(f"sub_c_{idx}"):
            kwargs = {"font_size": "x-small", "width": w}
            if color:
                kwargs["font_color"] = color
            hd.text(val, **kwargs)


def _interval_row(i, r, col_w, ts, has_hr, ess_segments=None, *, prior_pace_t=None):
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

    hr_col_idx = 6 if has_hr else None

    pace_delta = _pace_delta_inline(pace_t, prior_pace_t)
    cells = [
        (num_str, col_w[0], "neutral-400" if is_rest else "neutral-500", None),
        (fmt_distance(d) if d else "—", col_w[1], muted, None),
        (format_time(t) if t else "—", col_w[2], muted, None),
        (fmt_split(pace_t), col_w[3], muted, pace_delta),
        (
            str(r["avg_watts"]) if r.get("avg_watts") is not None else "",
            col_w[4],
            muted,
            None,
        ),
        (str(spm) if spm else "", col_w[5], muted, None),
    ]
    if has_hr:
        cells.append((f"{hr:.0f}" if hr else "", col_w[6], muted, None))
    if ess_segments is not None:
        seg = ess_segments[i] if i < len(ess_segments) else None
        if_eff = seg.get("IF_eff_avg") if seg else None
        cells.append((f"{if_eff:.2f}" if if_eff else "—", col_w[-1], muted, None))

    _render_cells(cells, ts)


def _table_frame(
    rows,
    col_w,
    headers,
    header_color,
    ts,
    focused_idx,
    on_focus,
    row_renderer,
    *,
    children=None,
    child_renderer=None,
    expanded=None,
    on_expand=None,
):
    """Shared table chrome: header + body rows with click-to-focus.

    Work rows (any row without _is_rest=True) are rendered as hd.link so the
    entire row is clickable; clicking toggles the zoom focus for that band.
    Rest rows are rendered as plain hboxes with no click target.

    Optional expansion support (used by intervals + sub-splits):

      ``children``   : list aligned with ``rows`` where each element is either
                       a list of sub-row dicts or ``None``/``[]`` (no children).
      ``child_renderer(parent_idx, sub_row, col_w)`` : renders a sub-row.  It
                       receives the *outer* column widths so cells can align.
      ``expanded``   : iterable/set of parent indices currently expanded.
      ``on_expand(idx, opening: bool)`` : toggle callback.  When provided, a
                       chevron icon is shown at the start of rows that have
                       children.

    Sub-rows are not focusable and don't participate in ``focused_idx``
    accounting — band-index ↔ row-index alignment is preserved.
    """
    border = "1px solid neutral-200"
    focus_bg = "primary-50"
    expanded_set = set(expanded or ())

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
                row_children = (
                    children[i] if children and i < len(children) else None
                ) or []
                has_children = bool(row_children) and child_renderer is not None
                is_expanded = has_children and i in expanded_set

                row_kwargs = dict(
                    gap=0.5,
                    background_color=focus_bg if is_focused else None,
                    align="center",
                    padding=(0.35, 0.75, 0.35, 0.75),
                )

                if is_focusable:
                    with hd.hbox(
                        gap=0,
                        align="center",
                        background_color=focus_bg if is_focused else None,
                    ):
                        # Chevron column (only when row has children).
                        if has_children and on_expand is not None:
                            chev_icon = (
                                "chevron-down" if is_expanded else "chevron-right"
                            )
                            with hd.scope("chev"):
                                chev_btn = hd.icon_button(
                                    chev_icon,
                                    font_size="x-small",
                                    font_color="neutral-500",
                                )
                                if chev_btn.clicked:
                                    on_expand(i, not is_expanded)
                        elif has_children:
                            # Reserve width even if expand callback is missing
                            # so columns stay aligned.
                            hd.box(width=1.5)
                        with hd.link(
                            href="#",
                            target="_self",
                            direction="horizontal",
                            font_color="neutral-700",
                            underline=False,
                            hover_background_color="neutral-50",
                            grow=True,
                            **row_kwargs,
                        ) as row_el:
                            row_renderer(i, row, col_w)
                        if row_el.clicked:
                            on_focus(None if is_focused else i, row)
                else:
                    with hd.hbox(gap=0, align="center"):
                        if has_children:
                            hd.box(width=1.5)
                        with hd.hbox(grow=True, **row_kwargs):
                            row_renderer(i, row, col_w)

                if is_expanded:
                    with hd.box(
                        gap=0,
                        padding=(0, 0, 0.2, 2.5),
                        background_color="neutral-50",
                        border_bottom=border,
                    ):
                        for j, sub in enumerate(row_children):
                            with hd.scope(f"sub_{j}"):
                                with hd.hbox(
                                    padding=(0.25, 0.75, 0.25, 0.75),
                                    gap=0.5,
                                    align="center",
                                ):
                                    child_renderer(i, sub, col_w)


# ---------------------------------------------------------------------------
# Similar workouts
# ---------------------------------------------------------------------------


def _interval_dimension(w: dict) -> str:
    """Return 'time' or 'distance' based on the first work interval's type;
    'unknown' if no work intervals."""
    ivs = (w.get("workout") or {}).get("intervals") or []
    for iv in ivs:
        t = (iv["type"]).lower()
        if t in ("time", "distance"):
            return t
    return "unknown"


# Work fraction >= this counts as "continuous-like": an interval workout with
# essentially no programmed rest, structurally comparable to a non-interval
# piece. 0.99 allows encoding noise (e.g. a 1s gap between back-to-back work
# intervals) but excludes any real rest (5×4'/10" rest is ~0.96 and NOT
# continuous).
CONTINUOUS_LIKE_THRESHOLD = 0.99

# Maximum cross-type similarity score. Cross-type pairs scale linearly up to
# this value (rather than 100) so they sit below typical same-type scores —
# the structural axes that don't apply across types (work fraction, rep count,
# distance-per-interval) are missing signal, and the ceiling acknowledges
# that. Scaling (rather than clamping) preserves relative ordering near the
# top, so a perfect-pace cross-type match still ranks above a near-miss.
CROSS_TYPE_SCORE_CEILING = 75.0


def _is_continuous_like(w: dict) -> bool:
    """True for non-interval workouts, or interval workouts with no real rest."""
    if not w.get("is_interval"):
        return True
    _, wf = _interval_volume_and_work_fraction(w)
    return wf >= CONTINUOUS_LIKE_THRESHOLD


def _interval_volume_and_work_fraction(w: dict) -> tuple:
    """Return (total_work_volume, work_fraction).

    Volume is total work distance (m) for distance-dim workouts, or total work
    time (tenths) for time-dim workouts.

    work_fraction = total_work_time / (total_work_time + total_rest_time),
    in [0, 1]. Captures the work:rest character — 0.5 means 1:1 work:rest,
    0.2 means 1:4, 1.0 means continuous (no rest).
    """
    ivs = (w.get("workout") or {}).get("intervals") or []
    work_ivs = [iv for iv in ivs if (iv["type"]).lower() != "rest"]
    if not work_ivs:
        return 0, 1.0
    dim = _interval_dimension(w)
    if dim == "distance":
        vol = sum((iv.get("distance") or 0) for iv in work_ivs)
    else:
        vol = sum((iv.get("time") or 0) for iv in work_ivs)
    total_work_t = sum((iv.get("time") or 0) for iv in work_ivs)
    total_rest_t = sum((iv.get("rest_time") or 0) for iv in work_ivs)
    denom = total_work_t + total_rest_t
    work_fraction = (total_work_t / denom) if denom > 0 else 1.0
    return vol, work_fraction


def _cross_type_similarity(ref: dict, cand: dict) -> Optional[float]:
    """Score similarity between an interval and a non-interval workout.

    Caller guarantees exactly one of {ref, cand} is_interval, and the interval
    side has work_fraction >= CONTINUOUS_LIKE_THRESHOLD. Volume axis is chosen
    by the interval side's dimension: time-dim → compare top-level time;
    distance-dim → compare top-level distance. ±20% volume filter.
    """
    interval = ref if ref.get("is_interval") else cand

    dim = _interval_dimension(interval)
    if dim == "time":
        ref_vol = ref.get("time") or 0
        cand_vol = cand.get("time") or 0
    elif dim == "distance":
        ref_vol = ref.get("distance") or 0
        cand_vol = cand.get("distance") or 0
    else:
        return None

    if not ref_vol or not cand_vol:
        return None
    if abs(cand_vol - ref_vol) / ref_vol > 0.20:
        return None

    volume_term = max(0.0, 1.0 - abs(cand_vol - ref_vol) / (0.20 * ref_vol))

    # Top-level w["pace"] is in seconds (from compute_pace), unlike work_pace
    # and pace_tenths() which return tenths. No /10 conversion needed here.
    ref_pace = ref.get("pace")
    cand_pace = cand.get("pace")
    if ref_pace is None or cand_pace is None:
        return None
    pace_delta_s = abs(cand_pace - ref_pace)
    pace_term = max(0.0, 1.0 - pace_delta_s / 30.0)

    return CROSS_TYPE_SCORE_CEILING * (volume_term + pace_term) / 2.0


def _pick_prior_exact(workout: dict, all_workouts: list) -> Optional[dict]:
    """Return the most-recent prior workout with the *exact* same structure.

    Used to power the splits/intervals table pace-delta annotation when
    the "Similar workouts" tab is open.  Exact match semantics:

    * Interval workouts: same ``intervals_label`` (which preserves rep
      count — ``5×500m`` matches ``5×500m`` but not ``3×500m``).
    * Non-interval workouts: same ``workout_type`` and same
      ``distance`` (two 5000m workouts match).

    Ties are broken by date descending.  Returns ``None`` if there's no
    prior exact match.
    """
    wid = workout["id"]
    date = workout.get("date") or ""
    if workout.get("is_interval"):
        ref_label = workout.get("intervals_label")
        if not ref_label:
            return None
        candidates = [
            w
            for w in all_workouts
            if w["id"] != wid
            and w.get("is_interval")
            and w.get("intervals_label") == ref_label
            and (w.get("date") or "") < date
        ]
    else:
        ref_type = workout.get("workout_type")
        ref_dist = workout.get("distance")
        if not ref_dist:
            return None
        candidates = [
            w
            for w in all_workouts
            if w["id"] != wid
            and not w.get("is_interval")
            and w.get("workout_type") == ref_type
            and w.get("distance") == ref_dist
            and (w.get("date") or "") < date
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.get("date") or "")


def _prior_split_paces(prior: dict) -> list:
    """Extract a per-split pace_tenths list from a prior split-based workout.

    Aligned with the order ``_splits_table`` renders.  Returns an empty
    list when the workout has no ``splits``.
    """
    wo = (prior or {}).get("workout") or {}
    paces: list = []
    for sp in wo.get("splits") or []:
        t = sp.get("time") or 0
        d = sp.get("distance") or 0
        paces.append((t * 500 / d) if (t and d) else None)
    return paces


def _prior_interval_work_paces(prior: dict) -> list:
    """Extract a per-work-interval pace_tenths list from a prior interval workout.

    Indexed by ``_work_idx`` (0-based, rest intervals skipped).
    """
    wo = (prior or {}).get("workout") or {}
    paces: list = []
    for iv in wo.get("intervals") or []:
        if (iv.get("type") or "").lower() == "rest":
            continue
        t = iv.get("time") or 0
        d = iv.get("distance") or 0
        paces.append((t * 500 / d) if (t and d) else None)
    return paces


def _find_similar(workout: dict, all_workouts: list) -> list:
    """Return shallow-copied workouts similar to ``workout``, with ``_similarity``
    (0–100, higher = more similar) attached, sorted descending."""
    wtype = workout.get("workout_type", "")
    wid = workout["id"]
    is_interval = workout["is_interval"]

    pool: list = []
    if is_interval:
        ref_dim = _interval_dimension(workout)
        ref_vol, ref_wf = _interval_volume_and_work_fraction(workout)
        ref_pace_t = workout["work_pace"]
        ref_interv_count = len(workout.get("workout", {}).get("intervals"))
        ref_d_p_i = ref_vol / ref_interv_count
        for w in all_workouts:
            if w["id"] == wid:
                continue
            if not w["is_interval"]:
                continue
            if _interval_dimension(w) != ref_dim:
                continue
            vol, wf = _interval_volume_and_work_fraction(w)
            # Drop wildly-different workouts: total work volume must be within
            # ±40% of the reference (e.g. 5×1000m can match 4×1250m or 6×800m
            # but not 10×500m).
            if not ref_vol or not vol:
                continue
            if abs(vol - ref_vol) / ref_vol > 0.40:
                continue
            row = dict(w)
            terms = []

            # Volume term: 1.0 at exact match
            volume_term = max(0.0, 1.0 - abs(vol - ref_vol) / (vol + ref_vol))

            # Interval count term
            interv_count = len(w.get("workout", {}).get("intervals"))
            interval_count_term = max(
                0.0,
                1.0
                - abs(ref_interv_count - interv_count)
                / (ref_interv_count + interv_count),
            )

            # Distance per interval
            d_p_i = vol / interv_count
            dist_term = max(0.0, 1.0 - abs(d_p_i - ref_d_p_i) / (d_p_i + ref_d_p_i))

            # Work-fraction term: captures work:rest character (e.g. 1:1 vs 1:4).
            # Both values are in [0, 1], so the absolute difference is bounded.
            work_fraction_term = max(0.0, 1.0 - abs(wf - ref_wf))
            # Pace term: 1.0 at exact match, 0.0 at ≥30s/500m off.
            pace_term = 0
            if ref_pace_t is not None:
                p = w["work_pace"]
                if p is not None:
                    pace_delta_s = abs(p - ref_pace_t) / 10.0
                    pace_term = max(0.0, 1.0 - pace_delta_s / 30.0)

            row["_similarity"] = (
                100
                * volume_term
                * work_fraction_term
                * work_fraction_term
                * pace_term
                * dist_term
                * interval_count_term
            )  # (100.0 * sum(terms) / len(terms)) if terms else None
            pool.append(row)
    else:
        ref_dist = workout.get("distance", 0)
        ref_pace = pace_tenths(workout)
        for w in all_workouts:
            if w["id"] == wid or w.get("workout_type") != wtype:
                continue
            d = w.get("distance", 0)
            if ref_dist and d and abs(d - ref_dist) / ref_dist > 0.20:
                continue
            row = dict(w)
            dist_term = None
            if ref_dist and d:
                dist_term = max(0.0, 1.0 - abs(d - ref_dist) / (0.20 * ref_dist))
            pace_term = None
            p = pace_tenths(w)
            if ref_pace and p:
                pace_delta_s = abs(p - ref_pace) / 10.0
                pace_term = max(0.0, 1.0 - pace_delta_s / 30.0)
            terms = [t for t in (dist_term, pace_term) if t is not None]
            row["_similarity"] = (100.0 * sum(terms) / len(terms)) if terms else None
            pool.append(row)

    # Cross-type bridge: continuous-like intervals ↔ non-interval workouts.
    # Only emit when the *reference* is continuous-like — a rest-bearing
    # interval is structurally unlike a non-interval piece and shouldn't show
    # one as "similar". The asymmetry runs through the reference.
    if _is_continuous_like(workout):
        already = {row["id"] for row in pool}
        for w in all_workouts:
            if w["id"] == wid or w["id"] in already:
                continue
            if bool(w.get("is_interval")) == bool(is_interval):
                continue
            if not _is_continuous_like(w):
                continue
            sim = _cross_type_similarity(workout, w)
            if sim is None:
                continue
            row = dict(w)
            row["_similarity"] = sim
            pool.append(row)

    pool.sort(
        key=lambda w: w.get("_similarity")
        if w.get("_similarity") is not None
        else -1.0,
        reverse=True,
    )
    return pool


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


def _render_similar_workouts(workout, all_workouts, max_hr, profile, state):
    similar = _find_similar(workout, all_workouts)
    if similar:
        try:
            add_metrics(similar, with_timeline=False)
        except Exception:
            pass
        with hd.box(align="center"):
            # hd.h2(
            #     "Similar workouts",
            #     font_weight="semibold",
            #     font_size="x-large",
            #     font_color="neutral-800",
            # )
            is_interval_workout = workout["is_interval"]
            compare_col_entry = {
                "key": "compare",
                "compared_ids": list(state.compared_workouts),
                "stack_active": state.stack,
            }
            if is_interval_workout:
                # Similar workouts are mostly intervals — show the interval
                # structure (which already encodes the rep count when present).
                cols = [
                    "date",
                    "workout_structure",
                    "distance",
                    "time",
                    "pace",
                    "watts",
                    "spm",
                    "hr",
                    "severity",
                    "stimulus",
                    "similarity",
                    compare_col_entry,
                    "link",
                ]
            else:
                # Non-interval: show standard performance columns
                cols = [
                    "date",
                    "distance",
                    "time",
                    "pace",
                    "watts",
                    "drag",
                    "spm",
                    "hr",
                    "severity",
                    "stimulus",
                    "similarity",
                    compare_col_entry,
                    "link",
                ]

            def _on_compare_toggle(payload):
                wid = payload["workout_id"]
                current = set(state.compared_workouts)
                if payload["checked"]:
                    current.add(wid)
                    state.stack = False
                else:
                    current.discard(wid)
                state.compared_workouts = tuple(sorted(current))

            WorkoutTable(
                similar,
                cols,
                default_sort_col="similarity",
                default_sort_asc=False,
                on_event={"compare_toggle": _on_compare_toggle},
                searchable=False,
            )


def _render_prior_training(workout, all_workouts, state):
    """Render the Prior Training tab.

    Shows a tree-mode workout table of workouts up to and including the
    current workout, filterable by severity bucket via a chip group.
    Defaults to ``Maximal`` severity selected so the table reads as "prior
    PB-territory sessions leading up to today's effort."  Releasing all
    chips disables the severity filter (show everything).
    """
    is_dark = hd.theme().is_dark
    current_day = workout.get("day")
    current_sid = workout.get("session_id")
    active_severity: set[str] = set(state.prior_training_severity)

    with hd.box(align="center", gap=1, width="100%"):
        # ── Severity chip filter ─────────────────────────────────────────
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(0, 0, 0.5, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "Severity",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            for label, _upper in SEVERITY_THRESHOLDS:
                with hd.scope(f"prior_severity_{label}"):
                    color_rgba = SEVERITY_STYLE[label]["bg"]
                    color_str = (
                        f"rgba({color_rgba[0]},{color_rgba[1]},{color_rgba[2]},"
                        f"{color_rgba[3]})"
                    )
                    clicked = legend_chip(
                        name=label,
                        color_str=color_str,
                        is_active=label in active_severity,
                        definition=SEVERITY_DEFINITION_TEXT[label],
                        filter_rule=SEVERITY_FILTER_TEXT[label],
                    )
                    if clicked:
                        sel = set(state.prior_training_severity)
                        if label in sel:
                            sel.discard(label)
                        else:
                            sel.add(label)
                        state.prior_training_severity = tuple(
                            sorted(sel, key=lambda q: SEVERITY_ORDER[q])
                        )

        # ── Filter workouts ──────────────────────────────────────────────
        # severity_bucket is set by add_metrics, so we have to compute it
        # before filtering by severity.  Pre-filter by day first to limit
        # the metric-enrichment workload to relevant rows.
        candidates: list = []
        for w in all_workouts:
            day = w.get("day")
            if not day or (current_day and day > current_day):
                continue
            candidates.append(w)

        try:
            add_metrics(candidates, with_timeline=False)
        except Exception:
            pass

        if active_severity:
            prior_rows = [
                w for w in candidates if w.get("_severity") in active_severity
            ]
        else:
            prior_rows = candidates

        if not prior_rows:
            hd.text(
                "No prior workouts match the selected filter.",
                font_color="neutral-500",
                font_size="small",
            )
            return

        # ── Table ────────────────────────────────────────────────────────
        cols = [
            "date",
            "main_work",
            "work_duration",
            "pace",
            "watts",
            "spm",
            "hr",
            "severity",
            "stimulus",
            {"key": "link", "current_id": str(workout["id"])},
        ]
        WorkoutTable(
            prior_rows,
            cols,
            default_sort_col="date",
            default_sort_asc=False,
            highlight=lambda r: str(r.get("id")) == str(workout["id"]),
            tree_mode=True,
            sessions_dict=AppContext().sessions_dict,
            searchable=False,
            default_expanded_session_ids=([current_sid] if current_sid else []),
        )


# ---------------------------------------------------------------------------
# Time-of-day override editor (for manually-added workouts)
# ---------------------------------------------------------------------------


_NO_TOD_SUFFIX = " 00:00:00"


def _format_hhmmss_friendly(hhmmss: str) -> str:
    """``"09:30:00"`` → ``"9:30 AM"``.  Drops seconds when zero."""
    try:
        h = int(hhmmss[0:2])
        m = int(hhmmss[3:5])
        s = int(hhmmss[6:8])
    except (ValueError, IndexError):
        return hhmmss
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if s:
        return f"{h12}:{m:02d}:{s:02d} {ampm}"
    return f"{h12}:{m:02d} {ampm}"


def _hhmmss_from_seconds(total_s: int) -> Optional[str]:
    """Clamp-or-reject: returns ``"HH:MM:SS"`` if ``total_s`` lies inside
    a single day, else None (caller should skip the shortcut)."""
    if total_s < 0 or total_s >= 86400:
        return None
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _seconds_from_hhmmss(hhmmss: str) -> Optional[int]:
    try:
        h = int(hhmmss[0:2])
        m = int(hhmmss[3:5])
        s = int(hhmmss[6:8])
    except (ValueError, IndexError):
        return None
    return h * 3600 + m * 60 + s


def _workout_duration_seconds(w: dict) -> int:
    """Total elapsed seconds (work + rest for intervals)."""
    base = (w.get("time") or 0) // 10
    if w.get("is_interval"):
        base += (w.get("rest_time") or 0) // 10
    return int(base)


#: Fields added by ``enrich_for_storage`` — must be stripped before
#: writing a workout back to IDB so the persisted shape matches the
#: post-normalize / pre-enrich canonical form (``concept2_sync`` writes
#: workouts before enrichment for the same reason).  ``date_dt`` is the
#: critical one: it's a ``datetime.date`` and won't JSON-serialise.
_ENRICHMENT_FIELDS = (
    "date_dt",
    "date_ms",
    "day",
    "season",
    "machine",
    "cat_key",
    "is_interval",
    "reps",
    "structure_key",
    "intervals_label",
    "work_pace",
    "work_spm",
    "pace",
    "watts",
)


def _persistable_workout(w: dict) -> dict:
    """Return a shallow copy of ``w`` stripped of enrichment-only fields,
    suitable for writing to ``WORKOUTS_STORE``."""
    return {k: v for k, v in w.items() if k not in _ENRICHMENT_FIELDS}


def _apply_override_to_live_state(
    workout_id_str: str,
    new_date: str,
    workouts_dict: dict,
    sessions_dict: dict,
) -> None:
    """Mutate workout date in place, recluster the affected sessions, and
    write the changes through to IndexedDB.

    A re-cluster can shift session_ids on workouts the user did NOT edit
    (e.g. workout B that joins the edited workout A's new session).  Every
    such workout is persisted back to IDB so its stored ``session_id``
    matches the live session record.  Without this, workouts on the same
    day end up referencing dropped session uuids and the dashboard fails
    to attach session-level metrics on the next reload.

    Re-runs ``enrich_for_storage`` on the mutated workout so the derived
    fields (``date_dt``, ``date_ms``, ``day``, ``season`` …) match the
    new date.

    No-op when ``workout_id_str`` is missing from ``workouts_dict``.
    """
    w = workouts_dict.get(workout_id_str)
    if w is None:
        return
    w["date"] = new_date

    # Re-run Stage-2 enrichment so derived fields stay consistent with the
    # new date.  ``enrich_for_storage`` is idempotent.
    enrich_for_storage(w)

    # Recluster only the (machine, day) buckets touched by this workout.
    new_sessions, mutations, dropped_sids = assign_sessions_incremental(
        workouts_dict, sessions_dict or {}, {workout_id_str}
    )

    # Apply the new session_ids to in-memory workouts.  Note that
    # ``mutations`` covers EVERY workout in the rebuilt bucket whose
    # session_id changed — when the edited workout joins an existing
    # session, the other members of that session are reassigned too
    # because ``_build_for_bucket`` always mints fresh session uuids.
    for wid, sid in mutations.items():
        if wid in workouts_dict:
            workouts_dict[wid]["session_id"] = sid

    # Push session-record changes to IDB.  ``new_sessions`` is the full
    # post-cluster dict (carried-over + freshly built); identity-compare
    # against the in-memory ``sessions_dict`` to write only the records
    # that actually changed.
    for sid in dropped_sids:
        indexed_db.delete(SESSIONS_STORE, sid)
        sessions_dict.pop(sid, None)
    session_writes: dict = {}
    for sid, rec in new_sessions.items():
        if sid in dropped_sids:
            continue
        if sessions_dict.get(sid) is not rec:
            session_writes[sid] = rec
            sessions_dict[sid] = rec
    if session_writes:
        indexed_db.put_many(SESSIONS_STORE, session_writes)

    # Persist every workout whose session_id changed (plus the edited
    # workout, whose ``date`` field also changed).  Without this, sibling
    # workouts in the rebuilt bucket would carry stale session_ids on
    # their next IDB read — referring to sessions we just dropped.
    affected_wids = set(mutations.keys()) | {workout_id_str}
    workout_writes = {
        wid: _persistable_workout(workouts_dict[wid])
        for wid in affected_wids
        if wid in workouts_dict
    }
    if workout_writes:
        indexed_db.put_many(WORKOUTS_STORE, workout_writes)


def _republish_after_edit(
    user_id: str, profile: dict, workouts_dict: dict, sessions_dict: dict
) -> None:
    """Mirror the edited workouts + sessions to the public profile, when the
    owner has opted in.  Best-effort: errors are logged, not surfaced."""
    if not (profile or {}).get("public"):
        return
    try:
        from services import public_profiles

        snapshot = {wid: _persistable_workout(w) for wid, w in workouts_dict.items()}
        public_profiles.publish_workouts(user_id, snapshot)
        public_profiles.publish_sessions(user_id, sessions_dict)
    except Exception as exc:
        print(f"[time_overrides] post-edit republish failed: {exc}")


def _workout_short_label(w: dict) -> str:
    """Compact label for the shortcut workout selector.

    Time-prefixed so the user can pick by when-it-happened, with a short
    distance / structure tag for disambiguation when several workouts on
    the day are similar."""
    raw = w.get("date") or ""
    time_lbl = (
        _format_hhmmss_friendly(raw[11:19])
        if len(raw) >= 19 and not raw.endswith(_NO_TOD_SUFFIX)
        else ""
    )
    if w.get("is_interval"):
        body = w.get("intervals_label") or w.get("structure_key") or ""
    else:
        body = ""
        d = w.get("distance")
        t = w.get("time")
        if d:
            if d >= 1000:
                k = d / 1000
                body = f"{int(k)}k" if k == int(k) else f"{k:.1f}k"
            else:
                body = f"{int(d)}m"
        elif t:
            mins = t // 600
            body = f"{int(mins)}min"
    if time_lbl and body:
        return f"{time_lbl} — {body}"
    return time_lbl or body or f"workout {w.get('id')}"


def _same_day_workouts_with_times(
    workout: dict,
    workouts_dict: dict,
) -> list[dict]:
    """Return workouts on the same (machine, day) as ``workout`` that have
    a real time-of-day (i.e. their date does not end in ``00:00:00``).

    Excludes the current workout itself.  Sorted by start time.
    """
    own_id = str(workout.get("id"))
    own_day = (workout.get("date") or "")[:10]
    own_machine = workout.get("type") or "rower"
    if not own_day:
        return []
    out: list[dict] = []
    for w in (workouts_dict or {}).values():
        if str(w.get("id")) == own_id:
            continue
        if (w.get("type") or "rower") != own_machine:
            continue
        date = w.get("date") or ""
        if date[:10] != own_day:
            continue
        if date.endswith(_NO_TOD_SUFFIX):
            continue  # another manually-added workout — no time to anchor on
        if len(date) < 19:
            continue
        out.append(w)
    out.sort(key=lambda w: (w.get("date") or "")[11:19])
    return out


def _hhmmss_for_anchor(
    anchor: dict,
    position: str,
    gap_minutes: float,
    duration_s: int,
) -> Optional[str]:
    """Compute the override hhmmss (workout end) when inserting the current
    workout ``position`` (``"before"`` or ``"after"``) the ``anchor``
    workout with a ``gap_minutes``-minute gap.

    Returns None when the result would cross a midnight boundary.
    """
    raw = anchor.get("date") or ""
    if len(raw) < 19:
        return None
    anchor_end_s = _seconds_from_hhmmss(raw[11:19])
    if anchor_end_s is None:
        return None
    anchor_duration = _workout_duration_seconds(anchor)
    anchor_start_s = anchor_end_s - anchor_duration
    gap_s = int(round(gap_minutes * 60))
    if position == "before":
        # Our end is at anchor_start - gap.  ``date`` is the workout end.
        new_end_s = anchor_start_s - gap_s
        if new_end_s - duration_s < 0:
            return None
        return _hhmmss_from_seconds(new_end_s)
    # "after": our start is at anchor_end + gap; our end = start + duration.
    new_start_s = anchor_end_s + gap_s
    new_end_s = new_start_s + duration_s
    if new_end_s >= 86400:
        return None
    return _hhmmss_from_seconds(new_end_s)


def _time_of_day_editor(
    workout: dict,
    workouts_dict: dict,
    sessions_dict: dict,
) -> None:
    """Render the time-of-day editor panel for a manually-added workout.

    Visibility:
      Renders only when the workout's current date ends with ``00:00:00``
      OR an override is currently set (so the user can revisit / clear).

    Owner gate:
      Saves are gated on ``AppContext().is_owner``; for public viewers
      the panel renders read-only (showing the value but not the form).
    """
    ctx = AppContext()
    user_id = ctx.user_id
    is_owner = ctx.is_owner and bool(user_id)

    raw_date = workout.get("date") or ""
    is_manually_added = raw_date.endswith(_NO_TOD_SUFFIX)

    # Detect existing overrides via the on-disk record.  Owner-only check:
    # public viewers see the override-applied date directly (already baked
    # into the published workouts.zb64) and don't need this panel.
    overrides = time_overrides.load_overrides(user_id) if is_owner else {}
    wid_str = str(workout["id"])
    has_override = wid_str in overrides

    if not is_manually_added and not has_override:
        return

    # In public mode we have no editor — bail silently.  Owners get the
    # full UI even when no override is set yet (since is_manually_added).
    if not is_owner:
        return

    # ── Local state ──────────────────────────────────────────────────────
    # Pre-populate the text input with the current override (if any) so the
    # user sees what's saved and can fine-tune from there.
    initial_text = _format_hhmmss_friendly(overrides[wid_str]) if has_override else ""
    same_day = _same_day_workouts_with_times(workout, workouts_dict)
    initial_anchor = str(same_day[0]["id"]) if same_day else ""

    edit_state = hd.state(
        text=initial_text,
        error="",
        feedback="",
        feedback_kind="",  # "" | "saved" | "cleared"
        # Insert-relative-to UI state.
        rel_position="after",  # "before" | "after"
        rel_anchor_id=initial_anchor,
        rel_gap_text="5",  # minutes between, free-form text
    )

    profile = ctx.profile or {}

    def _commit(hhmmss: str) -> None:
        """Persist + apply: write to disk, mutate live workouts/sessions,
        and trigger a public republish if opted in."""
        try:
            time_overrides.save_override(user_id, wid_str, hhmmss)
        except Exception as exc:
            edit_state.error = f"Save failed: {exc}"
            edit_state.feedback = ""
            return
        new_date = (
            raw_date[: -len(_NO_TOD_SUFFIX)] + " " + hhmmss
            if is_manually_added
            else (raw_date[:10] + " " + hhmmss)
        )
        _apply_override_to_live_state(wid_str, new_date, workouts_dict, sessions_dict)
        _republish_after_edit(user_id, profile, workouts_dict, sessions_dict)
        edit_state.error = ""
        edit_state.feedback = f"Saved {_format_hhmmss_friendly(hhmmss)}."
        edit_state.feedback_kind = "saved"
        edit_state.text = _format_hhmmss_friendly(hhmmss)

    def _on_clear() -> None:
        try:
            time_overrides.clear_override(user_id, wid_str)
        except Exception as exc:
            edit_state.error = f"Clear failed: {exc}"
            return
        # Live revert: reset the date's time component to 00:00:00 so the
        # downstream consumers (table flag, session clustering) see the
        # original "no time-of-day" state again.
        reset_date = raw_date[:10] + _NO_TOD_SUFFIX
        _apply_override_to_live_state(wid_str, reset_date, workouts_dict, sessions_dict)
        _republish_after_edit(user_id, profile, workouts_dict, sessions_dict)
        edit_state.error = ""
        edit_state.feedback = "Override cleared."
        edit_state.feedback_kind = "cleared"
        edit_state.text = ""

    # ── Layout ───────────────────────────────────────────────────────────
    current_hhmmss = overrides.get(wid_str) or (raw_date[11:19] or "")
    with hd.box(
        padding=(0.75, 1.25, 0.75, 1.25),
        gap=0.5,
        border="1px solid neutral-200",
        background_color="warning-50",
        border_radius="medium",
        margin_bottom=1,
        width="100%",
    ):
        with hd.hbox(gap=0.5, align="center"):
            hd.icon("exclamation-triangle", font_color="warning-700")
            hd.text(
                (
                    "This workout was added manually and has no recorded "
                    "time-of-day."
                    if is_manually_added
                    else f"Time-of-day override: {_format_hhmmss_friendly(current_hhmmss)}"
                ),
                font_size="small",
                font_color="warning-700",
                font_weight="semibold",
            )

        with hd.hbox(gap=0.5, align="center"):
            hd.text("Set time:", font_size="small", font_color="neutral-700")
            ti = hd.text_input(
                value=edit_state.text,
                placeholder="9:30 AM",
                width=12,
                size="small",
            )
            if ti.changed:
                edit_state.text = ti.value
                edit_state.error = ""
            save_btn = hd.button("Save", size="small", variant="primary")
            if save_btn.clicked:
                hhmmss, err = time_overrides.parse_time_input(edit_state.text)
                if err or hhmmss is None:
                    edit_state.error = err or "Invalid time."
                else:
                    _commit(hhmmss)
            if has_override:
                clear_btn = hd.button("Clear", size="small", variant="default")
                if clear_btn.clicked:
                    _on_clear()

        if edit_state.error:
            hd.text(
                edit_state.error,
                font_size="x-small",
                font_color="danger-700",
            )
        elif edit_state.feedback:
            hd.text(
                edit_state.feedback,
                font_size="x-small",
                font_color="success-700",
            )

        # ── Insert-relative-to-another-workout selector ──────────────────
        if same_day:
            hd.text(
                f"Or insert relative to another workout on {fmt_date(raw_date)}:",
                font_size="x-small",
                font_color="neutral-500",
            )
            duration_s = _workout_duration_seconds(workout)

            # Resolve current selections, falling back to defaults if state
            # is stale (e.g. another workout's override removed it from the
            # list since the last render).
            anchor_ids = [str(w["id"]) for w in same_day]
            if edit_state.rel_anchor_id not in anchor_ids:
                edit_state.rel_anchor_id = anchor_ids[0]
            anchor = next(
                (w for w in same_day if str(w["id"]) == edit_state.rel_anchor_id),
                same_day[0],
            )

            with hd.hbox(gap=0.5, align="center", wrap="wrap"):
                pos_sel = hd.select(value=edit_state.rel_position, size="small")
                with pos_sel:
                    with hd.scope("before"):
                        hd.option("Before", value="before")
                    with hd.scope("after"):
                        hd.option("After", value="after")
                if pos_sel.changed:
                    edit_state.rel_position = pos_sel.value

                anchor_sel = hd.select(
                    value=edit_state.rel_anchor_id,
                    size="small",
                )
                with anchor_sel:
                    for w in same_day:
                        wid = str(w["id"])
                        with hd.scope(wid):
                            hd.option(_workout_short_label(w), value=wid)
                if anchor_sel.changed:
                    edit_state.rel_anchor_id = anchor_sel.value

                hd.text("with", font_size="small", font_color="neutral-700")
                gap_input = hd.text_input(
                    value=edit_state.rel_gap_text,
                    width=4,
                    size="small",
                )
                if gap_input.changed:
                    edit_state.rel_gap_text = gap_input.value
                hd.text("min gap", font_size="small", font_color="neutral-700")

                apply_btn = hd.button(
                    "Apply",
                    size="small",
                    variant="primary",
                )
                if apply_btn.clicked:
                    try:
                        gap_minutes = float(edit_state.rel_gap_text)
                    except ValueError:
                        edit_state.error = "Gap must be a number of minutes."
                        gap_minutes = None
                    if gap_minutes is not None:
                        if gap_minutes < 0:
                            edit_state.error = "Gap must be non-negative."
                        else:
                            hhmmss = _hhmmss_for_anchor(
                                anchor,
                                edit_state.rel_position,
                                gap_minutes,
                                duration_s,
                            )
                            if hhmmss is None:
                                edit_state.error = (
                                    "That insertion would cross midnight — "
                                    "set the time directly above instead."
                                )
                            else:
                                _commit(hhmmss)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def workout_page(workout_id: int) -> None:
    """Render the full-screen workout detail overlay."""
    _theme = hd.theme()
    state = hd.state(
        metric="pace",  # "pace" | "watts"
        focused_interval=None,  # int | None  (raw band index)
        focused_interval_excluding_rest=None,  # int | None  (1-based work interval #)
        custom_splits=None,  # list[{u:"m"|"s", v:int}] | None — applied chip list
        interval_sub_splits=None,  # interval workouts: {mode, n|unit+value} | None
        expanded_intervals=(),  # tuple[int,...] of band indices currently expanded
        splits_store=None,  # full localStorage dict | None until first load
        splits_loaded_for=None,  # workout_id we've populated splits state for
        show_splits_modal=False,
        show_ranked_modal=False,
        stack=False,  # stacked-intervals overlay mode
        show_pace=True,  # show pace/watts in stacked / compare mode
        show_spm=False,  # show SPM in stacked / compare mode
        show_hr=False,  # show HR in stacked / compare mode
        compared_workouts=(),  # tuple[int,...] of other workout ids to overlay
        last_click_seq=0,  # last chart.click_seq we've processed
        active_tab="Full Session",  # "Full Session" | "Similar Workouts" | "Prior Training"
        prior_training_severity=(
            "Maximal",
        ),  # tuple[str] of selected severity buckets on Prior Training tab
    )

    # ── Pre-fetch workout list (task-cached; free on repeat renders) ────────
    sync_result = sync_workouts()
    profile = get_profile()

    if sync_result is None:
        hd.box(padding=2, min_height="80vh")
        return

    _workouts_dict, all_workouts = sync_result
    workout = _workouts_dict.get(str(workout_id))

    if profile is None or workout is None or AppContext().sessions_dict is None:
        hd.box(padding=2, min_height="80vh")
        return

    # ── Load custom splits from localStorage ────────────────────────────────
    # We populate state.custom_splits and state.interval_sub_splits from the
    # saved entry on first visit to a workout (and again on workout-id
    # change).  Saved splits then drive the splits/intervals table and the
    # chart annotation bands without requiring the user to reopen the modal.
    if state.splits_loaded_for != workout_id:
        ls = hd.local_storage.get_item(_CUSTOM_SPLITS_LS_KEY)
        if not ls.done:
            hd.box(padding=2, min_height="80vh")
            return
        try:
            parsed = json.loads(ls.result) if ls.result else {}
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        state.splits_store = parsed if isinstance(parsed, dict) else {}
        entry = normalize_entry(state.splits_store.get(str(workout_id)))
        values = (entry or {}).get("splits", {}).get("values") if entry else None
        sub_spec = (entry or {}).get("interval_sub") if entry else None
        state.custom_splits = list(values) if values else None
        state.interval_sub_splits = dict(sub_spec) if sub_spec else None
        state.expanded_intervals = ()
        state.splits_loaded_for = workout_id

    # ── Fetch stroke data (unified via concept2_sync.strokes_for) ────────────

    has_strokes = bool(workout.get("stroke_data"))
    wtype = workout.get("workout_type", "")
    is_interval = workout["is_interval"]

    stroke_result = strokes_for(workout)
    stroke_status = stroke_result["status"]
    stroke_error = stroke_result["error"]
    strokes = stroke_result["strokes"]

    if stroke_status == "loading":
        hd.box(padding=2, min_height="80vh")
        return

    # All workouts done on this day
    same_day = [w for w in all_workouts if w.get("day") == workout.get("day")]
    print(len(same_day))

    # Attach Power Spread, HR Spread, and ESS family fields so the summary
    # cells can render them.  Best-effort: if reference watts haven't loaded
    # yet, the fields stay as None and the summary row hides the cells.
    max_hr, _ = resolve_max_hr(profile, all_workouts)
    try:
        add_metrics(same_day, with_timeline=True)
    except Exception:
        pass

    # ── Fetch strokes for each compared workout ───────────────────────────────

    compare_results: dict = {}
    compare_loading = False
    for cid in state.compared_workouts:
        cw = _workouts_dict.get(str(cid)) or {"id": cid, "stroke_data": True}
        cr = strokes_for(cw)
        if cr["status"] == "loaded":
            compare_results[cid] = cr["strokes"] or []
        elif cr["status"] == "loading":
            compare_loading = True

    # ── Title ────────────────────────────────────────────────────────────────

    if is_interval:
        title = workout.get("intervals_full_label") or [workout["structure_key"]]
    else:
        title = [fmt_distance_label(workout)]

    # ── Callbacks ────────────────────────────────────────────────────────────

    def on_split_focus(idx, row):
        state.focused_interval = idx
        if idx is None:
            state.focused_interval_excluding_rest = None
        else:
            state.focused_interval_excluding_rest = row.get("_work_idx", 0) + 1

    # ── Pace-vs-prior comparison data ────────────────────────────────────────
    # Resolved once per render so both the splits table (above the tabs)
    # and the Similar Workouts tab body can share it.  We only compute
    # the prior when the user is viewing the Similar Workouts tab — for
    # the Full Session tab the deltas would be a distraction.
    prior_workout: Optional[dict] = None
    prior_pace_by_idx: Optional[list] = None
    if state.active_tab == "Similar Workouts":
        prior_workout = _pick_prior_exact(workout, all_workouts)
        if prior_workout is not None:
            if is_interval:
                prior_pace_by_idx = _prior_interval_work_paces(prior_workout)
                ref_count = sum(
                    1
                    for iv in (workout.get("workout") or {}).get("intervals") or []
                    if (iv.get("type") or "").lower() != "rest"
                )
            else:
                prior_pace_by_idx = _prior_split_paces(prior_workout)
                ref_count = len((workout.get("workout") or {}).get("splits") or [])
            # Counts must match exactly for index-by-index alignment to
            # be meaningful; otherwise drop the annotations.
            if len(prior_pace_by_idx) != ref_count:
                prior_workout = None
                prior_pace_by_idx = None

    # ── Layout ───────────────────────────────────────────────────────────────

    total_dist = workout.get("distance") or 0
    total_time_tenths = workout.get("time") or 0
    is_time_based = wtype in TIME_BASED_WORKOUT_TYPES
    show_custom = has_strokes and (
        total_dist > 0 or (is_time_based and total_time_tenths > 0) or is_interval
    )

    with hd.box(padding=(1, 2, 0, 4), gap=3, align="center", min_height="80vh"):
        with hd.hbox(gap=4, align="center", justify="end"):
            # ── Header ───────────────────────────────────────────────────────

            with hd.box(padding_top=1, gap=0, align="start"):
                with hd.hbox(gap=0.5):
                    hd.text(fmt_date(workout["date"]), font_color="neutral-500")
                    _hhmmss = (workout.get("date") or "")[11:19]
                    if _hhmmss and _hhmmss != "00:00:00":
                        hd.text(
                            _format_hhmmss_friendly(_hhmmss),
                            font_color="neutral-500",
                        )
                with hd.box():
                    for i, t in enumerate(title):
                        with hd.scope(f"{i}-{t}"):
                            hd.text(t, font_weight="bold", font_size="2x-large")

                if workout.get("comments"):
                    with blockquote(
                        border_left="3px solid neutral-200",
                        padding_left="15px",
                        margin_left=0,
                    ):
                        hd.text(
                            workout["comments"],
                            font_color="neutral-500",
                            font_size="medium",
                        )

            # ── Summary stats ─────────────────────────────────────────────────

            _summary_section(workout, strokes)

        # ── Time-of-day editor (manually-added workouts only) ────────────
        _time_of_day_editor(
            workout,
            _workouts_dict,
            AppContext().sessions_dict or {},
        )

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
                            "Stroke data not available for this workout.",
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
                            "Stroke-level data for this workout is not yet available.",
                            font_color="neutral-500",
                            text_align="center",
                        )
                        hd.text(
                            "It appears after the owner opens this workout.",
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
                        build_compare_series(
                            state.compared_workouts,
                            compare_results,
                            _workouts_dict,
                            show_watts=(state.metric == "watts"),
                        )
                        if has_compares and not state.stack
                        else None
                    )

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
                        interval_sub=state.interval_sub_splits,
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

            # Right: splits/intervals table + modal trigger button
            with hd.box(gap=0.75):
                with hd.hbox(gap=0.5, align="center"):
                    hd.h2(
                        "Intervals" if is_interval else "Splits",
                        font_weight="semibold",
                        font_size="x-large",
                        font_color="neutral-800",
                    )
                    if show_custom:
                        if is_interval:
                            edit_label = (
                                "Edit interval splits"
                                if state.interval_sub_splits
                                else "Add splits to intervals"
                            )
                        else:
                            edit_label = (
                                "Edit splits" if state.custom_splits else "Add splits"
                            )
                        edit_btn = hd.button(edit_label, variant="text", size="small")
                        if edit_btn.clicked:
                            state.show_splits_modal = True
                        if has_strokes:
                            with hd.tooltip("Best splits at ranked distances"):
                                ranked_btn = hd.icon_button(
                                    "trophy",
                                    font_size="small",
                                    font_color="neutral-400",
                                )
                            if ranked_btn.clicked:
                                state.show_ranked_modal = True

                def _toggle_interval_expand(idx, opening):
                    current = set(state.expanded_intervals or ())
                    if opening:
                        current.add(idx)
                    else:
                        current.discard(idx)
                    state.expanded_intervals = tuple(sorted(current))

                if prior_workout is not None:
                    with hd.hbox(gap=0.4):
                        hd.text(
                            "vs",
                            font_size="x-small",
                            font_color="neutral-500",
                        )
                        prior_link = hd.link(
                            fmt_date(prior_workout["date"]),
                            href=f"/workout/{prior_workout['id']}",
                            font_size="x-small",
                            font_color="neutral-500",
                        )
                        hd.text(
                            "(faster = green, slower = red)",
                            font_size="x-small",
                            font_color="neutral-400",
                        )

                _splits_table(
                    workout,
                    strokes,
                    state.custom_splits,
                    focused_idx=state.focused_interval
                    if state.focused_interval is not None
                    else -1,
                    on_focus=on_split_focus,
                    interval_sub=state.interval_sub_splits,
                    expanded_intervals=state.expanded_intervals,
                    on_interval_expand=_toggle_interval_expand,
                    prior_pace_by_idx=prior_pace_by_idx,
                )

        # ── Tabs: Full session (default) / Similar workouts ──────────────
        # The splits table above reads state.active_tab so it can show
        # pace deltas in "Similar workouts" mode.  We sync state from the
        # tab_group's reactive prop *after* rendering the tabs — a tab
        # click therefore takes one extra render-frame to propagate to
        # the splits annotations, which is imperceptible in practice.
        ess_cfg = build_effort_stress_chart_config(workout, is_dark=_theme.is_dark)

        with hd.box(width="100%"):
            with hd.hbox(gap=2, align="center", justify="center"):
                tabs = hd.tab_group(
                    "Full Session",
                    "Similar Workouts",
                    "Prior Training",
                    font_size="x-large",
                    font_weight="bold",
                )
                if tabs.active and tabs.active != state.active_tab:
                    if (
                        state.active_tab == "Similar Workouts"
                        and tabs.active != "Similar Workouts"
                    ):
                        state.compared_workouts = ()
                    state.active_tab = tabs.active

            with hd.box(padding_top=1, gap=2, width="100%"):
                _active = tabs.active or "Full Session"
                if _active == "Full Session":
                    day_cols = [
                        "date",
                        "time_of_day",
                        "main_work",
                        "work_duration",
                        "pace",
                        "watts",
                        "distance_combined",
                        "spm",
                        "drag",
                        "hr",
                        "severity",
                        "stimulus",
                        "ess",
                        {"key": "link", "current_id": str(workout["id"])},
                    ]
                    _current_sid = workout.get("session_id")
                    WorkoutTable(
                        same_day,
                        day_cols,
                        default_sort_col="date",
                        default_sort_asc=True,
                        paginate=False,
                        highlight=lambda r: str(r.get("id")) == str(workout["id"]),
                        tree_mode=True,
                        searchable=False,
                        default_expanded_session_ids=(
                            [_current_sid] if _current_sid else []
                        ),
                    )

                    if ess_cfg:
                        with hd.box(gap=0.5, width="100%"):
                            hd.h2(
                                "Session Stimulus & Intensity",
                                font_weight="semibold",
                                font_size="x-large",
                                font_color="neutral-800",
                            )
                            EffortStressChart(config=ess_cfg, height=220)

                    # Dev affordance: dump every workout in the session to
                    # /tmp/session-<id>/ so the dev (or Claude reviewing real
                    # data) can read the full session payload off disk.
                    _dump_state = hd.state(last_path=None)
                    with hd.hbox(gap=0.5, align="center"):
                        dump_btn = hd.button(
                            "Download session",
                            prefix_icon="download",
                            size="small",
                            variant="default",
                        )
                        if dump_btn.clicked:
                            try:
                                _dump_state.last_path = _dump_session_to_tmp(
                                    same_day, workout.get("id")
                                )
                            except Exception as exc:
                                _dump_state.last_path = f"error: {exc}"
                        if _dump_state.last_path:
                            hd.text(
                                _dump_state.last_path,
                                font_family="mono",
                                font_size="small",
                                font_color="neutral-600",
                            )

                elif _active == "Similar Workouts":
                    _render_similar_workouts(
                        workout, all_workouts, max_hr, profile, state
                    )

                elif _active == "Prior Training":
                    _render_prior_training(workout, all_workouts, state)

        # ── Custom-splits modal ──────────────────────────────────────────
        # The dialog is created at this top level so its component state
        # survives re-renders of the right column above.  Visibility is
        # driven by state.show_splits_modal; the modal contents short-
        # circuit when dlg.opened is False (see render_splits_modal).
        if show_custom:
            dlg = hd.dialog(
                "Edit interval splits" if is_interval else "Edit splits",
                panel_style=hd.style(width="720px", max_width="95vw"),
            )
            if dlg.was_closed:
                state.show_splits_modal = False
            if state.show_splits_modal and not dlg.opened:
                dlg.opened = True

            def _on_splits_apply(payload):
                """Persist whichever field of the payload was supplied.

                payload is one of:
                    {"values": [...]}        — non-interval: replace chip list
                    {"values": None}         — non-interval: clear
                    {"sub_spec": {...}}      — interval: replace sub-spec
                    {"sub_spec": None}       — interval: clear sub-spec
                """
                store = dict(state.splits_store or {})
                wid = str(workout["id"])
                existing = dict(store.get(wid) or {})

                if "values" in payload:
                    new_values = payload["values"]
                    if new_values:
                        existing["splits"] = {"values": list(new_values)}
                    else:
                        existing.pop("splits", None)
                    state.custom_splits = list(new_values) if new_values else None
                if "sub_spec" in payload:
                    new_sub = payload["sub_spec"]
                    if new_sub:
                        existing["interval_sub"] = dict(new_sub)
                    else:
                        existing.pop("interval_sub", None)
                    state.interval_sub_splits = dict(new_sub) if new_sub else None
                    # Collapse expanded interval rows when the sub-spec
                    # changes — old expansion state references the prior
                    # row count which may not match the new computation.
                    state.expanded_intervals = ()

                if existing:
                    store[wid] = existing
                else:
                    store.pop(wid, None)
                state.splits_store = store
                hd.local_storage.set_item(_CUSTOM_SPLITS_LS_KEY, json.dumps(store))
                state.show_splits_modal = False

            def _on_splits_close():
                # Cancel (and any other programmatic close from inside the
                # modal) needs to drop the trigger flag — ``dialog.opened
                # = False`` doesn't fire ``was_closed``, so without this
                # callback workout_page would re-open the dialog on the
                # next render.
                state.show_splits_modal = False

            render_splits_modal(
                dlg,
                workout,
                strokes,
                current_values=state.custom_splits,
                current_sub_spec=state.interval_sub_splits,
                on_apply=_on_splits_apply,
                on_close=_on_splits_close,
            )

            # Ranked events modal — separate dialog so the splits editor
            # stays single-purpose.
            ranked_dlg = hd.dialog(
                "Ranked events",
                panel_style=hd.style(width="720px", max_width="95vw"),
            )
            if ranked_dlg.was_closed:
                state.show_ranked_modal = False
            if state.show_ranked_modal and not ranked_dlg.opened:
                ranked_dlg.opened = True

            def _on_ranked_close():
                state.show_ranked_modal = False

            render_ranked_events_modal(
                ranked_dlg,
                workout,
                strokes,
                on_close=_on_ranked_close,
            )
