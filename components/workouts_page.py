"""
Workouts page — pace-vs-date scatter chart + recent-workouts table.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile

import hyperdiv as hd

from scipy.optimize import brentq

from services.rowing_utils import (
    ranked_dist_set,
    ranked_time_set,
    workout_machine,
)

from services.formatters import fmt_date

from services.interval_utils import (
    wrap_parts as _wrap_parts,
    build_interval_lines as _build_interval_lines,
    interval_totals as _interval_totals,
)
from services.critical_power_model import (
    critical_power_model,
    fit_critical_power,
)
from components.workouts_chart_plugin import WorkoutsChart
from components.workout_table import WorkoutTable
from components.app_context import get_profile, AppContext
from components.reference_watts_loader import reference_watts_loader
from components.shared_ui import global_filter_ui, header_dropdown
from components.spread_quality_legends import spread_severity_legends, SpreadSeverityFilters
from services.heartrate_utils import (
    hr_bin_passes,
    resolve_max_hr,
)
from services.reference_watts import get_reference_watts
from services.volume_bins import power_bin_passes
from services.workout_enrichment import attach_ess_metrics, attach_spread
from services.erg_stress import SEVERITY_STYLE

from components.concept2_sync import get_all_workouts



# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

# 12-color palette (H, S%, L%).  Balanced saturation; readable on both themes.
_PALETTE = [
    (210, 75, 55),  # cornflower blue
    (18, 82, 57),  # burnt orange
    (163, 58, 44),  # seafoam teal
    (338, 68, 58),  # watermelon pink
    (44, 88, 50),  # golden amber
    (122, 54, 44),  # sage green
    (278, 54, 60),  # soft violet
    (196, 71, 50),  # sky blue
    (28, 74, 54),  # terra cotta
    (252, 60, 62),  # periwinkle
    (82, 63, 47),  # olive
    (312, 58, 57),  # mauve
]

_MS_PER_DAY = 86_400_000

# Window sizes selectable by the user.
WINDOW_OPTIONS = ("Week", "Month", "Quarter", "Year", "2 Years", "All")
_WINDOW_DAYS = {
    "Week": 7,
    "Month": 30,
    "Quarter": 91,
    # "Season": 183,
    "Year": 365,
    "2 Years": 2 * 365,
    "All": 99 * 365,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hsla(h: int, s: int, l_: int, a: float) -> str:
    return f"hsla({h},{s}%,{l_}%,{a:.2f})"


def _dump_workout_to_tmp(workout: dict) -> None:
    """Write a workout dict to ``$TMPDIR/erg-nerd-workout-<id>.json``.

    Strips fields starting with ``_`` (these are render-time enrichments —
    SVG bar URIs, segment lists, full timeline arrays — that bloat the file
    without adding diagnostic value).  Anything else (including ``date_dt``,
    which we coerce to an ISO string) is preserved.
    """
    def _safe(v):
        if isinstance(v, dict):
            return {k: _safe(x) for k, x in v.items() if not str(k).startswith("_")}
        if isinstance(v, (list, tuple)):
            return [_safe(x) for x in v]
        try:
            json.dumps(v)
            return v
        except TypeError:
            return str(v)

    payload = {k: _safe(v) for k, v in workout.items() if not str(k).startswith("_")}
    wid = workout.get("id") or "unknown"
    path = os.path.join("tmp", f"erg-nerd-workout-{wid}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def _dot_r(meters: float) -> float:
    """Outer dot radius in px: ½ √meters."""
    return 0.25 * math.sqrt(max(0.0, meters))


# ---------------------------------------------------------------------------
# Season-best detection
# ---------------------------------------------------------------------------


def compute_sb_ids(workouts: list) -> set:
    """
    Return the set of workout IDs that are a season best for their
    (season, ranked event) combination.

    Only non-interval workouts at a ranked distance (100m … marathon) or
    ranked timed duration (1 min … 1 hr) are eligible for SB status.
    """
    bests: dict = {}  # (season, event_key) → (best_pace, rid)
    for r in workouts:
        if r["is_interval"] or r["cat_key"] is None or r["pace"] is None:
            continue
        key = (r["season"], r["cat_key"])
        rid = r["id"]
        if key not in bests or r["pace"] < bests[key][0]:
            bests[key] = (r["pace"], rid)
    return {rid for _, rid in bests.values() if rid is not None}


# ---------------------------------------------------------------------------
# Point preparation
# ---------------------------------------------------------------------------


def _severity_hsl(category: str | None) -> tuple:
    """Return (h, s, l) for a workout's severity category in 'severity' color mode.

    Workouts without a severity value (None) get a neutral grey.
    """
    if not category:
        return (0, 0, 50)  # neutral grey
    rgba = SEVERITY_STYLE[category]["bg"]
    r, g, b = rgba[0] / 255.0, rgba[1] / 255.0, rgba[2] / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        h = 0.0
        s = 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6 if g < b else 0)
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h *= 60
    return (round(h), round(s * 100), round(l * 100))


def prepare_points(
    workouts: list,
    sb_ids: set,
    show_watts: bool = False,
    color_mode: str = "gander",
) -> list:
    """
    Convert raw workout dicts into the compact point dicts expected by the JS plugin.
    Returns list sorted largest-dist-first so big dots render behind small ones.

    color_mode = "gander" (default): each workout gets a deterministic palette
    colour from its id.  color_mode = "severity": each workout is coloured by
    its severity category (workouts must already have ``_severity``
    attached); workouts without a severity value get a neutral grey.

    Each dict has:
      id        — workout id (for click-to-open)
      x         — ms timestamp
      y         — pace (sec/500m) or watts depending on show_watts, rounded to 2dp
      r         — outer dot radius (px)  = ½√total_m
      r2        — inner fill radius (px); equals r for non-intervals
      c         — full-opacity HSLA color string
      c33       — 33% opacity (regular dot fill)
      c25       — 25% opacity (hatch tile background, work area)
      c60       — 60% opacity (interval circle border)
      cHatch    — 60% opacity (hatch stripe color; independent from c60)
      c70       — 70% opacity (overview in-window dots)
      ivl       — bool: is interval workout
      sb        — bool: is season best
      dist      — total meters (work + rest) — used for draw-order sort
      work_m    — work meters
      rest_m    — rest meters (0 for non-intervals)
      ivl_desc  — list[str]: one tooltip line per structural block
      rest_desc — totals summary string ("Xm work  ·  Ym rest")
      date_str  — formatted date for tooltip
      dist_str  — formatted distance for tooltip (total meters for intervals)
    """
    pts = []
    for r in workouts:
        x = r["date_ms"]
        if not x:
            continue
        pace = r["pace"]
        if pace is None or not (70.0 <= pace <= 420.0):
            continue
        y_val = round(r["watts"], 1) if show_watts else round(pace, 2)

        dist = r.get("distance") or 0
        is_ivl = r["is_interval"]
        rid = r["id"]

        # Outer radius: ½√(total meters including rest)
        # For interval workouts r["distance"] = work meters only;
        # r["rest_distance"] = rest meters (top-level field from the API).
        if is_ivl:
            work_m = dist  # work meters only (API field)
            rest_m = r.get("rest_distance") or 0  # rest meters (top-level API field)
            total_m = work_m + rest_m
            radius = round(_dot_r(total_m), 2)
            radius2 = round(radius * (work_m / total_m), 2) if total_m > 0 else radius

            ivl_desc = _build_interval_lines(r)  # list[str], one line per block
            rest_desc = _interval_totals(round(work_m), round(rest_m))
            dist_str = f"{total_m:,}m" if total_m else ""
        else:
            work_m = dist
            rest_m = 0
            total_m = dist
            radius = round(_dot_r(dist), 2)
            radius2 = radius
            ivl_desc = []
            rest_desc = ""
            dist_str = f"{dist:,}m" if dist else ""

        if color_mode == "severity":
            h, s, l_ = _severity_hsl(r.get("_severity"))
        else:
            # Deterministic color from workout ID
            idx = int(hashlib.md5(str(rid).encode()).hexdigest(), 16) % len(_PALETTE)
            h, s, l_ = _PALETTE[idx]

        pts.append(
            {
                "id": rid,
                "x": x,
                "y": y_val,
                "r": radius,
                "r2": radius2,
                # color variants — each serves a specific visual role;
                # keep them separate so they can be tuned independently.
                "c": _hsla(h, s, l_, 1.00),  # full opacity (outlines)
                "c33": _hsla(h, s, l_, 0.33),  # regular dot fill
                "c25": _hsla(h, s, l_, 0.25),  # hatch tile background (work area)
                "c60": _hsla(h, s, l_, 1.00),  # interval circle border
                "cHatch": _hsla(h, s, l_, 0.60),  # hatch stripe color
                "c70": _hsla(h, s, l_, 0.70),  # overview in-window dots
                # Metadata
                "ivl": is_ivl,
                "sb": rid in sb_ids,
                "dist": total_m,  # used for draw-order sort
                "work_m": round(work_m),
                "rest_m": round(rest_m),
                "ivl_desc": ivl_desc,  # list[str] — one line per block
                "rest_desc": rest_desc,  # totals summary string
                "date_str": fmt_date(r["date"]),
                "dist_str": dist_str,
            }
        )

    pts.sort(key=lambda p: p["dist"], reverse=True)
    return pts


# ---------------------------------------------------------------------------
# Window calculation
# ---------------------------------------------------------------------------


def window_bounds_ms(
    all_ms: list, window_size: str, window_end_ms: int, window_start_ms: int = 0
) -> tuple:
    """
    Return (start_ms, end_ms) for the current view window.

    window_end_ms is the right edge; if 0 (uninitialised) defaults to latest workout.
    window_start_ms overrides the left edge when non-zero (set after a brush resize).
    When 0, the left edge is derived from window_size.
    """
    days = _WINDOW_DAYS.get(window_size, 183)
    window_ms = days * _MS_PER_DAY

    if not all_ms:
        now_ms = int(datetime.now().timestamp() * 1_000)
        return now_ms - window_ms, now_ms

    min_ms = min(all_ms)
    max_ms = max(all_ms)

    end_ms = window_end_ms if window_end_ms else max_ms
    end_ms = min(end_ms, max_ms)

    if window_start_ms:
        # Custom start from a brush resize — honor it directly.
        start_ms = max(window_start_ms, min_ms)
        end_ms = max(end_ms, start_ms + _MS_PER_DAY)
    else:
        # Derive start from the preset window size.
        end_ms = max(end_ms, min_ms + window_ms)
        start_ms = max(end_ms - window_ms, min_ms)

    return start_ms, end_ms


def step_ms(all_ms: list, window_size: str) -> int:
    """75% of the window width — used for ◄/► button steps."""
    days = _WINDOW_DAYS.get(window_size, 183)
    return int(days * _MS_PER_DAY * 0.75)



# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------
# Page-level UI state lives in a connection-wide ``@hd.global_state`` so it
# survives the round-trip when the user clicks a workout, lands on
# ``/workout/<id>``, and presses the browser back button.  An ordinary
# ``hd.state(...)`` call is keyed on the call-site stack — robust against
# navigation in practice but fragile against code edits and not idiomatic
# for state that's meant to outlive a single render of this page.


@hd.global_state
class WorkoutsPageState(hd.BaseState):
    window_size = hd.Prop(hd.String, "Year")
    # 0 = uninitialised → defaults to latest workout
    window_end_ms = hd.Prop(hd.Int, 0)
    # 0 = derive from window_size; non-zero after a brush resize
    window_start_ms = hd.Prop(hd.Int, 0)
    last_change_id = hd.Prop(hd.Int, 0)
    last_click_seq = hd.Prop(hd.Int, 0)
    filter_10k = hd.Prop(hd.Bool, False)
    # "All" | "Intervals" | "Continuous"
    filter_ivl = hd.Prop(hd.String, "All")
    # False = pace (sec/500m); True = watts
    show_watts = hd.Prop(hd.Bool, False)
    # "gander" | "severity"
    color_mode = hd.Prop(hd.String, "gander")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def workouts_page() -> None:
    """
    Render the pace-vs-date focus+context chart with brush navigator,
    workout filters, and an in-window workouts table.
    """
    state = WorkoutsPageState()


    """Top-level component for the Workouts page."""
    result = get_all_workouts()
    profile = get_profile() or {}

    if not result:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    if not profile or AppContext().sessions_dict is None: 
        hd.box(padding=2, min_height="80vh")
        return


    # ── Pace-vs-date scatter + windowed workouts table ────────────────────────
    all_workouts = result[1]


    # ── Attach Power Spread + HR Spread fields ────────────────────────────────
    # Block on the reference-watts loader so the spread metrics resolve.
    if not reference_watts_loader(all_workouts):
        return

    workouts = all_workouts #_apply_outlier_filter(all_workouts)

    max_hr, _ = resolve_max_hr(profile, workouts)
    attach_spread(workouts, workouts, max_hr)
    # ``with_timeline=False`` skips the per-second timeline build inside
    # ``compute_session_metrics``: the workouts table never reads
    # ``_ess_timeline`` (it's stripped from the JS row payload via
    # ``_TABLE_IRRELEVANT_KEYS``), so the ~1.8 M dict allocations are pure
    # waste here.  The workout detail page calls ``attach_ess_metrics``
    # again with the default and renders the chart from that scope.
    attach_ess_metrics(
        workouts,
        workouts,
        AppContext().sessions_dict or {},
        profile,
        max_hr,
        with_timeline=False,
    )

    # ── Apply filters ──────────────────────────────────────────────────────────

    filtered = workouts

    if state.filter_10k:
        filtered = [
            r
            for r in filtered
            if (r.get("distance") or 0) + (r.get("rest_distance") or 0) >= 10_000
        ]

    if state.filter_ivl == "Intervals":
        filtered = [
            r for r in filtered if r["is_interval"]
        ]
    elif state.filter_ivl == "Continuous":
        filtered = [
            r for r in filtered if not r["is_interval"]
        ]


    spread_severity_filters = SpreadSeverityFilters()
    # Apply Power Spread / HR Spread / Severity legend filters (disjunctive within,
    # conjunctive across).
    if spread_severity_filters.active_bins:
        sel = set(spread_severity_filters.active_bins)
        filtered = [
            r for r in filtered
            if any(
                power_bin_passes(r.get("_zone_bin_fractions") or [], i)
                for i in sel
            )
        ]
    if spread_severity_filters.active_hr_bins:
        sel = set(spread_severity_filters.active_hr_bins)
        filtered = [
            r for r in filtered
            if any(hr_bin_passes(r.get("_hr_bin_meters"), i) for i in sel)
        ]
    if spread_severity_filters.active_severity:
        sel = set(spread_severity_filters.active_severity)
        filtered = [r for r in filtered if r.get("_severity") in sel]
    if spread_severity_filters.active_stimulus_bands:
        sel = set(spread_severity_filters.active_stimulus_bands)
        # Workout passes if dose ≥ 1.0 for *any* selected band.
        # ``_stimulus_doses`` is a dict keyed by band-seconds (int).
        def _stim_passes(r: dict) -> bool:
            doses = r.get("_stimulus_doses") or {}
            return any(float(doses.get(b, 0.0)) >= 1.0 for b in sel)
        filtered = [r for r in filtered if _stim_passes(r)]

    sb_ids = compute_sb_ids(filtered)
    pts = prepare_points(
        filtered, sb_ids, show_watts=state.show_watts, color_mode=state.color_mode
    )

    if not pts:
        hd.text("No workouts match the selected filters.", font_color="neutral-500")
        return

    all_ms = [p["x"] for p in pts]

    # ── Compute target window ─────────────────────────────────────────────────
    target_start, target_end = window_bounds_ms(
        all_ms, state.window_size, state.window_end_ms, state.window_start_ms
    )

    with hd.box(padding=2, min_height="80vh", gap=2):

        with hd.box(gap=1, justify="center", align="center"):
            with hd.box(gap=0.2, align="center"):

                with hd.h1(font_weight="normal"):
                    with hd.hbox(gap=0, align="center"):
                        # Verb selector — same styling as Race page header dropdowns.
                        header_dropdown(
                            state,
                            key="color_mode_dd",
                            labels={
                                "gander": "Take a Gander at",
                                "severity": "Gape at the Severity of",
                            },
                            current_value=state.color_mode,
                            field="color_mode",
                        )
                        with hd.dropdown() as _workouts_dd:
                            from components.app_context import your as _your

                            _poss = _your()
                            _workouts_label = f"All {_poss}{" Long " if state.filter_10k else " "} {" " if state.filter_ivl == "All" else state.filter_ivl} Work"
                            _workouts_btn = hd.button(
                                _workouts_label, caret=True, size="small", font_color="neutral-800",font_size=2,font_weight="bold", padding=(1, 0, 1, 0),border="none",label_style=hd.style(padding_right=0), slot=_workouts_dd.trigger
                            )
                            if _workouts_btn.clicked:
                                _workouts_dd.opened = not _workouts_dd.opened

                            with hd.hbox(gap=1, padding=1,background_color="neutral-50", align="center"):
                                with hd.radio_group(value=state.filter_ivl) as ivl_rg:
                                    hd.radio_button("All", size="small")
                                    hd.radio_button("Intervals", size="small")
                                    hd.radio_button("Continuous", size="small")
                                if ivl_rg.changed:
                                    state.filter_ivl = ivl_rg.value
                                cb_10k = hd.checkbox("10k+", checked=state.filter_10k)
                                if cb_10k.changed:
                                    state.filter_10k = cb_10k.checked
                global_filter_ui()


            # with hd.hbox(gap=2, align="center", wrap="wrap", padding_bottom=1):
            #         with hd.radio_group(value=state.window_size) as rg:
            #             hd.radio_button("Month", size="small")
            #             hd.radio_button("Quarter", size="small")
            #             hd.radio_button("Season", size="small")
            #             hd.radio_button("Year", size="small")
            #             hd.radio_button("2 Years", size="small")
            #             hd.radio_button("All", size="small")

            #         if rg.changed:
            #             state.window_size = rg.value
            #             state.window_end_ms = 0  # snap to latest when window size changes

            #     with hd.radio_group(value=state.filter_ivl) as ivl_rg:
            #         hd.radio_button("All", size="small")
            #         hd.radio_button("Intervals Only", size="small")
            #         hd.radio_button("No Intervals", size="small")
            #     if ivl_rg.changed:
            #         state.filter_ivl = ivl_rg.value
            # with hd.scope("filter_10k"):
            #     cb_10k = hd.checkbox("10k+", checked=state.filter_10k)
            #     if cb_10k.changed:
            #         state.filter_10k = cb_10k.checked


            # ── Plugin ────────────────────────────────────────────────────────────────
            chart = WorkoutsChart(
                points=pts,
                target_window_start=target_start,
                target_window_end=target_end,
                is_dark=hd.theme().is_dark,
                show_watts=state.show_watts,
                height="75vh",
            )

            # ── Controls ───────────────────────────────────────────────────────────────

            with hd.hbox(gap=2, align="center", wrap="wrap", padding_bottom=1):

                with hd.radio_group(
                    value="Watts" if state.show_watts else "Pace"
                ) as metric_rg:
                    hd.radio_button("Pace", size="small")
                    hd.radio_button("Watts", size="small")
                if metric_rg.changed:
                    state.show_watts = metric_rg.value == "Watts"

            # ── Sync window bounds from brush drags / resizes ─────────────────────────
            if chart.change_id != state.last_change_id:
                state.last_change_id = chart.change_id
                state.window_end_ms = chart.brush_end
                state.window_start_ms = chart.brush_start

            # ── Click-to-open: navigate to /workout/<id> when a chart dot is clicked ──
            if chart.click_seq > state.last_click_seq:
                state.last_click_seq = chart.click_seq
                if chart.clicked_workout_id:
                    _ctx = AppContext()
                    _prefix = f"/u/{_ctx.user_id}" if _ctx.is_public else ""
                    hd.location().go(path=f"{_prefix}/workout/{chart.clicked_workout_id}")

            # ── Spread + Severity legend (Power Spread / HR Spread / Severity) ────────
            spread_severity_legends(max_hr)

            # ── Workouts-in-view table ────────────────────────────────────────────────
            in_window = [
                r
                for r in filtered
                if target_start <= r["date_ms"] <= target_end
            ]
            in_window.sort(key=lambda r: r["date"], reverse=True)
            if in_window:
                in_window_by_id = {str(r["id"]): r for r in in_window}


                with hd.box(padding=(2, 0, 0, 0), align="center", width="100%"):
                    hd.h2(f"Sessions in View")
                    WorkoutTable(
                        in_window,
                        [
                            "date",
                            "time_of_day",
                            "main_work",
                            "work_duration",
                            "pace",
                            "watts",
                            "distance_combined",
                            # "work_distance",
                            # "other_distance",
                            "spm",
                            "drag",
                            "hr",
                            # "power_spread",
                            "severity",
                            "stimulus",
                            "ess",
                            "glycogen_used",
                            "link",
                        ],
                        tree_mode=True,
                        sessions_dict=AppContext().sessions_dict,
                    )

