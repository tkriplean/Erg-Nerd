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
  include_filter  str    "All" | "SBs"
  sort_mode       str    "date" | "result"
  strokes_cache_loaded  bool
  strokes_by_id         dict  {str(id): [{t,d}]}
  fetch_queue           tuple[int]
  fetch_total / fetch_done  int
  last_batch_key        str
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
from services.ranked_filters import is_ranked_noninterval, apply_quality_filters
from services.formatters import format_time, fmt_split
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
    pre-applied) — e.g. the `all_ranked` list from race_page().

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
        strokes_cache_loaded=False,
        strokes_by_id={},
        fetch_queue=(),  # tuple of int workout IDs still to fetch
        fetch_total=0,  # size of the current fetch batch
        fetch_done=0,  # completed fetches in current batch
        last_batch_key="",  # changes when qualifying set changes
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
    all_ranked_raw = [w for w in all_workouts if is_ranked_noninterval(w)]
    all_ranked = apply_quality_filters(
        all_ranked_raw,
        selected_dists=set(),
        selected_times=set(),
        excluded_seasons=set(),
    )

    # ── Apply global filters ──────────────────────────────────────────────────
    if excluded_seasons:
        _excl = set(excluded_seasons)
        all_ranked = [
            w for w in all_ranked if get_season(w.get("date", "")) not in _excl
        ]
    if machine != "All":
        all_ranked = [w for w in all_ranked if w.get("type", "rower") == machine]

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

    # ── Derived workout sets ───────────────────────────────────────────────────
    # Table scope: event + global filters (include_filter ignored for table)
    table_wkts = _event_workouts(all_ranked, state.event_type, state.event_value, "All")

    # Race scope: additionally apply include_filter
    race_wkts = _include_filtered(table_wkts, state.include_filter)

    # Season color palette
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
    _all_race_ids = tuple(sorted(w.get("id") for w in race_wkts if w.get("id")))
    _batch_key = f"{state.event_type}_{state.event_value}_{_all_race_ids}"

    if _batch_key != state.last_batch_key:
        missing_ids = tuple(
            w.get("id")
            for w in race_wkts
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
            next_wkt = next((w for w in race_wkts if w.get("id") == next_id), None)
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
            sorted_race_wkts = sorted(
                race_wkts, key=lambda w: w.get("time") or float("inf")
            )
        else:
            sorted_race_wkts = sorted(
                race_wkts, key=lambda w: w.get("distance") or 0, reverse=True
            )
    else:  # "date" — newest first
        sorted_race_wkts = sorted(
            race_wkts, key=lambda w: w.get("date") or "", reverse=True
        )

    # ── Build races payload ────────────────────────────────────────────────────
    races_data = (
        build_races_data(sorted_race_wkts, state.strokes_by_id, wkt_seasons)
        if sorted_race_wkts
        else []
    )

    # ── Race title (interactive) ───────────────────────────────────────────────
    # "A Race Between Your [Season Bests ▾] at [2k ▾]!"
    _include_long = {
        "All": "Great Efforts",
        "SBs": "Season Bests",
    }
    _cur_event_lbl = _fmt_event_long(state.event_type, state.event_value)
    _cur_include_lbl = _include_long.get(state.include_filter, state.include_filter)

    with hd.box(align="center", gap=1, padding=2):
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
                                row_lbl = f"{_fmt_event_long(etype, evalue)}  ({count})"
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

        # ── Sort toggle (below the race) ──────────────────────────────────────────
        with hd.box(gap=0.2, align="center", padding_top=0.75, padding_bottom=0.5):
            hd.text("Sort lanes by", font_size="medium", font_color="neutral-500")
            with hd.scope("sort_mode"):
                with radio_group(value=state.sort_mode, size="medium") as sort_rg:
                    hd.radio_button("Date", value="date")
                    hd.radio_button("Result", value="result")
                if sort_rg.changed:
                    state.sort_mode = sort_rg.value

        # ── Results table ─────────────────────────────────────────────────────────
        if table_wkts:
            hd.text(
                f"{len(table_wkts)} result(s) — {_fmt_event_long(state.event_type, state.event_value)}",
                font_weight="semibold",
                font_size="small",
                font_color="neutral-600",
                padding_top=0.5,
                padding_bottom=0.5,
            )
            _results_table(table_wkts, state.event_type, pb_id)
        elif not is_loading:
            with hd.box(padding=3, align="center"):
                hd.text(
                    f"No {_fmt_event_long(state.event_type, state.event_value)} results in the selected scope.",
                    font_color="neutral-500",
                )
