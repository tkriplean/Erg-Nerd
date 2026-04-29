"""
components/workout_table.py — CSS Grid-based sortable data table.

Exported:
  WorkoutTable()    — HyperDiv component: renders a sortable, paginated table
  COLUMN_REGISTRY   — dict mapping column key → ColumnDef (the catalog of
                      every column the app can render)
  ColumnDef         — dataclass describing one registry entry

Shared cell-renderer helpers (also used outside the table — e.g. by
workout_page summary cells):
  always_white(is_dark)               — Shoelace neutral token reading white
  render_spread_cell(...)             — score + zone-bar + tooltip cell
  render_quality_cell(...)            — colored quality pill with tooltip

Design
------
Consumers no longer build ColumnDef objects.  They pass an ordered list
of column entries — each entry is either a string key
(`"date"`) or a dict (`{"key": "compare", "compared_ids": [...]}`).
WorkoutTable looks up `COLUMN_REGISTRY[key]` for the base definition and
applies any overrides from the entry.

Every registry render function takes `(row, opts, emit)`:
  • `row`  — the data row dict
  • `opts` — JSON-serializable per-instance config (entry keys other than
             layout-overrides become opts; empty for stateless columns)
  • `emit(event_name, payload)` — fired from interactive widgets; routed
             to the matching handler in the WorkoutTable's `on_event` map

Sort functions take `(row, opts)`.

This contract is the same one a future JS port will follow: render
functions are pure functions of `(row, opts)` plus a typed event channel.
Callbacks never need to cross the Python ↔ JavaScript boundary.

The table uses a single CSS Grid container (grid_box) whose
grid-template-columns encodes all column widths.  Every header cell and
every data cell is a direct child of that grid, so column widths are
perfectly consistent without setting width= on each individual cell.

Sort state (col, asc, page) lives in an internal hd.state().  Callers
trigger a page reset by wrapping WorkoutTable in hd.scope(filter_key) —
when filter_key changes, the internal state is discarded and page resets
to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

import hyperdiv as hd

from components.hyperdiv_extensions import grid_box
from components.lazy_tooltip_plugin import LazyTooltip
from components.rank_distribution import distribution_svg
from services.formatters import (
    fmt_date,
    fmt_split,
    pace_tenths,
    fmt_distance,
    fmt_hr,
    machine_label,
    fmt_watts,
    format_time,
)
from services.volume_bins import BIN_NAMES, BIN_COLORS, swatch_svg
from services.heartrate_utils import HR_ZONE_NAMES, HR_ZONE_COLORS
from services.workout_quality import QUALITY_STYLE


_ROWS_PER_PAGE = 25
_HEADER_BG = "neutral-50"
_HEADER_BORDER = "1px solid neutral-200"
_ROW_BORDER = "1px solid neutral-100"
_ROW_ALT_BG = "neutral-50"
_HEADER_COLOR = "neutral-500"
_TEXT_SIZE = "small"

_LAYOUT_KEYS = frozenset(
    {"header", "width", "align", "sortable", "default_asc"}
)


# ---------------------------------------------------------------------------
# Column definition
# ---------------------------------------------------------------------------


@dataclass
class ColumnDef:
    """One registry entry.  Render functions all take `(row, opts, emit)`;
    sort_value takes `(row, opts)`.  See module docstring."""

    key: str
    header: str
    width: str
    render_value: Callable | None = None
    render_cell: Callable | None = None
    sortable: bool = True
    sort_value: Callable | None = None
    default_asc: bool = False
    align: str = "center"


# ---------------------------------------------------------------------------
# Cached pure formatters
# ---------------------------------------------------------------------------


@hd.cached
def _cached_date(d):
    return fmt_date(d)


@hd.cached
def _cached_distance(d):
    return fmt_distance(d)


@hd.cached
def _cached_pace(p):
    return fmt_split(p)


@hd.cached
def _link_cell_inner(id) -> None:
    hd.link(
        "view",
        href=f"/session/{id}",
        font_size="small",
        underline=False,
        text_align="center",
    )


# ---------------------------------------------------------------------------
# Stateless renderers — workout-row columns
# ---------------------------------------------------------------------------


def _r_date(w, opts, emit):
    return _cached_date(w["date"])


def _s_date(w, opts):
    return w["date"]


def _r_type(w, opts, emit):
    return machine_label(w.get("type", ""))


def _s_type(w, opts):
    return machine_label(w.get("type", ""))


def _r_distance(w, opts, emit):
    return _cached_distance(w.get("distance"))


def _s_distance(w, opts):
    return w.get("distance") or 0


def _r_time(w, opts, emit):
    tf = w.get("time_formatted")
    if tf:
        return tf
    t = w.get("time")
    return format_time(t) if t else "—"


def _s_time(w, opts):
    return w.get("time") or 0


def _r_pace(w, opts, emit):
    return _cached_pace(pace_tenths(w))


def _s_pace(w, opts):
    p = pace_tenths(w)
    return p if p else float("inf")


def _r_watts(w, opts, emit):
    return fmt_watts(w)


def _s_watts(w, opts):
    return w["watts"]


def _r_drag(w, opts, emit):
    return str(w.get("drag_factor") or "—")


def _s_drag(w, opts):
    return w.get("drag_factor") or 0


def _r_spm(w, opts, emit):
    return str(w.get("stroke_rate") or "—")


def _s_spm(w, opts):
    return w.get("stroke_rate") or 0


def _r_hr(w, opts, emit):
    return fmt_hr(w.get("heart_rate"))


def _s_hr(w, opts):
    return (w.get("heart_rate") or {}).get("average") or 0


def _r_season(w, opts, emit):
    return w["season"]


def _s_season(w, opts):
    return w["date"]


def _r_link_cell(w, opts, emit):
    _link_cell_inner(w["id"])


def _r_structure(w, opts, emit):
    if not w.get("is_interval"):
        return ""
    reps = w.get("reps")
    label = w.get("structure_key")
    if not reps or not label:
        return ""
    return f"{reps} x {label}"


def _r_reps(w, opts, emit):
    return str(w["reps"]) if w.get("reps") else "—"


def _s_reps(w, opts):
    return w.get("reps") or 0


def _r_work_pace(w, opts, emit):
    return fmt_split(w["work_pace"]) if w.get("work_pace") else "—"


def _s_work_pace(w, opts):
    return w.get("work_pace") or float("inf")


def _r_work_spm(w, opts, emit):
    return f"{w['work_spm']:.0f}" if w.get("work_spm") else "—"


def _s_work_spm(w, opts):
    return w.get("work_spm") or 0


def _r_stimulus(w, opts, emit):
    s = w.get("_stimulus", "")
    if s and s != "—":
        hd.text(s, font_size="x-small", font_color="neutral-500", font_style="italic")


def _r_workout_structure(w, opts, emit):
    return w["structure_key"] if w.get("is_interval") else ""


def _r_similarity(w, opts, emit):
    sim = w.get("_similarity")
    return f"{sim:.0f}" if sim is not None else "—"


def _s_similarity(w, opts):
    s = w.get("_similarity")
    return s if s is not None else -1.0


# ---------------------------------------------------------------------------
# Spread + Quality cell renderers (shared with workout_page summary cells)
# ---------------------------------------------------------------------------

# Width (in HyperDiv units) of the small zone bar rendered inside each
# spread cell — half the full zone-bar width so the score reads as the
# dominant signal.
_SPREAD_BAR_WIDTH = 5.0
_SPREAD_BAR_HEIGHT = 0.5


def always_white(is_dark: bool) -> str:
    """Return a Shoelace neutral token that renders as white in either theme."""
    return "neutral-1000" if is_dark else "neutral-0"


@hd.cached
def render_spread_cell(
    score: float | None,
    bar_uri: str | None,
    _bin_meters: tuple | None,
    zone_names: tuple[str],
    zone_colors: tuple[tuple[str, str]],
    is_dark: bool,
    *,
    skip_indices: tuple[int, ...] = (0,),
) -> None:
    """
    Cell renderer for Power Spread / HR Spread columns.

    Layout: score (bold) on top, a small stacked zone bar (half-width)
    underneath, and a rich tooltip — built lazily by LazyTooltip on first
    hover — listing each non-empty zone with its swatch and percentage.
    Workouts with no meaningful meters (score is None) render as a single
    "—" with no bar and no tooltip.

    skip_indices — zone indices to exclude entirely from the tooltip (e.g.
    Rest, or Rest + No HR).  They are also excluded from the percentage
    denominator so the zone percentages sum to 100%.
    """
    if score is None or _bin_meters is None:
        hd.text("—", font_size="medium", font_color="neutral-400")
        return

    skip_set = set(skip_indices)
    total = sum(m for idx, m in enumerate(_bin_meters) if idx not in skip_set)
    items = []
    for idx in range(len(zone_names)):
        if idx in skip_set:
            continue
        meters = _bin_meters[idx] if idx < len(_bin_meters) else 0
        if meters <= 0:
            continue
        pct = (meters / total) if total > 0 else 0.0
        if pct < 0.005:
            continue
        color_str = zone_colors[idx][0 if is_dark else 1]
        items.append(
            {
                "swatch_uri": swatch_svg(color_str, size=10, radius=2),
                "pct_text": f"{pct:.0%}",
            }
        )

    LazyTooltip(
        config={
            "kind": "spread",
            "score": f"{score:.0f}",
            "bar_uri": bar_uri,
            "bar_w": _SPREAD_BAR_WIDTH,
            "bar_h": _SPREAD_BAR_HEIGHT,
            "items": items,
        },
        placement="top",
    )


def _rgba_css(rgba: tuple) -> str:
    r, g, b, a = rgba
    return f"rgba({r},{g},{b},{a})"


@hd.cached
def render_quality_cell(
    q: str | None, score: float, energy: tuple, is_dark: bool
) -> None:
    """Colored quality pill with score + top-3 contributing categories tooltip."""
    if q is None:
        hd.text("—", font_size="small", font_color="neutral-400")
        return

    style = QUALITY_STYLE[q]
    top_cats = sorted(
        energy,
        key=lambda p: p[1],
        reverse=True,
    )[:3]
    if q == "Low":
        headline = (
            f"Quality score {score:.2f} — below the 0.50 threshold for a "
            f"Medium session."
        )
    elif q == "Medium":
        headline = (
            f"Quality score {score:.2f} — clears the 0.50 Medium threshold, "
            f"below the 0.75 cutoff for High."
        )
    elif q == "High":
        headline = f"Quality score {score:.2f} — clears the 0.75 High threshold."
    else:  # Ultra
        headline = f"Quality score {score:.2f} — beyond reference power."

    if top_cats:
        total = sum(e for _, e in top_cats) or 1.0
        top = [
            {"name": cat, "pct_text": f"{100.0 * e / total:.0f}%"}
            for cat, e in top_cats
        ]
    else:
        top = []

    LazyTooltip(
        config={
            "kind": "quality",
            "label": style["label"],
            "bg": _rgba_css(style["bg"]),
            "tt_title": f"{q} quality",
            "headline": headline,
            "top": top,
        },
        placement="top",
    )


def _r_power_spread(w, opts, emit):
    render_spread_cell(
        score=w.get("_power_spread_score"),
        bar_uri=w.get("_bar_uri"),
        _bin_meters=w.get("_bin_meters"),
        zone_names=BIN_NAMES,
        zone_colors=BIN_COLORS,
        is_dark=hd.theme().is_dark,
        skip_indices=(0,),
    )


def _s_power_spread(w, opts):
    s = w.get("_power_spread_score")
    return s if s is not None else -1.0


def _r_hr_spread(w, opts, emit):
    render_spread_cell(
        score=w.get("_hr_spread_score"),
        bar_uri=w.get("_hr_bar_uri"),
        _bin_meters=w.get("_hr_bin_meters"),
        zone_names=HR_ZONE_NAMES,
        zone_colors=HR_ZONE_COLORS,
        is_dark=hd.theme().is_dark,
        skip_indices=(0, 6),
    )


def _s_hr_spread(w, opts):
    s = w.get("_hr_spread_score")
    return s if s is not None else -1.0


def _r_quality(w, opts, emit):
    render_quality_cell(
        w.get("_quality"),
        w.get("_quality_score", 0.0),
        tuple(
            (cat, e) for cat, e in (w.get("_quality_energy", {}) or {}).items() if e > 0
        ),
        hd.theme().is_dark,
    )


def _s_quality(w, opts):
    s = w.get("_quality_score")
    return s if s is not None else -1.0


# ---------------------------------------------------------------------------
# Stateful renderers — emit events through the WorkoutTable on_event map
# ---------------------------------------------------------------------------


def _r_structure_filter(w, opts, emit):
    structure_key = w.get("structure_key")
    is_active = opts.get("active_key") == structure_key
    btn = hd.button(
        structure_key,
        variant="text",
        size="medium",
        padding=(0, 0),
        font_weight="semibold" if is_active else "normal",
        font_color="primary-500" if is_active else "neutral-700",
    )
    if btn.clicked and structure_key:
        emit("structure_click", {"structure_key": structure_key})


def _r_compare(w, opts, emit):
    if not w.get("stroke_data"):
        hd.text("—", font_color="neutral-300", font_size="small")
        return
    wid = w.get("id")
    if wid is None:
        hd.text("—", font_color="neutral-300", font_size="small")
        return
    compared_ids = opts.get("compared_ids") or ()
    stack_active = opts.get("stack_active", False)
    cb = hd.checkbox(
        checked=wid in compared_ids, disabled=stack_active, size="small"
    )
    if cb.changed:
        emit("compare_toggle", {"workout_id": wid, "checked": cb.checked})


# ---------------------------------------------------------------------------
# Stateless renderers — rank-page rows (different row shape: ranking records)
# ---------------------------------------------------------------------------


def _r_rank_event(r, opts, emit):
    return r["event_label"]


def _s_rank_event(r, opts):
    order = opts.get("event_order") or {}
    return order.get(r["event_key"], 99)


def _r_rank_date(r, opts, emit):
    return r["date_label"]


def _s_rank_date(r, opts):
    return r["date_iso"]


def _r_rank_age(r, opts, emit):
    return str(r["age"])


def _s_rank_age(r, opts):
    return r["age"]


def _r_rank_age_group(r, opts, emit):
    return r["age_band_rankings"]


def _s_rank_age_group(r, opts):
    return r["age_band_rankings"]


def _r_rank_result(r, opts, emit):
    vt = r.get("value_tenths") or 0
    txt = format_time(vt) if r["event_kind"] == "dist" else fmt_distance(vt)
    hd.text(txt, font_size="small")


def _s_rank_result(r, opts):
    return r.get("value_tenths") or 0


def _r_rank_pace(r, opts, emit):
    return fmt_split(r["pace_tenths"]) if r.get("pace_tenths") else "—"


def _s_rank_pace(r, opts):
    return r.get("pace_tenths") or float("inf")


def _r_rank_watts(r, opts, emit):
    return f"{r['watts']:.0f}" if r.get("watts") else "—"


def _s_rank_watts(r, opts):
    return r.get("watts") or 0


def _r_rank_wr_pct_pace(r, opts, emit):
    return f"{r['wr_pct_pace']:.1f}%" if "wr_pct_pace" in r else "—"


def _s_rank_wr_pct_pace(r, opts):
    return r.get("wr_pct_pace") or 0


def _r_rank_wr_pct_watts(r, opts, emit):
    return f"{r['wr_pct_watts']:.1f}%" if "wr_pct_watts" in r else "—"


def _s_rank_wr_pct_watts(r, opts):
    return r.get("wr_pct_watts") or 0


def _r_rank_wr_pace(r, opts, emit):
    return (
        fmt_split(int(round(r["wr_pace"] * 10))) if r.get("wr_pace") else "—"
    )


def _s_rank_wr_pace(r, opts):
    return r.get("wr_pace") or float("inf")


def _r_rank_cell(r, opts, emit):
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
    if btn.clicked:
        emit("rank_click", {"row": r})


def _s_rank_rank(r, opts):
    return r.get("rank") or 10**9


def _r_rank_percentile(r, opts, emit):
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


def _s_rank_percentile(r, opts):
    return r.get("percentile") or 0


def _r_rank_distribution(r, opts, emit):
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


# ---------------------------------------------------------------------------
# Column registry
# ---------------------------------------------------------------------------


COLUMN_REGISTRY: dict[str, ColumnDef] = {
    # ── Workout columns ─────────────────────────────────────────────────
    "date": ColumnDef(
        key="date",
        header="Date",
        width="10rem",
        render_value=_r_date,
        sort_value=_s_date,
    ),
    "type": ColumnDef(
        key="type",
        header="Type",
        width="7rem",
        render_value=_r_type,
        sort_value=_s_type,
    ),
    "distance": ColumnDef(
        key="distance",
        header="Distance",
        width="7rem",
        render_value=_r_distance,
        sort_value=_s_distance,
        align="end",
    ),
    "time": ColumnDef(
        key="time",
        header="Time",
        width="7rem",
        render_value=_r_time,
        sort_value=_s_time,
        align="end",
    ),
    "pace": ColumnDef(
        key="pace",
        header="Pace /500m",
        width="7rem",
        render_value=_r_pace,
        sort_value=_s_pace,
        default_asc=True,
    ),
    "watts": ColumnDef(
        key="watts",
        header="Watts",
        width="5rem",
        render_value=_r_watts,
        sort_value=_s_watts,
    ),
    "drag": ColumnDef(
        key="drag",
        header="Drag",
        width="5rem",
        render_value=_r_drag,
        sort_value=_s_drag,
    ),
    "spm": ColumnDef(
        key="spm",
        header="SPM",
        width="4rem",
        render_value=_r_spm,
        sort_value=_s_spm,
    ),
    "hr": ColumnDef(
        key="hr",
        header="HR",
        width="8rem",
        render_value=_r_hr,
        sort_value=_s_hr,
    ),
    "season": ColumnDef(
        key="season",
        header="Season",
        width="6rem",
        render_value=_r_season,
        sort_value=_s_season,
    ),
    "link": ColumnDef(
        key="link",
        header="",
        width="2.5rem",
        render_cell=_r_link_cell,
        sortable=False,
    ),
    "structure": ColumnDef(
        key="structure",
        header="Structure",
        width="9rem",
        render_value=_r_structure,
        sort_value=lambda w, _o: _r_structure(w, _o, None),
        align="start",
    ),
    "reps": ColumnDef(
        key="reps",
        header="Reps",
        width="4rem",
        render_value=_r_reps,
        sort_value=_s_reps,
    ),
    "work_pace": ColumnDef(
        key="work_pace",
        header="Avg Split",
        width="7rem",
        render_value=_r_work_pace,
        sort_value=_s_work_pace,
        default_asc=True,
    ),
    "work_spm": ColumnDef(
        key="work_spm",
        header="SPM",
        width="4rem",
        render_value=_r_work_spm,
        sort_value=_s_work_spm,
    ),
    "stimulus": ColumnDef(
        key="stimulus",
        header="Stimulus",
        width="10rem",
        render_cell=_r_stimulus,
        sortable=False,
    ),
    "workout_structure": ColumnDef(
        key="workout_structure",
        header="Workout",
        width="minmax(8rem,1fr)",
        render_value=_r_workout_structure,
    ),
    "similarity": ColumnDef(
        key="similarity",
        header="Similarity",
        width="6rem",
        render_value=_r_similarity,
        sort_value=_s_similarity,
    ),
    "power_spread": ColumnDef(
        key="power_spread",
        header="Power Spread",
        width="8rem",
        render_cell=_r_power_spread,
        sort_value=_s_power_spread,
    ),
    "hr_spread": ColumnDef(
        key="hr_spread",
        header="HR Spread",
        width="8rem",
        render_cell=_r_hr_spread,
        sort_value=_s_hr_spread,
    ),
    "quality": ColumnDef(
        key="quality",
        header="Quality",
        width="6rem",
        render_cell=_r_quality,
        sort_value=_s_quality,
    ),
    # ── Stateful columns ────────────────────────────────────────────────
    "structure_filter": ColumnDef(
        key="structure_filter",
        header="Structure",
        width="minmax(8rem,1fr)",
        render_cell=_r_structure_filter,
        sortable=False,
    ),
    "compare": ColumnDef(
        key="compare",
        header="Compare",
        width="5.5rem",
        render_cell=_r_compare,
        sortable=False,
    ),
    # ── Rank-page columns (rows are ranking records, not workouts) ──────
    "rank_event": ColumnDef(
        key="rank_event",
        header="Event",
        width="7rem",
        render_value=_r_rank_event,
        sort_value=_s_rank_event,
        default_asc=True,
    ),
    "rank_date": ColumnDef(
        key="rank_date",
        header="Date",
        width="9rem",
        render_value=_r_rank_date,
        sort_value=_s_rank_date,
    ),
    "rank_age": ColumnDef(
        key="rank_age",
        header="Age",
        width="4rem",
        render_value=_r_rank_age,
        sort_value=_s_rank_age,
    ),
    "rank_age_group": ColumnDef(
        key="rank_age_group",
        header="Age Group",
        width="6rem",
        render_value=_r_rank_age_group,
        sort_value=_s_rank_age_group,
    ),
    "rank_result": ColumnDef(
        key="rank_result",
        header="Result",
        width="7rem",
        render_cell=_r_rank_result,
        sort_value=_s_rank_result,
        align="end",
    ),
    "rank_pace": ColumnDef(
        key="rank_pace",
        header="Pace",
        width="6rem",
        render_value=_r_rank_pace,
        sort_value=_s_rank_pace,
        default_asc=True,
    ),
    "rank_watts": ColumnDef(
        key="rank_watts",
        header="Watts",
        width="5rem",
        render_value=_r_rank_watts,
        sort_value=_s_rank_watts,
    ),
    "rank_wr_pct_pace": ColumnDef(
        key="rank_wr_pct_pace",
        header="% WR Pace",
        width="6rem",
        render_value=_r_rank_wr_pct_pace,
        sort_value=_s_rank_wr_pct_pace,
    ),
    "rank_wr_pct_watts": ColumnDef(
        key="rank_wr_pct_watts",
        header="% WR Watts",
        width="6rem",
        render_value=_r_rank_wr_pct_watts,
        sort_value=_s_rank_wr_pct_watts,
    ),
    "rank_wr_pace": ColumnDef(
        key="rank_wr_pace",
        header="WR Pace",
        width="6rem",
        render_value=_r_rank_wr_pace,
        sort_value=_s_rank_wr_pace,
        default_asc=True,
    ),
    "rank": ColumnDef(
        key="rank",
        header="Rank",
        width="9rem",
        render_cell=_r_rank_cell,
        sort_value=_s_rank_rank,
        default_asc=True,
    ),
    "rank_percentile": ColumnDef(
        key="rank_percentile",
        header="%ile",
        width="5rem",
        render_cell=_r_rank_percentile,
        sort_value=_s_rank_percentile,
    ),
    "rank_distribution": ColumnDef(
        key="rank_distribution",
        header="Watts Distribution",
        width="minmax(10rem,1fr)",
        render_cell=_r_rank_distribution,
        sortable=False,
    ),
}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _resolve_column(entry, on_event: dict | None) -> ColumnDef:
    """Look up `entry["key"]` in COLUMN_REGISTRY and apply per-instance overrides.

    Layout overrides (header/width/align/sortable/default_asc) replace the
    base values.  Any other entry keys become opts passed to the render and
    sort functions.
    """
    if isinstance(entry, str):
        entry = {"key": entry}

    key = entry["key"]
    base = COLUMN_REGISTRY[key]

    layout = {k: entry[k] for k in entry if k in _LAYOUT_KEYS}
    opts = {k: v for k, v in entry.items() if k != "key" and k not in _LAYOUT_KEYS}

    def emit(name, payload):
        h = (on_event or {}).get(name)
        if h is not None:
            h(payload)

    rv = base.render_value
    rc = base.render_cell
    sv = base.sort_value

    bound_rv = (lambda w, _f=rv: _f(w, opts, emit)) if rv else None
    bound_rc = (lambda w, _f=rc: _f(w, opts, emit)) if rc else None
    bound_sv = (lambda w, _f=sv: _f(w, opts)) if sv else None

    return replace(
        base,
        header=layout.get("header", base.header),
        width=layout.get("width", base.width),
        align=layout.get("align", base.align),
        sortable=layout.get("sortable", base.sortable),
        default_asc=layout.get("default_asc", base.default_asc),
        render_value=bound_rv,
        render_cell=bound_rc,
        sort_value=bound_sv,
    )


# ---------------------------------------------------------------------------
# WorkoutTable
# ---------------------------------------------------------------------------


def WorkoutTable(
    results: list,
    columns: list,
    *,
    paginate: bool = True,
    rows_per_page: int = _ROWS_PER_PAGE,
    highlight: Callable | None = None,
    default_sort_col: str = "date",
    default_sort_asc: bool = False,
    on_event: dict[str, Callable] | None = None,
) -> None:
    """
    Render a CSS Grid-based, sortable data table of rows.

    Parameters
    ----------
    results           List of row dicts.
    columns           Ordered list of column entries.  Each entry is either
                      a string key (`"date"`) or a dict with a "key" plus
                      optional layout overrides (header/width/align/
                      sortable/default_asc) and per-instance opts.
    paginate          Show prev/next pagination controls (default True).
    rows_per_page     Rows per page when paginate=True (default 25).
    highlight         fn(row) -> bool.  True → row styled with primary-50.
    default_sort_col  Column key for the initial sort (default "date").
    default_sort_asc  Initial sort direction (default False = descending).
    on_event          Optional dict mapping event names to handlers.  Each
                      handler receives a single JSON-serializable payload.

    Page reset on filter change
    ---------------------------
    WorkoutTable's internal page lives in hd.state(), which is keyed by
    HyperDiv's component identity.  Wrap the call in hd.scope(filter_key)
    and change filter_key when the data source changes to force a page reset.
    """
    if not results:
        hd.text("No results.", font_color=_HEADER_COLOR, font_size=_TEXT_SIZE)
        return

    resolved = [_resolve_column(c, on_event) for c in columns]

    tbl = hd.state(col=default_sort_col, asc=default_sort_asc, page=0)

    # ── Sort ─────────────────────────────────────────────────────────────────
    active_col = next((c for c in resolved if c.key == tbl.col), None)
    if active_col is not None:
        key_fn = active_col.sort_value or active_col.render_value or (lambda w: "")
        sorted_results = sorted(results, key=key_fn, reverse=not tbl.asc)
    else:
        sorted_results = list(results)

    total = len(sorted_results)
    per_page = rows_per_page if paginate else total
    total_pages = max(1, (total + per_page - 1) // per_page)
    if tbl.page >= total_pages:
        tbl.page = total_pages - 1

    page_start = tbl.page * per_page
    page_rows = sorted_results[page_start : page_start + per_page]

    # ── Grid ─────────────────────────────────────────────────────────────────
    col_template = " ".join(c.width for c in resolved)

    with grid_box(
        grid_template_columns=col_template,
        width="100%",
        horizontal_scroll=True,
        border="1px solid neutral-200",
        border_radius="medium",
    ):
        # Header cells
        for col in resolved:
            with hd.scope(f"hdr_{col.key}"):
                with hd.box(
                    padding=(0.4, 0.75),
                    background_color=_HEADER_BG,
                    border_bottom=_HEADER_BORDER,
                    justify="center",
                    align=col.align,
                ):
                    if col.sortable and col.header:
                        is_active = tbl.col == col.key
                        indicator = (" ▲" if tbl.asc else " ▼") if is_active else ""
                        btn = hd.button(
                            f"{col.header}{indicator}",
                            variant="text",
                            size="small",
                            font_size=_TEXT_SIZE,
                            font_weight="bold" if is_active else "normal",
                            font_color="neutral-600" if is_active else _HEADER_COLOR,
                        )
                        if btn.clicked:
                            if tbl.col == col.key:
                                tbl.asc = not tbl.asc
                            else:
                                tbl.col = col.key
                                tbl.asc = col.default_asc
                            tbl.page = 0
                    elif col.header:
                        hd.text(
                            col.header,
                            font_size=_TEXT_SIZE,
                            font_weight="semibold",
                            font_color=_HEADER_COLOR,
                        )

        # Data cells
        for i, w in enumerate(page_rows):
            with hd.scope(w.get("id", id(w))):
                for col in resolved:
                    with hd.scope(col.key):
                        is_hl = highlight(w) if highlight else False
                        row_bg = (
                            "primary-50" if is_hl else (_ROW_ALT_BG if i % 2 else None)
                        )

                        with hd.box(
                            padding=(0.5, 0.75),
                            background_color=row_bg,
                            border_bottom=_ROW_BORDER,
                            justify="center",
                            align=col.align,
                        ):
                            if col.render_cell:
                                col.render_cell(w)
                            else:
                                val = col.render_value(w) if col.render_value else ""
                                hd.text(
                                    val,
                                    font_size=_TEXT_SIZE,
                                    font_color="primary-700"
                                    if is_hl
                                    else "neutral-700",
                                    font_weight="semibold" if is_hl else "normal",
                                )

    # ── Pagination ────────────────────────────────────────────────────────────
    if paginate and total_pages > 1:
        with hd.hbox(gap=1, align="center", padding=(0.75, 0), justify="center"):
            if tbl.page > 0:
                if hd.button("← Prev", variant="neutral", size="small").clicked:
                    tbl.page -= 1
            hd.text(
                f"Page {tbl.page + 1} of {total_pages}  ({total} workouts)",
                font_size="small",
                font_color="neutral-500",
            )
            if tbl.page < total_pages - 1:
                if hd.button("Next →", variant="neutral", size="small").clicked:
                    tbl.page += 1
