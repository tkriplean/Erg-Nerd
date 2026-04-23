"""
Rank Page — "how do I rank?" view comparing a user's ranked performances
against the world record or the Concept2 rankings field.

Exported:
    rank_page(ctx, excluded_seasons=(), machine="All")
        Top-level HyperDiv component; call from app.py.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UI LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Heading (inline dropdowns):
      "Your [Season Bests ▾] Against [C2 Age-Matched Rankings ▾]"

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
    compute_pace,
    compute_watts,
    get_season,
    parse_date,
    profile_complete,
    seasons_from,
    is_rankable_noninterval,
    apply_quality_filters,
    SEASON_PALETTE,
    season_color,
    workout_cat_key,
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
)
from services.concept2_rankings_index import load_event_index

from components.concept2_sync import sync_from_context
from components.profile_page import get_profile_from_context
from components.view_context import your
from components.rank_chart_plugin import RankChart
from components.rank_distribution import distribution_svg
from components.rank_ranking_modal import render_rankings_modal
from components.shared_ui import global_filter_ui, header_dropdown

from services.volume_bins import swatch_svg


_INCLUDE_LABELS = {"SBs": "Season Bests", "PBs": "Personal Bests"}
_FOCUS_LABELS = {
    "c2_age_matched": "C2 Age-Matched Peers",
    "c2_age_group": "C2 Age-Group Peers",
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


def _qualifying_performances(
    raw_workouts: list, *, machine: str, excluded_seasons: tuple, best_filter: str
) -> list:
    """Return the list of ranked performances (one per workout) to display."""
    excl = set(excluded_seasons)
    quality = [
        w
        for w in raw_workouts
        if (machine == "All" or w.get("type") == machine)
        and is_rankable_noninterval(w)
        and get_season(w.get("date", "")) not in excl
    ]
    quality = apply_quality_filters(quality)
    # Keep only rows whose (etype, evalue) is in the ranked set.
    ranked = [w for w in quality if workout_cat_key(w) is not None]
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

    indices_cache: dict[str, list] = {}

    rows: list[dict] = []
    for w in qualifying:
        ck = workout_cat_key(w)
        if ck is None:
            continue
        etype, evalue = ck
        d = parse_date(w.get("date", ""))
        age = age_on_date(dob, d) if dob else age_from_dob(dob)
        if age <= 0:
            continue
        w_class = weight_class_str(weight_kg, gender_api, age)
        # Convert the workout's performance into value_tenths matching the
        # index schema.
        if etype == "dist":
            value_tenths = w.get("time")
        else:
            value_tenths = w.get("distance")
        if not value_tenths:
            continue

        ev_key = _event_key(etype, evalue)
        entries = indices_cache.get(ev_key)
        if entries is None:
            try:
                entries = load_event_index(etype, evalue) or []
            except Exception:
                entries = []
            indices_cache[ev_key] = entries

        pace = compute_pace(w)
        watts = compute_watts(pace) if pace else None

        row = {
            "workout": w,
            "event_kind": etype,
            "event_value": evalue,
            "event_label": _event_label(etype, evalue),
            "event_key": ev_key,
            "date_label": d.strftime("%b %d, %Y") if d.toordinal() > 1 else "",
            "date_iso": w.get("date", ""),
            "age": age,
            "age_band_canonical": age_category(age),
            "age_band_rankings": rankings_age_band(age),
            "weight_class": w_class,
            "gender": gender_api,
            "value_tenths": value_tenths,
            "pace_tenths": int(round(pace * 10)) if pace else None,
            "watts": watts,
            "season": get_season(w.get("date", "")),
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
            )
        elif state.ranking_focus == "c2_age_group":
            pool = age_group_matched_rankings(
                entries,
                age_band=rankings_age_band(age),
                gender=gender_api,
                weight_class=w_class,
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
                # WR stores seconds for dist, meters for time.
                if etype == "dist":
                    wr_time_s = float(rec)
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
                    wr_meters = float(rec)
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
        pts = []
        for r in rows:
            if not r.get("rank_total"):
                continue
            pts.append(
                {
                    "x_key": r["event_key"],
                    "y": round(r["percentile"], 2),
                    "tooltip": (
                        f"{r['event_label']} · {r['date_label']} · Age {r['age']}"
                        f" · rank {r['rank']:,} of {r['rank_total']:,}"
                        f" · {r['percentile']:.1f}%ile"
                    ),
                }
            )
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
        pts = [
            {
                "x_key": r["event_key"],
                "y": round(r["percentile"], 2),
                "tooltip": (
                    f"{r['event_label']} · {r['date_label']} · Age {r['age']}"
                    f" · rank {r['rank']:,} of {r['rank_total']:,}"
                    f" · {r['percentile']:.1f}%ile"
                ),
            }
            for r in by_season[s]
        ]
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


def _build_wr_series(rows: list[dict], state) -> list:
    """Build chart series for the world_record focus."""
    if state.include_filter == "PBs":
        ages = [r["age"] for r in rows]
        pts_pace = []
        pts_watts = []
        for r in rows:
            if "wr_pct_pace" not in r:
                continue
            pts_pace.append(
                {
                    "x_key": r["event_key"],
                    "y": round(r["wr_pct_pace"], 2),
                    "tooltip": (
                        f"{r['event_label']} · {r['date_label']} · {r['wr_pct_pace']:.1f}% of WR pace"
                    ),
                }
            )
            pts_watts.append(
                {
                    "x_key": r["event_key"],
                    "y": round(r["wr_pct_watts"], 2),
                    "tooltip": (
                        f"{r['event_label']} · {r['date_label']} · {r['wr_pct_watts']:.1f}% of WR watts"
                    ),
                }
            )
        return [
            {
                "label": f"% of WR pace · Ages {_fmt_ages(ages)}",
                "color": _palette_hsla(0, 0, 0.9),
                "border_color": _palette_hsla(0, -10, 1.0),
                "points": pts_pace,
            },
            {
                "label": f"% of WR watts · Ages {_fmt_ages(ages)}",
                "color": _palette_hsla(1, 0, 0.9),
                "border_color": _palette_hsla(1, -10, 1.0),
                "points": pts_watts,
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
        pts = [
            {
                "x_key": r["event_key"],
                "y": round(r["wr_pct_pace"], 2),
                "tooltip": (
                    f"{r['event_label']} · {r['date_label']} · {r['wr_pct_pace']:.1f}% of WR pace"
                ),
            }
            for r in by_season[s]
        ]
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


def rank_page(
    ctx, global_state, excluded_seasons: tuple = (), machine: str = "All"
) -> None:
    """Top-level Rank Page component. Called from app.py."""
    sync = sync_from_context(ctx)
    profile = get_profile_from_context(ctx)

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
    )

    qualifying = _qualifying_performances(
        list(workouts_dict.values()),
        machine=machine,
        excluded_seasons=tuple(excluded_seasons),
        best_filter=state.include_filter,
    )
    rows = _build_rows(qualifying, profile=profile, state=state)

    event_order_prop, series_prop = _build_series(rows, state)

    with hd.box(align="center", gap=2, padding=2, min_height="80vh"):
        # ── Heading ─────────────────────────────────────────────────────────
        with hd.box(gap=0.2, align="center"):
            with hd.h1(font_weight="normal"):
                with hd.hbox(gap=0.2, align="center", wrap="wrap"):
                    hd.text(f"{your(ctx)}")
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
            global_filter_ui(global_state, ctx)

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

        # ── Data table ──────────────────────────────────────────────────────
        _render_table(rows, state)


def _render_table(rows: list[dict], state) -> None:
    if not rows:
        return
    focus = state.ranking_focus

    # Header
    with hd.box(
        direction="horizontal",
        padding=0.3,
        background_color="neutral-50",
        font_weight="bold",
        font_size="small",
        width="100%",
    ):
        hd.text("Event", width="6rem")
        hd.text("Date", width="8rem")
        hd.text("Age", width="3rem")
        if focus == "c2_age_group":
            hd.text("Age Group", width="6rem")
        hd.text("Result", width="7rem")
        hd.text("Pace", width="6rem")
        hd.text("Watts", width="5rem")
        if focus == "world_record":
            hd.text("% WR Pace", width="6rem")
            hd.text("% WR Watts", width="6rem")
            hd.text("WR Pace", width="6rem")
        else:
            hd.text("Rank", width="9rem")
            hd.text("%ile", width="5rem")
            hd.text("Distribution", width="10rem")

    # Sort rows by event_order then date.
    order_idx = {_event_key(et, ev): i for i, (et, ev, _) in enumerate(_EVENT_ORDER)}
    rows_sorted = sorted(
        rows, key=lambda r: (order_idx.get(r["event_key"], 99), r["date_iso"])
    )

    for i, r in enumerate(rows_sorted):
        with hd.scope(f"row_{i}"):
            _render_row(r, state, focus)


def _render_row(r: dict, state, focus: str) -> None:
    etype = r["event_kind"]
    evalue = r["event_value"]
    value_tenths = r["value_tenths"]
    result = (
        format_time(value_tenths) if etype == "dist" else fmt_distance(value_tenths)
    )
    pace = fmt_split(r.get("pace_tenths")) if r.get("pace_tenths") else "—"
    watts = f"{r['watts']:.0f}" if r.get("watts") else "—"

    with hd.box(
        direction="horizontal",
        padding=0.3,
        font_size="small",
        width="100%",
        border_bottom="1px solid neutral-100",
    ):
        hd.text(r["event_label"], width="6rem")
        hd.text(r["date_label"], width="8rem")
        hd.text(str(r["age"]), width="3rem")
        if focus == "c2_age_group":
            hd.text(r["age_band_rankings"], width="6rem")
        hd.text(result, width="7rem")
        hd.text(pace, width="6rem")
        hd.text(watts, width="5rem")

        if focus == "world_record":
            p_pace = f"{r['wr_pct_pace']:.1f}%" if "wr_pct_pace" in r else "—"
            p_watts = f"{r['wr_pct_watts']:.1f}%" if "wr_pct_watts" in r else "—"
            wr_pace_disp = (
                fmt_split(int(round(r["wr_pace"] * 10))) if r.get("wr_pace") else "—"
            )
            hd.text(p_pace, width="6rem")
            hd.text(p_watts, width="6rem")
            hd.text(wr_pace_disp, width="6rem")
        else:
            if r.get("rank_total"):
                with hd.box(width="9rem"):
                    btn = hd.button(
                        f"{r['rank']:,} of {r['rank_total']:,}",
                        size="small",
                        variant="text",
                        font_size="small",
                    )
                    dlg = hd.dialog(
                        f"{r['event_label']} · Age {r['age']} "
                        f"· {r['gender']} {r['weight_class'] or ''}"
                    )
                    if btn.clicked:
                        dlg.opened = True
                    if dlg.opened:
                        render_rankings_modal(
                            dlg,
                            pool=r["pool"],
                            event_kind=etype,
                            event_value=evalue,
                            user_rank=r["rank"],
                            user_value_tenths=value_tenths,
                            user_row_label=f"You — {r['date_label']}",
                            user_age=r["age"],
                            user_date_label=r["date_label"],
                        )
            else:
                hd.text("—", width="9rem")
            pct_txt = f"{r['percentile']:.1f}" if r.get("rank_total") else "—"
            hd.text(pct_txt, width="5rem")
            if r.get("hist_counts") and r.get("watts"):
                uri = distribution_svg(
                    r["hist_counts"],
                    float(r["watts"]),
                    r["hist_min"],
                    r["hist_max"],
                    is_dark=hd.theme().is_dark,
                )
                hd.image(src=uri, width=140, height=32)
            else:
                hd.text("—", width="10rem")
