"""
Race Page — Regatta-style race animation for a single ranked event.

Exported:
    race_page(client, user_id, excluded_seasons=(), machine="All")
        Top-level HyperDiv component; call from app.py.
        excluded_seasons / machine come from the global filter in app.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Race title:   "A Race Between [Your Season Bests ▾] at [2k ▾]!"  (inline dropdowns)
                Both dropdowns are interactive — clicking changes event / filter.
                include_filter options: All Great Efforts | Season Bests
                Default include_filter: All Great Efforts
  Loading bar:  "Fetching stroke data… N / M"  (while fetching)
  Race canvas:  RaceChart plugin (auto-sized: 26px header + 44px × N lanes)
  Sort toggle:  Sort lanes by [Date | Result]  (below race canvas)
  Results table (all qualifying workouts; include_filter ignored)

Season and machine filtering are applied globally via app.py params.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATE VARIABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  event_type      str    "dist" | "time"
  event_value     int    meters or tenths-of-sec
  include_filter  str    "All" | "SBs" | "top"
  sort_mode       str    "date" | "result"
  show_wr_boat    bool   whether the WR ghost boat is enabled
  wr_records      dict   cached {(etype,evalue): result} from concept2_records
  wr_records_key  str    "gender|age|weight_kg" — invalidation key for wr_records
  strokes_cache_loaded  bool
  strokes_by_id         dict  {str(id): [{t,d}]}
  fetch_queue           tuple[int]
  fetch_total / fetch_done  int
  last_batch_key        str
"""

from __future__ import annotations

import json

import hyperdiv as hd

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    get_season,
    apply_best_only,
    compute_pace,
    compute_watts,
    age_from_dob,
)
from services.formatters import format_time, fmt_split
from services.stroke_utils import build_races_data, fetch_one_stroke, build_wr_boat
from services.concept2_records import get_age_group_records
from components.profile_page import get_profile
from services.rowing_utils import (
    apply_quality_filters,
    is_rankable_noninterval,
    profile_complete,
)
from services.local_storage_compression import (
    compress_strokes_cache,
    decompress_strokes_cache,
)
from components.concept2_sync import concept2_sync
from components.race_chart_plugin import RaceChart
from components.hyperdiv_extensions import radio_group
from components.workout_table import (
    WorkoutTable,
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

_STROKES_LS_KEY = "strokes_cache"
_DEFAULT_EVENT_TYPE = "dist"
_DEFAULT_EVENT_VALUE = 2000


# ── Event formatting ──────────────────────────────────────────────────────────


def _fmt_event(etype: str, evalue: int) -> str:
    """Return a compact display string for a ranked event (e.g. '2k', '30 min')."""
    if etype == "dist":
        m = evalue
        if m >= 1000:
            return f"{m // 1000}k" if m % 1000 == 0 else f"{m / 1000:.3g}k"
        return f"{m}m"
    else:
        mins = round((evalue / 10) / 60)
        return f"{mins} min"


def _fmt_event_long(etype: str, evalue: int) -> str:
    """Return the canonical display label from RANKED_DISTANCES / RANKED_TIMES."""
    if etype == "dist":
        return next(
            (lbl for d, lbl in RANKED_DISTANCES if d == evalue),
            _fmt_event(etype, evalue),
        )
    return next(
        (lbl for t, lbl in RANKED_TIMES if t == evalue), _fmt_event(etype, evalue)
    )


# ── Workout filtering helpers ─────────────────────────────────────────────────


def _event_workouts(workouts: list, etype: str, evalue: int, machine: str) -> list:
    """
    Return all workouts matching the event + machine filter (any season).

    Expects workouts to already be quality-filtered (is_rankable_noninterval()
    pre-applied) — e.g. the `rankable_efforts` list from race_page().

    Time events: match `time == evalue` as long as `distance` is not itself a
    ranked distance (avoids treating a 2k that happened to take exactly 30 min
    as a time event).
    """
    out = []
    for w in workouts:
        if machine != "All" and w.get("type", "rower") != machine:
            continue
        d = w.get("distance") or 0
        if etype == "dist" and d == evalue:
            out.append(w)
        elif etype == "time" and w.get("time") == evalue and d not in RANKED_DIST_SET:
            out.append(w)
    return out


def _include_filtered(state, workouts: list, include_filter: str) -> list:
    if include_filter == "All":
        return workouts
    elif include_filter == "top":
        if state.event_type == "dist":
            workouts = sorted(workouts, key=lambda w: w.get("time") or float("inf"))
        else:
            workouts = sorted(
                workouts, key=lambda w: w.get("distance") or 0, reverse=True
            )

        return workouts[:10]

    return apply_best_only(workouts, by_season=True)


# ── Results table ─────────────────────────────────────────────────────────────


def _results_table(workouts: list, etype: str, pb_id: int | None) -> None:
    """Render a sortable summary table for all in-scope workouts."""
    if not workouts:
        hd.text("No results in the current scope.", font_color="neutral-500")
        return

    types = {r.get("type") for r in workouts}
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
    WorkoutTable(
        workouts,
        cols,
        paginate=False,
        highlight=lambda w: w.get("id") == pb_id,
    )


# ── Main page entry point ─────────────────────────────────────────────────────


def race_page(
    client,
    user_id: str,
    excluded_seasons: tuple = (),
    machine: str = "All",
) -> None:
    """
    Top-level entry point for the Race tab.

    Parameters
    ----------
    excluded_seasons  Global season filter from app.py (tuple of "YYYY-YY" strings).
    machine           Global machine filter from app.py ("All" or machine type string).

    Renders:
      1. Filter bar (event selector + include filter)
      2. Race title
      3. RaceChart plugin
      4. Sort toggle (below the race)
      5. Results table
    """

    state = hd.state(
        event_type=_DEFAULT_EVENT_TYPE,
        event_value=_DEFAULT_EVENT_VALUE,
        include_filter="All",
        sort_mode="date",  # "date" | "result"
        show_wr_boat=False,
        wr_records={},  # {(etype, evalue): result} — cached from concept2_records
        wr_records_key="",  # "gender|age|weight_kg" — invalidation key
        strokes_cache_loaded=False,
        strokes_by_id={},
        fetch_queue=(),  # tuple of int workout IDs still to fetch
        fetch_total=0,  # size of the current fetch batch
        fetch_done=0,  # completed fetches in current batch
        last_batch_key="",  # changes when qualifying set changes
    )

    is_dark = hd.theme().is_dark

    # ── Phase 1: load profile + stroke cache from localStorage (once) ────────

    profile = get_profile()

    # ── Fetch workouts ─────────────────────────────────────────────────────────
    sync_result = concept2_sync(client)

    if not state.strokes_cache_loaded:
        ls_strokes = hd.local_storage.get_item(_STROKES_LS_KEY)
        if not ls_strokes.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
        else:
            if ls_strokes.result:
                state.strokes_by_id = decompress_strokes_cache(ls_strokes.result)
            state.strokes_cache_loaded = True

    if sync_result is None or profile is None or not state.strokes_cache_loaded:
        hd.box(padding=2, min_height="80vh")
        return

    _workouts_dict, sorted_workouts = sync_result
    all_workouts = list(sorted_workouts)

    # ── Apply quality filter (same strategy as Performance page) ─────────────
    rankable_efforts = [w for w in all_workouts if is_rankable_noninterval(w)]
    rankable_efforts = apply_quality_filters(rankable_efforts)

    # ── Apply global filters ──────────────────────────────────────────────────
    if excluded_seasons:
        _excl = set(excluded_seasons)
        rankable_efforts = [
            w for w in rankable_efforts if get_season(w.get("date", "")) not in _excl
        ]
    if machine != "All":
        rankable_efforts = [
            w for w in rankable_efforts if w.get("type", "rower") == machine
        ]

    # ── Compute available events ──────────────────────────────────────────────
    event_counts: dict = {}
    for w in rankable_efforts:
        d = w.get("distance") or 0
        t = w.get("time")
        if d in RANKED_DIST_SET:
            event_counts[("dist", d)] = event_counts.get(("dist", d), 0) + 1
        elif t in RANKED_TIME_SET and d not in RANKED_DIST_SET:
            event_counts[("time", t)] = event_counts.get(("time", t), 0) + 1

    available_events: list = []
    for dist, _ in RANKED_DISTANCES:
        if event_counts.get(("dist", dist), 0) > 0:
            available_events.append(("dist", dist))
    for tenths, _ in RANKED_TIMES:
        if event_counts.get(("time", tenths), 0) > 0:
            available_events.append(("time", tenths))

    # Default to first available if current selection has no data
    if (
        available_events
        and (state.event_type, state.event_value) not in available_events
    ):
        state.event_type, state.event_value = available_events[0]

    # ── Derived workout sets ───────────────────────────────────────────────────
    # Table scope: event + global filters (include_filter ignored for table)
    racing_workouts = _event_workouts(
        rankable_efforts, state.event_type, state.event_value, "All"
    )

    # Race scope: additionally apply include_filter
    racing_workouts = _include_filtered(state, racing_workouts, state.include_filter)

    # Season color palette
    wkt_seasons = sorted(
        {
            get_season(w.get("date", ""))
            for w in racing_workouts
            if get_season(w.get("date", "")) != "Unknown"
        }
    )

    # PB identification
    pb_id: int | None = None
    if racing_workouts:
        if state.event_type == "dist":
            pb = min(
                (w for w in racing_workouts if w.get("time")),
                key=lambda w: w["time"],
                default=None,
            )
        else:
            pb = max(
                (w for w in racing_workouts if w.get("distance")),
                key=lambda w: w["distance"],
                default=None,
            )
        pb_id = pb.get("id") if pb else None

    # ── Phase 2: one-at-a-time stroke fetch with real progress bar ────────────
    _all_race_ids = tuple(sorted(w.get("id") for w in racing_workouts if w.get("id")))
    _batch_key = f"{state.event_type}_{state.event_value}_{_all_race_ids}"

    if _batch_key != state.last_batch_key:
        missing_ids = tuple(
            w.get("id")
            for w in racing_workouts
            if w.get("id") and str(w.get("id")) not in state.strokes_by_id
        )
        state.fetch_queue = missing_ids
        state.fetch_total = len(missing_ids)
        state.fetch_done = 0
        state.last_batch_key = _batch_key

    is_loading = bool(state.fetch_queue)

    if state.fetch_queue:
        next_id = state.fetch_queue[0]
        with hd.scope(f"fetch_{next_id}"):
            stroke_task = hd.task()
            next_wkt = next(
                (w for w in racing_workouts if w.get("id") == next_id), None
            )
            if not stroke_task.running and not stroke_task.done and next_wkt:
                stroke_task.run(fetch_one_stroke, client, int(user_id), next_wkt)

            if stroke_task.done and not stroke_task.error:
                wid_str, strokes = stroke_task.result
                if wid_str == str(next_id):
                    merged = dict(state.strokes_by_id)
                    merged[wid_str] = strokes
                    state.strokes_by_id = merged
                    state.fetch_queue = state.fetch_queue[1:]
                    state.fetch_done += 1
                    hd.local_storage.set_item(
                        _STROKES_LS_KEY, compress_strokes_cache(merged)
                    )

    fetch_pct = (
        round(100 * state.fetch_done / state.fetch_total)
        if state.fetch_total > 0
        else 0
    )

    # ── Sort race workouts for lane assignment ─────────────────────────────────
    if state.sort_mode == "result":
        if state.event_type == "dist":
            sorted_racing_workouts = sorted(
                racing_workouts, key=lambda w: w.get("time") or float("inf")
            )
        else:
            sorted_racing_workouts = sorted(
                racing_workouts, key=lambda w: w.get("distance") or 0, reverse=True
            )
    else:  # "date" — newest first
        sorted_racing_workouts = sorted(
            racing_workouts, key=lambda w: w.get("date") or "", reverse=True
        )

    # ── Build races payload ────────────────────────────────────────────────────
    races_data = (
        build_races_data(sorted_racing_workouts, state.strokes_by_id, wkt_seasons)
        if sorted_racing_workouts
        else []
    )

    # ── World Record ghost boat ────────────────────────────────────────────────
    # Available only when: profile is complete, machine filter is rower (WR
    # records are RowErg only), and the user has enabled the toggle.
    _wr_available = profile_complete(profile) and machine in ("All", "rower")

    # Compute the profile key regardless of toggle state so UI status text
    # can reference it when the checkbox is visible.
    if _wr_available:
        _g_api = "M" if profile.get("gender") == "Male" else "F"
        _wr_age = age_from_dob(profile.get("dob", ""))
        _wr_wt_kg = (
            float(profile.get("weight") or 0) * 0.453592
            if profile.get("weight_unit") == "lbs"
            else float(profile.get("weight") or 0)
        )
        _wr_key = f"{_g_api}|{_wr_age}|{_wr_wt_kg:.1f}"
    else:
        _wr_key = ""

    _wr_boat: dict | None = None
    if _wr_available and state.show_wr_boat:
        # Fetch records if not yet cached or profile changed.
        if state.wr_records_key != _wr_key:
            with hd.scope(f"wr_task_{_wr_key}"):
                _wr_task = hd.task()
                if not _wr_task.running and not _wr_task.done:
                    _wr_task.run(get_age_group_records, _g_api, _wr_age, _wr_wt_kg)
                if _wr_task.done and not _wr_task.error:
                    state.wr_records = _wr_task.result
                    state.wr_records_key = _wr_key

        # Build the WR boat if we have a record for the selected event.
        if state.wr_records_key == _wr_key:
            _rec = state.wr_records.get((state.event_type, state.event_value))
            if _rec is not None:
                _wr_boat = build_wr_boat(state.event_type, state.event_value, _rec)

    # Prepend the WR boat so it occupies the first lane and is always visible.
    if _wr_boat is not None:
        races_data = [_wr_boat] + races_data

    # ── Race title (interactive) ───────────────────────────────────────────────
    # "A Race Between Your [Season Bests ▾] at [2k ▾]!"
    _include_long = {
        "All": "Great Efforts",
        "SBs": "Season Bests",
        "top": "Top 10 Efforts",
    }
    _cur_event_lbl = _fmt_event_long(state.event_type, state.event_value)
    _cur_include_lbl = _include_long.get(state.include_filter, state.include_filter)

    with hd.box(align="center", gap=3, padding=2, min_height="80vh"):
        with hd.box(align="center", gap=1, width="100%"):
            with hd.h1():
                with hd.hbox(gap=0.6, align="center", wrap="wrap"):
                    hd.text("A Race Between Your")

                    # ── Include filter dropdown ─────────────────────────────────────
                    with hd.scope("include_dd"):
                        with hd.dropdown() as _inc_dd:
                            _inc_btn = hd.button(
                                _cur_include_lbl,
                                caret=True,
                                size="large",
                                font_color="neutral-800",
                                font_size=2,
                                font_weight="bold",
                                slot=_inc_dd.trigger,
                            )
                            if _inc_btn.clicked:
                                _inc_dd.opened = not _inc_dd.opened
                            with hd.box(
                                gap=0.1,
                                background_color="neutral-0",
                                min_width=20,
                            ):
                                for val, lbl in _include_long.items():
                                    with hd.scope(f"inc_{val}"):
                                        _inc_item = hd.button(
                                            lbl,
                                            size="small",
                                            variant="primary"
                                            if state.include_filter == val
                                            else "text",
                                            width="100%",
                                            border_radius="small",
                                            font_size="medium",
                                            font_color="neutral-0"
                                            if state.include_filter == val
                                            else "neutral-800",
                                            label_style=hd.style(
                                                padding_top=0.5, padding_bottom=0.5
                                            ),
                                            hover_background_color="neutral-100",
                                        )
                                        if _inc_item.clicked:
                                            state.include_filter = val
                                            _inc_dd.opened = False

                    hd.text("at")

                    # ── Event selector dropdown ─────────────────────────────────────
                    with hd.scope("event_dd"):
                        with hd.dropdown() as _ev_dd:
                            _ev_btn = hd.button(
                                _cur_event_lbl,
                                caret=True,
                                size="large",
                                font_color="neutral-800",
                                font_size=2,
                                font_weight="bold",
                                slot=_ev_dd.trigger,
                            )
                            if _ev_btn.clicked:
                                _ev_dd.opened = not _ev_dd.opened
                            with hd.box(
                                gap=0.1,
                                min_width=17,
                                background_color="neutral-0",
                            ):
                                for etype, evalue in available_events:
                                    count = event_counts.get((etype, evalue), 0)
                                    row_lbl = (
                                        f"{_fmt_event_long(etype, evalue)}  ({count})"
                                    )
                                    is_sel = (
                                        state.event_type == etype
                                        and state.event_value == evalue
                                    )
                                    with hd.scope(f"ev_{etype}_{evalue}"):
                                        _ev_item = hd.button(
                                            row_lbl,
                                            size="small",
                                            variant="primary" if is_sel else "text",
                                            width="100%",
                                            border_radius="small",
                                            font_size="medium",
                                            font_color="neutral-0"
                                            if is_sel
                                            else "neutral-800",
                                            label_style=hd.style(
                                                padding_top=0.5, padding_bottom=0.5
                                            ),
                                            hover_background_color="neutral-100",
                                        )
                                        if _ev_item.clicked:
                                            state.event_type = etype
                                            state.event_value = evalue
                                            state.last_batch_key = ""
                                            _ev_dd.opened = False

                    hd.text("!")

            # ── Loading progress bar ──────────────────────────────────────────────────
            if is_loading:
                with hd.box(align="center", padding=2, gap=1, margin_bottom=0.5):
                    with hd.box(width=32):
                        hd.progress_bar(value=fetch_pct)
                    hd.text(
                        f"Fetching stroke data… {state.fetch_done} / {state.fetch_total}",
                        font_color="neutral-500",
                        font_size="small",
                    )

            # ── Race canvas ───────────────────────────────────────────────────────────
            RaceChart(
                races=races_data,
                event_type=state.event_type,
                event_value=state.event_value,
                is_dark=is_dark,
            )

            with hd.hbox(
                gap=3,
                align="center",
                justify="center",
                wrap="wrap",
                padding_top=0.75,
                padding_bottom=0.5,
            ):
                # Sort toggle
                with hd.box(gap=0.2, align="center"):
                    hd.text(
                        "Sort lanes by", font_size="medium", font_color="neutral-500"
                    )
                    with hd.scope("sort_mode"):
                        with radio_group(
                            value=state.sort_mode, size="medium"
                        ) as sort_rg:
                            hd.radio_button("Date", value="date")
                            hd.radio_button("Result", value="result")
                        if sort_rg.changed:
                            state.sort_mode = sort_rg.value

                # World Record ghost boat toggle (RowErg + complete profile only)
                if _wr_available:
                    with hd.scope("wr_toggle"):
                        with hd.box(gap=0.2, align="center"):
                            _wr_cb = hd.checkbox(
                                "Include World Record boat",
                                checked=state.show_wr_boat,
                            )
                            if _wr_cb.changed:
                                state.show_wr_boat = _wr_cb.checked
                            if state.show_wr_boat and state.wr_records_key != _wr_key:
                                # Records still loading — show a subtle note
                                hd.text(
                                    "Loading records…",
                                    font_size="2x-small",
                                    font_color="neutral-400",
                                )
                            elif state.show_wr_boat and _wr_boat is None:
                                hd.text(
                                    "No world record available for this event / category.",
                                    font_size="2x-small",
                                    font_color="neutral-400",
                                )

        with hd.box(gap=1, align="center"):
            with hd.h2():
                if state.include_filter == "All":
                    hd.text(f"Your Quality {_cur_event_lbl} Efforts")
                elif state.include_filter == "SBs":
                    hd.text(f"Your {_cur_event_lbl} Season Bests")
                elif state.include_filter == "top":
                    hd.text(f"Your Top 10 {_cur_event_lbl} Efforts")

            # ── Results table ─────────────────────────────────────────────────────────
            if racing_workouts:
                _results_table(racing_workouts, state.event_type, pb_id)
            elif not is_loading:
                with hd.box(padding=3, align="center"):
                    hd.text(
                        f"No {_fmt_event_long(state.event_type, state.event_value)} results in the selected scope.",
                        font_color="neutral-500",
                    )
