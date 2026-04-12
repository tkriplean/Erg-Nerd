"""
Event Page — Regatta-style race animation for a single ranked event.

Exported:
    event_page(client, user_id)  — top-level HyperDiv component; call from app.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Event selector + Filter bar:
    Event: [2000m ▾]  Include [All|PBs|SBs]  Season [▾]  Machine [▾]

  Race area:
    [  RaceChart plugin  height="58vh"  ]
    (while loading: spinner overlay + "Fetching stroke data…")

  Results table:
    All qualifying workouts in season + machine scope (ignores include_filter).
    Sorted ascending by time (dist events) or descending by distance (time events).
    PB row highlighted.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATE VARIABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  event_type             str    "dist" | "time" — selected event category
  event_value            int    meters (dist) or tenths-of-sec (time)

  excluded_seasons       tuple  seasons hidden from view (sorted)
  machine                str    "All" or machine type string (e.g. "rower")
  include_filter         str    "All" | "PBs" | "SBs" — governs race boats
  sort_mode              str    "date" (newest lane 1) | "result" (best lane 1)

  strokes_cache_loaded   bool   True after localStorage read completes
  strokes_by_id          dict   {str(workout_id): [{t, d}, …]} in-memory cache
  fetch_queue            tuple  Workout IDs (int) still waiting to be fetched
  fetch_total            int    Total IDs in current fetch batch (for % bar)
  fetch_done             int    How many have been fetched so far
  last_batch_key         str    Identifies the current fetch batch; changes when
                                filters change so a new batch is initialised
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATA FLOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  concept2_sync() → all_workouts
      ↓  filter by event + season + machine + include_filter
  qualifying_wkts (race boats)
      ↓  check strokes_by_id for missing IDs
  fetch_one_stroke() [hd.task] → one workout fetched per render, queue advances
      ↓
  build_races_data() → RaceChart plugin
      ↓
  Results table from season+machine filtered workouts (all, not just PBs/SBs)
"""

from __future__ import annotations

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
)
from services.ranked_filters import is_ranked_noninterval, seasons_from, apply_quality_filters
from services.formatters import format_time, machine_label, fmt_split
from services.stroke_utils import build_races_data, fetch_one_stroke
from services.local_storage_compression import (
    compress_strokes_cache,
    decompress_strokes_cache,
)
from components.concept2_sync import concept2_sync
from components.race_chart_plugin import RaceChart
from components.hyperdiv_extensions import radio_group

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

    Expects workouts to already be quality-filtered (is_ranked_noninterval()
    pre-applied) — e.g. the `all_ranked` list from event_page().

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


def _season_filtered(workouts: list, excluded_seasons: tuple) -> list:
    excl = set(excluded_seasons)
    return [w for w in workouts if get_season(w.get("date", "")) not in excl]


def _include_filtered(workouts: list, include_filter: str) -> list:
    if include_filter == "All":
        return workouts
    if include_filter == "PBs":
        return apply_best_only(workouts, by_season=False)
    return apply_best_only(workouts, by_season=True)


# ── Results table ─────────────────────────────────────────────────────────────


def _results_table(workouts: list, etype: str, pb_id: int | None) -> None:
    """Render a sortable-by-result summary table for all in-scope workouts."""
    if not workouts:
        hd.text("No results in the current scope.", font_color="neutral-500")
        return

    is_time_event = etype == "time"
    if etype == "dist":
        rows = sorted(workouts, key=lambda w: w.get("time") or float("inf"))
    else:
        rows = sorted(workouts, key=lambda w: w.get("distance") or 0, reverse=True)

    headers = [
        "Date",
        "Season",
        "Distance" if is_time_event else "Time",
        "Avg Pace",
        "Avg Watts",
        "Avg SPM",
        "Avg HR",
    ]

    with hd.table(border_bottom="1px solid neutral-200"):
        with hd.thead():
            with hd.tr():
                for col in headers:
                    with hd.scope(col):
                        with hd.td(padding=(0.5, 1)):
                            hd.text(
                                col,
                                font_size="small",
                                font_weight="semibold",
                                font_color="neutral-600",
                            )

        with hd.tbody():
            for w in rows:
                with hd.scope(w.get("id")):
                    is_pb = w.get("id") == pb_id
                    date_str = (w.get("date") or "")[:10]
                    season = get_season(w.get("date", ""))
                    pace = compute_pace(w)
                    pace_str = fmt_split(round(pace * 10)) if pace else "—"
                    watts_str = str(round(compute_watts(pace))) if pace else "—"
                    hr = (w.get("heart_rate") or {}).get("average") or 0
                    hr_str = str(hr) if hr else "—"
                    spm = w.get("stroke_rate") or 0
                    spm_str = str(spm) if spm else "—"
                    metric = (
                        f"{w.get('distance', 0):,}m"
                        if is_time_event
                        else format_time(w.get("time") or 0)
                    )
                    vals = [
                        date_str,
                        season,
                        metric,
                        pace_str,
                        watts_str,
                        spm_str,
                        hr_str,
                    ]
                    with hd.tr(background_color="primary-50" if is_pb else "neutral-0"):
                        for idx, v in enumerate(vals):
                            with hd.scope(idx):
                                with hd.td(padding=(0.4, 1)):
                                    hd.text(
                                        v,
                                        font_size="small",
                                        font_weight="semibold" if is_pb else "normal",
                                        font_color="primary-700"
                                        if is_pb
                                        else "neutral-800",
                                    )


# ── Main page entry point ─────────────────────────────────────────────────────


def event_page(client, user_id: str) -> None:
    """
    Top-level entry point for the Event tab.

    Renders:
      1. Filter bar (event selector, include filter, season, machine)
      2. RaceChart plugin (regatta animation)
      3. Results table
    """
    state = hd.state(
        event_type=_DEFAULT_EVENT_TYPE,
        event_value=_DEFAULT_EVENT_VALUE,
        excluded_seasons=(),
        machine="All",
        include_filter="All",
        sort_mode="date",  # "date" | "result"
        strokes_cache_loaded=False,
        strokes_by_id={},
        fetch_queue=(),       # tuple of workout dicts still to fetch
        fetch_total=0,        # size of the current batch
        fetch_done=0,         # completed fetches in current batch
        last_batch_key="",    # changes when the qualifying set changes
    )

    is_dark = hd.theme().is_dark

    # ── Phase 1: load stroke cache from localStorage (once) ──────────────────
    if not state.strokes_cache_loaded:
        ls_strokes = hd.local_storage.get_item(_STROKES_LS_KEY)
        if not ls_strokes.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return
        if ls_strokes.result:
            state.strokes_by_id = decompress_strokes_cache(ls_strokes.result)
        state.strokes_cache_loaded = True

    # ── Fetch workouts ─────────────────────────────────────────────────────────
    sync_result = concept2_sync(client)
    if sync_result is None:
        return

    _workouts_dict, sorted_workouts = sync_result
    all_workouts = list(sorted_workouts)

    # ── Apply quality filter (same strategy as Performance page) ─────────────
    # This excludes warmup rows and non-max-effort sessions before event counting
    # and any subsequent filtering.
    all_ranked_raw = [w for w in all_workouts if is_ranked_noninterval(w)]
    all_ranked = apply_quality_filters(
        all_ranked_raw,
        selected_dists=set(),
        selected_times=set(),
        excluded_seasons=set(),
    )

    # ── Compute available events ──────────────────────────────────────────────
    event_counts: dict = {}
    for w in all_ranked:
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

    # ── Season + machine helpers ──────────────────────────────────────────────
    all_seasons_global = seasons_from(all_ranked)  # newest-first
    machine_types = sorted(
        {w.get("type", "rower") for w in all_ranked if w.get("type")}
    )

    # ── Filter bar ────────────────────────────────────────────────────────────
    with hd.box(
        padding=1,
        border="1px solid neutral-200",
        border_radius="medium",
        background_color="neutral-50",
        margin_bottom=1,
    ):
        with hd.hbox(gap=2, align="center", wrap="wrap"):
            # ── Event selector ──────────────────────────────────────────────
            hd.text("Event", font_weight="semibold", font_size="small")
            _cur_lbl = _fmt_event(state.event_type, state.event_value)
            with hd.scope("event_dd"):
                with hd.dropdown() as _ev_dd:
                    _ev_btn = hd.button(
                        _cur_lbl, caret=True, size="medium", slot=_ev_dd.trigger
                    )
                    if _ev_btn.clicked:
                        _ev_dd.opened = not _ev_dd.opened
                    with hd.box(
                        padding=0.5,
                        gap=0.1,
                        min_width=13,
                        background_color="neutral-50",
                    ):
                        for etype, evalue in available_events:
                            count = event_counts.get((etype, evalue), 0)
                            row_lbl = f"{_fmt_event_long(etype, evalue)}  ({count})"
                            is_sel = (
                                state.event_type == etype
                                and state.event_value == evalue
                            )
                            with hd.scope(f"ev_{etype}_{evalue}"):
                                _item = hd.button(
                                    row_lbl,
                                    size="small",
                                    variant="primary" if is_sel else "text",
                                    width="100%",
                                )
                                if _item.clicked:
                                    state.event_type = etype
                                    state.event_value = evalue
                                    state.last_batch_key = ""  # force new batch
                                    _ev_dd.opened = False

            hd.text("|", font_color="neutral-300")

            # ── Include filter ──────────────────────────────────────────────
            hd.text("Include", font_weight="semibold", font_size="small")
            with hd.scope("include_filter"):
                with radio_group(value=state.include_filter, size="medium") as rg:
                    hd.radio_button("All")
                    hd.radio_button("PBs")
                    hd.radio_button("SBs")
                if rg.changed:
                    state.include_filter = rg.value

            hd.text("|", font_color="neutral-300")

            # ── Season dropdown ─────────────────────────────────────────────
            if all_seasons_global:
                _excl_valid = set(state.excluded_seasons) & set(all_seasons_global)
                _n_sel = len(all_seasons_global) - len(_excl_valid)
                _seas_lbl = (
                    "All"
                    if not _excl_valid
                    else f"{_n_sel} of {len(all_seasons_global)}"
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
                            with hd.hbox(gap=0.5, padding_bottom=0.5):
                                if hd.button(
                                    "Select all", size="small", variant="text"
                                ).clicked:
                                    state.excluded_seasons = ()
                                if hd.button(
                                    "Clear all", size="small", variant="text"
                                ).clicked:
                                    state.excluded_seasons = tuple(all_seasons_global)
                            _shown = [
                                (n, lbl)
                                for n, lbl in [
                                    (1, "Last season"),
                                    (2, "Last 2"),
                                    (5, "Last 5"),
                                ]
                                if len(all_seasons_global) >= n
                            ]
                            if _shown:
                                with hd.hbox(gap=0.5, padding_bottom=0.5, wrap="wrap"):
                                    for _cn, _clbl in _shown:
                                        with hd.scope(f"conv_{_cn}"):
                                            if hd.button(
                                                _clbl, size="medium", variant="text"
                                            ).clicked:
                                                state.excluded_seasons = tuple(
                                                    sorted(all_seasons_global[_cn:])
                                                )
                            with hd.hbox(gap=0.75):
                                with hd.scope(str(state.excluded_seasons)):
                                    for season in all_seasons_global:
                                        with hd.scope(f"s_{season}"):
                                            _is_sel = (
                                                season not in state.excluded_seasons
                                            )
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

            # ── Machine filter ──────────────────────────────────────────────
            if len(machine_types) > 1:
                hd.text("|", font_color="neutral-300")
                with hd.scope("machine_filter"):
                    machine_sel = hd.select(value=state.machine, size="small")
                    with machine_sel:
                        hd.option("All Machines", value="All")
                        for mt in machine_types:
                            hd.option(machine_label(mt), value=mt)
                    if machine_sel.changed:
                        state.machine = machine_sel.value
            else:
                state.machine = "All"

            hd.text("|", font_color="neutral-300")

            # ── Sort order toggle ───────────────────────────────────────────
            hd.text("Sort", font_weight="semibold", font_size="small")
            with hd.scope("sort_mode"):
                with radio_group(value=state.sort_mode, size="medium") as sort_rg:
                    hd.radio_button("date", value="date")
                    hd.radio_button("result", value="result")
                if sort_rg.changed:
                    state.sort_mode = sort_rg.value

    # ── Derived workout sets ───────────────────────────────────────────────────
    # Table scope: event + machine + season (include_filter ignored)
    table_wkts = _event_workouts(
        all_ranked, state.event_type, state.event_value, state.machine
    )
    table_wkts = _season_filtered(table_wkts, state.excluded_seasons)

    # Race scope: additionally apply include_filter
    race_wkts = _include_filtered(table_wkts, state.include_filter)

    # Season color palette (sorted chronologically = lexicographically for "YYYY-YY")
    wkt_seasons = sorted(
        {
            get_season(w.get("date", ""))
            for w in race_wkts
            if get_season(w.get("date", "")) != "Unknown"
        }
    )

    # PB identification
    pb_id: int | None = None
    if table_wkts:
        if state.event_type == "dist":
            pb = min(
                (w for w in table_wkts if w.get("time")),
                key=lambda w: w["time"],
                default=None,
            )
        else:
            pb = max(
                (w for w in table_wkts if w.get("distance")),
                key=lambda w: w["distance"],
                default=None,
            )
        pb_id = pb.get("id") if pb else None

    # ── Phase 2: one-at-a-time stroke fetch with real progress bar ────────────
    # The fetch queue stores workout IDs (ints).  Each ID gets its own
    # hd.scope so HyperDiv creates a fresh task per workout, avoiding the
    # "done task can't restart" problem.
    _all_race_ids = tuple(sorted(w.get("id") for w in race_wkts if w.get("id")))
    _batch_key = f"{state.event_type}_{state.event_value}_{_all_race_ids}"

    # When the qualifying set changes, initialise a fresh fetch queue.
    if _batch_key != state.last_batch_key:
        missing_ids = tuple(
            w.get("id")
            for w in race_wkts
            if w.get("id") and str(w.get("id")) not in state.strokes_by_id
        )
        state.fetch_queue = missing_ids   # tuple of ints
        state.fetch_total = len(missing_ids)
        state.fetch_done = 0
        state.last_batch_key = _batch_key

    is_loading = bool(state.fetch_queue)

    if state.fetch_queue:
        next_id = state.fetch_queue[0]
        # Scope key changes per workout → fresh task for each one.
        with hd.scope(f"fetch_{next_id}"):
            stroke_task = hd.task()
            next_wkt = next(
                (w for w in race_wkts if w.get("id") == next_id), None
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
    # "date" mode: newest piece in lane 1 (top), oldest at bottom.
    # "result" mode: best result in lane 1, worst at bottom.
    if state.sort_mode == "result":
        if state.event_type == "dist":
            sorted_race_wkts = sorted(
                race_wkts, key=lambda w: w.get("time") or float("inf")
            )
        else:
            sorted_race_wkts = sorted(
                race_wkts, key=lambda w: w.get("distance") or 0, reverse=True
            )
    else:  # "date" — newest first
        sorted_race_wkts = sorted(
            race_wkts,
            key=lambda w: w.get("date") or "",
            reverse=True,
        )

    # ── Build races payload ────────────────────────────────────────────────────
    # Build with whatever strokes we have so far — workouts still in the queue
    # will use synthesised pacing until their real data arrives.
    races_data = (
        build_races_data(sorted_race_wkts, state.strokes_by_id, wkt_seasons)
        if sorted_race_wkts
        else []
    )

    # ── Loading progress bar (matches concept2_sync style) ───────────────────
    if is_loading:
        with hd.box(align="center", padding=2, gap=1, margin_bottom=0.5):
            with hd.box(width=32):
                hd.progress_bar(value=fetch_pct)
            hd.text(
                f"Fetching stroke data… {state.fetch_done} / {state.fetch_total}",
                font_color="neutral-500",
                font_size="small",
            )

    RaceChart(
        races=races_data,
        event_type=state.event_type,
        event_value=state.event_value,
        is_dark=is_dark,
        height="70vh",
    )

    # ── Results table ─────────────────────────────────────────────────────────
    if table_wkts:
        hd.text(
            f"{len(table_wkts)} result(s) — {_fmt_event_long(state.event_type, state.event_value)}",
            font_weight="semibold",
            font_size="small",
            font_color="neutral-600",
            padding_top=1.5,
            padding_bottom=0.5,
        )
        _results_table(table_wkts, state.event_type, pb_id)
    elif not is_loading:
        with hd.box(padding=3, align="center"):
            hd.text(
                f"No {_fmt_event_long(state.event_type, state.event_value)} results in the selected scope.",
                font_color="neutral-500",
            )
