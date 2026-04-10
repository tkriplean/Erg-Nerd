"""
Ranked Events tab — UI and orchestration for the ranked-events view.

Exported:
  performance_page()   — top-level HyperDiv component; call from app.py

Helper logic is split across:
  components/ranked_formatters.py    — formatting helpers + result_table
  services/ranked_filters.py         — quality filters + season helpers
  services/ranked_predictions.py     — multi-model prediction computation
  components/performance_chart_builder.py — chart config builder

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT (inside performance_page)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Filter bar:
    Include [All|PBs|SBs]  |  Events [dropdown]  |  Season [dropdown]

  Chart box:
    Header: "Qualifying Performances" / date label
    RowingLevel warning/spinner (only when predictor == "rowinglevel")
    Transport bar: ▶/⏸  speed-cycle-button  ──── DateSlider ────
    PerformanceChart (85vh)
    Log Y / Log X switches
    Centered settings row:
      [Pace|Watts]  |  Power curves: [PBs|SBs|None]
      |  Prediction: <select>  |  Log Y  |  Log X
      # commented-out MP4 export button (see services/mp4_export.py)

  Workout count / result_table()

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATE VARIABLES  (declared at the top of performance_page())
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  dist_enabled       tuple[bool]   one flag per RANKED_DISTANCES entry (index-aligned)
  time_enabled       tuple[bool]   one flag per RANKED_TIMES entry (index-aligned)
  excluded_seasons   tuple[str]    seasons hidden from the view (sorted)
  best_filter        str           "All" | "PBs" | "SBs" — row filter for display/table
  chart_metric       str           "Pace" | "Watts"
  chart_predictor    str           "none" | "pauls_law" | "loglog" | "rowinglevel"
                                   (lowercase/underscore; match hd.option value= kwarg)
  chart_lines        str           "PBs" | "SBs" | "None" — which best-curves to draw
  chart_log_x        bool          log scale on x-axis
  chart_log_y        bool          log scale on y-axis
  sim_week           int           day offset from sim_start; _SIM_TODAY (999999) = end
  sim_speed          str           one of _SPEED_OPTIONS: "0.5x"|"1x"|"4x"|"16x"
  sim_playing        bool          whether the animation ticker is running
  sim_tick_id        int           monotonically increasing; increment to advance animation

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

  Playback speed UI: cycling variant="text" button (no border, no caret).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SEASONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Format: "YYYY-YY"  e.g. "2024-25", spanning May 1 → Apr 30.
  all_seasons: sorted newest-first (seasons_from uses sorted(..., reverse=True)).
  Use services/rowing_utils.py:get_season() to compute from an ISO date string.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART / PREDICTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Chart rendered by PerformanceChart (components/performance_chart_plugin.py), an hd.Plugin
  wrapping Chart.js with custom JS in chart_assets/performance_chart_plugin.js.

  Custom JS features:
    - canvasLabelsPlugin: stable overlay labels stored in a Map keyed by
      "${x}|${line_label}" — positions never move once assigned.
    - isPrediction: true  dataset property — controls tooltip and point styling.
    - Prediction line style: amber, borderDash: [5, 4], small hoverable points.
    - Gridlines: x-axis at every ranked distance; y-axis every 5s/pace or 50W.

  Prediction colors:
    dark:   rgba(220, 160, 55, 0.80)
    light:  rgba(185, 120, 20, 0.80)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HYPERDIV QUIRKS APPLIED HERE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  radio_group subclass: hd.radio_group doesn't expose size. A local subclass
  adds it (see below). Even then, group size doesn't shrink button padding;
  use label_style on each radio_button:
    hd.radio_button("X", label_style=hd.style(padding=(0, "0.5rem", 0, "0.5rem")))

  hd.option() value= kwarg: hd.option() replaces spaces with underscores
  internally. Always pass value= explicitly:
    hd.option("Paul's Law", value="pauls_law")
  Then compare state.chart_predictor against "pauls_law" etc. everywhere.
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
from components.performance_chart_plugin import PerformanceChart
from components.date_slider_plugin import DateSlider
from components.ranked_formatters import result_table
from services.ranked_filters import (
    is_ranked_noninterval,
    seasons_from,
    apply_quality_filters,
    sim_workouts_at,
)
from components.performance_chart_builder import (
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
            and get_season(w.get("date", "")) not in set(state.excluded_seasons)
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


def _filter_bar(state, all_seasons: list) -> None:
    """Renders the Include / Events / Seasons filter controls."""
    with hd.box(
        padding=1,
        border="1px solid neutral-200",
        border_radius="medium",
        background_color="neutral-50",
    ):
        with hd.hbox(gap=2, align="center", wrap="wrap"):
            # ---- Include radio ----
            hd.text("Include", font_weight="semibold", font_size="small")
            with hd.scope("best_filter"):
                with radio_group(value=state.best_filter, size="medium") as rg:
                    hd.radio_button("All")
                    hd.radio_button("PBs")
                    hd.radio_button("SBs")
                if rg.changed:
                    state.best_filter = rg.value

            hd.text("|", font_color="neutral-300")

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
                        with hd.hbox(gap=1, padding_bottom=0.5):
                            with hd.tooltip("Select all"):
                                if hd.icon_button(
                                    "check2-all", font_size="small"
                                ).clicked:
                                    state.dist_enabled = tuple(
                                        True for _ in RANKED_DISTANCES
                                    )
                                    state.time_enabled = tuple(
                                        True for _ in RANKED_TIMES
                                    )
                            with hd.tooltip("Clear all"):
                                if hd.icon_button(
                                    "dash-square", font_size="small"
                                ).clicked:
                                    state.dist_enabled = tuple(
                                        False for _ in RANKED_DISTANCES
                                    )
                                    state.time_enabled = tuple(
                                        False for _ in RANKED_TIMES
                                    )
                        with hd.scope(str(state.dist_enabled)):
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

            hd.text("|", font_color="neutral-300")

            # ---- Season dropdown ----
            if all_seasons:
                _excl_valid = set(state.excluded_seasons) & set(all_seasons)
                _n_seas_sel = len(all_seasons) - len(_excl_valid)
                _seas_lbl = (
                    "All" if not _excl_valid else f"{_n_seas_sel} of {len(all_seasons)}"
                )
                hd.text("Season", font_weight="semibold", font_size="small")
                with hd.scope("season_dd"):
                    with hd.dropdown() as _se_dd:
                        _se_btn = hd.button(
                            _seas_lbl, caret=True, size="medium", slot=_se_dd.trigger
                        )
                        if _se_btn.clicked:
                            _se_dd.opened = not _se_dd.opened
                        with hd.box(padding=1, gap=0.5, background_color="neutral-50"):
                            with hd.hbox(gap=1, padding_bottom=0.5):
                                with hd.tooltip("Select all"):
                                    if hd.icon_button(
                                        "check2-all", font_size="small"
                                    ).clicked:
                                        state.excluded_seasons = ()
                                with hd.tooltip("Clear all"):
                                    if hd.icon_button(
                                        "dash-square", font_size="small"
                                    ).clicked:
                                        state.excluded_seasons = tuple(all_seasons)
                            _convenience = [
                                (1, "Last season"),
                                (2, "Last 2 seasons"),
                                (5, "Last 5 seasons"),
                                (10, "Last 10 seasons"),
                            ]
                            _shown = [
                                (n, lbl)
                                for n, lbl in _convenience
                                if len(all_seasons) >= n
                            ]
                            if _shown:
                                with hd.hbox(gap=0.5, padding_bottom=0.5, wrap="wrap"):
                                    for _cn, _clbl in _shown:
                                        with hd.scope(f"conv_{_cn}"):
                                            if hd.button(
                                                _clbl, size="medium", variant="text"
                                            ).clicked:
                                                state.excluded_seasons = tuple(
                                                    sorted(all_seasons[_cn:])
                                                )
                            with hd.scope(str(state.excluded_seasons)):
                                for season in all_seasons:
                                    with hd.scope(f"season_{season}"):
                                        _is_sel = season not in state.excluded_seasons
                                        cb = hd.checkbox(season, checked=_is_sel)
                                        if cb.changed:
                                            _excl = set(state.excluded_seasons)
                                            if cb.checked:
                                                _excl.discard(season)
                                            else:
                                                _excl.add(season)
                                            state.excluded_seasons = tuple(
                                                sorted(_excl)
                                            )
                                        if cb.checked != _is_sel:
                                            cb.checked = _is_sel


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
    pauls_k_fit: float | None = None,
) -> None:
    """
    Renders the performance chart box: header, RL status, transport bar,
    PerformanceChart, settings row, and components toggle.
    """
    is_dark = hd.theme().is_dark

    with shadowed_box(
        gap=0,
        padding=2,
        box_shadow="0 1px 2px rgba(255,255,255,.1)"
        if is_dark
        else "0 1px 2px rgba(0,0,0,.5)",
        background_color="#252525" if is_dark else "#f9f9f9",
        border_radius="medium",
    ):
        # ---- header row ----
        _date_label = sim_date.strftime("%b %d, %Y")
        with hd.hbox(gap=1, align="center", padding_bottom=0, justify="center"):
            hd.text(
                "Qualifying Performances", font_weight="normal", font_size="2x-large"
            )
            hd.text("through ", font_size="medium")
            hd.text(f"{_date_label}", font_size="2x-large", font_weight="normal")

        # ---- RowingLevel status indicators (only shown when RL predictor selected) ----
        if _at_today and rl_task is not None and state.chart_predictor == "rowinglevel":
            if not profile_complete(profile):
                hd.alert(
                    "Please complete your profile (Gender, Age, and Bodyweight) "
                    "in the Profile tab before using RowingLevel predictions.",
                    variant="warning",
                    opened=True,
                )

        # ---- transport bar ----
        # Three-column layout: [controls (fixed)] | [slider (grows)] | [spacer (= controls width)]
        with hd.box(align="center"):
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
                    if hd.button(
                        _play_label, size="medium", variant=_play_variant
                    ).clicked:
                        if state.sim_playing:
                            state.sim_playing = False
                        else:
                            if _at_today:
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
            PerformanceChart(config=chart_cfg, show_watts=show_watts, height="75vh")
        else:
            hd.text("No chart data available.", font_color="neutral-500")

        # ---- settings row ----
        with hd.hbox(
            gap=1, align="center", justify="center", padding_top=0.5, wrap="wrap"
        ):
            with hd.scope("chart_metric"):
                with radio_group(value=state.chart_metric, size="medium") as rg:
                    hd.radio_button("Pace")
                    hd.radio_button("Watts")
                if rg.changed:
                    state.chart_metric = rg.value

            hd.text("|", font_color="neutral-300")

            hd.text("Power curves:", font_color="neutral-600", font_size="small")
            with hd.scope("chart_lines"):
                with radio_group(value=state.chart_lines, size="medium") as rg:
                    hd.radio_button("PBs")
                    hd.radio_button("SBs")
                    hd.radio_button("None")
                if rg.changed:
                    state.chart_lines = rg.value

            hd.text("|", font_color="neutral-300")

            hd.text("Prediction line:", font_color="neutral-600", font_size="small")
            with hd.scope("chart_predictor"):
                sel = hd.select(value=state.chart_predictor, size="medium")
                with sel:
                    hd.option("None", value="none")
                    hd.option("Paul's Law", value="pauls_law")
                    hd.option("Log-Log Watts Fit", value="loglog")
                    hd.option("Critical Power", value="critical_power")
                    hd.option("RowingLevel", value="rowinglevel")
                if sel.changed:
                    state.chart_predictor = sel.value

            hd.text("|", font_color="neutral-300")

            with hd.scope("chart_log_y"):
                sw = hd.switch("Log Y", checked=state.chart_log_y, size="medium")
                if sw.changed:
                    state.chart_log_y = sw.checked
            with hd.scope("chart_log_x"):
                sw = hd.switch("Log X", checked=state.chart_log_x, size="medium")
                if sw.changed:
                    state.chart_log_x = sw.checked

            # MP4 export is parked in services/experimental/mp4_export.py for future re-enablement.

        # ---- "Show components" row (only when predictor supports it) ----
        if state.chart_predictor in ("pauls_law", "rowinglevel", "critical_power"):
            with hd.hbox(gap=1, align="center", justify="center", padding_top=1):
                with hd.scope("chart_show_components"):
                    _sw_comp = hd.switch(
                        "Show component lines",
                        checked=state.chart_show_components,
                        size="medium",
                    )
                    if _sw_comp.changed:
                        state.chart_show_components = _sw_comp.checked

                if pauls_k_fit is not None:
                    hd.text(
                        f"Your Paul's Value: +{pauls_k_fit:.1f}s per doubling",
                        font_color="neutral-400",
                        font_size="small",
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
        (
            "cp",
            "Critical Power",
            "Two-component power-duration model (veloclinic). "
            "Requires ≥ 5 PBs spanning a 10:1 duration ratio. Method from rowsandall.com.",
        ),
        (
            "loglog",
            "Log-Log Watts Fit",
            "Fits a power law (log watts vs log distance) across all scoped PBs. "
            "Similar to the Free Spirits Pace Predictor (freespiritsrowing.com) but uses all PBs, not just two.",
        ),
        ("pl", "Avg. Paul's Law", _pl_tip),
    ]
    if rl_available:
        _PRED_COLS.append(
            (
                "rl",
                "Avg. RowingLevel",
                "Predictions from rowinglevel.com based on your profile (gender, age, bodyweight). "
                "Distance-weighted average across all anchor PBs. Distance events only.",
            )
        )
    _PRED_COLS.append(
        ("avg", "Average", "Mean of all available predictions for this event.")
    )

    _HEADER_BG = "neutral-100"
    _COL_PROPS = dict(
        grow=True, width=0, padding=0.5, border_right="1px solid neutral-200"
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
                                        font_color="neutral-300",
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
                                                font_size="medium",
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
                                font_color="neutral-300",
                            )

                with hd.box(**_COL_PROPS, background_color=_ACC_BG):
                    hd.text("—", font_size="small", font_color="neutral-300")

                for _ck, _cl, _ct in _PRED_COLS[1:]:
                    with hd.scope(f"acc_{_ck}"):
                        _av = _acc_vals.get(_ck, {"rmse": None, "r2": None, "n": 0})
                        with hd.box(**_COL_PROPS, background_color=_ACC_BG):
                            if _av["rmse"] is not None:
                                hd.text(
                                    f"{_av['rmse']:.2f}s",
                                    font_size="small",
                                    font_weight="semibold",
                                    font_color="neutral-600",
                                )
                                if _av["r2"] is not None:
                                    hd.text(
                                        f"R²={_av['r2']:.3f}",
                                        font_size="x-small",
                                        font_color="neutral-400",
                                    )
                                hd.text(
                                    f"n={_av['n']}",
                                    font_size="x-small",
                                    font_color="neutral-400",
                                )
                            else:
                                hd.text(
                                    "—", font_size="small", font_color="neutral-300"
                                )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def performance_page(client, user_id: str) -> None:
    """
    Top-level entry point for the Performance tab.
    Fetches data, computes all derived state, then calls sub-components.
    """
    state = hd.state(
        dist_enabled=tuple(True for _ in RANKED_DISTANCES),
        time_enabled=tuple(True for _ in RANKED_TIMES),
        excluded_seasons=(),
        best_filter="SBs",  # "All" | "PBs" | "SBs"
        chart_log_x=True,
        chart_log_y=False,
        chart_show_lifetime_line=True,
        chart_metric="Pace",  # "Pace" | "Watts"
        chart_predictor="loglog",  # "none" | "pauls_law" | "loglog" | "rowinglevel" | "critical_power"
        chart_show_components=False,
        chart_season_lines=(),
        chart_lines="PBs",  # "PBs" | "SBs" | "None"
        show_chart_settings=False,
        # ---- timeline / simulation ----
        sim_playing=False,
        sim_week=_SIM_TODAY,
        sim_speed="1x",
        sim_tick_id=0,
        sim_last_pb_label="",
        sim_pb_set_at_day=-9999,
        sim_pb_stored_labels_json="[]",
        last_ds_change_id=0,
        # ---- critical power model fit cache ----
        cp_fit_key="",
        cp_fit_result=None,
    )

    is_dark = hd.theme().is_dark

    # ---- profile from localStorage ----
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

    # ---- fetch (shared sync component) ----
    sync_result = concept2_sync(client)

    # ---- base set + quality filters (empty while loading) ----
    if sync_result is not None:
        _workouts_dict, sorted_workouts = sync_result
        all_ranked = [r for r in sorted_workouts if is_ranked_noninterval(r)]
        all_ranked = apply_quality_filters(
            all_ranked,
            selected_dists=set(),
            selected_times=set(),
            excluded_seasons=set(),
        )
        all_ranked_raw = list(all_ranked)
        all_seasons = seasons_from(all_ranked)
    else:
        all_ranked = []
        all_ranked_raw = []
        all_seasons = []

    # ---- filter bar (always visible, even while loading) ----
    _filter_bar(state, all_seasons)

    # ---- loading / error gate ----
    if sync_result is None:
        return

    # ---- apply event / season / best filters ----
    selected_dists = {
        dist for i, (dist, _) in enumerate(RANKED_DISTANCES) if state.dist_enabled[i]
    }
    selected_times = {
        tenths for i, (tenths, _) in enumerate(RANKED_TIMES) if state.time_enabled[i]
    }
    filtered = [
        r
        for r in all_ranked
        if (r.get("distance") in selected_dists or r.get("time") in selected_times)
        and get_season(r.get("date", "")) not in state.excluded_seasons
    ]
    if state.best_filter == "PBs":
        display = apply_best_only(filtered)
    elif state.best_filter == "SBs":
        display = apply_season_best_only(filtered)
    else:
        display = filtered
    chart_workouts = display

    # ---- simulation timeline ----
    _included_seasons = [s for s in all_seasons if s not in set(state.excluded_seasons)]
    if _included_seasons:
        _ey = int(min(_included_seasons)[:4])
        sim_start = date(_ey, 5, 1)
        _max_season_end_year = int(max(_included_seasons)[:4]) + 1
        sim_end = min(date.today(), date(_max_season_end_year, 4, 30))
    else:
        sim_start = date.today() - timedelta(days=365)
        sim_end = date.today()
    total_days = max(1, (sim_end - sim_start).days + 1)
    sim_day_idx = max(0, min(state.sim_week, total_days - 1))
    sim_date = sim_start + timedelta(days=sim_day_idx)
    _at_today = sim_day_idx >= total_days - 1

    # ---- stable axis bounds ----
    show_watts = state.chart_metric == "Watts"
    _show_lb_line = state.chart_lines == "PBs"
    _season_lines_set = set(all_seasons) if state.chart_lines == "SBs" else set()
    _bounds_src = [
        w
        for w in all_ranked_raw
        if (w.get("distance") in selected_dists or w.get("time") in selected_times)
        and get_season(w.get("date", "")) not in set(state.excluded_seasons)
    ]
    _bounds_bests = apply_best_only(_bounds_src)
    _sim_x_bounds = None
    _sim_y_bounds = None
    if _bounds_bests:
        _bp = [p for w in _bounds_bests if (p := compute_pace(w)) and 60 < p < 400]
        _bd = [w.get("distance") for w in _bounds_bests if w.get("distance")]
        if _bp and _bd:
            _xr, _xR = min(_bd), max(_bd)
            _sim_x_bounds = (
                (_xr / 1.45, _xR * 1.45)
                if state.chart_log_x
                else (
                    max(0, _xr - max((_xR - _xr) * 0.1, _xr * 0.1)),
                    _xR + max((_xR - _xr) * 0.1, _xr * 0.1),
                )
            )
            _by = [compute_watts(p) if show_watts else p for p in _bp]
            _yr, _yR = min(_by), max(_by)
            _ypad = max((_yR - _yr) * 0.15, 5 if not show_watts else 2)
            _sim_y_bounds = (_yr - _ypad, _yR + _ypad)

    # ---- sim workouts + lifetime bests ----
    sim_wkts = sim_workouts_at(
        all_ranked_raw,
        sim_date,
        selected_dists,
        selected_times,
        set(state.excluded_seasons),
        state.best_filter,
    )
    _lb, _lb_anchor = compute_lifetime_bests(sim_wkts)
    _lb_all, _lb_all_anchor = compute_lifetime_bests(
        [
            w
            for w in all_ranked_raw
            if parse_date(w.get("date", "")) <= sim_date
            and get_season(w.get("date", "")) not in set(state.excluded_seasons)
        ]
    )
    _pauls_k_fit = compute_pauls_constant(_lb, _lb_anchor)
    _pauls_k = _pauls_k_fit if _pauls_k_fit is not None else 5.0

    # ---- critical power model fit (cached in state) ----
    _cp_src = [
        w
        for w in all_ranked_raw
        if (w.get("distance") in selected_dists or w.get("time") in selected_times)
        and get_season(w.get("date", "")) not in set(state.excluded_seasons)
        and parse_date(w.get("date", "")) <= sim_date
    ]
    _cp_pb_list = []
    for _w in apply_best_only(_cp_src):
        _dur = compute_duration_s(_w)
        _pac = compute_pace(_w)
        if _dur and _pac:
            _cp_pb_list.append({"duration_s": _dur, "watts": compute_watts(_pac)})
    _cp_fit_key = str(
        sorted((round(p["duration_s"], 1), round(p["watts"], 1)) for p in _cp_pb_list)
    )
    if _cp_fit_key != state.cp_fit_key:
        state.cp_fit_key = _cp_fit_key
        state.cp_fit_result = fit_critical_power(_cp_pb_list)
    _cp_params = state.cp_fit_result

    # ---- RowingLevel scrape (runs at performance_page scope, result passed down) ----
    rl_task = None
    rl_predictions: dict = {}
    if _at_today and profile_complete(profile):
        weight_kg = (
            profile["weight"] * 0.453592
            if profile["weight_unit"] == "lbs"
            else profile["weight"]
        )
        _lbest: dict = {}
        _lbest_anchor: dict = {}
        _lbest_dates: dict = {}
        for w in chart_workouts:
            p = compute_pace(w)
            c = workout_cat_key(w)
            d = w.get("distance")
            if p is None or c is None or not d:
                continue
            if c not in _lbest or p < _lbest[c]:
                _lbest[c] = p
                _lbest_anchor[c] = d
                _lbest_dates[c] = w.get("date", "")
        _lbest_hash = hashlib.md5(
            json.dumps(
                sorted((str(k), round(v, 2)) for k, v in _lbest.items())
            ).encode()
        ).hexdigest()[:8]
        _scope_key = f"rl_{_profile_hash(profile)}_{_lbest_hash}"
        with hd.scope(_scope_key):
            rl_task = hd.task()

            def _do_scrape(gender, current_age, wkg, lbest, lbest_anchor, lbest_dates):
                return fetch_all_pb_predictions(
                    [],
                    lbest,
                    lbest_anchor,
                    gender,
                    current_age,
                    wkg,
                    lbest_dates=lbest_dates,
                )

            rl_task.run(
                _do_scrape,
                profile["gender"],
                age_from_dob(profile.get("dob", "")),
                weight_kg,
                _lbest,
                _lbest_anchor,
                _lbest_dates,
            )
        if rl_task.done and rl_task.result:
            rl_predictions = rl_task.result

    # ---- animation tick (advances sim_week while playing) ----
    # Use sim_tick_id as scope key so backward scrubs never revisit done tasks.
    if state.sim_playing:
        with hd.scope(f"sim_tick_{state.sim_tick_id}"):
            _tick = hd.task()
            if not _tick.running and not _tick.done:
                _tick.run(time.sleep, _BASE_TICK_SECS)
            if _tick.done:
                _step = _SPEED_DAYS.get(state.sim_speed, 7)
                _nd = state.sim_week + _step
                if _nd >= total_days:
                    state.sim_week = _SIM_TODAY
                    state.sim_playing = False
                else:
                    state.sim_week = _nd
                    state.sim_tick_id += 1

    # ---- lookahead overlays ----
    _sim_overlays = None
    _overlay_labels = []
    if not _at_today:
        _sim_overlays, _overlay_labels = _compute_lookahead_overlays(
            sim_wkts,
            all_ranked_raw,
            sim_date,
            sim_day_idx,
            state,
            total_days,
            selected_dists,
            selected_times,
            _included_seasons,
            show_watts,
        )

    # ---- chart config ----
    _predictor = (
        state.chart_predictor
        if _at_today or state.chart_predictor != "rowinglevel"
        else "none"
    )
    chart_cfg = build_chart_config(
        sim_wkts,
        log_x=state.chart_log_x,
        log_y=state.chart_log_y,
        show_lifetime_line=_show_lb_line,
        show_watts=show_watts,
        is_dark=is_dark,
        predictor=_predictor,
        rl_predictions=rl_predictions,
        critical_power_params=_cp_params,
        season_lines=_season_lines_set,
        all_seasons=all_seasons,
        x_bounds=_sim_x_bounds,
        y_bounds=_sim_y_bounds,
        sim_overlays=_sim_overlays,
        overlay_labels=_overlay_labels,
        show_components=state.chart_show_components,
        lifetime_best=_lb,
        lifetime_best_anchor=_lb_anchor,
        pauls_k=_pauls_k,
    )

    # ---- render chart section ----
    _chart_section(
        state,
        chart_cfg=chart_cfg,
        rl_task=rl_task,
        rl_predictions=rl_predictions,
        profile=profile,
        show_watts=show_watts,
        sim_date=sim_date,
        _at_today=_at_today,
        sim_day_idx=sim_day_idx,
        total_days=total_days,
        sim_start=sim_start,
        all_ranked_raw=all_ranked_raw,
        selected_dists=selected_dists,
        selected_times=selected_times,
        _included_seasons=_included_seasons,
        all_seasons=all_seasons,
        pauls_k_fit=_pauls_k_fit,
    )

    # ---- RL profile notice (shown between chart and table when profile incomplete) ----
    _rl_available = profile_complete(profile)
    if not _rl_available:
        _rl_profile_notice()

    # ---- render prediction table ----
    _pred_rows = build_prediction_table_data(
        lifetime_best=_lb,
        lifetime_best_anchor=_lb_anchor,
        all_lifetime_best=_lb_all,
        all_lifetime_best_anchor=_lb_all_anchor,
        critical_power_params=_cp_params,
        rl_predictions=rl_predictions if rl_predictions else None,
        pauls_k=_pauls_k,
    )
    _prediction_table(
        state,
        _pred_rows,
        selected_dists,
        selected_times,
        rl_available=_rl_available,
        pauls_k=_pauls_k,
    )

    # ---- result count + table ----
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
