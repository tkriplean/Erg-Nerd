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

Stroke data is fetched uniformly via ``components.concept2_sync.strokes_batch``
(owner: one-at-a-time API fetch with a progress bar; public: synchronous disk
reads from the cache-on-owner-view directory).  The cache stores raw Concept2
strokes; ``normalize_strokes`` converts at the boundary for ``build_races_data``.
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
from services.stroke_utils import (
    build_races_data,
    build_boat_label,
    build_wr_boat,
    normalize_strokes,
)
from services.rowing_utils import season_color
from services.concept2_records import get_age_group_records
from components.profile_page import get_profile_from_context
from services.rowing_utils import (
    apply_quality_filters,
    is_rankable_noninterval,
    profile_complete,
)
from components.concept2_sync import sync_from_context, strokes_batch
from components.view_context import your
from components.race_chart_plugin import RaceChart
from components.hyperdiv_extensions import radio_group
from components.workout_chart_builder import (
    build_stroke_chart_config,
    build_compare_series,
)
from components.workout_chart_plugin import StrokeChart
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
from components.shared_ui import global_filter_ui, header_dropdown

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
        if event_type == "dist":
            workouts = sorted(workouts, key=lambda w: w.get("time") or float("inf"))
        else:
            workouts = sorted(
                workouts, key=lambda w: w.get("distance") or 0, reverse=True
            )

        return workouts[:10]

    return apply_best_only(workouts, by_season=True)


# ── Race stroke-data graph ────────────────────────────────────────────────────


def _race_stroke_graph(
    state,
    sorted_racing_workouts: list,
    raw_by_id: dict,
    workouts_dict: dict,
    pb_id: int | None,
    is_dark: bool,
) -> None:
    """Render a pace/SPM/HR multi-line chart, one line per boat.

    PB effort is the primary series; every other effort with real stroke
    data is drawn as a dashed compare overlay.  Boats whose strokes were
    synthesised from splits are skipped (their ~8-point curves look smooth
    but are misleading at stroke-level scale) and listed in a footer note.
    """
    if not sorted_racing_workouts:
        return

    # Partition boats by whether they have real (API-sourced) stroke data.
    # Matches the threshold in build_races_data.has_real_strokes.
    with_strokes: list = []
    without_strokes: list = []
    for w in sorted_racing_workouts:
        raw = raw_by_id.get(str(w.get("id")), [])
        if len(raw) > 20:
            with_strokes.append(w)
        else:
            without_strokes.append(w)

    if not with_strokes:
        with hd.box(padding=2, align="center"):
            hd.text(
                "No stroke-level data available for these workouts.",
                font_color="neutral-500",
                font_size="small",
            )
        return

    # Primary = PB if it has real strokes, else the best-by-event effort among
    # the real-stroke set.
    primary_wkt: dict | None = None
    if pb_id is not None:
        primary_wkt = next((w for w in with_strokes if w.get("id") == pb_id), None)
    if primary_wkt is None:
        first = sorted_racing_workouts[0]
        is_time_event = first.get("distance") not in RANKED_DIST_SET
        if is_time_event:
            primary_wkt = max(with_strokes, key=lambda w: w.get("distance") or 0)
        else:
            primary_wkt = min(
                (w for w in with_strokes if w.get("time")),
                key=lambda w: w["time"],
                default=with_strokes[0],
            )

    primary_id = primary_wkt.get("id")
    primary_strokes = raw_by_id.get(str(primary_id), [])
    compared_ids = tuple(w.get("id") for w in with_strokes if w.get("id") != primary_id)
    compare_results = {cid: raw_by_id.get(str(cid), []) for cid in compared_ids}

    # Season-colored per-id map — lines match their race lane color.
    color_of = {
        w.get("id"): season_color(get_season(w.get("date", "")), fmt="hex")
        for w in sorted_racing_workouts
    }
    label_of = {
        w.get("id"): build_boat_label(w, sorted_racing_workouts)
        for w in sorted_racing_workouts
    }

    # ── Controls row ─────────────────────────────────────────────────────
    has_hr_any = any(
        any(s.get("hr") for s in raw_by_id.get(str(w.get("id")), []))
        for w in with_strokes
    )

    # ── Build config ─────────────────────────────────────────────────────
    show_watts = state.chart_metric == "watts"
    race_compare_series = build_compare_series(
        compared_ids,
        compare_results,
        workouts_dict,
        show_watts=show_watts,
        colors=[color_of[cid] for cid in compared_ids],
        labels={cid: label_of[cid] for cid in compared_ids},
    )

    cfg = build_stroke_chart_config(
        primary_strokes,
        primary_wkt,
        metric=state.chart_metric,
        is_dark=is_dark,
        stack=False,
        show_pace=state.chart_show_pace,
        show_spm=state.chart_show_spm,
        show_hr=state.chart_show_hr,
        compare_series=race_compare_series,
        primary_color=color_of[primary_id],
        primary_label=label_of[primary_id],
    )

    with hd.box(align="center", gap=0.5):
        # ── note for skipped boats ────────────────────────────────────
        if without_strokes:
            skipped_dates = ", ".join(
                build_boat_label(w, sorted_racing_workouts) for w in without_strokes
            )
            n = len(without_strokes)
            hd.text(
                f"Stroke-level data isn't available for {n} "
                f"workout{'s' if n != 1 else ''}: {skipped_dates}.",
                font_color="neutral-500",
                font_size="small",
            )

        if cfg:
            with hd.scope("race_chart"):
                StrokeChart(config=cfg, height="45vh")

    with hd.hbox(gap=1.5, align="center", justify="center", wrap="wrap", padding=0.5):
        with hd.scope("race_chart_metric"):
            with radio_group(value=state.chart_metric, size="small") as mrg:
                hd.radio_button("Pace", value="pace")
                hd.radio_button("Watts", value="watts")
            if mrg.changed:
                state.chart_metric = mrg.value

        with hd.scope("race_chart_pace_sw"):
            _lbl = "Watts" if state.chart_metric == "watts" else "Pace"
            pace_sw = hd.switch(_lbl, checked=state.chart_show_pace, size="small")
            if pace_sw.changed:
                state.chart_show_pace = pace_sw.checked

        with hd.scope("race_chart_spm_sw"):
            spm_sw = hd.switch("SPM", checked=state.chart_show_spm, size="small")
            if spm_sw.changed:
                state.chart_show_spm = spm_sw.checked

        if has_hr_any:
            with hd.scope("race_chart_hr_sw"):
                hr_sw = hd.switch("HR", checked=state.chart_show_hr, size="small")
                if hr_sw.changed:
                    state.chart_show_hr = hr_sw.checked


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
    ctx,
    global_state,
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
        event=(_DEFAULT_EVENT_TYPE, _DEFAULT_EVENT_VALUE),
        event_type=_DEFAULT_EVENT_TYPE,
        event_value=_DEFAULT_EVENT_VALUE,
        include_filter="All",
        show_wr_boat=False,
        wr_records={},  # {(etype, evalue): result} — cached from concept2_records
        wr_records_key="",  # "gender|age|weight_kg" — invalidation key
        chart_metric="pace",  # "pace" | "watts" (stroke graph)
        chart_show_pace=True,
        chart_show_spm=False,
        chart_show_hr=False,
        scatter_metric="pace",  # "pace" | "watts" (pace-vs-date scatter)
    )

    (event_type, event_value) = state.event

    is_dark = hd.theme().is_dark

    profile = get_profile_from_context(ctx)
    sync_result = sync_from_context(ctx)

    if sync_result is None or profile is None:
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
    if available_events and (event_type, event_value) not in available_events:
        event_type, event_value = available_events[0]

    # ── Derived workout sets ───────────────────────────────────────────────────
    # Table scope: event + global filters (include_filter ignored for table)
    racing_workouts = _event_workouts(rankable_efforts, event_type, event_value, "All")

    # Race scope: additionally apply include_filter
    racing_workouts = _include_filtered(state, racing_workouts, state.include_filter)

    # PB identification
    pb_id: int | None = None
    if racing_workouts:
        if event_type == "dist":
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

    # ── Stroke fetch via unified concept2_sync.strokes_batch ─────────────────
    # Owner mode: one API fetch per render; public mode: synchronous disk
    # reads of the cache-on-owner-view directory.  Strokes are stored as raw
    # Concept2 format in the unified cache — normalize at the boundary.
    batch = strokes_batch(ctx, racing_workouts)
    raw_by_id = batch["by_id"]
    strokes_by_id = {k: normalize_strokes(v) for k, v in raw_by_id.items()}
    uncached_public_count = batch["uncached_count"]
    is_public = ctx.mode == "public"

    is_loading = batch["is_loading"]
    fetch_done = batch["done"]
    fetch_total = batch["total"]
    fetch_pct = round(100 * fetch_done / fetch_total) if fetch_total > 0 else 0

    # ── Sort race workouts for lane assignment ─────────────────────────────────
    # Stable "newest first" default; the JS plugin re-sorts on user toggle.
    sorted_racing_workouts = sorted(
        racing_workouts, key=lambda w: w.get("date") or "", reverse=True
    )

    # ── Build races payload ────────────────────────────────────────────────────
    races_data = (
        build_races_data(sorted_racing_workouts, strokes_by_id)
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
            _rec = state.wr_records.get((event_type, event_value))
            if _rec is not None:
                _wr_boat = build_wr_boat(event_type, event_value, _rec)

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
    _cur_event_lbl = _fmt_event_long(event_type, event_value)
    _cur_include_lbl = _include_long.get(state.include_filter, state.include_filter)

    with hd.box(align="center", gap=3, padding=2, min_height="80vh"):
        with hd.box(align="center", gap=1, width="100%"):
            with hd.box(gap=0.2, align="center"):
                with hd.h1(font_weight="normal"):
                    with hd.hbox(gap=0.2, align="center", wrap="wrap"):
                        hd.text(f"A Race Between {your(ctx)}")

                        header_dropdown(
                            state,
                            key="include_dd",
                            labels=_include_long,
                            current_value=state.include_filter,
                            field="include_filter",
                        )

                        hd.text("at")

                        header_dropdown(
                            state,
                            key="event_dd",
                            labels={
                                v: f"{_fmt_event_long(v[0], v[1])}"
                                for v in available_events
                            },
                            current_value=state.event,
                            field="event",
                        )

                global_filter_ui(global_state, ctx)

            # ── Loading progress bar ──────────────────────────────────────────────────
            if is_loading:
                with hd.box(align="center", padding=2, gap=1, margin_bottom=0.5):
                    with hd.box(width=32):
                        hd.progress_bar(value=fetch_pct)
                    hd.text(
                        f"Fetching stroke data… {fetch_done} / {fetch_total}",
                        font_color="neutral-500",
                        font_size="small",
                    )

            # ── Public-mode: notice for workouts whose strokes aren't cached yet ──
            if is_public and uncached_public_count > 0:
                with hd.hbox(
                    gap=0.5,
                    align="center",
                    justify="center",
                    padding=1,
                    background_color="neutral-50",
                    border="1px solid neutral-200",
                    border_radius="medium",
                    margin_bottom=0.5,
                ):
                    hd.icon("info-circle", font_color="neutral-500")
                    hd.text(
                        f"{uncached_public_count} workout"
                        f"{'s' if uncached_public_count != 1 else ''} stroke data not available.",
                        font_size="small",
                        font_color="neutral-600",
                    )

            # ── Race canvas ───────────────────────────────────────────────────────────
            # `_wr_ready` drives the phantom WR lane — profile complete AND records
            # loaded (or being loaded).  The records fetch is kicked off once the
            # user ticks the checkbox; the lane itself only requires profile.
            _wr_chart = RaceChart(
                races=races_data,
                event_type=event_type,
                event_value=event_value,
                is_dark=is_dark,
                wr_available=_wr_available,
                wr_requested=state.show_wr_boat,
            )
            # JS → Python: reflect the checkbox state into the page.  Changing
            # this triggers a re-render which fetches records + builds the WR
            # boat on the Python side.
            if _wr_chart.wr_requested != state.show_wr_boat:
                state.show_wr_boat = _wr_chart.wr_requested

        with hd.box(gap=1, align="center"):
            with hd.h2():
                _poss = your(ctx)
                if state.include_filter == "All":
                    hd.text(f"{_poss} Quality {_cur_event_lbl} Efforts")
                elif state.include_filter == "SBs":
                    hd.text(f"{_poss} {_cur_event_lbl} Season Bests")
                elif state.include_filter == "top":
                    hd.text(f"{_poss} Top 10 {_cur_event_lbl} Efforts")

            # ── Stroke-data graph ──────────────────────────────────────────────
            if sorted_racing_workouts and not is_loading:
                with hd.box(gap=0.5, align="stretch", width="100%"):
                    _race_stroke_graph(
                        state,
                        sorted_racing_workouts,
                        raw_by_id,
                        _workouts_dict,
                        pb_id,
                        is_dark,
                    )

            # ── Results table ─────────────────────────────────────────────────────────
            with hd.box():
                if racing_workouts:
                    _results_table(racing_workouts, event_type, pb_id)
                elif not is_loading:
                    with hd.box(padding=3, align="center"):
                        hd.text(
                            f"No {_fmt_event_long(event_type, event_value)} results in the selected scope.",
                            font_color="neutral-500",
                        )
