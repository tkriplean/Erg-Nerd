"""
Rank Page — "how do I rank?" view comparing a user's ranked performances
against the world record or the Concept2 rankings field.

Exported:
    rank_page()
        Top-level HyperDiv component; call from app.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Heading (inline dropdowns):
      "Your [Season Bests ▾] Against [C2 Same Age Peers ▾]"

  Main chart:
      RankChart — scatter over categorical x (ranked events), y = % of WR
      (world_record focus) or percentile (c2_*). Dashed reference line.

  Data table (rows = the user's qualifying performances):
      Columns vary by focus mode.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATE VARIABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  include_filter   str   "SBs" | "PBs"
  ranking_focus    str   "c2_age_matched" | "c2_age_group" | "world_record"
  k_age_match      int   age-match tolerance (default 0 — not UI-exposed)
  indices_loaded   tuple per-session memo of loaded (kind:value) index keys

Data flow: build_workout_view → for each ranked performance,
    * compute age_on_date → choose pool (exact age ±k or age_band)
    * rank_in_pool → (rank, total, percentile)
    * histogram_watts → distribution bin counts
    * get_records_for_age (when focus=world_record) → WR reference

See docs/rank_page.md for a fuller description.
"""

from __future__ import annotations

import hyperdiv as hd

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    age_from_dob,
    age_on_date,
    apply_best_only,
    apply_season_best_only,
    compute_watts,
    profile_complete,
    seasons_from,
    is_rankable_noninterval,
    apply_quality_filters,
    SEASON_PALETTE,
    season_color,
)
from services.formatters import fmt_split, format_time, fmt_distance
from services.concept2_records import (
    age_category,
    weight_class_str,
    get_records_for_age,
)
from services.concept2_rankings import (
    filter_matched_rankings,
    age_group_matched_rankings,
    rank_in_pool,
    histogram_watts,
    rankings_age_band,
    RankingModifiers,
)
from services.concept2_rankings_index import (
    iter_event_entries,
    load_event_slices,
)

from components.concept2_sync import get_all_workouts
from components.app_context import get_profile
from components.app_context import your
from components.rank_chart_plugin import RankChart
from components.rank_distribution import distribution_svg
from components.rank_ranking_modal import render_rankings_modal
from components.shared_ui import global_filter_ui, header_dropdown
from components.workout_table import WorkoutTable, ColumnDef

from services.volume_bins import swatch_svg


_INCLUDE_LABELS = {"SBs": "Season Bests", "PBs": "Personal Bests"}
_FOCUS_LABELS = {
    "c2_age_matched": "C2 Same Age Peers",
    "c2_age_group": "C2 Age Group Peers",
    "world_record": "World Record",
}

# Ordered list used by the chart x-axis (left → right).
_EVENT_ORDER: list[tuple[str, int, str]] = [
    ("dist", 100, "100m"),
    ("time", 600, "1 min"),
    ("dist", 500, "500m"),
    ("dist", 1000, "1k"),
    ("time", 2400, "4 min"),
    ("dist", 2000, "2k"),
    ("dist", 5000, "5k"),
    ("dist", 6000, "6k"),
    ("time", 18000, "30 min"),
    ("dist", 10000, "10k"),
    ("time", 36000, "60 min"),
    ("dist", 21097, "½ Marathon"),
    ("dist", 42195, "Marathon"),
]


def _event_key(etype: str, evalue: int) -> str:
    return f"{etype}:{evalue}"


def _fmt_ages(ages: list[int]) -> str:
    """Compress a sorted list of ages into 'n, m-p, q' style."""
    if not ages:
        return ""
    ages = sorted(set(ages))
    out: list[str] = []
    run_start = ages[0]
    prev = ages[0]
    for a in ages[1:]:
        if a == prev + 1:
            prev = a
            continue
        out.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
        run_start = a
        prev = a
    out.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
    return ", ".join(out)


def _palette_hsla(idx: int, light_offset: int = 0, alpha: float = 0.9) -> str:
    """Palette slot color for non-season series (PBs, WR pace/watts)."""
    h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
    return f"hsla({h},{s}%,{max(l + light_offset, 0)}%,{alpha:.2f})"


def _season_color_pair(season: str) -> tuple[str, str]:
    """(color, border_color) for a season, stable across filter changes."""
    return (
        season_color(season, alpha=0.9),
        season_color(season, lightness_offset=-10, alpha=1.0),
    )


def _weight_kg_from_profile(profile: dict) -> float:
    w = float(profile.get("weight") or 0)
    unit = profile.get("weight_unit", "kg")
    return w * 0.453592 if unit == "lbs" else w


def _rankings_weight_class(wr_class: str | None) -> str | None:
    """Translate WR-API weight class (Hwt/Lwt/None) to C2 rankings style (H/L/None)."""
    if wr_class == "Hwt":
        return "H"
    if wr_class == "Lwt":
        return "L"
    return None


# ──────────────────────────────────────────────────────────────────────────
# Cross-event name aggregates (lazy, module-global cache)
# ──────────────────────────────────────────────────────────────────────────
# Loading these scans every ranking index file — expensive (millions of rows)
# but cached for the process lifetime. Only computed when the user enables
# the corresponding modifier in the "additional filters" dropdown.

_NAME_AGG_CACHE: dict = {}


def _load_name_counts() -> dict:
    """Return {name: total_entries_across_all_events}. Cached process-wide."""
    if "counts" in _NAME_AGG_CACHE:
        return _NAME_AGG_CACHE["counts"]
    counts: dict = {}
    for et, ev, _ in _EVENT_ORDER:
        try:
            for e in iter_event_entries(et, ev):
                n = e.get("name")
                if n:
                    counts[n] = counts.get(n, 0) + 1
        except Exception:
            pass
    _NAME_AGG_CACHE["counts"] = counts
    return counts


def _load_name_event_sets(required: frozenset) -> dict:
    """Return {name: frozenset((kind, value))} for names appearing in the
    supplied required events. Cached per required-set."""
    key = ("events", tuple(sorted(required)))
    if key in _NAME_AGG_CACHE:
        return _NAME_AGG_CACHE[key]
    tmp: dict[str, set] = {}
    for et, ev in required:
        seen: set = set()
        try:
            for e in iter_event_entries(et, ev):
                n = e.get("name")
                if not n or n in seen:
                    continue
                seen.add(n)
                s = tmp.get(n)
                if s is None:
                    s = set()
                    tmp[n] = s
                s.add((et, ev))
        except Exception:
            pass
    frozen = {n: frozenset(s) for n, s in tmp.items()}
    _NAME_AGG_CACHE[key] = frozen
    return frozen


def _build_modifiers(state) -> RankingModifiers | None:
    """Construct a RankingModifiers from UI state, or None if all defaults."""
    must_have_raw = tuple(state.modifier_must_have_events or ())
    exclude_unv = bool(state.modifier_exclude_unverified)
    min_perf = int(state.modifier_min_performances or 1)

    if not must_have_raw and not exclude_unv and min_perf <= 1:
        return None

    must_have: frozenset = frozenset()
    name_event_sets = None
    if must_have_raw:
        parsed: set = set()
        for key in must_have_raw:
            try:
                k, v = key.split(":", 1)
                parsed.add((k, int(v)))
            except ValueError:
                continue
        must_have = frozenset(parsed)
        if must_have:
            name_event_sets = _load_name_event_sets(must_have)

    name_counts = None
    if min_perf > 1:
        name_counts = _load_name_counts()

    return RankingModifiers(
        must_have_event_kinds=must_have,
        exclude_unverified=exclude_unv,
        min_ranked_performances=min_perf,
        name_counts=name_counts,
        name_event_sets=name_event_sets,
    )


def _qualifying_performances(raw_workouts: list, *, best_filter: str) -> list:
    """Return the list of ranked performances (one per workout) to display."""
    quality = [w for w in raw_workouts if is_rankable_noninterval(w)]
    quality = apply_quality_filters(quality)
    # Keep only rows whose (etype, evalue) is in the ranked set.
    ranked = [w for w in quality if w["cat_key"] is not None]
    if best_filter == "PBs":
        return apply_best_only(ranked)
    return apply_season_best_only(ranked)


def _build_rows(
    qualifying: list,
    *,
    profile: dict,
    state,
) -> list[dict]:
    """Compute one enriched row dict per qualifying performance.

    Returns rows augmented with the rank / pool / histogram / WR fields the
    chart and data table will consume.
    """
    gender_api = "M" if profile.get("gender") == "Male" else "F"
    weight_kg = _weight_kg_from_profile(profile)
    dob = profile.get("dob", "")

    indices_cache: dict[tuple, list] = {}
    modifiers = _build_modifiers(state)

    # Determine how wide an age window each row's slice load needs to cover.
    # ``c2_age_matched`` widens the load by ``k_age_match`` so adjacent age
    # bands are included when the window crosses a band boundary.
    if state.ranking_focus == "c2_age_matched":
        load_k = int(state.k_age_match or 0)
    else:
        load_k = 0
    needs_entries = state.ranking_focus in ("c2_age_matched", "c2_age_group")

    rows: list[dict] = []
    for w in qualifying:
        ck = w["cat_key"]
        if ck is None:
            continue
        etype, evalue = ck
        d = w["date_dt"]
        age = age_on_date(dob, d) if dob else age_from_dob(dob)
        if age <= 0:
            continue
        w_class_wr = weight_class_str(weight_kg, gender_api, age)
        w_class = _rankings_weight_class(w_class_wr)
        # Convert the workout's performance into value_tenths matching the
        # index schema.
        if etype == "dist":
            value_tenths = w.get("time")
        else:
            value_tenths = w.get("distance")
        if not value_tenths:
            continue

        ev_key = _event_key(etype, evalue)
        row_id = f"{ev_key}|{w.get('id') or w.get('date', '')}"
        cache_key = (etype, evalue, age, w_class)
        entries = indices_cache.get(cache_key)
        if entries is None:
            if needs_entries:
                try:
                    entries = (
                        load_event_slices(
                            etype,
                            evalue,
                            gender=gender_api,
                            target_age=age,
                            k=load_k,
                            weight=w_class,
                        )
                        or []
                    )
                except Exception:
                    entries = []
            else:
                entries = []
            indices_cache[cache_key] = entries

        pace = w["pace"]
        watts = w["watts"]

        row = {
            "id": row_id,
            "workout": w,
            "event_kind": etype,
            "event_value": evalue,
            "event_label": _event_label(etype, evalue),
            "event_key": ev_key,
            "date_label": d.strftime("%b %d, %Y") if d.toordinal() > 1 else "",
            "date_iso": w["date"],
            "age": age,
            "age_band_canonical": age_category(age),
            "age_band_rankings": rankings_age_band(age),
            "weight_class": w_class,
            "gender": gender_api,
            "value_tenths": value_tenths,
            "pace_tenths": int(round(pace * 10)) if pace else None,
            "watts": watts,
            "season": w["season"],
            "entries": entries,
        }

        # Per-focus analytics.
        if state.ranking_focus == "c2_age_matched":
            pool = filter_matched_rankings(
                entries,
                target_age=age,
                k=state.k_age_match,
                gender=gender_api,
                weight_class=w_class,
                modifiers=modifiers,
            )
        elif state.ranking_focus == "c2_age_group":
            pool = age_group_matched_rankings(
                entries,
                age_band=rankings_age_band(age),
                gender=gender_api,
                weight_class=w_class,
                modifiers=modifiers,
            )
        else:
            pool = []

        row["pool"] = pool
        if pool:
            rank, total, pct = rank_in_pool(pool, value_tenths, etype)
            hist, wmin, wmax = histogram_watts(pool, etype, evalue)
            row.update(
                {
                    "rank": rank,
                    "rank_total": total,
                    "percentile": pct,
                    "hist_counts": hist,
                    "hist_min": wmin,
                    "hist_max": wmax,
                }
            )
        else:
            row.update(
                {
                    "rank": 0,
                    "rank_total": 0,
                    "percentile": 0.0,
                    "hist_counts": [],
                    "hist_min": 0.0,
                    "hist_max": 0.0,
                }
            )

        if state.ranking_focus == "world_record":
            wr = get_records_for_age(gender_api, age, weight_kg)
            rec = wr.get((etype, evalue))
            if rec is not None:
                # WR metadata dict; ``result`` is seconds (dist) or meters (time).
                rec_val = rec["result"] if isinstance(rec, dict) else rec
                if etype == "dist":
                    wr_time_s = float(rec_val)
                    wr_pace = wr_time_s / (evalue / 500.0)
                    wr_watts = compute_watts(wr_pace)
                    user_time_s = value_tenths / 10.0
                    row["wr_pct_pace"] = (
                        100.0 * wr_time_s / user_time_s if user_time_s else 0.0
                    )
                    row["wr_pct_watts"] = (
                        100.0 * (watts / wr_watts) if (watts and wr_watts) else 0.0
                    )
                    row["wr_pace"] = wr_pace
                    row["wr_watts"] = wr_watts
                    row["wr_value"] = wr_time_s
                else:
                    wr_meters = float(rec_val)
                    dur_s = evalue / 10.0
                    wr_pace = dur_s / (wr_meters / 500.0)
                    wr_watts = compute_watts(wr_pace)
                    row["wr_pct_pace"] = (
                        100.0 * (value_tenths / wr_meters) if wr_meters else 0.0
                    )
                    row["wr_pct_watts"] = (
                        100.0 * (watts / wr_watts) if (watts and wr_watts) else 0.0
                    )
                    row["wr_pace"] = wr_pace
                    row["wr_watts"] = wr_watts
                    row["wr_value"] = wr_meters

        rows.append(row)
    return rows


def _event_label(etype: str, evalue: int) -> str:
    for et, ev, lbl in _EVENT_ORDER:
        if et == etype and ev == evalue:
            return lbl
    return f"{etype}:{evalue}"


def _build_series(rows: list[dict], state) -> tuple[list, list]:
    """Return (event_order_prop, series_prop) for the RankChart."""
    event_order = [
        {"key": _event_key(et, ev), "label": lbl} for et, ev, lbl in _EVENT_ORDER
    ]

    if state.ranking_focus == "world_record":
        return event_order, _build_wr_series(rows, state)

    # Percentile series: one per season for SBs, one combined for PBs.
    if state.include_filter == "PBs":
        ages = [r["age"] for r in rows]
        pts = [_pct_point(r) for r in rows if r.get("rank_total")]
        return event_order, [
            {
                "label": f"Personal Bests · Ages {_fmt_ages(ages)}",
                "color": _palette_hsla(0, 0, 0.9),
                "border_color": _palette_hsla(0, -10, 1.0),
                "points": pts,
            }
        ]

    # SBs — group by season.
    by_season: dict[str, list[dict]] = {}
    season_ages: dict[str, list[int]] = {}
    for r in rows:
        if not r.get("rank_total"):
            continue
        by_season.setdefault(r["season"], []).append(r)
        season_ages.setdefault(r["season"], []).append(r["age"])
    seasons_sorted = sorted(by_season.keys())
    series = []
    for s in seasons_sorted:
        ages = season_ages.get(s, [])
        label = f"{s} · Ages {_fmt_ages(ages)}" if ages else s
        pts = [_pct_point(r) for r in by_season[s]]
        color, border = _season_color_pair(s)
        series.append(
            {
                "label": label,
                "color": color,
                "border_color": border,
                "points": pts,
            }
        )
    return event_order, series


def _duration_seconds(r: dict) -> float:
    """Return the physical duration of this performance in seconds.

    For ``time`` events that's the fixed event window. For ``dist`` events
    that's the user's actual elapsed time, so per-row positions on the log
    x-axis honestly reflect effort length.
    """
    if r["event_kind"] == "time":
        return r["event_value"] / 10.0
    vt = r.get("value_tenths") or 0
    return vt / 10.0


def _pct_point(r: dict) -> dict:
    """Point payload for percentile-mode series."""
    return {
        "x": _duration_seconds(r),
        "y": round(r["percentile"], 2),
        "event": r["event_label"],
        "date": r["date_label"],
        "age": r["age"],
        "rank": r["rank"],
        "rank_total": r["rank_total"],
        "percentile": round(r["percentile"], 2),
    }


def _wr_point(r: dict, *, kind: str) -> dict:
    """Point payload for WR-mode series. ``kind`` is "pace" or "watts"."""
    val = r["wr_pct_pace"] if kind == "pace" else r["wr_pct_watts"]
    return {
        "x": _duration_seconds(r),
        "y": round(val, 2),
        "event": r["event_label"],
        "date": r["date_label"],
        "age": r["age"],
        "wr_pct": round(val, 2),
        "wr_kind": kind,
    }


def _build_wr_series(rows: list[dict], state) -> list:
    """Build chart series for the world_record focus."""
    if state.include_filter == "PBs":
        ages = [r["age"] for r in rows]
        wr_rows = [r for r in rows if "wr_pct_pace" in r]
        return [
            {
                "label": f"% of WR pace · Ages {_fmt_ages(ages)}",
                "color": _palette_hsla(0, 0, 0.9),
                "border_color": _palette_hsla(0, -10, 1.0),
                "points": [_wr_point(r, kind="pace") for r in wr_rows],
            },
            {
                "label": f"% of WR watts · Ages {_fmt_ages(ages)}",
                "color": _palette_hsla(1, 0, 0.9),
                "border_color": _palette_hsla(1, -10, 1.0),
                "points": [_wr_point(r, kind="watts") for r in wr_rows],
            },
        ]

    # SBs — one series per season (using % of WR pace as the y-value).
    by_season: dict[str, list[dict]] = {}
    season_ages: dict[str, list[int]] = {}
    for r in rows:
        if "wr_pct_pace" not in r:
            continue
        by_season.setdefault(r["season"], []).append(r)
        season_ages.setdefault(r["season"], []).append(r["age"])
    seasons_sorted = sorted(by_season.keys())
    series = []
    for s in seasons_sorted:
        ages = season_ages.get(s, [])
        label = f"{s} · Ages {_fmt_ages(ages)}"
        pts = [_wr_point(r, kind="pace") for r in by_season[s]]
        color, border = _season_color_pair(s)
        series.append(
            {
                "label": label,
                "color": color,
                "border_color": border,
                "points": pts,
            }
        )
    return series


# ──────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────


def rank_page() -> None:
    """Top-level Rank Page component. Called from app.py."""
    sync = get_all_workouts()
    profile = get_profile()

    if sync is None or profile is None:
        hd.box(padding=2, min_height="80vh")
        return

    workouts_dict, sorted_workouts = sync

    if not profile_complete(profile):
        with hd.box(align="center", padding=4, gap=1):
            hd.text(
                "Fill in your profile to see rankings.",
                font_size="large",
                font_weight="bold",
            )
            hd.link("Open profile", href="/profile")
        return

    state = hd.state(
        include_filter="SBs",
        ranking_focus="c2_age_matched",
        k_age_match=0,
        modifier_must_have_events=(),
        modifier_exclude_unverified=False,
        modifier_min_performances=1,
    )

    qualifying = _qualifying_performances(
        sorted_workouts,
        best_filter=state.include_filter,
    )
    rows = _build_rows(qualifying, profile=profile, state=state)
    _annotate_display_metadata(rows)

    event_order_prop, series_prop = _build_series(rows, state)

    with hd.box(align="center", gap=2, padding=2, min_height="80vh"):
        # ── Heading ─────────────────────────────────────────────────────────
        with hd.box(gap=0.2, align="center"):
            with hd.h1(font_weight="normal"):
                with hd.hbox(gap=0.2, align="center", wrap="wrap"):
                    hd.text(f"{your()}")
                    header_dropdown(
                        state,
                        key="inc_dd",
                        labels=_INCLUDE_LABELS,
                        current_value=state.include_filter,
                        field="include_filter",
                    )
                    hd.text("vs.")
                    header_dropdown(
                        state,
                        key="focus_dd",
                        labels=_FOCUS_LABELS,
                        current_value=state.ranking_focus,
                        field="ranking_focus",
                    )
            global_filter_ui()

        # ── Chart ──────────────────────────────────────────────────────────
        y_label = (
            "% of World Record"
            if state.ranking_focus == "world_record"
            else "Percentile (higher = better)"
        )
        y_mode = "pct" if state.ranking_focus == "world_record" else "percentile"

        if not rows:
            with hd.box(align="center", padding=2):
                hd.text(
                    "No qualifying performances in the selected filter.",
                    font_color="neutral-500",
                )
        else:
            RankChart(
                event_order=event_order_prop,
                series=series_prop,
                y_label=y_label,
                y_mode=y_mode,
                is_dark=hd.theme().is_dark,
                height_css="55vh",
                width="100%",
            )

        # ── Legend ──────────────────────────────────────────────────────────
        if series_prop:
            with hd.hbox(gap=1, wrap="wrap", align="center"):
                for i, s in enumerate(series_prop):
                    with hd.scope(f"legend_{i}"):
                        with hd.hbox(gap=0.3, align="center"):
                            hd.image(
                                src=swatch_svg(s["color"], size=12, radius=2),
                                width=0.8,
                                height=0.8,
                            )
                            hd.text(s["label"], font_size="small")

        # ── Age-matched explainer ──────────────────────────────────────────
        if state.ranking_focus in ("c2_age_matched", "c2_age_group"):
            _render_c2_comparison_explainer(state)

        # ── Esoteric filters (c2_* focus only) ─────────────────────────────
        if state.ranking_focus in ("c2_age_matched", "c2_age_group"):
            _render_filters_dropdown(state)

        # ── Data table ──────────────────────────────────────────────────────
        _render_table(rows, state)


_EVENT_ORDER_IDX = {_event_key(et, ev): i for i, (et, ev, _) in enumerate(_EVENT_ORDER)}


def _annotate_display_metadata(rows: list[dict]) -> None:
    """Stash cross-row display values used by cell renderers.

    * ``_rank_chars`` / ``_total_chars`` — max widths (in characters) across
      all rows with a rank, so the "X of Y" cell can right-align uniformly.
    * ``_dom_min`` / ``_dom_max`` — shared watts domain for the distribution
      column so histograms of different events share an x-axis.
    """
    ranked = [r for r in rows if r.get("rank_total")]
    if ranked:
        rank_chars = max(len(f"{r['rank']:,}") for r in ranked)
        total_chars = max(len(f"{r['rank_total']:,}") for r in ranked)
    else:
        rank_chars = total_chars = 1

    with_hist = [
        r
        for r in rows
        if r.get("hist_counts") and r.get("hist_max") > r.get("hist_min", 0)
    ]
    if with_hist:
        dom_min = min(r["hist_min"] for r in with_hist)
        dom_max = max(r["hist_max"] for r in with_hist)
        if dom_max <= dom_min:
            dom_max = dom_min + 1.0
        # Include each row's user watts so the marker stays in-range.
        for r in with_hist:
            w = r.get("watts")
            if w is None:
                continue
            if w < dom_min:
                dom_min = w
            if w > dom_max:
                dom_max = w
    else:
        dom_min = dom_max = 0.0

    for r in rows:
        r["_rank_chars"] = rank_chars
        r["_total_chars"] = total_chars
        r["_dom_min"] = dom_min
        r["_dom_max"] = dom_max


def _render_result_cell(r: dict) -> None:
    vt = r.get("value_tenths") or 0
    txt = format_time(vt) if r["event_kind"] == "dist" else fmt_distance(vt)
    hd.text(txt, font_size="small")


def _render_rank_cell(r: dict) -> None:
    if not r.get("rank_total"):
        hd.text("—", font_size="small", font_color="neutral-400")
        return
    rank_w = f"{7 * max(1, r.get('_rank_chars', 1))}px"
    total_w = f"{7 * max(1, r.get('_total_chars', 1))}px"
    rank_s = f"{r['rank']:,}"
    total_s = f"{r['rank_total']:,}"
    with hd.button(
        size="small",
        variant="text",
        padding=(0, 0.3),
    ) as btn:
        with hd.hbox(gap=0.3, align="center", justify="center"):
            hd.text(
                rank_s,
                width=rank_w,
                text_align="end",
                font_size="small",
                font_family="mono",
            )
            hd.text("of", font_size="x-small", padding_left=0.3)
            hd.text(
                total_s,
                width=total_w,
                text_align="end",
                font_size="small",
                font_family="mono",
            )
    title_parts = [r["event_label"], f"Age {r['age']}"]
    if r["date_label"]:
        title_parts.append(r["date_label"])
    wc = r["weight_class"]
    title_parts.append(f"{r['gender']} {wc}" if wc else r["gender"])
    dlg = hd.dialog(
        " · ".join(title_parts),
        panel_style=hd.style(width="1000px", max_width="95vw"),
    )
    if btn.clicked:
        dlg.opened = True
    if dlg.opened:
        render_rankings_modal(
            dlg,
            pool=r["pool"],
            event_kind=r["event_kind"],
            event_value=r["event_value"],
            user_rank=r["rank"],
            user_value_tenths=r["value_tenths"],
            user_row_label=f"You — {r['date_label']}",
            user_age=r["age"],
            user_date_label=r["date_label"],
            user_season=r["season"],
        )


def _render_percentile_cell(r: dict) -> None:
    if not r.get("rank_total"):
        hd.text("—", font_size="small", font_color="neutral-400")
        return
    pct = r["percentile"]
    whole = int(pct)
    tenth = int(round((pct - whole) * 10))
    if tenth >= 10:
        whole += 1
        tenth = 0
    with hd.hbox(gap=0, align="start", justify="center"):
        hd.text(
            f"{whole}",
            font_size="large",
            font_weight="semibold",
        )
        hd.text(
            f".{tenth}",
            font_size="x-small",
            font_color="neutral-500",
            padding_top=0.1,
        )


def _render_distribution_cell(r: dict) -> None:
    if r.get("hist_counts") and r.get("watts"):
        dom_min = r.get("_dom_min") or r["hist_min"]
        dom_max = r.get("_dom_max") or r["hist_max"]
        uri = distribution_svg(
            r["hist_counts"],
            float(r["watts"]),
            r["hist_min"],
            r["hist_max"],
            x_min=dom_min,
            x_max=dom_max,
            is_dark=hd.theme().is_dark,
        )
        with hd.box(width="100%"):
            hd.image(src=uri, width="100%", height="32px")
    else:
        hd.text("—", font_size="small", font_color="neutral-400")


def _columns_for(focus: str) -> list[ColumnDef]:
    col_event = ColumnDef(
        key="event",
        header="Event",
        width="7rem",
        render_value=lambda r: r["event_label"],
        sort_value=lambda r: _EVENT_ORDER_IDX.get(r["event_key"], 99),
        default_asc=True,
    )
    col_date = ColumnDef(
        key="date",
        header="Date",
        width="9rem",
        render_value=lambda r: r["date_label"],
        sort_value=lambda r: r["date_iso"],
    )
    col_age = ColumnDef(
        key="age",
        header="Age",
        width="4rem",
        render_value=lambda r: str(r["age"]),
        sort_value=lambda r: r["age"],
    )
    col_age_group = ColumnDef(
        key="age_group",
        header="Age Group",
        width="6rem",
        render_value=lambda r: r["age_band_rankings"],
        sort_value=lambda r: r["age_band_rankings"],
    )
    col_result = ColumnDef(
        key="result",
        header="Result",
        width="7rem",
        render_cell=_render_result_cell,
        sort_value=lambda r: r["value_tenths"] or 0,
        align="end",
    )
    col_pace = ColumnDef(
        key="pace",
        header="Pace",
        width="6rem",
        render_value=lambda r: (
            fmt_split(r["pace_tenths"]) if r.get("pace_tenths") else "—"
        ),
        sort_value=lambda r: r.get("pace_tenths") or float("inf"),
        default_asc=True,
    )
    col_watts = ColumnDef(
        key="watts",
        header="Watts",
        width="5rem",
        render_value=lambda r: f"{r['watts']:.0f}" if r.get("watts") else "—",
        sort_value=lambda r: r.get("watts") or 0,
    )

    if focus == "world_record":
        col_pct_pace = ColumnDef(
            key="wr_pct_pace",
            header="% WR Pace",
            width="6rem",
            render_value=lambda r: (
                f"{r['wr_pct_pace']:.1f}%" if "wr_pct_pace" in r else "—"
            ),
            sort_value=lambda r: r.get("wr_pct_pace") or 0,
        )
        col_pct_watts = ColumnDef(
            key="wr_pct_watts",
            header="% WR Watts",
            width="6rem",
            render_value=lambda r: (
                f"{r['wr_pct_watts']:.1f}%" if "wr_pct_watts" in r else "—"
            ),
            sort_value=lambda r: r.get("wr_pct_watts") or 0,
        )
        col_wr_pace = ColumnDef(
            key="wr_pace",
            header="WR Pace",
            width="6rem",
            render_value=lambda r: (
                fmt_split(int(round(r["wr_pace"] * 10))) if r.get("wr_pace") else "—"
            ),
            sort_value=lambda r: r.get("wr_pace") or float("inf"),
            default_asc=True,
        )
        return [
            col_event,
            col_date,
            col_age,
            col_result,
            col_pace,
            col_watts,
            col_pct_pace,
            col_pct_watts,
            col_wr_pace,
        ]

    col_rank = ColumnDef(
        key="rank",
        header="Rank",
        width="9rem",
        render_cell=_render_rank_cell,
        sort_value=lambda r: r.get("rank") or 10**9,
        default_asc=True,
    )
    col_pct = ColumnDef(
        key="percentile",
        header="%ile",
        width="5rem",
        render_cell=_render_percentile_cell,
        sort_value=lambda r: r.get("percentile") or 0,
    )
    col_dist = ColumnDef(
        key="distribution",
        header="Watts Distribution",
        width="minmax(10rem,1fr)",
        render_cell=_render_distribution_cell,
        sortable=False,
    )

    if focus == "c2_age_group":
        return [
            col_event,
            col_date,
            col_age,
            col_age_group,
            col_result,
            col_pace,
            col_watts,
            col_rank,
            col_pct,
            col_dist,
        ]
    return [
        col_event,
        col_date,
        col_age,
        col_result,
        col_pace,
        col_watts,
        col_rank,
        col_pct,
        col_dist,
    ]


def _render_table(rows: list[dict], state) -> None:
    if not rows:
        return
    columns = _columns_for(state.ranking_focus)
    WorkoutTable(
        rows,
        columns,
        default_sort_col="event",
        default_sort_asc=True,
    )


def _render_c2_comparison_explainer(state) -> None:
    """Small muted-box explainer shown above the filters for age-matched focus."""

    k = int(state.k_age_match or 0)
    tolerance = "at exactly your age" if k == 0 else f"within ±{k} years of your age"
    with hd.box(
        padding=1,
        background_color="primary-50",
        border_radius="medium",
        border="1px solid primary-100",
        max_width="700px",
        width="100%",
        align="center",
    ):
        if state.ranking_focus == "c2_age_matched":
            hd.text(
                "Comparing to your Same Age Peers is different from what you see on the Concept2 logbook.",
                font_size="medium",
                font_weight="semibold",
                font_color="primary-700",
            )
            hd.text(
                f"Taking all leaderboard entries back to 2002, each of your performances is ranked against all "
                "the entries by people who were your exact age at the time you recorded the respective performance.",
                font_size="small",
                font_color="neutral-700",
            )
        else:
            hd.text(
                "Comparing to your Age Group Peers is different from what you see on the Concept2 logbook.",
                font_size="medium",
                font_weight="semibold",
                font_color="primary-700",
            )
            hd.text(
                f"Taking all leaderboard entries back to 2002, each of your performances is ranked against your "
                "respective age group at the time of your performance.",
                font_size="small",
                font_color="neutral-700",
            )


def _render_filters_dropdown(state) -> None:
    """Render the additional-filters dropdown.

    Controls ``modifier_must_have_events`` (event checklist), ``modifier_exclude_unverified``
    (checkbox), and ``modifier_min_performances`` (radio 1/5/10/20).
    """
    active_count = (
        (1 if state.modifier_exclude_unverified else 0)
        + (1 if int(state.modifier_min_performances or 1) > 1 else 0)
        + (1 if state.modifier_must_have_events else 0)
    )
    trigger_label = (
        "Additional filters on leaderboard results"
        if active_count == 0
        else f"Additional filters on leaderboard results — {active_count} active"
    )

    with hd.box(width="100%", max_width="92vw", align="start"):
        with hd.dropdown() as dd:
            btn = hd.button(
                trigger_label,
                caret=True,
                size="small",
                variant="text",
                slot=dd.trigger,
                font_size="small",
                font_weight="semibold" if active_count else "normal",
                font_color="neutral-700",
            )
            if btn.clicked:
                dd.opened = not dd.was_opened

            with hd.box(
                padding=1,
                gap=1,
                background_color="neutral-50",
                min_width=22,
            ):
                with hd.box(gap=0.5):
                    # ── Min performances ─────────────────────────────────────
                    hd.text(
                        "Minimum ranked performances per rower:",
                        font_size="small",
                        font_weight="semibold",
                        font_color="neutral-700",
                    )
                    with hd.hbox(gap=0.3, wrap="wrap", padding_left=0.5):
                        for n in (1, 2, 5, 10, 20):
                            with hd.scope(f"mp_{n}"):
                                is_active = (
                                    int(state.modifier_min_performances or 1) == n
                                )
                                label = "Any" if n == 1 else f"≥ {n}"
                                if hd.button(
                                    label,
                                    size="small",
                                    variant="primary" if is_active else "text",
                                ).clicked:
                                    state.modifier_min_performances = n

                hd.divider()

                # ── Must-have events ─────────────────────────────────────
                with hd.box(gap=0.5):
                    hd.text(
                        "Only include leaderboard entries from rowers who have also ranked in:",
                        font_size="small",
                        font_color="neutral-700",
                    )
                    with hd.box(gap=0.25, padding_left=0.5):
                        current = set(state.modifier_must_have_events or ())
                        for et, ev, lbl in _EVENT_ORDER:
                            key = _event_key(et, ev)
                            with hd.scope(f"mh_{key}"):
                                cb = hd.checkbox(lbl, checked=key in current)
                                if cb.changed:
                                    new = set(current)
                                    if cb.checked:
                                        new.add(key)
                                    else:
                                        new.discard(key)
                                    state.modifier_must_have_events = tuple(sorted(new))

                hd.divider()

                # ── Exclude unverified ───────────────────────────────────
                cb = hd.checkbox(
                    "Exclude unverified performances",
                    checked=state.modifier_exclude_unverified,
                )
                if cb.changed:
                    state.modifier_exclude_unverified = cb.checked

                # ── Reset ────────────────────────────────────────────────
                if active_count:
                    with hd.hbox(justify="end", padding_top=0.5):
                        if hd.button(
                            "Clear filters",
                            size="small",
                            variant="text",
                        ).clicked:
                            state.modifier_must_have_events = ()
                            state.modifier_exclude_unverified = False
                            state.modifier_min_performances = 1
