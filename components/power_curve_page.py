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
  components/power_curve_chart_builder.py  — chart config builder
  services/critical_power_model.py    — CP model fitting
  services/ranked_predictions.py      — build_prediction_table_data

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
  chart_predictor    str           "none" | "pauls_law" | "loglog" | "rowinglevel"
                                   | "critical_power" | "average"
  draw_power_curves        str           "PBs" | "SBs" | "None" — which best-curves to draw
  chart_log_x        bool          log scale on x-axis
  chart_log_y        bool          log scale on y-axis
  chart_show_components bool       show component sub-curves for supported predictors
  sim_week           int           day offset from sim_start; _SIM_TODAY (999999) = end
  sim_speed          str           one of _SPEED_OPTIONS: "0.5x"|"1x"|"4x"|"16x"
  sim_playing        bool          whether the animation is running
  sim_bundle         dict|None     precomputed animation bundle; None until task completes
  sim_bundle_key     str           hash of bundle inputs; invalidated on settings change
  last_sim_day_out   int           tracks chart.sim_day_out changes (ticks + user seeks)
  last_sim_done      int           tracks chart.sim_done changes to detect animation end
  cp_fit_key         str           hash of CP input data; used to cache the CP fit
  cp_fit_result      dict|None     cached CP fit params from fit_critical_power()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SIMULATION / TIMELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  sim_start  = May 1 of the earliest included season
  sim_end    = min(date.today(), Apr 30 of the year after the latest included season)
  total_days = (sim_end - sim_start).days + 1

  _SIM_TODAY     = 999999  (sentinel meaning "end of timeline / show all data")
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
import time
from datetime import date, timedelta
from typing import NamedTuple
import hyperdiv as hd

from services.rowinglevel import fetch_all_pb_predictions
from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    parse_date,
    compute_pace,
    compute_watts,
    get_season,
    workout_cat_key,
    apply_best_only,
    apply_season_best_only,
    compute_featured_workouts,
    compute_duration_s,
    compute_pauls_constant,
    age_from_dob,
)
from components.concept2_sync import concept2_sync
from components.profile_page import get_profile
from services.rowing_utils import profile_complete


from services.critical_power_model import fit_critical_power
from services.concept2_records import (
    age_category as wc_age_category,
    weight_class_str as wc_weight_class_str,
    wr_category_label,
    fetch_wc_data,
)
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
from services.ranked_filters import (
    is_ranked_noninterval,
    seasons_from,
    apply_quality_filters,
)
from components.power_curve_chart_builder import (
    build_sb_annotations,
    ol_event_line,
    pcts,
    build_chart_config,
    build_pred_datasets,
    build_wc_static_datasets,
    compute_lifetime_bests,
)
from services.ranked_predictions import build_prediction_table_data
from components.hyperdiv_extensions import radio_group, shadowed_box, grid_box


# ---------------------------------------------------------------------------
# Constants local to this module
# ---------------------------------------------------------------------------

_SIM_TODAY = 999999  # sentinel: sim_week value meaning "end of timeline / today"
_SPEED_OPTIONS = ("0.5x", "1x", "4x", "16x")
_SPEED_DAYS = {"0.5x": 1, "1x": 7, "4x": 30, "16x": 91}
_SIM_LOOKAHEAD_STEPS = 4  # ghost/arrow lookahead = this many sim steps ahead

# ---------------------------------------------------------------------------
# Predictor descriptions — shared by the dropdown and the prediction table.
# Paul's Law description is dynamic (personalised K) and built at render time.
# ---------------------------------------------------------------------------

_DESC_CP = (
    "Two-component power-duration model (veloclinic). "
    "Requires 5 or more PBs spanning a 10:1 duration ratio. Method from rowsandall.com."
)
_DESC_LL = (
    "Fits a power law (log watts vs log distance) across all scoped PBs. "
    "Similar to the Free Spirits Pace Predictor (freespiritsrowing.com) "
    "but uses all PBs, not just two."
)
_DESC_RL = (
    "Predictions from rowinglevel.com based on your profile (gender, age, bodyweight). "
    "Distance-weighted average across all anchor PBs. Distance events only."
)

_DESC_PL = (
    "Predicts +5.0 s/500m for each doubling of distance "
    "(population default — needs 2 or more PBs to personalise), "
    "applied from each anchor PB and averaged."
)

_DESC_AVG = "Mean of all available predictions for this event."

# Predictors that support the "Show components" toggle, with tooltip descriptions
# and checkbox labels customised per technique.
_COMP_DESCRIPTIONS = {
    "pauls_law": "Shows one curve per PB anchor, before averaging.",
    "rowinglevel": "Shows the RL curve from each PB anchor, before distance-weighted averaging.",
    "critical_power": "Shows the fast-twitch and slow-twitch power components separately.",
    "average": "Shows all individual model curves that were averaged.",
}
_COMP_LABELS = {
    "pauls_law": "Show one curve per anchor",
    "rowinglevel": "Show one RL curve per anchor",
    "critical_power": "Show fast-twitch & slow-twitch components",
    "average": "Show individual model curves",
}


# ---------------------------------------------------------------------------
# Simulation data container
# ---------------------------------------------------------------------------


class SimData(NamedTuple):
    """Return value of _build_sim_data — all data derived from the current sim position."""

    sim_wkts: list  # workouts visible at sim_date (filtered by best_filter)
    excluded_wkts: list  # PBs of user-disabled events (plotted faintly, not in fits)
    lb: dict  # selected-event lifetime bests (pace)
    lb_anchor: dict  # selected-event lifetime bests (anchor format)
    lb_all: dict  # all-event lifetime bests (pace)
    lb_all_anchor: dict  # all-event lifetime bests (anchor format)
    pauls_k_fit: "float | None"  # personalised Paul's constant; None if < 2 PBs
    pauls_k: float  # pauls_k_fit or population default 5.0


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
# Sub-component helpers
# ---------------------------------------------------------------------------


def _compute_rewind_day(ranked_prefilt: list, sim_start: date) -> int:
    """Start day for the Play button: 30 days before the earliest qualifying event.

    ranked_prefilt is already filtered by selected dists/times and excluded seasons,
    so no further filtering is needed here.
    """
    dates = [parse_date(w.get("date", "")) for w in ranked_prefilt if w.get("date")]
    if not dates:
        return 0
    return max(0, (min(dates) - sim_start).days - 30)


# ---------------------------------------------------------------------------
# Sub-component: chart section
# ---------------------------------------------------------------------------


def _chart_section(
    state,
    *,
    chart_cfg,
    rl_task,
    rl_predictions: dict,
    profile: dict,
    show_watts: bool,
    total_days: int,
    sim_start,
    sb_annotations: list,
    rewind_day: int,
    pauls_k_fit: float | None = None,
    wc_task=None,
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
                rewind_day=rewind_day,
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
            # Always update sim_week so Python knows the current position
            # (used to rebuild the static config when paused).
            if chart.sim_day_out != state.last_sim_day_out:
                state.last_sim_day_out = chart.sim_day_out
                if chart.sim_day_out >= 0:
                    state.sim_week = chart.sim_day_out

            # JS increments sim_done when the animation completes.
            if chart.sim_done != state.last_sim_done:
                state.last_sim_done = chart.sim_done
                state.sim_playing = False

    _chart_settings(state, wc_task, profile, pauls_k_fit)


def _chart_settings(state, wc_task, profile, pauls_k_fit):
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

                _PRED_OPTIONS = [
                    ("critical_power", "Critical Power", _DESC_CP),
                    ("loglog", "Log-Log Watts Fit", _DESC_LL),
                    (
                        "pauls_law",
                        "Paul's Law (average)",
                        _DESC_PL
                        + (
                            f" Personalised to your K = {pauls_k_fit:.1f}s/doubling."
                            if pauls_k_fit is not None
                            else ""
                        ),
                    ),
                    ("rowinglevel", "RowingLevel (average)", _DESC_RL),
                    ("average", "Average of all techniques", _DESC_AVG),
                    (
                        "none",
                        "...actually, don't predict",
                        "Hide the prediction curve.",
                    ),
                ]
                _pred_name = next(
                    (
                        name
                        for val, name, _ in _PRED_OPTIONS
                        if val == state.chart_predictor
                    ),
                    state.chart_predictor,
                )

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
                if state.chart_predictor in _COMP_DESCRIPTIONS:
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
                                            _COMP_LABELS[state.chart_predictor],
                                            checked=state.chart_show_components,
                                        )
                                        if _comp_cb.changed:
                                            state.chart_show_components = (
                                                _comp_cb.checked
                                            )
                                    hd.text(
                                        _COMP_DESCRIPTIONS[state.chart_predictor],
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
                _wc_compare_section(state, profile, wc_task)


# ---------------------------------------------------------------------------
# Sub-component: World-class comparison toggles
# ---------------------------------------------------------------------------


def _wc_compare_section(state, profile: dict, wc_task) -> None:
    """
    Renders the 'Compare vs World Class' toggle.
    Placed below the Settings Row 2 prediction box.
    """

    _loading = (
        state.chart_compare_wc and not state.wc_fetch_done and state.wc_fetch_key != ""
    )
    _failed = state.chart_compare_wc and state.wc_fetch_done and state.wc_data is None

    # Compare toggle
    wc_label = wr_category_label(profile)
    if wc_label is None:
        hd.text(
            "Set age, gender and weight in Profile to compare vs world class.",
            font_color="neutral-500",
            font_size="small",
        )
        return

    compare_to_wr_sel = hd.switch(f"Show {wc_label} world records", size="medium")

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
    selected_dists: set,
    selected_times: set,
    rl_available: bool = True,
    pauls_k: float = 5.0,
) -> None:
    """
    Renders the multi-model prediction grid (Your PB, CP, Log-Log, Paul's Law,
    RowingLevel, Average) plus an accuracy footer row.
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
    _PRED_COLS = [
        ("pb", "Your PB", "Your personal best for each event."),
        ("cp", "Critical Power", _DESC_CP),
        ("loglog", "Log-Log Watts Fit", _DESC_LL),
        ("pl", "Paul's Law (average)", _pl_tip),
    ]
    if rl_available:
        _PRED_COLS.append(("rl", "RowingLevel (average)", _DESC_RL))
    _PRED_COLS.append(("avg", "Average", _DESC_AVG))

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
        _acc_vals: dict = {}
        for _ck in ["avg", "cp", "loglog", "pl", "rl"]:
            _pairs = [
                (r[f"{_ck}_raw"], r["pb_raw"])
                for r in pred_rows
                if r.get(f"{_ck}_raw") is not None
                and r.get("pb_raw") is not None
                and (
                    (r["event_type"] == "dist" and r["event_value"] in selected_dists)
                    or (
                        r["event_type"] == "time" and r["event_value"] in selected_times
                    )
                )
            ]
            if _pairs:
                _preds_v, _actuals_v = zip(*_pairs)
                _mean_actual = sum(_actuals_v) / len(_actuals_v)
                _ss_res = sum((p - a) ** 2 for p, a in _pairs)
                _ss_tot = sum((a - _mean_actual) ** 2 for a in _actuals_v)
                _acc_vals[_ck] = {
                    "rmse": (_ss_res / len(_pairs)) ** 0.5,
                    "r2": 1.0 - _ss_res / _ss_tot if _ss_tot > 0 else None,
                    "n": len(_pairs),
                }
            else:
                _acc_vals[_ck] = {"rmse": None, "r2": None, "n": 0}

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
                _av = _acc_vals.get(_ck, {"rmse": None, "r2": None, "n": 0})
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


def _build_ranked_workouts(sync_result, machine: str) -> tuple:
    """
    Extract and quality-filter the ranked non-interval workouts from sync_result.
    Returns (all_ranked, all_ranked_raw, all_seasons); empty lists while loading.
    """
    if sync_result is None:
        return [], [], []
    _workouts_dict, sorted_workouts = sync_result
    if machine != "All":
        sorted_workouts = [w for w in sorted_workouts if w.get("type") == machine]
    all_ranked = [r for r in sorted_workouts if is_ranked_noninterval(r)]
    all_ranked = apply_quality_filters(
        all_ranked, selected_dists=set(), selected_times=set(), excluded_seasons=set()
    )
    all_ranked_raw = list(all_ranked)
    all_seasons = seasons_from(all_ranked)
    return all_ranked, all_ranked_raw, all_seasons


def _apply_display_filter(
    state, all_ranked: list, selected_dists: set, selected_times: set, excluded_seasons
) -> list:
    """Apply event, season, and best-filter; return the chart/table display list."""
    filtered = [
        r
        for r in all_ranked
        if (r.get("distance") in selected_dists or r.get("time") in selected_times)
        and get_season(r.get("date", "")) not in excluded_seasons
    ]
    if state.best_filter == "PBs":
        return apply_best_only(filtered)
    elif state.best_filter == "SBs":
        return apply_season_best_only(filtered)
    return filtered


def _compute_sim_timeline(excluded_seasons, all_seasons: list, sim_week: int) -> tuple:
    """
    Derive the simulation timeline from the included seasons.
    Returns (sim_start, total_days, sim_date, at_today, included_seasons).
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
    sim_day_idx = max(0, min(sim_week, total_days - 1))
    sim_date = sim_start + timedelta(days=sim_day_idx)
    at_today = sim_day_idx >= total_days - 1
    return sim_start, total_days, sim_date, at_today, included_seasons


def _expand_y_bounds_for_wc(
    y_bounds: tuple | None,
    wc_data: dict | None,
    show_watts: bool,
) -> tuple | None:
    """Expand y_bounds to include world-class pace/watts values.

    _compute_axis_bounds uses user PBs only; WC rowers are faster, so without
    expansion the WC overlay points would be clipped.  Returns the original
    y_bounds unchanged when wc_data is absent or empty.
    """
    if y_bounds is None or not wc_data:
        return y_bounds
    _wc_y_vals = [
        compute_watts(pace) if show_watts else pace
        for pace in wc_data["lb"].values()
        if pace > 0
    ]
    if not _wc_y_vals:
        return y_bounds
    _ypad = max((y_bounds[1] - y_bounds[0]) * 0.1, 5.0 if not show_watts else 2.0)
    return (
        min(y_bounds[0], min(_wc_y_vals) - _ypad),
        max(y_bounds[1], max(_wc_y_vals) + _ypad),
    )


def _compute_axis_bounds(
    all_ranked_raw: list,
    excluded_seasons,
    show_watts: bool,
    use_duration: bool,
    log_x: bool,
) -> tuple:
    """
    Compute stable x/y bounds from all-time PBs so the chart doesn't rescale
    when the user toggles individual events.
    Returns (x_bounds, y_bounds); either may be None if data is insufficient.
    """
    bounds_src = [
        w
        for w in all_ranked_raw
        if get_season(w.get("date", "")) not in set(excluded_seasons)
        and (w.get("distance") in RANKED_DIST_SET or w.get("time") in RANKED_TIME_SET)
    ]
    bests = apply_best_only(bounds_src)
    if not bests:
        return None, None
    bp = [p for w in bests if (p := compute_pace(w)) and 60 < p < 400]
    if use_duration:
        bx = [
            w.get("distance") * p / 500
            for w in bests
            if w.get("distance") and (p := compute_pace(w)) and 60 < p < 400
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
    return x_bounds, (yr - ypad, yR + ypad)


def _build_sim_data(
    state,
    ranked_prefilt: list,
    featured_data: list,
    prefilt_excl: list,
    sim_date: date,
) -> SimData:
    """
    Build everything that depends on the current sim position.

      sim_wkts       — filtered workouts visible at sim_date
      excluded_wkts  — PBs of user-disabled events (faint background dots)
      lb, lb_anchor  — selected-event lifetime bests
      lb_all, lb_all_anchor — all-event lifetime bests
      pauls_k_fit    — personalised Paul's constant (None if < 2 PBs)
      pauls_k        — pauls_k_fit or population default 5.0

    ranked_prefilt: dist/time/excluded-seasons filtered, newest-first.
    featured_data:  subset of ranked_prefilt that ever set a historical PB/SB
                    (much smaller for PBs/SBs mode), newest-first.
    prefilt_excl:   all_ranked_raw filtered by excluded seasons only, newest-first.
    All three lists are binary-searched by date — no O(n) scan per tick.
    """

    date_str = sim_date.isoformat()

    if state.best_filter == "All":
        # "All" shows every quality-filtered workout.
        in_time = ranked_prefilt[_bisect_date_desc(ranked_prefilt, date_str) :]
        sim_wkts = in_time
    else:
        # PBs/SBs: featured_data is the (much smaller) historical PB/SB list.
        # apply_best_only on this tiny slice is fast.
        in_time = featured_data[_bisect_date_desc(featured_data, date_str) :]
        if state.best_filter == "PBs":
            sim_wkts = apply_best_only(in_time)
        else:
            sim_wkts = apply_season_best_only(in_time)

    # Binary-search prefilt_excl once for lb_all and excluded-event dots.
    excl_in_time = prefilt_excl[_bisect_date_desc(prefilt_excl, date_str) :]

    # Workouts for disabled events — plotted faintly, not used in model fits.
    excluded_cats = set()
    for i, (dist, _) in enumerate(RANKED_DISTANCES):
        if not state.dist_enabled[i]:
            excluded_cats.add(("dist", dist))
    for i, (tenths, _) in enumerate(RANKED_TIMES):
        if not state.time_enabled[i]:
            excluded_cats.add(("time", tenths))
    excluded_wkts: list = []
    if excluded_cats:
        if state.best_filter == "PBs":
            excluded_wkts = apply_best_only(
                [w for w in excl_in_time if workout_cat_key(w) in excluded_cats]
            )
        elif state.best_filter == "SBs":
            excluded_wkts = apply_season_best_only(
                [w for w in excl_in_time if workout_cat_key(w) in excluded_cats]
            )
        else:
            excluded_wkts = excl_in_time

    lb, lb_anchor = compute_lifetime_bests(sim_wkts)
    lb_all, lb_all_anchor = compute_lifetime_bests(excl_in_time)
    pauls_k_fit = compute_pauls_constant(lb, lb_anchor)
    pauls_k = pauls_k_fit if pauls_k_fit is not None else 5.0
    return SimData(
        sim_wkts=sim_wkts,
        excluded_wkts=excluded_wkts,
        lb=lb,
        lb_anchor=lb_anchor,
        lb_all=lb_all,
        lb_all_anchor=lb_all_anchor,
        pauls_k_fit=pauls_k_fit,
        pauls_k=pauls_k,
    )


# ---------------------------------------------------------------------------
# HyperDiv async helpers  (use hd.task / hd.scope / hd.state)
# ---------------------------------------------------------------------------


def _update_cp_fit(
    state,
    ranked_prefilt: list,
    sim_date: date,
):
    """
    Compute (or retrieve from cache) the Critical Power fit for the current sim
    position.  Mutates state.cp_fit_key / state.cp_fit_result; returns the params
    dict (or None if the fit failed / insufficient data).

    ranked_prefilt must already be filtered by selected dists/times and excluded
    seasons — only parse_date() (fast, C-level) is applied here per tick.
    """
    cp_src = [w for w in ranked_prefilt if parse_date(w.get("date", "")) <= sim_date]
    cp_pb_list = []
    for w in apply_best_only(cp_src):
        dur = compute_duration_s(w)
        pac = compute_pace(w)
        if dur and pac:
            cp_pb_list.append({"duration_s": dur, "watts": compute_watts(pac)})
    fit_key = str(
        sorted((round(p["duration_s"], 1), round(p["watts"], 1)) for p in cp_pb_list)
    )
    if fit_key != state.cp_fit_key:
        state.cp_fit_key = fit_key
        state.cp_fit_result = fit_critical_power(cp_pb_list)
    return state.cp_fit_result


def _fetch_rowinglevel(
    state, profile: dict, chart_workouts: list, at_today: bool
) -> tuple:
    """
    Launch (or resume) the background RowingLevel scrape.
    Only fires when at_today and profile_complete; otherwise returns (None, {}).
    Uses a scope key derived from profile + PB hash so the task re-fires only
    when its inputs change.
    Returns (rl_task, rl_predictions).
    """
    if not at_today or not profile_complete(profile):
        return None, {}

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

    return rl_task, rl_predictions


# ---------------------------------------------------------------------------
# Simulation bundle builder — pure Python, runs in hd.task() thread
# ---------------------------------------------------------------------------


def _build_sim_bundle_fn(
    ranked_prefilt: list,
    prefilt_excl: list,
    featured_data: list,
    *,
    sim_start: "date",
    total_days: int,
    best_filter: str,
    dist_enabled: tuple,
    time_enabled: tuple,
    show_watts: bool,
    is_dark: bool,
    x_mode: str,
    x_bounds: "tuple | None",
    y_bounds: "tuple | None",
    log_x: bool,
    predictor: str,
    draw_power_curves: str,
    show_components: bool,
    rl_predictions: dict,
    all_seasons: list,
    wc_data: "dict | None",
    bundle_key: str,
) -> dict:
    """Precompute the full client-side animation bundle.  No HyperDiv calls.

    ranked_prefilt — selected events, quality-filtered, newest-first.
    prefilt_excl   — all events (for excluded-event faint dots), newest-first.
    featured_data  — historical PB/SB workouts (used in PBs/SBs mode), newest-first.

    Returns the bundle dict consumed by power_curve_chart_plugin.js.
    """
    from services.rowing_utils import (
        RANKED_DISTANCES,
        RANKED_TIMES,
        SEASON_PALETTE,
        apply_best_only,
        apply_season_best_only,
        compute_duration_s,
        compute_pace,
        compute_watts as _compute_watts,
        get_season,
        parse_date,
        workout_cat_key,
        compute_pauls_constant,
    )
    from services.critical_power_model import fit_critical_power

    # ── Excluded categories ──────────────────────────────────────────────────
    excluded_cats = set()
    for i, (dist, _) in enumerate(RANKED_DISTANCES):
        if not dist_enabled[i]:
            excluded_cats.add(("dist", dist))
    for i, (tenths, _) in enumerate(RANKED_TIMES):
        if not time_enabled[i]:
            excluded_cats.add(("time", tenths))

    # ── Season metadata ──────────────────────────────────────────────────────
    sorted_seasons = sorted(all_seasons)
    season_idx_map = {s: i for i, s in enumerate(sorted_seasons)}

    def _hsla(idx, lightness_offset, alpha):
        h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
        return f"hsla({h},{s}%,{max(l + lightness_offset, 0)}%,{alpha:.2f})"

    season_meta = [
        {
            "label": s,
            "color": _hsla(i, 0, 0.90),
            "dim_color": _hsla(i, 0, 0.40),
            "border_color": _hsla(i, -12, 1.0),
        }
        for i, s in enumerate(sorted_seasons)
    ]

    # ── X/Y helpers ──────────────────────────────────────────────────────────
    _use_duration = x_mode == "duration"
    pb_color = "rgba(240,240,240,0.92)" if is_dark else "rgba(40,40,40,0.88)"

    def _x_val(w):
        """Return x value in current x_mode, or None."""
        if _use_duration:
            t = w.get("time")
            if t:
                return t / 10.0
            p = compute_pace(w)
            d = w.get("distance")
            if p and d:
                return round(d * p / 500.0, 2)
            return None
        return w.get("distance")

    # ── Workout manifest (all workouts, oldest-first) ─────────────────────────
    manifest = []

    def _add_to_manifest(w, excluded: bool):
        p = compute_pace(w)
        d = w.get("distance")
        xv = _x_val(w)
        ck = workout_cat_key(w)
        if p is None or d is None or xv is None or ck is None:
            return
        dt = parse_date(w.get("date", ""))
        if dt < sim_start:
            return
        day = (dt - sim_start).days
        if day < 0 or day > total_days:
            return
        season = get_season(w.get("date", ""))
        s_idx = season_idx_map.get(season, 0)
        etype, evalue = ck
        manifest.append(
            {
                "day": day,
                "season_idx": s_idx,
                "cat_key_str": f"{etype}:{evalue}",
                "x": xv,
                "y_pace": round(p, 4),
                "y_watts": round(_compute_watts(p), 1),
                "dist_m": d,
                "event_line": ol_event_line(etype, evalue, p, d),
                "date_label": dt.strftime("%b %d, %Y"),
                "wtype": w.get("workout_type", ""),
                "excluded": excluded,
            }
        )

    for w in ranked_prefilt:
        _add_to_manifest(w, excluded=False)
    for w in prefilt_excl:
        if workout_cat_key(w) in excluded_cats:
            _add_to_manifest(w, excluded=True)

    manifest.sort(key=lambda e: e["day"])

    # ── Keyframe builder ─────────────────────────────────────────────────────
    # Walk unique workout dates oldest→newest; emit a keyframe whenever the
    # lifetime-best dict changes (i.e. a new PB is set for any category).
    sorted_prefilt = sorted(ranked_prefilt, key=lambda w: w.get("date", ""))
    sorted_featured = sorted(featured_data, key=lambda w: w.get("date", ""))

    keyframes = [
        {
            "day": 0,
            "lifetime_best_pace": {},
            "lifetime_best_watts": {},
            "new_pbs": [],
            "new_pb_labels": [],
            "pred_datasets": [],
        }
    ]
    prev_lb_str = {}  # cat_key_str -> pace
    cp_fit_cache = {}  # fit_key -> result

    seen_dates = sorted(
        {w.get("date", "")[:10] for w in sorted_prefilt if w.get("date")}
    )

    for date_str in seen_dates:
        dt = parse_date(date_str)
        if dt < sim_start:
            continue
        day = (dt - sim_start).days
        if day < 0 or day > total_days:
            continue

        # Sim workouts up to this date (replicates _build_sim_data logic)
        if best_filter == "All":
            sim_wkts = [w for w in sorted_prefilt if w.get("date", "")[:10] <= date_str]
        else:
            in_time = [w for w in sorted_featured if w.get("date", "")[:10] <= date_str]
            sim_wkts = (
                apply_best_only(in_time)
                if best_filter == "PBs"
                else apply_season_best_only(in_time)
            )

        lb, lb_anchor = compute_lifetime_bests(sim_wkts)

        lb_str = {f"{k[0]}:{k[1]}": v for k, v in lb.items()}
        lb_anchor_str = {f"{k[0]}:{k[1]}": v for k, v in lb_anchor.items()}
        lb_watts_str = {ck: round(_compute_watts(p), 1) for ck, p in lb_str.items()}

        if lb_str == prev_lb_str:
            continue  # nothing improved — no keyframe needed

        # Which categories got a new PB?
        new_pb_strs = [
            ck
            for ck, p in lb_str.items()
            if p < prev_lb_str.get(ck, float("inf")) - 1e-9
        ]

        # Build PB labels (canvas label dicts)
        new_pb_labels = []
        for ck_str in new_pb_strs:
            pace = lb_str[ck_str]
            dist = lb_anchor_str.get(ck_str, 0)
            etype, evalue_str = ck_str.split(":", 1)
            evalue = int(evalue_str)
            prev_pace = prev_lb_str.get(ck_str)
            pp, pw = pcts(prev_pace, pace) if prev_pace else (0.0, 0.0)
            new_pb_labels.append(
                {
                    "x": dist,
                    "y_pace": round(pace, 4),
                    "y_watts": round(_compute_watts(pace), 1),
                    "line_event": ol_event_line(etype, evalue, pace, dist),
                    "pct_pace": round(pp, 1),
                    "pct_watts": round(pw, 1),
                    "line_label": "\u2746 New PB!",
                    "color": pb_color,
                    "bold": True,
                }
            )

        # CP fit (with local cache to avoid redundant scipy calls)
        cp_params = None
        if predictor in ("critical_power", "average"):
            cp_pb_list = []
            for w in apply_best_only(sim_wkts):
                dur = compute_duration_s(w)
                pac = compute_pace(w)
                if dur and pac:
                    cp_pb_list.append({"duration_s": dur, "watts": _compute_watts(pac)})
            fit_key = str(
                sorted(
                    (round(p["duration_s"], 1), round(p["watts"], 1))
                    for p in cp_pb_list
                )
            )
            if fit_key not in cp_fit_cache:
                cp_fit_cache[fit_key] = fit_critical_power(cp_pb_list)
            cp_params = cp_fit_cache[fit_key]

        pauls_k_fit = compute_pauls_constant(lb, lb_anchor)
        pauls_k = pauls_k_fit if pauls_k_fit is not None else 5.0

        pred_dsets, pred_canvas_labels = build_pred_datasets(
            predictor=predictor,
            lifetime_best=lb,
            lifetime_best_anchor=lb_anchor,
            critical_power_params=cp_params,
            rl_predictions=rl_predictions
            if predictor in ("rowinglevel", "average")
            else None,
            pauls_k=pauls_k,
            show_watts=show_watts,
            is_dark=is_dark,
            x_mode=x_mode,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
            show_components=show_components,
        )

        keyframes.append(
            {
                "day": day,
                "lifetime_best_pace": lb_str,
                "lifetime_best_watts": lb_watts_str,
                "new_pbs": new_pb_strs,
                "new_pb_labels": new_pb_labels,
                "pred_datasets": pred_dsets,
                "pred_canvas_labels": pred_canvas_labels,
            }
        )

        prev_lb_str = lb_str

    # ── Start day: 30 days before the first non-excluded workout ────────────────
    included_days = [m["day"] for m in manifest if not m.get("excluded")]
    start_day = max(0, min(included_days) - 30) if included_days else 0

    # ── Static datasets: WC records (time-invariant) ─────────────────────────
    # Compute pauls_k for the full lifetime best (all workouts in timeline).
    full_lb, full_lb_anchor = compute_lifetime_bests(ranked_prefilt)
    full_pauls_k = compute_pauls_constant(full_lb, full_lb_anchor) or 5.0

    static_datasets = (
        build_wc_static_datasets(
            wc_data,
            predictor=predictor,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
            show_watts=show_watts,
            is_dark=is_dark,
            x_mode=x_mode,
            pauls_k=full_pauls_k,
        )
        if wc_data
        else []
    )

    return {
        "workout_manifest": manifest,
        "keyframes": keyframes,
        "static_datasets": static_datasets,
        "season_meta": season_meta,
        "total_days": total_days,
        "start_day": start_day,
        "pb_badge_lifetime_steps": 40,
        "bundle_key": bundle_key,
        "draw_lifetime_line": draw_power_curves == "PBs",
        "draw_season_lines": draw_power_curves == "SBs",
        "pb_color": pb_color,
        "is_dark": is_dark,
        "show_watts": show_watts,
        "x_mode": x_mode,
        "x_bounds": list(x_bounds) if x_bounds else None,
        "y_bounds": list(y_bounds) if y_bounds else None,
        "log_x": log_x,
    }


# ---------------------------------------------------------------------------
# World-class CP helpers
# ---------------------------------------------------------------------------


def _load_wc_cp(state, profile: dict) -> tuple:
    """
    HyperDiv component: manage the background task that fetches world-class
    data.  Caches result in state.wc_data.

    Returns (task_or_None, wc_data_or_None).
    """
    # Derive API-format profile fields.
    gender_raw = profile.get("gender", "")  # "Male" or "Female"
    if gender_raw not in ("Male", "Female"):
        return None, None
    gender_api = "M" if gender_raw == "Male" else "F"
    age = age_from_dob(profile.get("dob", ""))
    weight_raw = profile.get("weight") or 0.0
    weight_unit = profile.get("weight_unit", "kg")
    weight_kg = weight_raw * 0.453592 if weight_unit == "lbs" else float(weight_raw)
    if age is None or weight_kg <= 0:
        return None, None

    age_cat = wc_age_category(age)
    wt_class = wc_weight_class_str(weight_kg, gender_api, age)
    fetch_key = f"{gender_api}|{age_cat}|{wt_class}"

    # Reset when profile changes.
    if fetch_key != state.wc_fetch_key:
        state.wc_fetch_key = fetch_key
        state.wc_fetch_done = False
        state.wc_data = None

    wc_task = None
    with hd.scope(f"wc_task_{fetch_key}"):
        wc_task = hd.task()
        if not wc_task.running and not wc_task.done:
            wc_task.run(fetch_wc_data, gender_api, age, weight_kg)
        if wc_task.done and not state.wc_fetch_done:
            state.wc_fetch_done = True
            state.wc_data = wc_task.result  # None if API returned nothing

    return wc_task, state.wc_data


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
        draw_power_curves="PBs",
        sim_playing=False,
        sim_week=_SIM_TODAY,
        sim_speed="1x",
        sim_bundle=None,  # precomputed animation bundle dict
        sim_bundle_key="",  # hash of bundle inputs; stale when settings change
        last_sim_day_out=-1,  # tracks chart.sim_day_out changes
        last_sim_done=0,  # tracks chart.sim_done changes
        cp_fit_key="",
        cp_fit_result=None,
        chart_compare_wc=False,
        wc_fetch_key="",
        wc_fetch_done=False,
        wc_data=None,
        _ranked_key="",  # cache invalidation key: f"{machine}:{len(workouts)}"
        _ranked_data=None,  # cached (all_ranked, all_ranked_raw, all_seasons)
        _display_key="",  # cache invalidation key for _apply_display_filter
        _display_data=None,  # cached display list
        _prefilt_key="",  # cache key for pre-filtered ranked list (dist/time/excluded_seasons)
        _prefilt_data=None,  # list of workouts already filtered by dist/time/excluded_seasons
        _prefilt_excl_key="",  # cache key for all-event pre-filter (excluded_seasons only)
        _prefilt_excl_data=None,  # all_ranked_raw filtered by excluded_seasons only
        _featured_key="",  # cache key for compute_featured_workouts result
        _featured_data=None,  # historical PB/SB workouts (newest-first)
        _annot_key="",  # cache key for slider annotations
        _annot_data=None,  # cached list of {day, label, color} dicts
        _bounds_key="",  # cache key for _compute_axis_bounds
        _bounds_data=None,  # cached (x_bounds, y_bounds)
    )
    is_dark = hd.theme().is_dark

    # ── Profile & Data ───────────────────────────────────────────────────────────────
    profile = get_profile()
    sync_result = concept2_sync(client)

    if sync_result is None or profile is None:
        return

    # Cache _build_ranked_workouts — it's expensive (quality-filters all workouts)
    # and its inputs only change when new workouts arrive or the machine filter changes.
    _ranked_key = f"{machine}:{len(sync_result[1])}"
    if state._ranked_key != _ranked_key or state._ranked_data is None:
        state._ranked_data = _build_ranked_workouts(sync_result, machine)
        state._ranked_key = _ranked_key
    all_ranked, all_ranked_raw, all_seasons = state._ranked_data

    # ── Filters ───────────────────────────────────────────────────────────────
    selected_dists = {
        dist for i, (dist, _) in enumerate(RANKED_DISTANCES) if state.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(RANKED_TIMES) if state.time_enabled[i]
    }

    _display_key = f"{state._ranked_key}:{sorted(selected_dists)}:{sorted(selected_times)}:{excluded_seasons}:{state.best_filter}"
    if state._display_key != _display_key or state._display_data is None:
        state._display_data = _apply_display_filter(
            state, all_ranked, selected_dists, selected_times, excluded_seasons
        )
        state._display_key = _display_key
    display = state._display_data

    # Pre-filter all_ranked_raw by selected dists/times and excluded seasons once.
    # Removes get_season() (strptime) from the hot animation path in _update_cp_fit
    # — only fast parse_date() runs per tick.
    _prefilt_key = f"{state._ranked_key}:{sorted(selected_dists)}:{sorted(selected_times)}:{excluded_seasons}"
    if state._prefilt_key != _prefilt_key or state._prefilt_data is None:
        _excl_set = set(excluded_seasons)
        state._prefilt_data = [
            w
            for w in all_ranked_raw
            if (w.get("distance") in selected_dists or w.get("time") in selected_times)
            and get_season(w.get("date", "")) not in _excl_set
        ]
        state._prefilt_key = _prefilt_key
    _ranked_prefilt = state._prefilt_data

    # All-event pre-filter: excluded seasons only (no dist/time gate).
    # Used by _build_sim_data for lb_all (all-event lifetime bests) and
    # excluded-event dots — removes get_season() from those per-tick paths.
    _prefilt_excl_key = f"{state._ranked_key}:{excluded_seasons}"
    if state._prefilt_excl_key != _prefilt_excl_key or state._prefilt_excl_data is None:
        _excl_set2 = set(excluded_seasons)
        state._prefilt_excl_data = [
            w for w in all_ranked_raw if get_season(w.get("date", "")) not in _excl_set2
        ]
        state._prefilt_excl_key = _prefilt_excl_key
    _prefilt_excl = state._prefilt_excl_data

    # Featured workouts: the subset of _ranked_prefilt that ever set a new
    # historical PB or SB.  Much smaller than _ranked_prefilt for PBs/SBs mode;
    # used by _build_sim_data (date-sliced per tick) and slider annotations.
    _featured_key = f"{state._prefilt_key}:{state.best_filter}"
    if state._featured_key != _featured_key or state._featured_data is None:
        state._featured_data = compute_featured_workouts(
            _ranked_prefilt, state.best_filter
        )
        state._featured_key = _featured_key
    _featured_data = state._featured_data

    # ── Simulation timeline ───────────────────────────────────────────────────
    (
        sim_start,
        total_days,
        sim_date,
        at_today,
        included_seasons,
    ) = _compute_sim_timeline(excluded_seasons, all_seasons, state.sim_week)
    show_watts = state.chart_y_metric == "watts"

    # Slider annotations — stable across animation ticks; only recompute when
    # filters or sim range changes (same inputs as _featured_data + sim_start).
    _annot_key = f"{_featured_key}:{sim_start}"
    if state._annot_key != _annot_key or state._annot_data is None:
        state._annot_data = build_sb_annotations(
            _featured_data,
            sim_start,
            included_seasons,
            best_filter=state.best_filter,
        )
        state._annot_key = _annot_key

    # ── Simulation data ───────────────────────────────────────────────────────
    _sim = _build_sim_data(
        state,
        _ranked_prefilt,
        _featured_data,
        _prefilt_excl,
        sim_date,
    )
    sim_wkts = _sim.sim_wkts
    excluded_wkts = _sim.excluded_wkts
    lb, lb_anchor = _sim.lb, _sim.lb_anchor
    lb_all, lb_all_anchor = _sim.lb_all, _sim.lb_all_anchor
    pauls_k_fit = _sim.pauls_k_fit
    pauls_k = _sim.pauls_k

    cp_params = _update_cp_fit(
        state,
        _ranked_prefilt,
        sim_date,
    )

    rl_task, rl_predictions = _fetch_rowinglevel(state, profile, display, at_today)
    wc_task, wc_data = (
        _load_wc_cp(state, profile) if state.chart_compare_wc else (None, None)
    )

    # ── Axis bounds ───────────────────────────────────────────────────────────
    _bounds_key = f"{state._ranked_key}:{excluded_seasons}:{show_watts}:{state.chart_x_metric}:{state.chart_log_x}"
    if state._bounds_key != _bounds_key or state._bounds_data is None:
        state._bounds_data = _compute_axis_bounds(
            all_ranked_raw,
            excluded_seasons,
            show_watts,
            state.chart_x_metric == "duration",
            state.chart_log_x,
        )
        state._bounds_key = _bounds_key
    x_bounds, y_bounds = state._bounds_data

    # Expand y_bounds to include WC records when comparing.
    if state.chart_compare_wc:
        y_bounds = _expand_y_bounds_for_wc(y_bounds, wc_data, show_watts)

    # ── Sim bundle management ─────────────────────────────────────────────────
    # Bundle key: hash of all inputs that affect the bundle content.
    # When any of these change, the existing bundle is stale and must be rebuilt.
    _bundle_key = hashlib.md5(
        json.dumps(
            [
                state.chart_predictor,
                state.best_filter,
                sorted(list(selected_dists)),
                sorted(list(selected_times)),
                sorted(list(excluded_seasons)),
                show_watts,
                state.chart_x_metric,
                state.chart_log_x,
                state.draw_power_curves,
                state.chart_show_components,
                state.chart_compare_wc,
                state._prefilt_key,  # includes machine + workout count + dist/time/season filters
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]

    if state.sim_bundle_key != _bundle_key:
        # Settings changed — invalidate the cached bundle.
        state.sim_bundle = None
        state.sim_bundle_key = _bundle_key

    if state.sim_bundle is None:
        # Bundle needed but not ready — launch the build task.
        with hd.scope(f"sim_bundle_{_bundle_key}"):
            _bt = hd.task()
            if not _bt.running and not _bt.done:
                _bt.run(
                    _build_sim_bundle_fn,
                    _ranked_prefilt,
                    _prefilt_excl,
                    _featured_data,
                    sim_start=sim_start,
                    total_days=total_days,
                    best_filter=state.best_filter,
                    dist_enabled=state.dist_enabled,
                    time_enabled=state.time_enabled,
                    show_watts=show_watts,
                    is_dark=is_dark,
                    x_mode=state.chart_x_metric,
                    x_bounds=x_bounds,
                    y_bounds=y_bounds,
                    predictor=state.chart_predictor,
                    draw_power_curves=state.draw_power_curves,
                    show_components=state.chart_show_components,
                    log_x=state.chart_log_x,
                    rl_predictions=rl_predictions,
                    all_seasons=all_seasons,
                    wc_data=wc_data,
                    bundle_key=_bundle_key,
                )
            if _bt.done:
                if _bt.result:
                    state.sim_bundle = _bt.result
                elif _bt.error:
                    # Task failed — stop playing and surface the error.
                    state.sim_playing = False
                    hd.alert(
                        f"Animation bundle failed: {_bt.error}",
                        variant="danger",
                        closable=True,
                    )

    # ── Compute sim_command ───────────────────────────────────────────────────
    # Seeking is handled entirely in JS via the integrated scrubber; Python only
    # signals play / pause / stop.
    if state.sim_playing and not at_today and state.sim_bundle is not None:
        _sim_command = "play"
    elif state.sim_playing and state.sim_bundle is None:
        _sim_command = "pause"  # bundle not ready yet — hold JS
    elif at_today:
        _sim_command = "stop"
    elif state.sim_bundle is not None:
        _sim_command = "pause"
    else:
        _sim_command = "stop"

    # ── Render ────────────────────────────────────────────────────────────────
    with hd.box(gap=5, align="center", padding=(2, 2, 2, 2)):
        with hd.box(width="100%", align="center"):
            with hd.h1():
                _date_label = sim_date.strftime("%b %d, %Y")
                _best_long = {
                    "All": "All Great Efforts",
                    "PBs": "Personal Bests",
                    "SBs": "Season Bests",
                }
                _cur_best_lbl = _best_long.get(state.best_filter, state.best_filter)
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
                                        hd.radio_button(
                                            "All Great Efforts", value="All"
                                        )
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
                                        value=state.draw_power_curves, size="small"
                                    ) as _dpc_rg:
                                        hd.radio_button("SBs", value="SBs")
                                        hd.radio_button("PBs", value="PBs")
                                        hd.radio_button("None", value="None")
                                    if _dpc_rg.changed:
                                        state.draw_power_curves = _dpc_rg.value

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
                                    state.dist_enabled = tuple(
                                        True for _ in RANKED_DISTANCES
                                    )
                                    state.time_enabled = tuple(
                                        True for _ in RANKED_TIMES
                                    )
                                if hd.button(
                                    "Clear all", size="small", variant="text"
                                ).clicked:
                                    state.dist_enabled = tuple(
                                        False for _ in RANKED_DISTANCES
                                    )
                                    state.time_enabled = tuple(
                                        False for _ in RANKED_TIMES
                                    )
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

                if (
                    at_today
                    and rl_task is not None
                    and state.chart_predictor == "rowinglevel"
                ):
                    if not profile_complete(profile):
                        hd.alert(
                            "Please complete your profile (Gender, Age, and Bodyweight) "
                            "in the Profile tab before using RowingLevel predictions.",
                            variant="warning",
                            opened=True,
                        )

            predictor = (
                state.chart_predictor
                if at_today or state.chart_predictor != "rowinglevel"
                else "none"
            )
            # Transparent CP → loglog fallback when not enough data for a CP fit.
            effective_predictor = predictor
            if effective_predictor == "critical_power" and cp_params is None:
                effective_predictor = "loglog"

            # Skip the expensive build_chart_config() while JS animation is running.
            # JS uses sim_bundle to render the chart; config is only needed for
            # the static (non-playing) view.
            if state.sim_playing and state.sim_bundle is not None:
                chart_cfg = None  # JS ignores config when bundle is active
            else:
                chart_cfg = build_chart_config(
                    sim_wkts,
                    log_x=state.chart_log_x,
                    log_y=state.chart_log_y,
                    show_lifetime_line=state.draw_power_curves == "PBs",
                    show_watts=show_watts,
                    is_dark=is_dark,
                    predictor=effective_predictor,
                    rl_predictions=rl_predictions,
                    critical_power_params=cp_params,
                    season_lines=set(all_seasons)
                    if state.draw_power_curves == "SBs"
                    else set(),
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
                    wc_data=wc_data,
                )

            _chart_section(
                state,
                chart_cfg=chart_cfg,
                rl_task=rl_task,
                rl_predictions=rl_predictions,
                profile=profile,
                show_watts=show_watts,
                total_days=total_days,
                sim_start=sim_start,
                sb_annotations=state._annot_data,
                rewind_day=_compute_rewind_day(_ranked_prefilt, sim_start),
                pauls_k_fit=pauls_k_fit,
                wc_task=wc_task,
                sim_command=_sim_command,
            )

            rl_available = profile_complete(profile)
            if not rl_available:
                _rl_profile_notice()

        pred_rows = build_prediction_table_data(
            lifetime_best=lb,
            lifetime_best_anchor=lb_anchor,
            all_lifetime_best=lb_all,
            all_lifetime_best_anchor=lb_all_anchor,
            critical_power_params=cp_params,
            rl_predictions=rl_predictions if rl_predictions else None,
            pauls_k=pauls_k,
        )

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
                selected_dists,
                selected_times,
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

            types = {r.get("type") for r in display}
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
            WorkoutTable(display, cols, paginate=False)
