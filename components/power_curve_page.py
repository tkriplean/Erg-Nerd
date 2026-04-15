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
    Transport bar: ▶/⏸  speed-cycle-button  ──── DateSlider ────
    PowerCurveChart (75vh)

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
  sim_playing        bool          whether the animation ticker is running
  sim_tick_id        int           monotonically increasing; increment to advance animation
  sim_last_pb_label  str           display text for the "New PB!" badge
  sim_pb_set_at_day  int           day index when most recent PB was set
  sim_pb_stored_labels_json str    JSON list of PB overlay labels captured at detection time
  last_ds_change_id  int           tracks DateSlider changes to avoid stale scrubs
  cp_fit_key         str           hash of CP input data; used to cache the CP fit
  cp_fit_result      dict|None     cached CP fit params from fit_critical_power()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SIMULATION / TIMELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  sim_start  = May 1 of the earliest included season
  sim_end    = min(date.today(), Apr 30 of the year after the latest included season)
  total_days = (sim_end - sim_start).days + 1
  _at_today  = sim_day_idx >= total_days - 1

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
import hyperdiv as hd

from services.rowinglevel import (
    _PROFILE_DEFAULTS,
    age_from_dob,
    fetch_all_pb_predictions,
    fetch_predictions as rl_fetch_predictions,
    profile_complete,
)
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
)
from components.concept2_sync import concept2_sync
from services.critical_power_model import fit_critical_power
from services.concept2_records import (
    get_age_group_records,
    records_to_cp_input,
    records_to_lbest,
    age_category as wc_age_category,
    weight_class_str as wc_weight_class_str,
)
from components.power_curve_chart_plugin import PowerCurveChart
from components.date_slider_plugin import DateSlider
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
    compute_lifetime_bests,
)
from services.ranked_predictions import build_prediction_table_data
from components.hyperdiv_extensions import radio_group, shadowed_box, grid_box


# ---------------------------------------------------------------------------
# Constants local to this module
# ---------------------------------------------------------------------------

# Animation: tick interval and step sizes per speed option.
_BASE_TICK_SECS = 0.35  # seconds per animation tick — tune if too fast/slow
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
# Lookahead overlay computation
# ---------------------------------------------------------------------------


def _compute_lookahead_overlays(
    sim_wkts: list,
    ranked_prefilt: list,
    sim_date: date,
    sim_day_idx: int,
    state,
    total_days: int,
    show_watts: bool,
) -> tuple:
    """
    Compute ghost/arrow/threatened/new-arrival overlay data for the lookahead
    window during simulation.

    Returns (sim_overlays dict, overlay_labels list, updated state side-effects).
    Should only be called when not at today (not _at_today).
    """
    _step = _SPEED_DAYS.get(state.sim_speed, 7)
    _lookahead_end = sim_date + timedelta(days=_step * _SIM_LOOKAHEAD_STEPS)
    _new_step_start = sim_date - timedelta(days=_step)

    is_dark = hd.theme().is_dark

    _has_any_lines = state.draw_power_curves != "None"

    # Current per-event best (pace + effective dist) from visible sim workouts.
    # Only computed when lines are shown (overlays gate on this).
    _cur_best_pace: dict = {}  # cat_key -> best pace
    _cur_best_dist: dict = {}  # cat_key -> effective dist of best
    if _has_any_lines:
        for _w in sim_wkts:
            _ck = workout_cat_key(_w)
            _p = compute_pace(_w)
            _d = _w.get("distance")
            if _ck and _p and _d:
                if _ck not in _cur_best_pace or _p < _cur_best_pace[_ck]:
                    _cur_best_pace[_ck] = _p
                    _cur_best_dist[_ck] = _d

    # Arrows + ghost dots: one per event where a better upcoming performance
    # beats the current best. Ghost dots = the "to" end of each arrow.
    # Only produced when at least one line type is active.
    _arrows: list = []
    _threatened_cats: set = set()  # cat_keys of currently-threatened events
    _ghost_pts: list = []  # derived from arrow targets

    if _has_any_lines and _cur_best_pace:
        _upcoming_raw = [
            w
            for w in ranked_prefilt
            if sim_date < parse_date(w.get("date", "")) <= _lookahead_end
        ]
        _seen_threat_cats: set = set()
        for _w in sorted(_upcoming_raw, key=lambda w: compute_pace(w) or 999):
            _ck = workout_cat_key(_w)
            _p = compute_pace(_w)
            _d = _w.get("distance")
            if not _ck or not _p or not _d:
                continue
            if (
                _ck in _cur_best_pace
                and _p < _cur_best_pace[_ck]
                and _ck not in _seen_threat_cats
            ):
                _etype, _evalue = _ck
                _elabel = f"{_evalue}m" if _etype == "dist" else f"{_evalue // 60}min"
                _arrows.append(
                    {
                        "from_dist": _cur_best_dist[_ck],
                        "from_pace": _cur_best_pace[_ck],
                        "to_dist": _d,
                        "to_pace": _p,
                        "to_season": get_season(_w.get("date", "")),
                        "cat_label": _elabel,
                    }
                )
                _ghost_pts.append(
                    {
                        "dist": _d,
                        "pace": _p,
                        "season": get_season(_w.get("date", "")),
                    }
                )
                _threatened_cats.add(_ck)
                _seen_threat_cats.add(_ck)

    # Newly arrived: cat_keys of events whose best appeared in the last step
    _new_arrival_cats: set = set()
    for _w in sim_wkts:
        _ck = workout_cat_key(_w)
        if _ck and _new_step_start < parse_date(_w.get("date", "")) <= sim_date:
            _new_arrival_cats.add(_ck)

    # Newly set PBs: events where the *current best* was set in the last step
    _new_pb_cats: set = set()
    _new_pb_events: list = []
    _ev_best: dict = {}  # cat_key -> (pace, date, effective_dist)
    for _w in sim_wkts:
        _ck = workout_cat_key(_w)
        _p = compute_pace(_w)
        _d = _w.get("distance")
        if _ck and _p and _d:
            if _ck not in _ev_best or _p < _ev_best[_ck][0]:
                _ev_best[_ck] = (_p, parse_date(_w.get("date", "")), _d)

    # Best pace per event strictly before this step — used both to gate tie
    # detection and to compute % improvement labels.
    _det_prev_pace: dict = {}
    for _w in sim_wkts:
        _ck2 = workout_cat_key(_w)
        _p2 = compute_pace(_w)
        if _ck2 and _p2 and parse_date(_w.get("date", "")) <= _new_step_start:
            if _ck2 not in _det_prev_pace or _p2 < _det_prev_pace[_ck2]:
                _det_prev_pace[_ck2] = _p2

    for _ck, (_p, _d_date, _d) in _ev_best.items():
        # Require the best to have arrived in this step AND be strictly faster
        # than the prior step's best — ties must not trigger a new PB callout.
        if _new_step_start < _d_date <= sim_date:
            _prev = _det_prev_pace.get(_ck)
            if _prev is None or _p < _prev:
                _new_pb_cats.add(_ck)
                _etype, _evalue = _ck
                _elabel = f"{_evalue}m" if _etype == "dist" else f"{_evalue // 60}min"
                _pm = int(_p // 60)
                _ps = _p % 60
                _new_pb_events.append(f"{_elabel} — {_pm}:{_ps:04.1f}")

    # Update PB label + timestamp when a new PB lands this step.
    # Also capture the full overlay label dicts now (while _ev_best and
    # the pre-PB bests are in scope) so they can be replayed during the
    # celebration window without depending on the rolling step window.
    if _new_pb_events:
        state.sim_last_pb_label = "New PB!  " + "  ·  ".join(_new_pb_events)
        state.sim_pb_set_at_day = sim_day_idx
        _stored: list = []
        for _ck in _new_pb_cats:
            if _ck in _ev_best:
                _pb_pace, _, _pb_dist = _ev_best[_ck]
                _etype, _evalue = _ck
                _pp, _pw = pcts(_det_prev_pace.get(_ck), _pb_pace)
                _stored.append(
                    {
                        "x": _pb_dist,
                        "y_raw": _pb_pace,
                        "line_event": ol_event_line(
                            _etype, _evalue, _pb_pace, _pb_dist
                        ),
                        "pct_pace": _pp,
                        "pct_watts": _pw,
                        "line_label": "✦ New PB!",
                        "color": "black" if not is_dark else "white",
                        "bold": True,
                    }
                )
        state.sim_pb_stored_labels_json = json.dumps(_stored)

    sim_overlays = {
        "ghost_pts": _ghost_pts,
        "arrows": _arrows,
        "threatened_cats": _threatened_cats,
        "new_arrival_cats": _new_arrival_cats,
        "new_pb_cats": _new_pb_cats,
    }

    # ---- canvas label overlays (drawn by JS plugin, not HyperDiv) ----
    _pb_celebrating = False
    overlay_labels = []

    # Show "New PB!" for ~40 sim-steps after it was set.  Labels are
    # captured in full (with % improvement) at detection time and stored
    # as JSON so they survive the rolling step window moving on.
    _pb_celebrating = state.sim_pb_set_at_day >= 0 and (
        0 <= sim_day_idx - state.sim_pb_set_at_day <= _step * 40
    )

    # New PB badges — loaded from state, not recomputed from rolling window
    if _pb_celebrating:
        for _lbl in json.loads(state.sim_pb_stored_labels_json):
            # Stored color was computed at detection time; re-apply current
            # theme so dark/light mode switches work instantly.
            _lbl["color"] = "black" if not is_dark else "white"
            overlay_labels.append(_lbl)

    # Upcoming PB badges: event+time / % improvement / upcoming PB
    for _arr in sim_overlays["arrows"]:
        _to_d, _to_p, _fr_p = (
            _arr["to_dist"],
            _arr["to_pace"],
            _arr["from_pace"],
        )
        _etype2 = "dist" if _to_d in RANKED_DIST_SET else "time"
        _evalue2 = (
            _to_d
            if _etype2 == "dist"
            else next(
                (
                    t
                    for t in RANKED_TIME_SET
                    if abs(round(_to_p * 10 * _to_d / 500) - t) < 5
                ),
                _to_d,
            )
        )
        _pp, _pw = pcts(_fr_p, _to_p)
        overlay_labels.append(
            {
                "x": _to_d,
                "y_raw": _to_p,
                "line_event": ol_event_line(_etype2, _evalue2, _to_p, _to_d),
                "pct_pace": _pp,
                "pct_watts": _pw,
                "line_label": "upcoming PB",
                "color": "black" if not is_dark else "white",
                "bold": False,
            }
        )

    return sim_overlays, overlay_labels


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
    sim_date,
    _at_today: bool,
    sim_day_idx: int,
    total_days: int,
    sim_start,
    sb_annotations: list,
    all_ranked_raw: list,
    selected_dists: set,
    selected_times: set,
    _included_seasons: list,
    all_seasons: list,
    excluded_seasons=(),
    pauls_k_fit: float | None = None,
    wc_task=None,
) -> None:
    """
    Renders the performance chart box: header, RL status, transport bar,
    PowerCurveChart, settings row, and components toggle.
    """
    is_dark = hd.theme().is_dark

    # ---- transport bar ----
    # Three-column layout: [controls (fixed)] | [slider (grows)] | [spacer (= controls width)]
    with hd.box(align="center", width="100%"):
        with hd.hbox(
            gap=0,
            align="center",
            padding_top=1,
            padding_bottom=0,
            width="100%",
            max_width="1280px",
        ):
            with hd.hbox(gap=0.5, align="center"):
                _play_label = "⏸  Pause" if state.sim_playing else "▶  Play"
                _play_variant = "default" if state.sim_playing else "primary"
                if hd.button(_play_label, size="medium", variant=_play_variant).clicked:
                    if state.sim_playing:
                        state.sim_playing = False
                    else:
                        if _at_today:
                            # Start 30 days before the first qualifying event
                            # so something appears almost immediately.
                            _earliest = [
                                parse_date(w.get("date", ""))
                                for w in all_ranked_raw
                                if (
                                    w.get("distance") in selected_dists
                                    or w.get("time") in selected_times
                                )
                                and get_season(w.get("date", ""))
                                not in set(excluded_seasons)
                            ]
                            if _earliest:
                                state.sim_week = max(
                                    0, (min(_earliest) - sim_start).days - 30
                                )
                            else:
                                state.sim_week = 0
                        state.sim_tick_id += 1
                        state.sim_playing = True

                _sp_idx = (
                    list(_SPEED_OPTIONS).index(state.sim_speed)
                    if state.sim_speed in _SPEED_OPTIONS
                    else 1
                )
                with hd.tooltip("Playback speed — click to change"):
                    if hd.button(
                        state.sim_speed, size="medium", variant="neutral"
                    ).clicked:
                        state.sim_speed = _SPEED_OPTIONS[
                            (_sp_idx + 1) % len(_SPEED_OPTIONS)
                        ]
                        if state.sim_playing:
                            state.sim_tick_id += 1

            with hd.box(grow=1):
                ds = DateSlider(
                    min_value=0,
                    max_value=max(1, total_days - 1),
                    target_value=sim_day_idx,
                    step=1,
                    start_date=sim_start.isoformat(),
                    annotations=sb_annotations,
                )
                if ds.change_id != state.last_ds_change_id:
                    state.last_ds_change_id = ds.change_id
                    state.sim_week = int(ds.value)
                    state.sim_playing = False

    if not chart_cfg:
        hd.text("No chart data available.", font_color="neutral-500")
        return

    with hd.hbox(align="center"):
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

        with hd.box():
            # ---- chart ----
            PowerCurveChart(
                config=chart_cfg,
                show_watts=show_watts,
                x_mode=state.chart_x_metric,
                height="75vh",
            )

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
    _wc_profile_ok = profile_complete(profile)

    if not _wc_profile_ok:
        hd.text(
            "Set age, gender and weight in Profile to compare vs world class.",
            font_color="neutral-500",
            font_size="small",
        )
        return

    # Derive current age-category label for display when loaded.
    _wc_label = ""

    gender_raw = profile.get("gender", "")
    gender_api = "M" if gender_raw == "Male" else "F"
    _age = age_from_dob(profile.get("dob", ""))
    _wt = profile.get("weight") or 0.0
    _wt_unit = profile.get("weight_unit", "kg")
    _wt_kg = _wt * 0.453592 if _wt_unit == "lbs" else float(_wt)
    if _age is not None and _wt_kg > 0:
        _age_cat = wc_age_category(_age)
        _wt_cls = wc_weight_class_str(_wt_kg, gender_api)
        _wc_label = f"{gender_api} {_age_cat} {_wt_cls}"

    _loading = (
        state.chart_compare_wc and not state.wc_fetch_done and state.wc_fetch_key != ""
    )
    _failed = state.chart_compare_wc and state.wc_fetch_done and state.wc_data is None

    # Compare toggle
    compare_to_wr_sel = hd.switch(f"Show {_wc_label} world records", size="medium")

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
    Returns (sim_start, total_days, sim_day_idx, sim_date, at_today, included_seasons).
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
    return sim_start, total_days, sim_day_idx, sim_date, at_today, included_seasons


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
) -> tuple:
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
    return (
        sim_wkts,
        excluded_wkts,
        lb,
        lb_anchor,
        lb_all,
        lb_all_anchor,
        pauls_k_fit,
        pauls_k,
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


def _run_animation_tick(state, total_days: int) -> None:
    """Advance sim_week by one step when the animation is playing."""
    if not state.sim_playing:
        return
    with hd.scope(f"sim_tick_{state.sim_tick_id}"):
        tick = hd.task()
        if not tick.running and not tick.done:
            tick.run(time.sleep, _BASE_TICK_SECS)
        if tick.done:
            step = _SPEED_DAYS.get(state.sim_speed, 7)
            nd = state.sim_week + step
            if nd >= total_days:
                state.sim_week = _SIM_TODAY
                state.sim_playing = False
            else:
                state.sim_week = nd
                state.sim_tick_id += 1


# ---------------------------------------------------------------------------
# World-class CP helpers
# ---------------------------------------------------------------------------


def _fetch_wc_data(gender_api: str, age: int, weight_kg: float) -> dict | None:
    """
    Blocking function — intended to run inside hd.task().
    Fetches Concept2 world records for the given gender/age/weight,
    fits the CP model (when enough data), builds lb/lba dicts, and
    optionally fetches RowingLevel predictions using the WC 2k record
    as the reference performance.

    Returns a dict {"records", "cp_params", "lb", "lba", "rl_predictions"}
    or None if the API returned no records at all.
    """
    records = get_age_group_records(gender_api, age, weight_kg)
    if not records:
        return None
    cp_input = records_to_cp_input(records)
    cp_params = fit_critical_power(cp_input) if len(cp_input) >= 5 else None
    lb, lba = records_to_lbest(records)

    # RowingLevel predictions: use WC record at best available dist event as
    # the reference performance (prefer 2k, the canonical RL anchor).
    rl_predictions: dict = {}
    gender_rl = "Male" if gender_api == "M" else "Female"
    _ref_dist, _ref_time_s = None, None
    for _d in [2000, 1000, 5000, 6000, 10000, 500, 21097]:
        _t = records.get(("dist", _d))
        if _t:
            _ref_dist, _ref_time_s = _d, _t
            break
    if _ref_dist is not None and _ref_time_s is not None:
        time_tenths = round(_ref_time_s * 10)
        preds = rl_fetch_predictions(gender_rl, age, weight_kg, _ref_dist, time_tenths)
        if preds:
            rl_predictions[str(("dist", _ref_dist))] = preds

    return {
        "records": records,
        "cp_params": cp_params,
        "lb": lb,
        "lba": lba,
        "rl_predictions": rl_predictions,
    }


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
    wt_class = wc_weight_class_str(weight_kg, gender_api)
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
            wc_task.run(_fetch_wc_data, gender_api, age, weight_kg)
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
        chart_show_lifetime_line=True,
        chart_y_metric="pace",
        chart_x_metric="distance",
        chart_predictor="critical_power",
        chart_show_components=False,
        chart_season_lines=(),
        draw_power_curves="PBs",
        show_chart_settings=False,
        sim_playing=False,
        sim_week=_SIM_TODAY,
        sim_speed="1x",
        sim_tick_id=0,
        sim_last_pb_label="",
        sim_pb_set_at_day=-9999,
        sim_pb_stored_labels_json="[]",
        last_ds_change_id=0,
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

    # ── Profile ───────────────────────────────────────────────────────────────
    ls_profile = hd.local_storage.get_item("profile")
    if not ls_profile.done:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return
    profile = {**_PROFILE_DEFAULTS}
    if ls_profile.result:
        try:
            profile = {**_PROFILE_DEFAULTS, **json.loads(ls_profile.result)}
        except Exception:
            pass

    # ── Data ──────────────────────────────────────────────────────────────────
    sync_result = concept2_sync(client)

    if sync_result is None:
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
    # and _compute_lookahead_overlays — only fast parse_date() runs per tick.
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
        sim_day_idx,
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
    (
        sim_wkts,
        excluded_wkts,
        lb,
        lb_anchor,
        lb_all,
        lb_all_anchor,
        pauls_k_fit,
        pauls_k,
    ) = _build_sim_data(
        state,
        _ranked_prefilt,
        _featured_data,
        _prefilt_excl,
        sim_date,
    )

    cp_params = _update_cp_fit(
        state,
        _ranked_prefilt,
        sim_date,
    )

    rl_task, rl_predictions = _fetch_rowinglevel(state, profile, display, at_today)
    wc_task, wc_data = (
        _load_wc_cp(state, profile) if state.chart_compare_wc else (None, None)
    )
    _run_animation_tick(state, total_days)

    # ── Overlays + bounds ─────────────────────────────────────────────────────
    sim_overlays, overlay_labels = None, []
    if not at_today:
        sim_overlays, overlay_labels = _compute_lookahead_overlays(
            sim_wkts,
            _ranked_prefilt,
            sim_date,
            sim_day_idx,
            state,
            total_days,
            show_watts,
        )

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
    # _compute_axis_bounds is derived from user PBs only; world-class rowers are
    # faster (lower pace / higher watts), so without expansion they'd be clipped.
    if y_bounds is not None and wc_data is not None and state.chart_compare_wc:
        _wc_y_vals = [
            compute_watts(pace) if show_watts else pace
            for pace in wc_data["lb"].values()
            if pace > 0
        ]
        if _wc_y_vals:
            _ypad = max(
                (y_bounds[1] - y_bounds[0]) * 0.1, 5.0 if not show_watts else 2.0
            )
            y_bounds = (
                min(y_bounds[0], min(_wc_y_vals) - _ypad),
                max(y_bounds[1], max(_wc_y_vals) + _ypad),
            )

    # ── Render ────────────────────────────────────────────────────────────────
    with hd.box(gap=5, align="center", padding=(2, 2, 2, 2)):
        with hd.box():
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
                sim_overlays=sim_overlays,
                overlay_labels=overlay_labels,
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
                sim_date=sim_date,
                _at_today=at_today,
                sim_day_idx=sim_day_idx,
                total_days=total_days,
                sim_start=sim_start,
                sb_annotations=state._annot_data,
                all_ranked_raw=all_ranked_raw,
                selected_dists=selected_dists,
                selected_times=selected_times,
                _included_seasons=included_seasons,
                all_seasons=all_seasons,
                excluded_seasons=excluded_seasons,
                pauls_k_fit=pauls_k_fit,
                wc_task=wc_task,
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
