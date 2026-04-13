"""
Performance Page — UI and orchestration for the ranked-events view.

Exported:
  power_curve_page()   — top-level HyperDiv component; call from app.py

See docs/power_curve_page.md for a full reference.

Helper logic is split across:
  services/formatters.py              — formatting helpers
  components/workout_table            — result_table
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
  chart_metric       str           "Pace" | "Watts"
  chart_x_mode       str           "distance" | "duration"
  chart_predictor    str           "none" | "pauls_law" | "loglog" | "rowinglevel"
                                   | "critical_power" | "average"
  chart_lines        str           "PBs" | "SBs" | "None" — which best-curves to draw
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
    compute_duration_s,
    compute_pauls_constant,
)
from components.concept2_sync import concept2_sync
from services.critical_power_model import fit_critical_power
from services.concept2_records import (
    get_age_group_records,
    records_to_cp_input,
    age_category as wc_age_category,
    weight_class_str as wc_weight_class_str,
)
from components.power_curve_chart_plugin import PowerCurveChart
from components.date_slider_plugin import DateSlider
from components.workout_table import result_table
from services.ranked_filters import (
    is_ranked_noninterval,
    seasons_from,
    apply_quality_filters,
    sim_workouts_at,
)
from components.power_curve_chart_builder import (
    build_sb_annotations,
    ol_event_line,
    pcts,
    build_chart_config,
    compute_lifetime_bests,
)
from services.ranked_predictions import build_prediction_table_data
from components.hyperdiv_extensions import radio_group, shadowed_box


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
_DESC_AVG = "Mean of all available predictions for this event."

# Predictors that support the "Show components" toggle, and their descriptions.
_COMP_DESCRIPTIONS = {
    "pauls_law": "Shows one curve per PB anchor, before averaging.",
    "rowinglevel": "Shows the RL curve from each PB anchor, before distance-weighted averaging.",
    "critical_power": "Shows the fast-twitch and slow-twitch power components separately.",
    "average": "Shows all individual model curves that were averaged.",
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
    all_ranked_raw: list,
    sim_date: date,
    sim_day_idx: int,
    state,
    total_days: int,
    selected_dists: set,
    selected_times: set,
    included_seasons: list,
    show_watts: bool,
    excluded_seasons=(),
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

    _has_any_lines = state.chart_lines != "None"

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
            for w in all_ranked_raw
            if (w.get("distance") in selected_dists or w.get("time") in selected_times)
            and get_season(w.get("date", "")) not in set(excluded_seasons)
            and sim_date < parse_date(w.get("date", "")) <= _lookahead_end
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
    for _ck, (_p, _d_date, _d) in _ev_best.items():
        if _new_step_start < _d_date <= sim_date:
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
        # Compute prev bests using the current step start as cutoff
        # (i.e. everything strictly before this step's new arrivals).
        _det_prev_pace: dict = {}
        for _w in sim_wkts:
            _ck2 = workout_cat_key(_w)
            _p2 = compute_pace(_w)
            if _ck2 and _p2 and parse_date(_w.get("date", "")) <= _new_step_start:
                if _ck2 not in _det_prev_pace or _p2 < _det_prev_pace[_ck2]:
                    _det_prev_pace[_ck2] = _p2
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
# Ranked tab UI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sub-component: filter bar
# ---------------------------------------------------------------------------


def _filter_bar(state) -> None:
    """Renders the Include / Events filter controls."""
    with hd.box(
        padding=1,
        border="1px solid neutral-200",
        border_radius="medium",
        background_color="neutral-50",
    ):
        with hd.hbox(gap=2, align="center", wrap="wrap"):
            # ---- Events dropdown ----
            _n_ev_sel = sum(state.dist_enabled) + sum(state.time_enabled)
            _n_ev_tot = len(RANKED_DISTANCES) + len(RANKED_TIMES)
            _ev_lbl = "All" if _n_ev_sel == _n_ev_tot else f"{_n_ev_sel} of {_n_ev_tot}"
            hd.text("Events", font_weight="semibold", font_size="small")
            with hd.scope("events_dd"):
                with hd.dropdown() as _ev_dd:
                    _ev_btn = hd.button(
                        _ev_lbl, caret=True, size="medium", slot=_ev_dd.trigger
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
                                state.time_enabled = tuple(True for _ in RANKED_TIMES)
                            if hd.button(
                                "Clear all", size="small", variant="text"
                            ).clicked:
                                state.dist_enabled = tuple(
                                    False for _ in RANKED_DISTANCES
                                )
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
            _CONTROLS_W = "9rem"
            with hd.hbox(gap=0.5, align="center", width=_CONTROLS_W):
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
                _sb_annotations = build_sb_annotations(
                    all_ranked_raw,
                    sim_start,
                    _included_seasons,
                    selected_dists,
                    selected_times,
                )
                ds = DateSlider(
                    min_value=0,
                    max_value=max(1, total_days - 1),
                    target_value=sim_day_idx,
                    step=1,
                    start_date=sim_start.isoformat(),
                    annotations=_sb_annotations,
                )
                if ds.change_id != state.last_ds_change_id:
                    state.last_ds_change_id = ds.change_id
                    state.sim_week = int(ds.value)
                    state.sim_playing = False

            with hd.box(width=_CONTROLS_W):
                pass

    # ---- chart ----
    if chart_cfg:
        PowerCurveChart(
            config=chart_cfg,
            show_watts=show_watts,
            x_mode=state.chart_x_mode,
            height="75vh",
        )
    else:
        hd.text("No chart data available.", font_color="neutral-500")

    # ---- Settings Row 1: axis controls + power curves ----
    with hd.hbox(
        gap=1.5, align="start", justify="center", padding_top=0.75, wrap="wrap"
    ):
        # Intensity group
        with hd.box(
            gap=0.5,
            align="center",
            background_color="neutral-50",
            border_radius="large",
            padding=1,
            border="1px solid neutral-100",
        ):
            with hd.scope("chart_metric"):
                with radio_group(value=state.chart_metric, size="medium") as rg:
                    hd.radio_button("Pace")
                    hd.radio_button("Watts")
                if rg.changed:
                    state.chart_metric = rg.value

            with hd.hbox(align="center", gap=0.75):
                # hd.text("Intensity", font_color="neutral-800", font_size="small")
                with hd.scope("chart_log_y"):
                    with hd.button(
                        size="small",
                        border_radius="small",
                        variant="primary" if state.chart_log_y else "default",
                        pill=True,
                    ) as _log_y_btn:
                        with hd.hbox(gap=0.2):
                            hd.text("Log(")
                            hd.text(state.chart_metric, font_style="italic")
                            hd.text(")")

                    if _log_y_btn.clicked:
                        state.chart_log_y = not state.chart_log_y

        # Length group
        with hd.box(
            gap=0.5,
            align="center",
            background_color="neutral-50",
            border_radius="large",
            padding=1,
            border="1px solid neutral-100",
        ):
            with hd.scope("chart_x_mode"):
                with radio_group(
                    value=state.chart_x_mode.capitalize(), size="medium"
                ) as rg:
                    hd.radio_button("Distance")
                    hd.radio_button("Duration")
                if rg.changed:
                    state.chart_x_mode = rg.value.lower()

            with hd.hbox(align="center", gap=0.75):
                # hd.text("Length", font_color="neutral-800", font_size="small")
                with hd.scope("chart_log_x"):
                    with hd.button(
                        size="small",
                        border_radius="small",
                        variant="primary" if state.chart_log_x else "default",
                        pill=True,
                    ) as _log_x_btn:
                        with hd.hbox(gap=0.2):
                            hd.text("Log(")
                            hd.text(state.chart_x_mode, font_style="italic")
                            hd.text(")")

                    if _log_x_btn.clicked:
                        state.chart_log_x = not state.chart_log_x

        # Power curves
        with hd.box(
            gap=0.5,
            align="center",
            background_color="neutral-50",
            border_radius="large",
            padding=1,
            border="1px solid neutral-100",
        ):
            hd.text("Power curves", font_color="neutral-800", font_size="small")
            with hd.scope("chart_lines"):
                with radio_group(value=state.chart_lines, size="medium") as rg:
                    hd.radio_button("PBs")
                    hd.radio_button("SBs")
                    hd.radio_button("None")
                if rg.changed:
                    state.chart_lines = rg.value

        # ---- Settings Row 2: prediction dropdown + show components ----
        # Build the dynamic Paul's Law tip (depends on personalized K).
        if pauls_k_fit is not None:
            _pl_tip = (
                f"Predicts +{pauls_k_fit:.1f} s/500m for each doubling of distance "
                f"(your personalised value), applied from each anchor PB and averaged."
            )
        else:
            _pl_tip = (
                "Predicts +5.0 s/500m for each doubling of distance "
                "(population default — needs 2 or more PBs to personalise), "
                "applied from each anchor PB and averaged."
            )

        _PRED_OPTIONS = [
            ("none", "None", "Hide the prediction curve."),
            ("loglog", "Log-Log Watts Fit", _DESC_LL),
            ("pauls_law", "Paul's Law (average)", _pl_tip),
            ("critical_power", "Critical Power", _DESC_CP),
            ("rowinglevel", "RowingLevel (average)", _DESC_RL),
            ("average", "Average", _DESC_AVG),
        ]
        _pred_name = next(
            (name for val, name, _ in _PRED_OPTIONS if val == state.chart_predictor),
            state.chart_predictor,
        )

        with hd.box(
            gap=0.5,
            align="center",
            background_color="neutral-50",
            border_radius="large",
            padding=1,
            border="1px solid neutral-100",
            justify="center",
            grow=True,
        ):
            hd.text("Prediction line", font_color="neutral-800", font_size="small")

            # Custom prediction dropdown (name + description per option).
            with hd.scope("pred_dd"):
                with hd.dropdown(_pred_name, grow=True) as _pred_dd:
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
                                        max_width=40,
                                        padding=(0, 2, 0, 2),
                                    )
                                if _opt_button.clicked:
                                    state.chart_predictor = _pval
                                    _pred_dd.opened = False

            # Show components toggle (only when predictor supports it).
            if state.chart_predictor in _COMP_DESCRIPTIONS:
                with hd.scope("chart_show_components"):
                    with hd.box(gap=0.5, align="center"):
                        with hd.hbox(align="center", gap=1):
                            _sw_comp = hd.switch(
                                "Show component lines",
                                checked=state.chart_show_components,
                                size="medium",
                            )
                            if _sw_comp.changed:
                                state.chart_show_components = _sw_comp.checked

                            with hd.tooltip(_COMP_DESCRIPTIONS[state.chart_predictor]):
                                hd.icon(
                                    "question-circle",
                                    font_size="small",
                                    font_color="neutral-700",
                                )
                        # ---- Paul's Law personalised K description ----
                        if (
                            state.chart_predictor == "pauls_law"
                            and pauls_k_fit is not None
                        ):
                            with hd.box(align="center", padding_top=0.25):
                                hd.text(
                                    f"Your Paul's constant: {pauls_k_fit:.1f}s/500m per distance doubling.",
                                    font_color="neutral-900",
                                    font_size="small",
                                    max_width=25,
                                )

                                hd.text(
                                    "Population default is 5.0s/500m. Lower values suggest aerobic dominance; "
                                    "higher values suggest sprint dominance.",
                                    font_color="neutral-600",
                                    font_size="x-small",
                                    max_width=22,
                                )

            else:
                # Keep show_components False when predictor doesn't support it.
                if state.chart_show_components:
                    state.chart_show_components = False

    # ---- World-class comparison toggles ----
    _wc_compare_section(state, profile, wc_task)


# ---------------------------------------------------------------------------
# Sub-component: World-class comparison toggles
# ---------------------------------------------------------------------------


def _wc_compare_section(state, profile: dict, wc_task) -> None:
    """
    Renders the 'Compare vs World Class' and 'Relative View' toggles.
    Placed below the Settings Row 2 prediction box.
    """
    _wc_profile_ok = profile_complete(profile)

    # Derive current age-category label for display when loaded.
    _wc_label = ""
    if _wc_profile_ok:
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
    _failed = (
        state.chart_compare_wc and state.wc_fetch_done and state.wc_cp_params is None
    )

    with hd.hbox(
        gap=1.5, align="start", justify="center", padding_top=0.5, wrap="wrap"
    ):
        with hd.box(
            gap=0.5,
            align="start",
            background_color="neutral-50",
            border_radius="large",
            padding=1,
            border="1px solid neutral-100",
        ):
            # Compare toggle
            with hd.hbox(gap=1, align="center"):
                if not _wc_profile_ok:
                    hd.text(
                        "Set age, gender and weight in Profile to compare vs world class.",
                        font_color="neutral-500",
                        font_size="small",
                    )
                else:
                    with hd.scope("chart_compare_wc"):
                        _sw_wc = hd.switch(
                            "Compare vs Age/Weight World Class",
                            checked=state.chart_compare_wc,
                            size="medium",
                        )
                        if _sw_wc.changed:
                            state.chart_compare_wc = _sw_wc.checked
                            if not _sw_wc.checked:
                                state.chart_wc_relative = False

                    if _loading:
                        with hd.hbox(gap=0.5, align="center"):
                            hd.spinner()
                            hd.text(
                                "Fetching world records…",
                                font_size="small",
                                font_color="neutral-400",
                            )
                    elif state.chart_compare_wc and state.wc_fetch_done and _wc_label:
                        if _failed:
                            hd.text(
                                "Could not fit CP model to world records.",
                                font_size="small",
                                font_color="warning-600",
                            )
                        else:
                            hd.text(
                                _wc_label,
                                font_size="small",
                                font_color="success-600",
                                font_weight="semibold",
                            )

            # Relative View toggle — always visible, disabled when compare is off.
            _rel_disabled = (
                not state.chart_compare_wc or not state.wc_fetch_done or _failed
            )
            with hd.box(
                # opacity=0.4 if _rel_disabled else 1.0
            ):
                with hd.scope("chart_wc_relative"):
                    _sw_rel = hd.switch(
                        "Relative View (% of World Class)",
                        checked=state.chart_wc_relative,
                        size="medium",
                        disabled=_rel_disabled,
                    )
                    if _sw_rel.changed and not _rel_disabled:
                        state.chart_wc_relative = _sw_rel.checked


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
    _COL_PROPS = dict(
        grow=True,
        width=0,
        padding=0.5,  # border_right="1px solid neutral-200"
    )

    with hd.box(padding=(2, 0, 0, 0)):
        hd.h2("Predicted Performances")
        hd.text(
            "Pace (top) and result — total time for distance events,"
            " predicted meters for timed events."
            " Paul's Law and RowingLevel are averaged across all anchor PBs.",
            font_color="neutral-500",
            font_size="small",
            padding_bottom=1,
        )

        with hd.box(border="1px solid neutral-200", border_radius="medium"):
            # ── header ────────────────────────────────────────────────────────
            with hd.hbox(background_color=_HEADER_BG):
                with hd.box(
                    padding=1,
                    background_color=_HEADER_BG,
                    border_right="1px solid neutral-200",
                    width=8,
                ):
                    hd.text("Event", font_weight="semibold", font_size="small")
                for col_key, col_label, col_tip in _PRED_COLS:
                    with hd.scope(col_key):
                        with hd.box(**_COL_PROPS, background_color=_HEADER_BG):
                            with hd.hbox(gap=0.5, align="center"):
                                hd.text(
                                    col_label, font_weight="semibold", font_size="small"
                                )
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

                    with hd.hbox(border_top="1px solid neutral-200"):
                        with hd.box(
                            padding=1,
                            background_color=_row_bg,
                            border_right="1px solid neutral-200",
                            width=8,
                        ):
                            with hd.hbox(gap=0.5, align="center"):
                                with hd.scope(f"ev_toggle_{_row['label']}"):
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
                                with hd.box(**_COL_PROPS, background_color=_row_bg):
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
                        (
                            r["event_type"] == "dist"
                            and r["event_value"] in selected_dists
                        )
                        or (
                            r["event_type"] == "time"
                            and r["event_value"] in selected_times
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

            _ACC_BG = "neutral-100"
            with hd.hbox(border_top="2px solid neutral-300"):
                with hd.box(
                    padding=1,
                    background_color=_ACC_BG,
                    border_right="1px solid neutral-200",
                    width=8,
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

                with hd.box(**_COL_PROPS, background_color=_ACC_BG):
                    hd.text("—", font_size="small", font_color="neutral-600")

                for _ck, _cl, _ct in _PRED_COLS[1:]:
                    with hd.scope(f"acc_{_ck}"):
                        _av = _acc_vals.get(_ck, {"rmse": None, "r2": None, "n": 0})
                        with hd.box(**_COL_PROPS, background_color=_ACC_BG):
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
                                hd.text(
                                    "—", font_size="small", font_color="neutral-600"
                                )


# ---------------------------------------------------------------------------
# Pure data helpers  (no HyperDiv, no side-effects)
# ---------------------------------------------------------------------------


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
    all_ranked_raw: list,
    sim_date: date,
    excluded_seasons,
    selected_dists: set,
    selected_times: set,
) -> tuple:
    """
    Build everything that depends on the current sim position:

      sim_wkts       — filtered workouts visible at sim_date
      excluded_wkts  — PBs of user-disabled events (faint background dots)
      lb, lb_anchor  — selected-event lifetime bests
      lb_all, lb_all_anchor — all-event lifetime bests
      pauls_k_fit    — personalised Paul's constant (None if < 2 PBs)
      pauls_k        — pauls_k_fit or population default 5.0
    """
    sim_wkts = sim_workouts_at(
        all_ranked_raw,
        sim_date,
        selected_dists,
        selected_times,
        set(excluded_seasons),
        state.best_filter,
    )

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
        excl_src = [
            w
            for w in all_ranked_raw
            if workout_cat_key(w) in excluded_cats
            and parse_date(w.get("date", "")) <= sim_date
            and get_season(w.get("date", "")) not in set(excluded_seasons)
        ]
        excluded_wkts = apply_best_only(excl_src)

    lb, lb_anchor = compute_lifetime_bests(sim_wkts)
    lb_all, lb_all_anchor = compute_lifetime_bests(
        [
            w
            for w in all_ranked_raw
            if parse_date(w.get("date", "")) <= sim_date
            and get_season(w.get("date", "")) not in set(excluded_seasons)
        ]
    )
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
    all_ranked_raw: list,
    sim_date: date,
    excluded_seasons,
    selected_dists: set,
    selected_times: set,
):
    """
    Compute (or retrieve from cache) the Critical Power fit for the current sim
    position.  Mutates state.cp_fit_key / state.cp_fit_result; returns the params
    dict (or None if the fit failed / insufficient data).
    """
    cp_src = [
        w
        for w in all_ranked_raw
        if (w.get("distance") in selected_dists or w.get("time") in selected_times)
        and get_season(w.get("date", "")) not in set(excluded_seasons)
        and parse_date(w.get("date", "")) <= sim_date
    ]
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


def _fetch_wc_cp(gender_api: str, age: int, weight_kg: float) -> dict | None:
    """
    Blocking function — intended to run inside hd.task().
    Fetches Concept2 world records for the given gender/age/weight,
    converts them to CP input, and fits the CP model.
    Returns the CP params dict, or None if fetch/fit failed.
    """
    records = get_age_group_records(gender_api, age, weight_kg)
    if not records:
        return None
    cp_input = records_to_cp_input(records)
    if not cp_input:
        return None
    return fit_critical_power(cp_input)


def _load_wc_cp(state, profile: dict) -> tuple:
    """
    HyperDiv component: manage the background task that fetches world-class
    CP params.  Caches result in state.wc_cp_params.

    Returns (task_or_None, wc_cp_params_or_None).
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
        state.wc_cp_params = None

    wc_task = None
    with hd.scope(f"wc_task_{fetch_key}"):
        wc_task = hd.task()
        if not wc_task.running and not wc_task.done:
            wc_task.run(_fetch_wc_cp, gender_api, age, weight_kg)
        if wc_task.done and not state.wc_fetch_done:
            state.wc_fetch_done = True
            state.wc_cp_params = wc_task.result  # may be None if fit failed

    return wc_task, state.wc_cp_params


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
        best_filter="All",
        chart_log_x=True,
        chart_log_y=False,
        chart_show_lifetime_line=True,
        chart_metric="Pace",
        chart_x_mode="distance",
        chart_predictor="loglog",
        chart_show_components=False,
        chart_season_lines=(),
        chart_lines="PBs",
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
        chart_wc_relative=False,
        wc_fetch_key="",
        wc_fetch_done=False,
        wc_cp_params=None,
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
    all_ranked, all_ranked_raw, all_seasons = _build_ranked_workouts(
        sync_result, machine
    )
    _filter_bar(state)
    if sync_result is None:
        return

    # ── Filters ───────────────────────────────────────────────────────────────
    selected_dists = {
        dist for i, (dist, _) in enumerate(RANKED_DISTANCES) if state.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(RANKED_TIMES) if state.time_enabled[i]
    }
    display = _apply_display_filter(
        state, all_ranked, selected_dists, selected_times, excluded_seasons
    )

    # ── Simulation timeline ───────────────────────────────────────────────────
    (
        sim_start,
        total_days,
        sim_day_idx,
        sim_date,
        at_today,
        included_seasons,
    ) = _compute_sim_timeline(excluded_seasons, all_seasons, state.sim_week)
    show_watts = state.chart_metric == "Watts"

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
        all_ranked_raw,
        sim_date,
        excluded_seasons,
        selected_dists,
        selected_times,
    )
    cp_params = _update_cp_fit(
        state,
        all_ranked_raw,
        sim_date,
        excluded_seasons,
        selected_dists,
        selected_times,
    )
    rl_task, rl_predictions = _fetch_rowinglevel(state, profile, display, at_today)
    wc_task, wc_cp_params = (
        _load_wc_cp(state, profile) if state.chart_compare_wc else (None, None)
    )
    _run_animation_tick(state, total_days)

    # ── Overlays + bounds ─────────────────────────────────────────────────────
    sim_overlays, overlay_labels = None, []
    if not at_today:
        sim_overlays, overlay_labels = _compute_lookahead_overlays(
            sim_wkts,
            all_ranked_raw,
            sim_date,
            sim_day_idx,
            state,
            total_days,
            selected_dists,
            selected_times,
            included_seasons,
            show_watts,
            excluded_seasons=excluded_seasons,
        )
    x_bounds, y_bounds = _compute_axis_bounds(
        all_ranked_raw,
        excluded_seasons,
        show_watts,
        state.chart_x_mode == "duration",
        state.chart_log_x,
    )

    # ── Render ────────────────────────────────────────────────────────────────
    with hd.box(gap=1, align="center"):
        with hd.h1():
            _date_label = sim_date.strftime("%b %d, %Y")
            _best_long = {
                "All": "All Great Efforts",
                "PBs": "Personal Bests",
                "SBs": "Season Bests",
            }
            _cur_best_lbl = _best_long.get(state.best_filter, state.best_filter)
            with hd.hbox(
                gap=0.6, align="center", padding_bottom=0, justify="center", wrap="wrap"
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
                            gap=0.1, background_color="neutral-0", min_width=20
                        ):
                            for val, lbl in _best_long.items():
                                with hd.scope(f"bf_{val}"):
                                    _bf_item = hd.button(
                                        lbl,
                                        size="small",
                                        variant="primary"
                                        if state.best_filter == val
                                        else "text",
                                        width="100%",
                                        border_radius="small",
                                        font_size="medium",
                                        font_color="neutral-0"
                                        if state.best_filter == val
                                        else "neutral-800",
                                        label_style=hd.style(
                                            padding_top=0.5, padding_bottom=0.5
                                        ),
                                        hover_background_color="neutral-100",
                                    )
                                    if _bf_item.clicked:
                                        state.best_filter = val
                                        _bf_dd.opened = False
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
        chart_cfg = build_chart_config(
            sim_wkts,
            log_x=state.chart_log_x,
            log_y=state.chart_log_y,
            show_lifetime_line=state.chart_lines == "PBs",
            show_watts=show_watts,
            is_dark=is_dark,
            predictor=predictor,
            rl_predictions=rl_predictions,
            critical_power_params=cp_params,
            season_lines=set(all_seasons) if state.chart_lines == "SBs" else set(),
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
            x_mode=state.chart_x_mode,
            wc_cp_params=wc_cp_params,
            wc_relative=state.chart_wc_relative
            if (state.chart_compare_wc and wc_cp_params is not None)
            else False,
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
        _prediction_table(
            state,
            pred_rows,
            selected_dists,
            selected_times,
            rl_available=rl_available,
            pauls_k=pauls_k,
        )

        count = len(display)
        hd.text(
            f"{count} workout{'s' if count != 1 else ''} matched",
            font_color="neutral-500",
            font_size="small",
            padding_top=1,
        )
        if not display:
            hd.text("No workouts match the selected filters.", font_color="neutral-500")
            return
        result_table(display, paginate=False)
