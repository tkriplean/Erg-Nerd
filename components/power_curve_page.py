"""
Performance Page — UI and orchestration for the ranked-events view.

Exported:
  power_curve_page()   — top-level HyperDiv component; call from app.py

See docs/power_curve_page.md for a full reference.

Helper logic is split across:
  services/formatters.py              — formatting helpers
  components/workout_table            — WorkoutTable
  services/ranked_filters.py          — quality filters + season helpers
  services/ranked_predictions.py      — multi-model prediction computation
  services/predictor_samplers.py      — pure per-model pace samplers
  components/power_curve_state.py     — FilterSpec/ChartStyle/AnimationState + PREDICTORS registry
  components/power_curve_pipeline.py  — WorkoutView + build_workout_view + compute_axis_bounds
  components/power_curve_animation.py — manage_animation_bundle + load_world_record_data
  components/power_curve_chart_builder.py  — chart config builder
  components/power_curve_timeline.py  — compute_timeline_snapshot + build_timeline_payload

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

  dist_enabled       tuple[bool]   one flag per RANKED_DISTANCES entry (index-aligned)
  time_enabled       tuple[bool]   one flag per RANKED_TIMES entry (index-aligned)
  best_filter        str           "All" | "PBs" | "SBs" — row filter for display/table
  chart_y_metric       str           "Pace" | "Watts"
  chart_x_metric       str           "distance" | "duration"
  chart_predictor    str           one of PREDICTORS_BY_KEY keys (see power_curve_state.py)
  overlay_bests      str           "PBs" | "SBs" | "None" — which best-curves to draw
  chart_log_x        bool          log scale on x-axis
  chart_log_y        bool          log scale on y-axis
  chart_show_components bool       show component sub-curves for supported predictors
  timeline_day       int|None      day offset from sim_start; None = end of timeline
  sim_speed          str           one of _SPEED_OPTIONS: "0.5x"|"1x"|"4x"|"16x"
  sim_playing        bool          whether the animation is running
  sim_bundle         dict|None     final js_payload sent to Chart.js (bundle_data + style wrapper)
  sim_bundle_key     str           combined data-style key baked into sim_bundle
  sim_bundle_data    dict|None     heavy keyframes dict; cached by sim_data_key (style-agnostic)
  sim_data_key       str           hash of data inputs for sim_bundle_data
  sim_pred_lookup    dict          {keyframe_day: {pred_rows, pauls_k_fit, accuracy}} from build_keyframes
  last_sim_day_out   int           tracks chart.sim_day_out changes (ticks + user seeks)
  last_sim_done      int           tracks chart.sim_done changes to detect animation end
  workout_view       WorkoutView|None  4-stage filtering pipeline result (see power_curve_pipeline.py)
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
  Use services/rowing_utils.py:get_season() to compute from an ISO date string.

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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HYPERDIV QUIRKS APPLIED HERE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  radio_group subclass: hd.radio_group doesn't expose size. A local subclass
  adds it (see below). Even then, group size doesn't shrink button padding;
  use label_style on each radio_button:
    hd.radio_button("X", label_style=hd.style(padding=(0, "0.5rem", 0, "0.5rem")))
"""

import hashlib
import json
from datetime import date, timedelta
import hyperdiv as hd

from services.rowinglevel import fetch_all_pb_predictions
from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    compute_pace,
    compute_watts,
    workout_cat_key,
    apply_best_only,
    age_from_dob,
)
from components.concept2_sync import concept2_sync
from components.profile_page import get_profile
from services.rowing_utils import profile_complete

from services.concept2_records import wr_category_label
from components.power_curve_chart_plugin import PowerCurveChart
from components.workout_table import (
    WorkoutTable,
    COL_DATE,
    COL_TYPE,
    COL_DISTANCE,
    COL_TIME,
    COL_PACE,
    COL_WATTS,
    COL_DRAG,
    COL_SPM,
    COL_HR,
    COL_LINK,
)
from components.power_curve_chart_builder import (
    build_sb_annotations,
    build_chart_config,
)
from components.power_curve_timeline import (
    compute_timeline_snapshot,
)
from components.power_curve_state import FilterSpec, PREDICTORS, PREDICTORS_BY_KEY
from components.power_curve_pipeline import (
    WorkoutView,
    build_workout_view,
    compute_axis_bounds,
)
from components.power_curve_animation import (
    load_world_record_data,
    lookup_bundle_entry,
    manage_animation_bundle,
)
from components.hyperdiv_extensions import radio_group, grid_box


# ---------------------------------------------------------------------------
# Constants local to this module
# ---------------------------------------------------------------------------

_SPEED_OPTIONS = ("0.5x", "1x", "4x", "16x")
_SPEED_DAYS = {"0.5x": 1, "1x": 7, "4x": 30, "16x": 91}
_SIM_LOOKAHEAD_STEPS = 4  # ghost/arrow lookahead = this many sim steps ahead


# ---------------------------------------------------------------------------
# Profile hash helper
# ---------------------------------------------------------------------------


def _profile_hash(profile: dict) -> str:
    """Short hash of profile fields that affect RowingLevel predictions."""
    key = (
        profile.get("gender", ""),
        age_from_dob(profile.get("dob", "")),
        profile.get("weight", 0.0),
        profile.get("weight_unit", "kg"),
    )
    return hashlib.md5(json.dumps(key).encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Sub-component: chart section
# ---------------------------------------------------------------------------


def _chart_section(
    state,
    *,
    chart_cfg,
    rl_predictions: dict,
    profile: dict,
    show_watts: bool,
    total_days: int,
    sim_start,
    sb_annotations: list,
    pauls_k_fit: float | None = None,
    sim_command: str = "stop",
) -> None:
    """
    Renders the performance chart box: header, RL status,
    PowerCurveChart (with integrated transport bar + timeline scrubber),
    settings row, and components toggle.

    All transport interaction (Play/Pause, Speed, seeking) is handled entirely
    inside the PowerCurveChart plugin — no Python UI is rendered for it.
    """
    is_dark = hd.theme().is_dark

    if not chart_cfg and state.sim_bundle is None:
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
                config=chart_cfg,
                show_watts=show_watts,
                x_mode=state.chart_x_metric,
                sim_bundle=state.sim_bundle,
                sim_command=sim_command,
                sim_speed=state.sim_speed,
                timeline_min=0,
                timeline_max=max(1, total_days - 1),
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
                        return f"{p.desc} Personalised to your K = {pauls_k_fit:.1f}s/doubling."
                    return p.desc

                _PRED_OPTIONS = [(p.key, p.name, _pred_desc(p)) for p in PREDICTORS]
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
                if _pred_meta.supports_components:
                    with hd.scope("comp_gear"):
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
                                    with hd.scope("comp_cb"):
                                        _comp_cb = hd.checkbox(
                                            _pred_meta.component_label,
                                            checked=state.chart_show_components,
                                        )
                                        if _comp_cb.changed:
                                            state.chart_show_components = (
                                                _comp_cb.checked
                                            )
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


def _prediction_table(
    state,
    pred_rows: list,
    accuracy: dict,
    rl_available: bool = True,
    pauls_k: float = 5.0,
) -> None:
    """
    Renders the multi-model prediction grid (Your PB, CP, Log-Log, Paul's Law,
    RowingLevel, Average) plus an accuracy footer row.  Pure renderer —
    ``pred_rows`` and ``accuracy`` are computed upstream by
    ``build_prediction_table_data`` (via the bundle lookup during animation
    or the slow-path snapshot when paused).

    Only renders when at least one row has any data.
    """
    if not any(
        r["pb_pace"] or r["cp_pace"] or r["loglog_pace"] or r["pl_pace"] or r["rl_pace"]
        for r in pred_rows
    ):
        return

    _pl_tip = (
        f"Predicts +{pauls_k:.1f} s/500m for each doubling of distance "
        f"(your personalised value), applied from each anchor PB and averaged."
    )
    _PRED_COLS = [("pb", "Your PB", "Your personal best for each event.")]
    for _p in PREDICTORS:
        if _p.table_column is None:
            continue
        if _p.key == "rowinglevel" and not rl_available:
            continue
        _tip = _pl_tip if _p.key == "pauls_law" else _p.desc
        _label = "Average" if _p.key == "average" else _p.name
        _PRED_COLS.append((_p.table_column, _label, _tip))

    _HEADER_BG = "neutral-100"
    _ACC_BG = "neutral-100"

    # CSS Grid: fixed Event column + one 1fr column per prediction model
    _col_template = "8rem " + " ".join(["1fr"] * len(_PRED_COLS))

    with grid_box(
        grid_template_columns=_col_template,
        border="1px solid neutral-200",
        border_radius="medium",
        width="100%",
        overflow="hidden",
    ):
        # ── header row ────────────────────────────────────────────────────
        with hd.scope("hdr_event"):
            with hd.box(
                padding=1,
                background_color=_HEADER_BG,
                border_right="1px solid neutral-200",
                border_bottom="1px solid neutral-200",
            ):
                hd.text("Event", font_weight="semibold", font_size="small")

        for col_key, col_label, col_tip in _PRED_COLS:
            with hd.scope(f"hdr_{col_key}"):
                with hd.box(
                    padding=(0.75, 0.5),
                    background_color=_HEADER_BG,
                    border_bottom="1px solid neutral-200",
                ):
                    with hd.hbox(gap=0.5, align="center"):
                        hd.text(col_label, font_weight="semibold", font_size="small")
                        with hd.tooltip(col_tip):
                            hd.icon(
                                "question-circle",
                                font_size="small",
                                font_color="neutral-500",
                            )

        # ── data rows ─────────────────────────────────────────────────────
        for _ri, _row in enumerate(pred_rows):
            with hd.scope(_row["label"]):
                _row_bg = "neutral-50" if _ri % 2 == 0 else "neutral-0"
                _pb_raw = _row.get("pb_raw")

                if _row["event_type"] == "dist":
                    _ev_idx = next(
                        i
                        for i, (d, _) in enumerate(RANKED_DISTANCES)
                        if d == _row["event_value"]
                    )
                    _ev_enabled = state.dist_enabled[_ev_idx]
                else:
                    _ev_idx = next(
                        i
                        for i, (t, _) in enumerate(RANKED_TIMES)
                        if t == _row["event_value"]
                    )
                    _ev_enabled = state.time_enabled[_ev_idx]

                # Event cell
                with hd.scope("ev"):
                    with hd.box(
                        padding=1,
                        background_color=_row_bg,
                        border_top="1px solid neutral-200",
                        border_right="1px solid neutral-200",
                    ):
                        with hd.hbox(gap=0.5, align="center"):
                            with hd.scope("toggle"):
                                with hd.tooltip(
                                    "Include this event's PB in prediction "
                                    "calculations? More accurate predictions "
                                    "when you include only current, max-effort results."
                                ):
                                    _ev_sw = hd.switch(
                                        "", checked=_ev_enabled, size="small"
                                    )
                                if _ev_sw.changed:
                                    if _row["event_type"] == "dist":
                                        _flags = list(state.dist_enabled)
                                        _flags[_ev_idx] = _ev_sw.checked
                                        state.dist_enabled = tuple(_flags)
                                    else:
                                        _flags = list(state.time_enabled)
                                        _flags[_ev_idx] = _ev_sw.checked
                                        state.time_enabled = tuple(_flags)
                            hd.text(
                                _row["label"],
                                font_weight="semibold",
                                font_size="small",
                                font_color="neutral-600"
                                if _ev_enabled
                                else "neutral-400",
                            )

                # Prediction cells
                for col_key, col_label, _tip in _PRED_COLS:
                    with hd.scope(col_key):
                        _pace_val = _row.get(f"{col_key}_pace")
                        _result_val = _row.get(f"{col_key}_result")
                        _pred_raw = _row.get(f"{col_key}_raw")
                        has_delta = (
                            col_key != "pb"
                            and _pred_raw is not None
                            and _pb_raw is not None
                        )
                        if has_delta:
                            _delta = _pred_raw - _pb_raw
                            _delta_s = f"{_delta:+.1f}s"
                            _delta_color = (
                                "success-600"
                                if _delta < 0
                                else "danger-600"
                                if _delta > 0
                                else "neutral-500"
                            )
                        _is_pb_col = col_key == "pb"
                        _pace_color = (
                            "neutral-300"
                            if _is_pb_col and not _ev_enabled
                            else "neutral-900"
                        )
                        _result_color = (
                            "neutral-300"
                            if _is_pb_col and not _ev_enabled
                            else "neutral-500"
                        )
                        with hd.box(
                            padding=0.5,
                            background_color=_row_bg,
                            border_top="1px solid neutral-200",
                        ):
                            if _pace_val:
                                with hd.hbox(gap=0.5):
                                    hd.text(
                                        _pace_val,
                                        font_size="large",
                                        font_weight="semibold",
                                        font_color=_pace_color,
                                    )
                                    if has_delta:
                                        hd.text(
                                            _delta_s,
                                            font_size="small",
                                            font_color=_delta_color,
                                        )
                                hd.text(
                                    _result_val or "",
                                    font_size="x-small",
                                    font_color=_result_color,
                                )
                            else:
                                hd.text(
                                    "—",
                                    font_size="small",
                                    font_color="neutral-300",
                                )

        # ── accuracy row ──────────────────────────────────────────────────
        # Accuracy label cell
        with hd.scope("acc_label"):
            with hd.box(
                padding=1,
                background_color=_ACC_BG,
                border_top="2px solid neutral-300",
                border_right="1px solid neutral-200",
            ):
                with hd.hbox(gap=0.5, align="center"):
                    hd.text(
                        "Accuracy",
                        font_size="small",
                        font_weight="semibold",
                        font_color="neutral-600",
                    )
                    with hd.tooltip(
                        "RMSE (root mean square error) in sec/500m and R² "
                        "across enabled events where both a prediction and "
                        "a PB exist. Lower RMSE and higher R² are better. "
                        "Disabled events (toggled off) are excluded."
                    ):
                        hd.icon(
                            "question-circle",
                            font_size="small",
                            font_color="neutral-600",
                        )

        # PB column accuracy cell (always —)
        with hd.scope("acc_pb"):
            with hd.box(
                padding=0.5,
                background_color=_ACC_BG,
                border_top="2px solid neutral-300",
            ):
                hd.text("—", font_size="small", font_color="neutral-600")

        # Other model accuracy cells
        for _ck, _cl, _ct in _PRED_COLS[1:]:
            with hd.scope(f"acc_{_ck}"):
                _av = accuracy.get(_ck, {"rmse": None, "r2": None, "n": 0})
                with hd.box(
                    padding=0.5,
                    background_color=_ACC_BG,
                    border_top="2px solid neutral-300",
                ):
                    if _av["rmse"] is not None:
                        hd.text(
                            f"{_av['rmse']:.2f}s",
                            font_size="large",
                            font_weight="semibold",
                            font_color="neutral-800",
                        )
                        if _av["r2"] is not None:
                            hd.text(
                                f"R²={_av['r2']:.3f}",
                                font_size="small",
                                font_color="neutral-600",
                            )
                        hd.text(
                            f"n={_av['n']}",
                            font_size="small",
                            font_color="neutral-600",
                        )
                    else:
                        hd.text("—", font_size="small", font_color="neutral-600")


# ---------------------------------------------------------------------------
# Pure data helpers  (no HyperDiv, no side-effects)
# ---------------------------------------------------------------------------


def _bisect_date_desc(workouts: list, date_str: str) -> int:
    """
    Binary search on a newest-first workout list.
    Returns the first index i such that workouts[i:] are all dated <= date_str.
    O(log n) versus the O(n) linear scan.
    """
    lo, hi = 0, len(workouts)
    while lo < hi:
        mid = (lo + hi) // 2
        if (workouts[mid].get("date") or "")[:10] > date_str:
            lo = mid + 1
        else:
            hi = mid
    return lo


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


def _expand_y_bounds_for_wc(
    y_bounds: tuple | None,
    wr_data: dict | None,
    show_watts: bool,
) -> tuple | None:
    """Expand y_bounds to include world-class pace/watts values.

    compute_axis_bounds uses user PBs only; WC rowers are faster, so without
    expansion the WC overlay points would be clipped.  Returns the original
    y_bounds unchanged when wr_data is absent or empty.
    """
    if y_bounds is None or not wr_data:
        return y_bounds
    _wr_y_vals = [
        compute_watts(pace) if show_watts else pace
        for pace in wr_data["lb"].values()
        if pace > 0
    ]
    if not _wr_y_vals:
        return y_bounds
    _ypad = max((y_bounds[1] - y_bounds[0]) * 0.1, 5.0 if not show_watts else 2.0)
    return (
        min(y_bounds[0], min(_wr_y_vals) - _ypad),
        max(y_bounds[1], max(_wr_y_vals) + _ypad),
    )


# ---------------------------------------------------------------------------
# HyperDiv async helpers  (use hd.task / hd.scope / hd.state)
# ---------------------------------------------------------------------------


def _fetch_rowinglevel(state, profile: dict, chart_workouts: list) -> dict:
    """
    Launch (or resume) the background RowingLevel scrape.
    Only fires when at_today and profile_complete; otherwise returns {}.
    Uses a scope key derived from profile + PB hash so the task re-fires only
    when its inputs change.
    Returns rl_predictions (a dict — possibly empty).
    """
    if not profile_complete(profile):
        return {}

    weight_kg = (
        profile["weight"] * 0.453592
        if profile["weight_unit"] == "lbs"
        else profile["weight"]
    )
    lbest: dict = {}
    lbest_anchor: dict = {}
    lbest_dates: dict = {}
    for w in chart_workouts:
        p = compute_pace(w)
        c = workout_cat_key(w)
        d = w.get("distance")
        if p is None or c is None or not d:
            continue
        if c not in lbest or p < lbest[c]:
            lbest[c] = p
            lbest_anchor[c] = d
            lbest_dates[c] = w.get("date", "")

    lbest_hash = hashlib.md5(
        json.dumps(sorted((str(k), round(v, 2)) for k, v in lbest.items())).encode()
    ).hexdigest()[:8]
    scope_key = f"rl_{_profile_hash(profile)}_{lbest_hash}"

    rl_predictions = {}
    with hd.scope(scope_key):
        rl_task = hd.task()

        def _do_scrape(gender, current_age, wkg, lb, lb_anchor, lb_dates):
            return fetch_all_pb_predictions(
                [], lb, lb_anchor, gender, current_age, wkg, lbest_dates=lb_dates
            )

        rl_task.run(
            _do_scrape,
            profile["gender"],
            age_from_dob(profile.get("dob", "")),
            weight_kg,
            lbest,
            lbest_anchor,
            lbest_dates,
        )
        if rl_task.done and rl_task.result:
            rl_predictions = rl_task.result

    return rl_predictions


# ---------------------------------------------------------------------------
# Sub-component: page header
# ---------------------------------------------------------------------------


def _page_header(
    state,
    *,
    timeline_date: date,
) -> None:
    """
    Renders the page title bar: best_filter dropdown, events dropdown, date label.
    """
    _date_label = timeline_date.strftime("%b %d, %Y")
    _best_long = {
        "All": "All Great Efforts",
        "PBs": "Personal Bests",
        "SBs": "Season Bests",
    }
    _cur_best_lbl = _best_long.get(state.best_filter, state.best_filter)
    with hd.h1():
        with hd.hbox(
            gap=0.6,
            align="center",
            padding_bottom=0,
            justify="center",
            wrap="wrap",
        ):
            with hd.scope("best_filter_dd"):
                with hd.dropdown() as _bf_dd:
                    _bf_btn = hd.button(
                        _cur_best_lbl,
                        caret=True,
                        size="large",
                        font_color="neutral-800",
                        font_size=2,
                        font_weight="bold",
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
                        with hd.scope("best_filter_rg"):
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
                        with hd.scope("draw_curves_rg"):
                            with radio_group(
                                value=state.overlay_bests, size="small"
                            ) as _dpc_rg:
                                hd.radio_button("SBs", value="SBs")
                                hd.radio_button("PBs", value="PBs")
                                hd.radio_button("None", value="None")
                            if _dpc_rg.changed:
                                state.overlay_bests = _dpc_rg.value

            # ---- Events dropdown ----
            _n_ev_sel = sum(state.dist_enabled) + sum(state.time_enabled)
            _n_ev_tot = len(RANKED_DISTANCES) + len(RANKED_TIMES)
            _ev_lbl = "All Events" if _n_ev_sel == _n_ev_tot else "Some Events"

            hd.text("for", font_size="medium")

            with hd.dropdown() as _ev_dd:
                _ev_btn = hd.button(
                    _ev_lbl,
                    font_color="neutral-800",
                    font_size=2,
                    font_weight="bold",
                    caret=True,
                    size="large",
                    slot=_ev_dd.trigger,
                )
                if _ev_btn.clicked:
                    _ev_dd.opened = not _ev_dd.opened
                with hd.box(padding=1, gap=0.5, background_color="neutral-50"):
                    with hd.hbox(gap=0.5, padding_bottom=0.5):
                        if hd.button(
                            "Select all", size="small", variant="text"
                        ).clicked:
                            state.dist_enabled = tuple(True for _ in RANKED_DISTANCES)
                            state.time_enabled = tuple(True for _ in RANKED_TIMES)
                        if hd.button("Clear all", size="small", variant="text").clicked:
                            state.dist_enabled = tuple(False for _ in RANKED_DISTANCES)
                            state.time_enabled = tuple(False for _ in RANKED_TIMES)
                    with hd.scope(str(state.dist_enabled)):
                        with hd.hbox(gap=0.5, wrap="wrap"):
                            for i, (dist, label) in enumerate(RANKED_DISTANCES):
                                with hd.scope(f"dist_{dist}"):
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
                    with hd.scope(str(state.time_enabled)):
                        with hd.hbox(gap=0.5, wrap="wrap"):
                            for i, (tenths, label) in enumerate(RANKED_TIMES):
                                with hd.scope(f"time_{tenths}"):
                                    cb = hd.checkbox(
                                        label, checked=state.time_enabled[i]
                                    )
                                    if cb.changed:
                                        flags = list(state.time_enabled)
                                        flags[i] = cb.checked
                                        state.time_enabled = tuple(flags)
                                    if cb.checked != state.time_enabled[i]:
                                        cb.checked = state.time_enabled[i]

            hd.text("through", font_size="medium")
            hd.text(_date_label, font_size="2x-large", font_weight="normal")


# ---------------------------------------------------------------------------
# Pure computation: chart config + prediction data for a single render cycle
# ---------------------------------------------------------------------------


def _slow_path_snapshot(
    state,
    *,
    view: WorkoutView,
    timeline_date: date,
    at_today: bool,
    rl_predictions: dict,
    show_watts: bool,
    is_dark: bool,
    x_bounds,
    y_bounds,
    all_seasons: list,
    wr_data,
    selected_dist_set: set,
    selected_time_set: set,
) -> tuple:
    """
    Slow path — full compute_timeline_snapshot + build_chart_config.

    Runs when paused (or when no bundle exists yet).  Produces a Python-side
    Chart.js config and the prediction-table data.  Expensive; do not call
    on every animation tick.
    """
    quality_efforts = view.quality_efforts
    date_str = timeline_date.isoformat()
    if state.best_filter == "All":
        efforts_filtered_by_event = view.efforts_filtered_by_event
        _in_time = efforts_filtered_by_event[
            _bisect_date_desc(efforts_filtered_by_event, date_str) :
        ]
        sim_wkts = _in_time
    else:
        featured_efforts = view.featured_efforts
        _in_time = featured_efforts[_bisect_date_desc(featured_efforts, date_str) :]
        sim_wkts = apply_best_only(_in_time, by_season=state.best_filter != "PBs")
    all_events_to_date = quality_efforts[
        _bisect_date_desc(quality_efforts, date_str) :
    ]

    # Disabled-event workouts (faint background dots in chart).
    excluded_cats = set()
    for i, (dist, _) in enumerate(RANKED_DISTANCES):
        if not state.dist_enabled[i]:
            excluded_cats.add(("dist", dist))
    for i, (tenths, _) in enumerate(RANKED_TIMES):
        if not state.time_enabled[i]:
            excluded_cats.add(("time", tenths))

    excluded_wkts: list = []
    if excluded_cats:
        if state.best_filter == "all":
            excluded_wkts = all_events_to_date
        else:
            excluded_wkts = apply_best_only(
                [
                    w
                    for w in all_events_to_date
                    if workout_cat_key(w) in excluded_cats
                ],
                by_season=state.best_filter == "SBs",
            )

    predictor = (
        state.chart_predictor
        if at_today or state.chart_predictor != "rowinglevel"
        else "none"
    )
    _snap = compute_timeline_snapshot(
        sim_wkts=sim_wkts,
        all_events_to_date=all_events_to_date,
        predictor=predictor,
        rl_predictions=rl_predictions,
        show_watts=show_watts,
        is_dark=is_dark,
        x_mode=state.chart_x_metric,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        show_components=state.chart_show_components,
        selected_dist_set=selected_dist_set,
        selected_time_set=selected_time_set,
    )
    lb, lb_anchor = _snap["lb"], _snap["lb_anchor"]
    pauls_k_fit = _snap["pauls_k_fit"]
    pauls_k = _snap["pauls_k"]
    cp_params = _snap["cp_params"]

    # CP → loglog fallback when insufficient data.
    effective_predictor = predictor
    if effective_predictor == "critical_power" and cp_params is None:
        effective_predictor = "loglog"

    chart_cfg = build_chart_config(
        sim_wkts,
        log_x=state.chart_log_x,
        log_y=state.chart_log_y,
        show_lifetime_line=state.overlay_bests == "PBs",
        show_watts=show_watts,
        is_dark=is_dark,
        predictor=effective_predictor,
        rl_predictions=rl_predictions,
        critical_power_params=cp_params,
        season_lines=set(all_seasons) if state.overlay_bests == "SBs" else set(),
        all_seasons=all_seasons,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        sim_overlays=None,
        overlay_labels=[],
        show_components=state.chart_show_components,
        lifetime_best=lb,
        lifetime_best_anchor=lb_anchor,
        pauls_k=pauls_k,
        excluded_workouts=excluded_wkts,
        x_mode=state.chart_x_metric,
        wr_data=wr_data,
    )
    return (
        chart_cfg,
        _snap["pred_table_rows"],
        pauls_k_fit,
        pauls_k,
        _snap["accuracy"],
    )


def _fast_path_snapshot(state) -> tuple:
    """
    Fast path — read pred_rows, pauls_k_fit, and accuracy from the bundle's
    per-keyframe lookup.  JS uses sim_bundle to render the chart, so
    chart_cfg is ``None``.  All values travel together per keyframe so the
    fast path never has to lean on a slow-path side channel.
    """
    entry = lookup_bundle_entry(state.sim_pred_lookup, state.timeline_day)
    pauls_k_fit = entry["pauls_k_fit"]
    return (
        None,  # chart_cfg — JS ignores config while bundle is active
        entry["pred_rows"],
        pauls_k_fit,
        pauls_k_fit if pauls_k_fit is not None else 5.0,
        entry.get("accuracy", {}),
    )


def _compute_chart_data(
    state,
    *,
    view: WorkoutView,
    timeline_date: date,
    at_today: bool,
    rl_predictions: dict,
    show_watts: bool,
    is_dark: bool,
    x_bounds,
    y_bounds,
    all_seasons: list,
    wr_data,
    selected_dist_set: set,
    selected_time_set: set,
) -> tuple:
    """
    Fast/slow path dispatcher — returns
    (chart_cfg, pred_rows, pauls_k_fit, pauls_k, accuracy).

    Fast path (animating): chart_cfg=None; all values read from the bundle's
        pred_table_lookup per-keyframe entry.
    Slow path (paused/static): full compute_timeline_snapshot +
        build_chart_config.
    """
    if state.sim_playing and state.sim_bundle is not None:
        return _fast_path_snapshot(state)
    return _slow_path_snapshot(
        state,
        view=view,
        timeline_date=timeline_date,
        at_today=at_today,
        rl_predictions=rl_predictions,
        show_watts=show_watts,
        is_dark=is_dark,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        all_seasons=all_seasons,
        wr_data=wr_data,
        selected_dist_set=selected_dist_set,
        selected_time_set=selected_time_set,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def power_curve_page(client, user_id: str, excluded_seasons=(), machine="All") -> None:
    """
    Top-level entry point for the Performance tab.
    Fetches data, computes all derived state, then calls sub-components.
    """
    state = hd.state(
        dist_enabled=tuple(True for _ in RANKED_DISTANCES),
        time_enabled=tuple(True for _ in RANKED_TIMES),
        best_filter="SBs",
        chart_log_x=True,
        chart_log_y=False,
        chart_y_metric="pace",
        chart_x_metric="distance",
        chart_predictor="critical_power",
        chart_show_components=False,
        overlay_bests="PBs",
        sim_playing=False,
        timeline_day=None,
        sim_speed="1x",
        sim_bundle=None,  # final js_payload (bundle_data + style wrapper)
        sim_bundle_key="",  # combined data-style key baked into sim_bundle
        sim_bundle_data=None,  # heavy keyframes dict; cached by sim_data_key
        sim_data_key="",  # hash of data inputs for sim_bundle_data
        sim_pred_lookup={},  # {keyframe_day: {pred_rows, pauls_k_fit, accuracy}} from build_keyframes
        last_sim_day_out=-1,  # tracks chart.sim_day_out changes
        last_sim_done=0,  # tracks chart.sim_done changes
        chart_compare_wc=False,
        wr_fetch_key="",
        wr_fetch_done=False,
        wr_data=None,
        workout_view=None,  # WorkoutView: collapses the 4 pipeline stages + all_seasons
        _view_key=(),  # cache key for workout_view: (hash(filters), len(workouts))
        _annot_key=(),  # cache key for slider annotations
        _annot_data=None,  # cached list of {day, label, color} dicts
        _bounds_key=(),  # cache key for compute_axis_bounds
        _bounds_data=None,  # cached (x_bounds, y_bounds)
    )
    is_dark = hd.theme().is_dark

    # ── Profile & Data ───────────────────────────────────────────────────────────────
    profile = get_profile()
    sync_result = concept2_sync(client)

    if sync_result is None or profile is None:
        return

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

    view: WorkoutView = state.workout_view
    all_seasons = view.all_seasons
    featured_efforts = view.featured_efforts
    efforts_filtered_by_event_and_display = view.efforts_filtered_by_event_and_display

    # ── Filters (selected sets used for chart-level excluded-event logic) ─────
    selected_dists = {
        dist for i, (dist, _) in enumerate(RANKED_DISTANCES) if state.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(RANKED_TIMES) if state.time_enabled[i]
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
            included_seasons,
            best_filter=state.best_filter,
        )
        state._annot_key = _annot_key

    if at_today:
        rl_predictions = _fetch_rowinglevel(
            state, profile, efforts_filtered_by_event_and_display
        )
    else:
        rl_predictions = {}

    wr_data = load_world_record_data(state, profile) if state.chart_compare_wc else None

    # ── Axis bounds ───────────────────────────────────────────────────────────
    _bounds_key = (
        state._view_key,
        show_watts,
        state.chart_x_metric,
        state.chart_log_x,
    )
    if state._bounds_key != _bounds_key or state._bounds_data is None:
        state._bounds_data = compute_axis_bounds(
            view.quality_efforts,
            show_watts,
            state.chart_x_metric == "duration",
            state.chart_log_x,
        )
        state._bounds_key = _bounds_key
    x_bounds, y_bounds = state._bounds_data

    # Expand y_bounds to include WC records when comparing.
    if state.chart_compare_wc:
        y_bounds = _expand_y_bounds_for_wc(y_bounds, wr_data, show_watts)

    # ── Animation bundle + sim_command ───────────────────────────────────────
    _sim_command = manage_animation_bundle(
        state,
        view=view,
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
    )

    chart_cfg, pred_rows, pauls_k_fit, pauls_k, accuracy = _compute_chart_data(
        state,
        view=view,
        timeline_date=timeline_date,
        at_today=at_today,
        rl_predictions=rl_predictions,
        show_watts=show_watts,
        is_dark=is_dark,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        all_seasons=all_seasons,
        wr_data=wr_data,
        selected_dist_set=selected_dists,
        selected_time_set=selected_times,
    )

    rl_available = profile_complete(profile)

    # ── Render ────────────────────────────────────────────────────────────────
    with hd.box(gap=5, align="center", padding=(2, 2, 2, 2)):
        with hd.box(width="100%", align="center"):
            _page_header(
                state,
                timeline_date=timeline_date,
            )

            _chart_section(
                state,
                chart_cfg=chart_cfg,
                rl_predictions=rl_predictions,
                profile=profile,
                show_watts=show_watts,
                total_days=total_days,
                sim_start=sim_start,
                sb_annotations=state._annot_data,
                pauls_k_fit=pauls_k_fit,
                sim_command=_sim_command,
            )

            if not rl_available:
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

            _prediction_table(
                state,
                pred_rows,
                accuracy,
                rl_available=rl_available,
                pauls_k=pauls_k,
            )

        with hd.box(align="center"):
            with hd.h2():
                if state.best_filter == "All":
                    hd.text("High Quality Efforts")
                elif state.best_filter == "SBs":
                    hd.text("Your Season Bests")
                elif state.best_filter == "PBs":
                    hd.text("Your Personal Bests")

            types = {r.get("type") for r in efforts_filtered_by_event_and_display}
            cols = [COL_DATE]
            if len(types) > 1:
                cols.append(COL_TYPE)
            cols += [
                COL_DISTANCE,
                COL_TIME,
                COL_PACE,
                COL_WATTS,
                COL_DRAG,
                COL_SPM,
                COL_HR,
                COL_LINK,
            ]
            WorkoutTable(efforts_filtered_by_event_and_display, cols, paginate=False)
