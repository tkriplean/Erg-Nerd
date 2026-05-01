"""
Performance Page — UI and orchestration for the ranked-events view.

Exported:
  power_curve_page()   — top-level HyperDiv component; call from app.py

See docs/power_curve_page.md for a full reference.

Helper logic is split across:
  services/formatters.py              — formatting helpers
  components/workout_table            — WorkoutTable
  services/rowing_utils.py            — quality filters, season helpers, PB/SB aggregators
  services/predictions.py             — Predictor registry + samplers + prediction-table builder
  components/power_curve_workouts.py  — FilterSpec + WorkoutView + build_workout_view
  components/power_curve_animation.py — timeline snapshot + keyframe build +
                                        bundle lifecycle + SB annotations
  components/power_curve_chart_prediction_datasets.py — wr prediction dataset
  components/concept2_sync.py         — concept2_sync + load_world_record_data

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT (inside power_curve_page)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Filter bar:
    Include [All|PBs|SBs]  |  Events [dropdown]

  Chart box:
    Header: "Qualifying Performances through <date>"
    RowingLevel warning (only when predictor == "rowinglevel" and profile incomplete)
    Transport bar: ▶/⏸  speed-cycle-button
    PowerCurveChart (75vh, with integrated timeline scrubber)

    Settings Row 1:
      [ [log] Intensity: [Pace|Watts] ]  |  [ [log] Length: [Distance|Duration] ]
      |  Power curves: [PBs|SBs|None]
    Settings Row 2:
      Prediction: <custom dropdown>   [Show components]  [predictor description]
      Paul's Law richer description (when PL selected and personalised K available)

  RowingLevel profile notice (when profile incomplete, dismissible)
  Prediction table (CP, Log-Log, Paul's Law, RowingLevel, Average columns)
  Workout count / result_table()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATE VARIABLES  (declared at the top of power_curve_page())
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  dist_enabled       tuple[bool]   one flag per ranked_distances(machine) entry (index-aligned)
  time_enabled       tuple[bool]   one flag per ranked_times(machine) entry (index-aligned)
  best_filter        str           "All" | "PBs" | "SBs" — row filter for display/table
  chart_y_metric       str           "Pace" | "Watts"
  chart_x_metric       str           "distance" | "duration"
  chart_predictor    str           one of PREDICTORS_BY_KEY keys (see services/predictions.py)
  overlay_bests      str           "PBs" | "SBs" | "None" — which best-curves to draw
  chart_log_x        bool          log scale on x-axis
  chart_log_y        bool          log scale on y-axis
  chart_show_components bool       show component sub-curves for supported predictors
  timeline_day       int|None      day offset from sim_start; None = end of timeline
  sim_speed          str           one of _SPEED_OPTIONS: "0.5x"|"1x"|"4x"|"16x"
  sim_playing        bool          whether the animation is running
  sim_bundle         dict|None     final js_payload sent to Chart.js (current bundle_data + style wrapper + merged snapshots)
  sim_bundle_key     str           combined identity-selection-style key baked into sim_bundle
  sim_snapshot_cache dict          {selection_key: (bundle_data, pred_lookup)}; LRU bounded
  sim_full_selections frozenset    selections whose bg build has finished (fast-bundle replaced)
  sim_identity_key   str           hash of selection-independent data inputs; change -> clear cache
  sim_pred_lookup    dict          current selection's {keyframe_day: {pred_rows, pauls_k_fit, accuracy}}
  last_sim_day_out   int           tracks chart.sim_day_out changes (ticks + user seeks)
  last_sim_done      int           tracks chart.sim_done changes to detect animation end
  workout_view       WorkoutView|None  4-stage filtering pipeline result (see power_curve_workouts.py)
  _view_key          tuple         (hash(FilterSpec), workout_count) — invalidates workout_view

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SIMULATION / TIMELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  sim_start  = May 1 of the earliest included season
  sim_end    = min(date.today(), Apr 30 of the year after the latest included season)
  total_days = (sim_end - sim_start).days + 1

  timeline_day is None  means "end of timeline / show all data"
  _SPEED_OPTIONS = ("0.5x", "1x", "4x", "16x")
  _SPEED_DAYS    = {"0.5x": 1, "1x": 7, "4x": 30, "16x": 91}  — days per tick

  Play button: when pressed at end-of-timeline, rewinds to 30 days before the
  first qualifying event so something appears immediately.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SEASONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Format: "YYYY-YY"  e.g. "2024-25", spanning May 1 -> Apr 30.
  all_seasons: sorted newest-first (seasons_from uses sorted(..., reverse=True)).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART / PREDICTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Chart rendered by PowerCurveChart (components/power_curve_chart_plugin.py), an hd.Plugin
  wrapping Chart.js with custom JS in chart_assets/power_curve_chart_plugin.js.

  Custom JS features:
    - canvasLabelsPlugin: stable overlay labels stored in a Map keyed by
      "${x}|${line_label}" — positions never move once assigned.
    - isPrediction: true  dataset property — controls tooltip and point styling.
    - Prediction line style: amber, borderDash: [5, 4], small hoverable points.
    - Gridlines: x-axis at every ranked distance/duration; y-axis every 5s/pace or 50W.
    - CP crossover: dashed teal vertical line + bottom-anchored text (show_components only).

  Prediction colors:
    dark:   rgba(220, 160, 55, 0.80)
    light:  rgba(185, 120, 20, 0.80)

  x_mode "duration": x-axis is time in seconds instead of meters.  Scatter
  points use workout["time"]/10; prediction curves are transformed via
  dist * pace / 500.
"""

import json
from datetime import date, timedelta
import hyperdiv as hd

from services.rowinglevel import fetch_all_pb_predictions
from services.rowing_utils import (
    apply_best_only,
    ranked_distances,
    ranked_times,
    compute_watts,
)
from components.concept2_sync import sync_workouts
from components.app_context import get_profile
from services.rowing_utils import profile_complete
from services.rowinglevel import fetch_rowinglevel

from services.concept2_records import wr_category_label, wr_machine_supported
from components.power_curve_chart_plugin import PowerCurveChart
from components.workout_table import WorkoutTable

from services.predictions import PREDICTORS, PREDICTORS_BY_KEY
from components.power_curve_workouts import (
    FilterSpec,
    WorkoutView,
    build_season_meta,
    build_workout_view,
    build_workouts_prop,
)
from components.power_curve_animation import (
    build_sb_annotations,
    lookup_bundle_entry,
    manage_animation_bundle,
)
from components.concept2_sync import load_world_record_data
from components.hyperdiv_extensions import radio_group, grid_box
from components.shared_ui import global_filter_ui
from services.global_state import GlobalFilters

from components.prediction_table import prediction_table


# ---------------------------------------------------------------------------
# Constants local to this module
# ---------------------------------------------------------------------------

_SPEED_OPTIONS = ("0.5x", "1x", "4x", "16x")
_SPEED_DAYS = {"0.5x": 1, "1x": 7, "4x": 30, "16x": 91}
_SIM_LOOKAHEAD_STEPS = 4  # ghost/arrow lookahead = this many sim steps ahead


# ---------------------------------------------------------------------------
# Sub-component: chart section
# ---------------------------------------------------------------------------


def _chart_section(
    *,
    rl_predictions: dict,
    profile: dict,
    show_watts: bool,
    total_days: int,
    sim_start,
    sb_annotations: list,
    pauls_k_fit: float | None = None,
    sim_command: str = "stop",
    workouts_prop: list,
    season_meta_prop: list,
    selected_dists: set,
    selected_times: set,
    is_dark: bool,
) -> None:
    """
    Renders the performance chart box: header, RL status,
    PowerCurveChart (with integrated transport bar + timeline scrubber),
    settings row, and components toggle.

    All transport interaction (Play/Pause, Speed, seeking) is handled entirely
    inside the PowerCurveChart plugin — no Python UI is rendered for it.
    """

    state = PowerCurveState()
    is_dark = hd.theme().is_dark

    if state.sim_bundle is None:
        hd.text("No chart data available.", font_color="neutral-500")
        return

    with hd.hbox(align="center", width="100%"):
        with hd.box(align="center", gap=0.5):
            with hd.button(
                size="small",
                border_radius="small",
                variant="primary" if state.chart_log_y else "default",
                pill=True,
                width="100%",
            ) as _log_y_btn:
                hd.text("Log")

            if _log_y_btn.clicked:
                state.chart_log_y = not state.chart_log_y

            with radio_group(value=state.chart_y_metric, size="small") as rg:
                with hd.box(gap=0):
                    hd.radio_button(
                        "Pace",
                        value="pace",
                        width="100%",
                        button_style=hd.style(border_radius="0px"),
                    )
                    hd.radio_button(
                        "Watts",
                        value="watts",
                        width="100%",
                        button_style=hd.style(border_radius="0px"),
                    )
            if rg.changed:
                state.chart_y_metric = rg.value

        with hd.box(width="100%"):
            # ---- chart (includes integrated transport bar + timeline scrubber) ----
            chart = PowerCurveChart(
                workouts=workouts_prop,
                season_meta=season_meta_prop,
                best_filter=state.best_filter,
                overlay_bests=state.overlay_bests,
                selected_dists=sorted(selected_dists),
                selected_times=sorted(selected_times),
                is_dark=is_dark,
                show_watts=show_watts,
                x_mode=state.chart_x_metric,
                sim_bundle=state.sim_bundle,
                sim_command=sim_command,
                sim_speed=state.sim_speed,
                timeline_min=0,
                timeline_max=max(1, total_days),
                timeline_start_date=sim_start.isoformat(),
                timeline_annotations=sb_annotations,
            )

            # ── Back-communication from JS transport + animation ──────────────
            # JS sends sim_playing_out when the user clicks Play or Pause.
            # Python uses this to gate bundle loading (sim_playing state).
            if chart.sim_playing_out != state.sim_playing:
                state.sim_playing = chart.sim_playing_out

            # JS writes sim_day_out on every tick and after user seeks.
            # Always update timeline_day so Python knows the current position
            # (used to rebuild the static config when paused).
            if chart.sim_day_out != state.last_sim_day_out:
                state.last_sim_day_out = chart.sim_day_out
                if chart.sim_day_out >= 0:
                    state.timeline_day = chart.sim_day_out

            # JS increments sim_done when the animation completes.
            if chart.sim_done != state.last_sim_done:
                state.last_sim_done = chart.sim_done
                state.sim_playing = False

    _chart_settings(state, profile, pauls_k_fit)


def _chart_settings(state, profile, pauls_k_fit):
    # ---- Chart settings ----
    with hd.box(gap=2, align="center"):
        with hd.hbox(gap=0.2, align="center"):
            with hd.button(
                size="small",
                border_radius="small",
                variant="primary" if state.chart_log_x else "default",
                pill=True,
                width="100%",
            ) as _log_x_btn:
                hd.text("Log")

            if _log_x_btn.clicked:
                state.chart_log_x = not state.chart_log_x

            with radio_group(value=state.chart_x_metric, size="small") as rg:
                hd.radio_button(
                    "Distance",
                    value="distance",
                    width="100%",
                    button_style=hd.style(border_radius="0px"),
                )
                hd.radio_button(
                    "Duration",
                    value="duration",
                    width="100%",
                    button_style=hd.style(border_radius="0px"),
                )
            if rg.changed:
                state.chart_x_metric = rg.value

        with hd.hbox(gap=3, align="center"):
            with hd.hbox(gap=0.5, align="center", justify="center", wrap="wrap"):
                hd.text("Draw a prediction line based on", font_size="medium")

                # Build dropdown options from the registry; Paul's Law's description
                # gets a dynamic personalised-K suffix when a fit is available.
                def _pred_desc(p):
                    if p.key == "pauls_law" and pauls_k_fit is not None:
                        return f"{p.extended_description} Personalised to your K = {pauls_k_fit:.1f}s/doubling."
                    return p.extended_description

                _section_machine = GlobalFilters().machine
                _PRED_OPTIONS = [
                    (p.key, p.name, _pred_desc(p))
                    for p in PREDICTORS
                    if not (p.key == "rowinglevel" and _section_machine != "rower")
                ]
                _pred_name = PREDICTORS_BY_KEY.get(
                    state.chart_predictor,
                    PREDICTORS_BY_KEY["none"],
                ).name

                # Custom prediction dropdown (name + description per option).
                with hd.dropdown(_pred_name, min_width=30, grow=True) as _pred_dd:
                    with hd.box(background_color="neutral-0", align="start", gap=0.2):
                        for _pval, _pname, _pdesc in _PRED_OPTIONS:
                            with hd.scope(f"pred_{_pval}"):
                                _is_active = state.chart_predictor == _pval
                                with hd.box(
                                    grow=True,
                                    gap=0,
                                    border="none",
                                    padding_bottom=0.25,
                                    border_bottom="1px solid neutral-200",
                                    background_color="primary-50"
                                    if _is_active
                                    else "neutral-0",
                                    width="100%",
                                    height="100%",
                                ):
                                    _opt_button = hd.button(
                                        _pname,
                                        variant="text",
                                        font_weight="bold",
                                        font_size="medium",
                                        font_color="primary",
                                        padding=(0, 1, 0, 1),
                                    )

                                    hd.text(
                                        _pdesc,
                                        font_color="neutral-600",
                                        font_size="small",
                                        min_width=40,
                                        padding=(0, 2, 0, 2),
                                    )
                                if _opt_button.clicked:
                                    state.chart_predictor = _pval
                                    _pred_dd.opened = False

                # Gear icon → component lines dropdown (only for predictors that support it).
                _pred_meta = PREDICTORS_BY_KEY[state.chart_predictor]
                if _pred_meta.computed_from_components:
                    with hd.dropdown() as _comp_dd:
                        _gear_btn = hd.icon_button(
                            "gear-fill" if state.chart_show_components else "gear",
                            font_size="medium",
                            font_color="primary"
                            if state.chart_show_components
                            else "neutral-500",
                            slot=_comp_dd.trigger,
                        )
                        if _gear_btn.clicked:
                            _comp_dd.opened = not _comp_dd.opened

                        with hd.box(
                            gap=2,
                            padding=1,
                            background_color="neutral-0",
                            min_width=22,
                            align="start",
                        ):
                            with hd.box(
                                gap=0.75,
                            ):
                                _comp_cb = hd.checkbox(
                                    _pred_meta.component_label,
                                    checked=state.chart_show_components,
                                )
                                if _comp_cb.changed:
                                    state.chart_show_components = _comp_cb.checked
                                hd.text(
                                    _pred_meta.component_desc,
                                    font_size="small",
                                    font_color="neutral-500",
                                )

                            # ---- Paul's Law personalised K scale graphic ----
                            if (
                                state.chart_predictor == "pauls_law"
                                and pauls_k_fit is not None
                            ):
                                _K_MIN, _K_MAX = 1.0, 9.0
                                _pos_def = (5.0 - _K_MIN) / (_K_MAX - _K_MIN)
                                _pos_usr = max(
                                    0.0,
                                    min(
                                        1.0,
                                        (pauls_k_fit - _K_MIN) / (_K_MAX - _K_MIN),
                                    ),
                                )
                                _SC = 1000
                                _gd = max(1, round(_pos_def * _SC))
                                _gu = max(1, round(_pos_usr * _SC))

                                with hd.box(gap=0.3, padding=("0.25rem", 0)):
                                    # Default (5.0s) marker — label + arrow above bar
                                    with hd.hbox(gap=0, align="end"):
                                        hd.box(grow=_gd)
                                        with hd.box(align="center", gap=0):
                                            hd.text(
                                                "5.0s - default",
                                                font_size="small",
                                                font_color="neutral-500",
                                            )
                                            hd.text(
                                                "▾",
                                                font_size="medium",
                                                font_color="neutral-500",
                                            )
                                        hd.box(grow=_SC - _gd)

                                    hd.box(
                                        grow=1,
                                        height=0.4,
                                        background_color=f"neutral-600",
                                    )

                                    # Side labels: aerobic ←→ sprint dominant
                                    with hd.hbox():
                                        hd.text(
                                            "aerobic",
                                            font_size="small",
                                            font_color="neutral-500",
                                        )
                                        hd.box(grow=1)
                                        hd.text(
                                            "sprint dominant",
                                            font_size="small",
                                            font_color="neutral-500",
                                        )

                                    # User marker — arrow + value below bar
                                    with hd.hbox(gap=0, align="start"):
                                        hd.box(grow=_gu)
                                        with hd.box(align="center", gap=0):
                                            hd.text(
                                                "▴",
                                                font_size="medium",
                                                font_color="primary-500",
                                            )
                                            hd.text(
                                                f"{pauls_k_fit:.1f}s — you",
                                                font_size="small",
                                                font_color="primary-600",
                                            )
                                        hd.box(grow=_SC - _gu)

                                    hd.text(
                                        f"Paul's Law predicts a balanced athlete's pace will slow 5 seconds per distance doubling.",
                                        font_color="neutral-600",
                                        font_size="small",
                                    )

                else:
                    # Keep show_components False when predictor doesn't support it.
                    if state.chart_show_components:
                        state.chart_show_components = False

            with hd.box():
                # ---- World-class comparison toggles ----
                _wr_compare_section(state, profile)


# ---------------------------------------------------------------------------
# Sub-component: World-class comparison toggles
# ---------------------------------------------------------------------------


def _wr_compare_section(state, profile: dict) -> None:
    """
    Renders the 'Compare vs world record' toggle.
    Placed below the Settings Row 2 prediction box.
    """

    # No WR data for the current machine (e.g. bikeerg) → nothing to render.
    if not wr_machine_supported(GlobalFilters().machine):
        return

    _loading = (
        state.chart_compare_wc and not state.wr_fetch_done and state.wr_fetch_key != ""
    )
    _failed = state.chart_compare_wc and state.wr_fetch_done and state.wr_data is None

    # Compare toggle
    wr_label = wr_category_label(profile)
    if wr_label is None:
        hd.text(
            "Set age, gender and weight in Profile to compare vs world record.",
            font_color="neutral-500",
            font_size="small",
        )
        return

    compare_to_wr_sel = hd.switch(f"Show {wr_label} world records", size="medium")

    if compare_to_wr_sel.changed:
        state.chart_compare_wc = compare_to_wr_sel.value

    if _loading:
        with hd.hbox(gap=0.5, align="center"):
            hd.spinner()
            hd.text(
                "Fetching world records…",
                font_size="small",
                font_color="neutral-400",
            )
    if _failed:
        hd.text(
            "Could not fetch world records.",
            font_size="small",
            font_color="warning-600",
        )


# ---------------------------------------------------------------------------
# Sub-component: RL profile incomplete notice
# ---------------------------------------------------------------------------


def _rl_profile_notice() -> None:
    """Dismissible warning shown between the chart and prediction table when the
    user profile is incomplete (missing gender, age, or weight) and RowingLevel
    predictions are therefore unavailable.  Dismissed state is persisted to
    localStorage so the user doesn't see it again after closing it.
    """
    s = hd.state(loaded=False, dismissed=False)
    if not s.loaded:
        ls = hd.local_storage.get_item("rl_notif_dismissed")
        if not ls.done:
            return  # still loading — show nothing, tab is loading anyway
        s.dismissed = bool(ls.result)
        s.loaded = True
    if s.dismissed:
        return
    with hd.hbox(
        gap=1,
        align="center",
        padding=1,
        border_radius="medium",
        background_color="warning-50",
        border="1px solid warning-200",
        margin=(1, 0),
    ):
        hd.icon("exclamation-triangle", font_color="warning-600", font_size=1.1)
        with hd.hbox(grow=True, gap=0.1):
            hd.text(
                "RowingLevel predictions aren't available until Age, Weight, and Gender "
                "are filled out in ",
                font_color="warning-800",
                font_size="small",
            )
            hd.link(
                "your Profile",
                href="/profile",
                font_color="warning-800",
                font_size="small",
                underline=True,
            )
            hd.text(
                ".",
                font_color="warning-800",
                font_size="small",
            )

        if hd.icon_button("x-lg", font_size="small", font_color="warning-600").clicked:
            s.dismissed = True
            hd.local_storage.set_item("rl_notif_dismissed", "1")


# ---------------------------------------------------------------------------
# Sub-component: prediction table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------


def _compute_sim_timeline(
    excluded_seasons, all_seasons: list, timeline_day: int | None
) -> tuple:
    """
    Derive the simulation timeline from the included seasons.
    Returns (sim_start, total_days, timeline_date, at_today, included_seasons).
    timeline_day=None means "end of timeline / show all data".
    """
    included_seasons = [s for s in all_seasons if s not in set(excluded_seasons)]
    if included_seasons:
        ey = int(min(included_seasons)[:4])
        sim_start = date(ey, 5, 1)
        max_end_year = int(max(included_seasons)[:4]) + 1
        sim_end = min(date.today(), date(max_end_year, 4, 30))
    else:
        sim_start = date.today() - timedelta(days=365)
        sim_end = date.today()
    total_days = max(1, (sim_end - sim_start).days + 1)
    if timeline_day is None:
        sim_day_idx = total_days - 1
    else:
        sim_day_idx = max(0, min(timeline_day, total_days - 1))
    timeline_date = sim_start + timedelta(days=sim_day_idx)
    at_today = sim_day_idx >= total_days - 1
    return sim_start, total_days, timeline_date, at_today, included_seasons


# ───────────────────────────────────────────────────────────────────────────
# Axis bounds — stable x/y bounds from all-time PBs
# ───────────────────────────────────────────────────────────────────────────


def compute_axis_bounds(
    quality_efforts: list,
    show_watts: bool,
    use_duration: bool,
    log_x: bool,
    wr_data=None,
) -> tuple:
    """Stable x/y bounds from all-time PBs so the chart doesn't rescale when
    the user toggles individual events.  Returns (x_bounds, y_bounds);
    either may be None if data is insufficient."""
    bests = apply_best_only(quality_efforts)
    if not bests:
        return None, None
    bp = [p for w in bests if (p := w["pace"]) and 60 < p < 400]
    if use_duration:
        bx = [
            w.get("distance") * p / 500
            for w in bests
            if w.get("distance") and (p := w["pace"]) and 60 < p < 400
        ]
    else:
        bx = [w.get("distance") for w in bests if w.get("distance")]
    if not bp or not bx:
        return None, None
    xr, xR = min(bx), max(bx)
    x_bounds = (
        (xr / 1.45, xR * 1.45)
        if log_x
        else (
            max(0, xr - max((xR - xr) * 0.1, xr * 0.1)),
            xR + max((xR - xr) * 0.1, xr * 0.1),
        )
    )
    by = [compute_watts(p) if show_watts else p for p in bp]
    yr, yR = min(by), max(by)
    ypad = max((yR - yr) * 0.15, 5 if not show_watts else 2)

    y_bounds = (yr - ypad, yR + ypad)

    if wr_data is not None:
        """Expand y_bounds to include world-class pace/watts values."""
        _wr_y_vals = [
            compute_watts(pace) if show_watts else pace
            for pace in wr_data["lb"].values()
            if pace > 0
        ]
        if _wr_y_vals:
            _ypad = max(
                (y_bounds[1] - y_bounds[0]) * 0.1, 5.0 if not show_watts else 2.0
            )

            y_bounds = (
                min(y_bounds[0], min(_wr_y_vals) - _ypad),
                max(y_bounds[1], max(_wr_y_vals) + _ypad),
            )

    return x_bounds, y_bounds


# ---------------------------------------------------------------------------
# Sub-component: page header
# ---------------------------------------------------------------------------


@hd.cached
def _page_header(
    *,
    timeline_date: date,
) -> None:
    """
    Renders the page title bar: best_filter dropdown, events dropdown, date label.
    """
    state = PowerCurveState()

    _date_label = timeline_date.strftime("%b %d, %Y")
    _best_long = {
        "All": "All Great Efforts",
        "PBs": "Personal Bests",
        "SBs": "Season Bests",
    }
    _cur_best_lbl = _best_long.get(state.best_filter, state.best_filter)

    with hd.box(gap=0.2, align="center"):
        with hd.h1(font_weight="normal"):
            with hd.hbox(
                gap=0,
                align="center",
                padding_bottom=0,
                justify="center",
                wrap="wrap",
            ):
                with hd.dropdown() as _bf_dd:
                    _bf_btn = hd.button(
                        _cur_best_lbl,
                        caret=True,
                        label_style=hd.style(padding_right=0),
                        border="none",
                        font_color="neutral-800",
                        font_size=2,
                        font_weight="bold",
                        padding=(1, 0.5, 1, 0),
                        slot=_bf_dd.trigger,
                    )
                    if _bf_btn.clicked:
                        _bf_dd.opened = not _bf_dd.opened
                    with hd.box(
                        padding=1,
                        gap=1,
                        background_color="neutral-0",
                        min_width=24,
                    ):
                        # ── Plot in graph ─────────────────────────────
                        hd.text(
                            "Plot in graph",
                            font_size="small",
                            font_weight="semibold",
                            font_color="neutral-500",
                        )
                        with radio_group(
                            value=state.best_filter, size="small"
                        ) as _bf_rg:
                            hd.radio_button("All Great Efforts", value="All")
                            hd.radio_button("PBs only", value="PBs")
                            hd.radio_button("SBs only", value="SBs")
                        if _bf_rg.changed:
                            state.best_filter = _bf_rg.value

                        hd.divider()

                        # ── Draw a Power Curve for ────────────────────
                        hd.text(
                            "Draw a Power Curve for",
                            font_size="small",
                            font_weight="semibold",
                            font_color="neutral-500",
                        )
                        with radio_group(
                            value=state.overlay_bests, size="small"
                        ) as _dpc_rg:
                            hd.radio_button("SBs", value="SBs")
                            hd.radio_button("PBs", value="PBs")
                            hd.radio_button("None", value="None")
                        if _dpc_rg.changed:
                            state.overlay_bests = _dpc_rg.value

                # ---- Events dropdown ----
                _hdr_machine = GlobalFilters().machine
                _hdr_dists = ranked_distances(_hdr_machine)
                _hdr_times = ranked_times(_hdr_machine)
                _n_ev_sel = sum(state.dist_enabled) + sum(state.time_enabled)
                _n_ev_tot = len(_hdr_dists) + len(_hdr_times)
                _ev_lbl = "All Events" if _n_ev_sel == _n_ev_tot else "Some Events"

                hd.text("for")

                with hd.dropdown() as _ev_dd:
                    _ev_btn = hd.button(
                        _ev_lbl,
                        font_color="neutral-800",
                        font_size=2,
                        font_weight="bold",
                        caret=True,
                        label_style=hd.style(padding_right=0),
                        border="none",
                        slot=_ev_dd.trigger,
                        padding=(1, 0.5, 1, 0),
                    )
                    if _ev_btn.clicked:
                        _ev_dd.opened = not _ev_dd.opened
                    with hd.box(padding=1, gap=0.5, background_color="neutral-50"):
                        with hd.hbox(gap=0.5, padding_bottom=0.5):
                            if hd.button(
                                "Select all",
                                size="small",
                                variant="text",
                            ).clicked:
                                state.dist_enabled = tuple(
                                    True for _ in _hdr_dists
                                )
                                state.time_enabled = tuple(True for _ in _hdr_times)
                            if hd.button(
                                "Clear all", size="small", variant="text"
                            ).clicked:
                                state.dist_enabled = tuple(
                                    False for _ in _hdr_dists
                                )
                                state.time_enabled = tuple(False for _ in _hdr_times)
                        with hd.hbox(gap=0.5, wrap="wrap"):
                            for i, (dist, label) in enumerate(_hdr_dists):
                                with hd.scope(f"dist_{dist} {state.dist_enabled}"):
                                    cb = hd.checkbox(
                                        label, checked=state.dist_enabled[i]
                                    )
                                    if cb.changed:
                                        flags = list(state.dist_enabled)
                                        flags[i] = cb.checked
                                        state.dist_enabled = tuple(flags)
                                    if cb.checked != state.dist_enabled[i]:
                                        cb.checked = state.dist_enabled[i]
                        hd.text(
                            "— timed —",
                            font_color="neutral-300",
                            font_size="x-small",
                            padding_top=0.25,
                        )
                        with hd.hbox(gap=0.5, wrap="wrap"):
                            for i, (tenths, label) in enumerate(_hdr_times):
                                with hd.scope(f"time_{tenths} {state.time_enabled}"):
                                    cb = hd.checkbox(
                                        label, checked=state.time_enabled[i]
                                    )
                                    if cb.changed:
                                        flags = list(state.time_enabled)
                                        flags[i] = cb.checked
                                        state.time_enabled = tuple(flags)
                                    if cb.checked != state.time_enabled[i]:
                                        cb.checked = state.time_enabled[i]

                hd.text(
                    f"through {_date_label}",
                    font_weight="normal",
                    min_width="225px",
                )

        global_filter_ui()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@hd.global_state
class PowerCurveState(hd.BaseState):
    # ``dist_enabled`` / ``time_enabled`` are sized to the *currently selected*
    # machine's ranked event tables.  Whenever the active machine changes,
    # ``power_curve_page`` resets these tuples to "all on" for the new machine.
    # ``_state_machine`` records which machine the current tuples belong to so
    # the page can detect the change.
    dist_enabled = hd.Prop(hd.Any, tuple(True for _ in ranked_distances("rower")))
    time_enabled = hd.Prop(hd.Any, tuple(True for _ in ranked_times("rower")))
    _state_machine = hd.Prop(hd.String, "rower")
    best_filter = hd.Prop(hd.String, "SBs")
    chart_log_x = hd.Prop(hd.Bool, True)
    chart_log_y = hd.Prop(hd.Bool, False)
    chart_y_metric = hd.Prop(hd.String, "pace")
    chart_x_metric = hd.Prop(hd.String, "distance")
    chart_predictor = hd.Prop(hd.String, "critical_power")
    chart_show_components = hd.Prop(hd.Bool, False)
    overlay_bests = hd.Prop(hd.String, "PBs")
    sim_playing = hd.Prop(hd.Bool, False)
    timeline_day = hd.Prop(hd.Any, None)
    sim_speed = hd.Prop(hd.String, "1x")
    sim_bundle = (
        hd.Prop(hd.Any, None)
        # final js_payload (current bundle_data + style wrapper + merged snapshots)
    )
    sim_bundle_key = hd.Prop(hd.String, "")
    # combined identity-selection-style key baked into sim_bundle
    sim_snapshot_cache = hd.Prop(
        hd.Any, {}
    )  # {selection_key: (bundle_data, pred_lookup)}; LRU, bounded
    sim_full_selections = hd.Prop(
        hd.Any, frozenset()
    )  # selections whose bg build has finished (fast-bundle replaced)
    sim_identity_key = hd.Prop(
        hd.String, ""
    )  # hash of selection-independent data inputs; change -> clear cache
    sim_pred_lookup = hd.Prop(
        hd.Any, {}
    )  # current selection's {keyframe_day: {pred_rows, pauls_k_fit, accuracy}}
    last_sim_day_out = hd.Prop(hd.Int, -1)  # tracks chart.sim_day_out changes
    last_sim_done = hd.Prop(hd.Int, 0)  # tracks chart.sim_done changes
    chart_compare_wc = hd.Prop(hd.Bool, False)
    wr_fetch_key = hd.Prop(hd.String, "")
    wr_fetch_done = hd.Prop(hd.Bool, False)
    wr_data = hd.Prop(hd.Any, None)
    workout_view = hd.Prop(
        hd.Any, None
    )  # WorkoutView: collapses the 4 pipeline stages + all_seasons
    _view_key = hd.Prop(
        hd.Any, ()
    )  # cache key for workout_view: (hash(filters), len(workouts))
    _annot_key = hd.Prop(hd.Any, ())  # cache key for slider annotations
    _annot_data = hd.Prop(hd.Any, None)  # cached list of {day, label, color} dicts
    _bounds_key = hd.Prop(hd.Any, ())  # cache key for compute_axis_bounds
    _bounds_data = hd.Prop(hd.Any, None)  # cached (x_bounds, y_bounds)
    _wk_prop_key = hd.Prop(hd.Any, ())  # cache key for workouts/season_meta props
    _wk_prop_data = hd.Prop(hd.Any, None)  # cached (workouts_prop, season_meta_prop)


def power_curve_page() -> None:
    """
    Top-level entry point for the Performance tab.
    Fetches data, computes all derived state, then calls sub-components.
    """
    gstate = GlobalFilters()
    state = PowerCurveState()

    excluded_seasons = gstate.excluded_seasons
    machine = gstate.machine

    is_dark = hd.theme().is_dark

    # ── Profile & Data ───────────────────────────────────────────────────────────────
    profile = get_profile()
    sync_result = sync_workouts()

    if sync_result is None or profile is None:
        hd.box(padding=2, min_height="80vh")
        return

    # If the user just switched machines, the persisted dist_enabled / time_enabled
    # tuples are sized for the previous machine's event grid.  Reset to "all on"
    # for the new machine before any consumer reads them.
    if state._state_machine != machine:
        state.dist_enabled = tuple(True for _ in ranked_distances(machine))
        state.time_enabled = tuple(True for _ in ranked_times(machine))
        state._state_machine = machine
        # If the user had RowingLevel selected and the new machine isn't rower,
        # snap to a predictor that works there.
        if state.chart_predictor == "rowinglevel" and machine != "rower":
            state.chart_predictor = "average"

    # Build the workout view — one traversal through all 4 pipeline stages.
    # A single hash(filters) + workout count invalidates the whole pipeline,
    # replacing the 4 per-stage caches + 4 hand-rolled string keys that used
    # to live here.
    filters = FilterSpec(
        machine=machine,
        excluded_seasons=tuple(excluded_seasons),
        dist_enabled=tuple(state.dist_enabled),
        time_enabled=tuple(state.time_enabled),
        best_filter=state.best_filter,
    )
    _view_key = (hash(filters), len(sync_result[1]))
    if state._view_key != _view_key or state.workout_view is None:
        state.workout_view = build_workout_view(sync_result[1], filters)
        state._view_key = _view_key

    workouts: WorkoutView = state.workout_view
    all_seasons = workouts.all_seasons
    featured_efforts = workouts.featured_efforts
    efforts_filtered_by_event_and_display = (
        workouts.efforts_filtered_by_event_and_display
    )

    # ── Filters (selected sets used for chart-level excluded-event logic) ─────
    machine_dists = ranked_distances(machine)
    machine_times = ranked_times(machine)
    selected_dists = {
        dist for i, (dist, _) in enumerate(machine_dists) if state.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(machine_times) if state.time_enabled[i]
    }

    # ── Simulation timeline ───────────────────────────────────────────────────
    (
        sim_start,
        total_days,
        timeline_date,
        at_today,
        included_seasons,
    ) = _compute_sim_timeline(excluded_seasons, all_seasons, state.timeline_day)
    show_watts = state.chart_y_metric == "watts"

    # Slider annotations — stable across animation ticks; only recompute when
    # filters or sim range changes (same inputs as featured_efforts + sim_start).
    _annot_key = (state._view_key, sim_start)
    if state._annot_key != _annot_key or state._annot_data is None:
        state._annot_data = build_sb_annotations(
            featured_efforts,
            sim_start,
            best_filter=state.best_filter,
            machine=machine,
        )
        state._annot_key = _annot_key

    rl_supported = machine == "rower"
    if at_today and rl_supported:
        rl_predictions = fetch_rowinglevel(
            state, profile, efforts_filtered_by_event_and_display
        )
    else:
        rl_predictions = {}

    wr_data = (
        load_world_record_data(state, profile, machine)
        if state.chart_compare_wc and wr_machine_supported(machine)
        else None
    )

    # ── Axis bounds ───────────────────────────────────────────────────────────
    _bounds_key = (
        state._view_key,
        show_watts,
        state.chart_x_metric,
        state.chart_log_x,
        state.chart_compare_wc and wr_data,
    )
    if state._bounds_key != _bounds_key or state._bounds_data is None:
        state._bounds_data = compute_axis_bounds(
            workouts.quality_efforts,
            show_watts,
            state.chart_x_metric == "duration",
            state.chart_log_x,
            wr_data=wr_data if state.chart_compare_wc else None,
        )
        state._bounds_key = _bounds_key
    x_bounds, y_bounds = state._bounds_data

    # ── Animation bundle + sim_command ───────────────────────────────────────
    _sim_command = manage_animation_bundle(
        state,
        workouts=workouts,
        sim_start=sim_start,
        total_days=total_days,
        selected_dists=selected_dists,
        selected_times=selected_times,
        excluded_seasons=excluded_seasons,
        show_watts=show_watts,
        is_dark=is_dark,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        rl_predictions=rl_predictions,
        all_seasons=all_seasons,
        wr_data=wr_data,
        at_today=at_today,
        machine=machine,
    )

    # ── JS-prop builders: workouts + season_meta ─────────────────────────────
    # These feed the JS scatter/best-line renderer.  Cached on workout-identity
    # + sim_start + total_days so toggling settings that don't change the
    # workout pipeline (predictor, log axes, watts/pace, etc.) stays O(1).
    _wk_prop_key = (state._view_key, sim_start, total_days)
    if state._wk_prop_key != _wk_prop_key or state._wk_prop_data is None:
        _sorted_seasons = sorted(all_seasons)
        state._wk_prop_data = (
            build_workouts_prop(
                workouts.quality_efforts, sim_start, total_days, _sorted_seasons
            ),
            build_season_meta(_sorted_seasons),
        )
        state._wk_prop_key = _wk_prop_key
    workouts_prop, season_meta_prop = state._wk_prop_data

    entry = lookup_bundle_entry(state.sim_pred_lookup, state.timeline_day)
    pred_rows = entry["pred_rows"]
    pauls_k_fit = entry["pauls_k_fit"]
    pauls_k = pauls_k_fit if pauls_k_fit is not None else 5.0
    accuracy = entry.get("accuracy", {})

    rl_available = profile_complete(profile) and rl_supported

    # ── Render ────────────────────────────────────────────────────────────────
    with hd.box(gap=5, align="center", padding=2, min_height="80vh"):
        with hd.box(width="100%", align="center"):
            _page_header(
                timeline_date=timeline_date,
            )

            _chart_section(
                rl_predictions=rl_predictions,
                profile=profile,
                show_watts=show_watts,
                total_days=total_days,
                sim_start=sim_start,
                sb_annotations=state._annot_data,
                pauls_k_fit=pauls_k_fit,
                sim_command=_sim_command,
                workouts_prop=workouts_prop,
                season_meta_prop=season_meta_prop,
                selected_dists=selected_dists,
                selected_times=selected_times,
                is_dark=is_dark,
            )

            # Only nag about an incomplete profile if RowingLevel is otherwise
            # available on this machine (rower).
            if not rl_available and rl_supported:
                _rl_profile_notice()

        with hd.box(align="center"):
            hd.h2("Predicted Performances")
            hd.text(
                "Pace (top) and result — total time for distance events,"
                " predicted meters for timed events."
                " Paul's Law and RowingLevel are averaged across all anchor PBs.",
                font_color="neutral-500",
                font_size="small",
                padding_bottom=1,
            )

            prediction_table(
                pred_rows,
                accuracy,
                rl_available=rl_available,
                pauls_k=pauls_k,
                machine=machine,
            )

        with hd.box(align="center"):
            with hd.h2():
                from components.app_context import your as _your_local

                _poss_h2 = _your_local()
                if state.best_filter == "All":
                    hd.text("High Quality Efforts")
                elif state.best_filter == "SBs":
                    hd.text(f"{_poss_h2} Season Bests")
                elif state.best_filter == "PBs":
                    hd.text(f"{_poss_h2} Personal Bests")

            types = {r.get("type") for r in efforts_filtered_by_event_and_display}
            cols = ["date"]
            if len(types) > 1:
                cols.append("type")
            cols += [
                "distance",
                "time",
                "pace",
                "watts",
                "drag",
                "spm",
                "hr",
                "link",
            ]
            WorkoutTable(efforts_filtered_by_event_and_display, cols, paginate=False)
