"""
components/workout_table.py — CSS Grid-based sortable data table for workouts.

Exported:
  ColumnDef       — dataclass describing one column (header, width, render, sort)
  WorkoutTable()  — HyperDiv component: renders a sortable, paginated table

Pre-defined column constants (import and compose into column lists):
  COL_DATE, COL_TYPE, COL_DISTANCE, COL_TIME, COL_PACE, COL_WATTS,
  COL_DRAG, COL_SPM, COL_HR, COL_SEASON, COL_LINK,
  COL_POWER_SPREAD, COL_HR_SPREAD, COL_QUALITY

Shared cell-renderer helpers (also used outside the table — e.g. by
workout_page summary cells):
  always_white(is_dark)               — Shoelace neutral token reading white
  render_spread_cell(...)             — score + zone-bar + tooltip cell
  render_quality_cell(workout, is_dark)— colored quality pill with tooltip

Design
------
The table uses a single CSS Grid container (grid_box) whose
grid-template-columns encodes all column widths.  Every header cell and
every data cell is a direct child of that grid, so column widths are
perfectly consistent without setting width= on each individual cell.

Columns with width "minmax(X,1fr)" absorb available space and reflow on
window resize; fixed-rem columns stay constant.

Sort state (col, asc, page) lives in an internal hd.state().  Callers
trigger a page reset by wrapping WorkoutTable in hd.scope(filter_key) —
when filter_key changes, the internal state is discarded and page resets
to 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import hyperdiv as hd

from components.hyperdiv_extensions import grid_box
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
from services.rowing_utils import (
    get_season,
    compute_pace,
    compute_watts as _compute_watts,
)
from services.volume_bins import BIN_NAMES, BIN_COLORS
from services.heartrate_utils import HR_ZONE_NAMES, HR_ZONE_COLORS
from services.workout_quality import QUALITY_STYLE


_ROWS_PER_PAGE = 25
_HEADER_BG = "neutral-50"
_HEADER_BORDER = "1px solid neutral-200"
_ROW_BORDER = "1px solid neutral-100"
_ROW_ALT_BG = "neutral-50"
_HEADER_COLOR = "neutral-500"
_TEXT_SIZE = "small"


# ---------------------------------------------------------------------------
# Column definition
# ---------------------------------------------------------------------------


@dataclass
class ColumnDef:
    """
    Describes a single column in WorkoutTable.

    For simple text columns supply render_value only.
    For columns with custom HyperDiv content (images, buttons, links) supply
    render_cell — a fn(workout) -> None that emits children directly into
    the pre-created cell box.
    """

    key: str
    """Unique identifier; used as HyperDiv scope ID and sort key."""

    header: str
    """Column header label.  Empty string → blank header cell."""

    width: str
    """
    CSS width string used verbatim in grid-template-columns.
    Examples: "10rem", "5rem", "minmax(8rem,1fr)", "2.5rem".
    """

    render_value: Callable = None
    """fn(workout) -> str for simple text cells."""

    render_cell: Callable | None = None
    """
    fn(workout) -> None; renders arbitrary HyperDiv content into the cell.
    When set, takes precedence over render_value for data cells.
    """

    sortable: bool = True
    """Whether clicking the column header sorts the table."""

    sort_value: Callable | None = None
    """
    fn(workout) -> comparable.  Used for sorting.
    Falls back to render_value when None.
    """

    default_asc: bool = False
    """
    Direction when this column is first activated.
    False = descending (date, distance, watts).
    True  = ascending (pace — lower is faster).
    """

    align: str = "center"
    """Horizontal content alignment in data cells: "start" | "center" | "end"."""


# ---------------------------------------------------------------------------
# Sort helpers
# ---------------------------------------------------------------------------


def _pace_sort(w: dict):
    p = pace_tenths(w)
    return p if p else float("inf")


def _watts_sort(w: dict):
    pace = compute_pace(w)
    return _compute_watts(pace) if pace else 0.0


def _hr_sort(w: dict):
    return (w.get("heart_rate") or {}).get("average") or 0


def _time_value(w: dict) -> str:
    tf = w.get("time_formatted")
    if tf:
        return tf
    t = w.get("time")
    return format_time(t) if t else "—"


# ---------------------------------------------------------------------------
# Link cell renderer
# ---------------------------------------------------------------------------


def _link_cell(w: dict) -> None:
    hd.link(
        "view",
        href=f"/session/{w.get('id')}",
        font_size="small",
        underline=False,
        text_align="center",
    )


# ---------------------------------------------------------------------------
# Pre-defined column constants
# ---------------------------------------------------------------------------

COL_DATE = ColumnDef(
    key="date",
    header="Date",
    width="10rem",
    render_value=lambda w: fmt_date(w.get("date", "")),
    sort_value=lambda w: w.get("date", ""),
)

COL_TYPE = ColumnDef(
    key="type",
    header="Type",
    width="7rem",
    render_value=lambda w: machine_label(w.get("type", "")),
    sort_value=lambda w: machine_label(w.get("type", "")),
)

COL_DISTANCE = ColumnDef(
    key="distance",
    header="Distance",
    width="7rem",
    render_value=lambda w: fmt_distance(w.get("distance")),
    sort_value=lambda w: w.get("distance") or 0,
    align="end",
)

COL_TIME = ColumnDef(
    key="time",
    header="Time",
    width="7rem",
    render_value=_time_value,
    sort_value=lambda w: w.get("time") or 0,
    align="end",
)

COL_PACE = ColumnDef(
    key="pace",
    header="Pace /500m",
    width="7rem",
    render_value=lambda w: fmt_split(pace_tenths(w)),
    sort_value=_pace_sort,
    default_asc=True,
)

COL_WATTS = ColumnDef(
    key="watts",
    header="Watts",
    width="5rem",
    render_value=lambda w: fmt_watts(w),
    sort_value=_watts_sort,
)

COL_DRAG = ColumnDef(
    key="drag",
    header="Drag",
    width="5rem",
    render_value=lambda w: str(w.get("drag_factor") or "—"),
    sort_value=lambda w: w.get("drag_factor") or 0,
)

COL_SPM = ColumnDef(
    key="spm",
    header="SPM",
    width="4rem",
    render_value=lambda w: str(w.get("stroke_rate") or "—"),
    sort_value=lambda w: w.get("stroke_rate") or 0,
)

COL_HR = ColumnDef(
    key="hr",
    header="HR",
    width="8rem",
    render_value=lambda w: fmt_hr(w.get("heart_rate")),
    sort_value=_hr_sort,
)

COL_SEASON = ColumnDef(
    key="season",
    header="Season",
    width="6rem",
    render_value=lambda w: get_season(w.get("date", "")),
    sort_value=lambda w: w.get("date", ""),
)

COL_LINK = ColumnDef(
    key="link",
    header="",
    width="2.5rem",
    render_value=lambda w: "",
    render_cell=_link_cell,
    sortable=False,
    align="center",
)


# ---------------------------------------------------------------------------
# Spread + Quality cell renderers (shared by the Intervals page, Sessions
# page, and Workout page summary)
# ---------------------------------------------------------------------------

# Width (in HyperDiv units) of the small zone bar rendered inside each
# spread cell — half the full zone-bar width so the score reads as the
# dominant signal.
_SPREAD_BAR_WIDTH = 5.0
_SPREAD_BAR_HEIGHT = 0.5


def always_white(is_dark: bool) -> str:
    """Return a Shoelace neutral token that renders as white in either theme."""
    return "neutral-1000" if is_dark else "neutral-0"


def render_spread_cell(
    score: float | None,
    bar_uri: str | None,
    bin_meters: list | None,
    zone_names: list[str],
    zone_colors: list[tuple[str, str]],
    is_dark: bool,
    *,
    skip_indices: tuple[int, ...] = (0,),
) -> None:
    """
    Cell renderer for Power Spread / HR Spread columns.

    Layout: score (bold) on top, a small stacked zone bar (half-width)
    underneath, and a rich content-slot tooltip listing each non-empty zone
    with its swatch and percentage.  Workouts with no meaningful meters
    (score is None) render as a single "—" with no bar.

    skip_indices — zone indices to exclude entirely from the tooltip (e.g.
    Rest, or Rest + No HR).  They are also excluded from the percentage
    denominator so the zone percentages sum to 100%.
    """
    # Lazy import — avoid hard dependency on swatch_svg at module load.
    from services.volume_bins import swatch_svg

    if score is None or bin_meters is None:
        hd.text("—", font_size="medium", font_color="neutral-400")
        return

    skip_set = set(skip_indices)
    total = sum(m for idx, m in enumerate(bin_meters) if idx not in skip_set)
    with hd.tooltip() as tt:
        with hd.box(slot=tt.content_slot, padding=0.4, gap=0.2, min_width=12):
            for idx, name in enumerate(zone_names):
                with hd.scope(f"{idx} {name}"):
                    if idx in skip_set:
                        continue
                    meters = bin_meters[idx] if idx < len(bin_meters) else 0
                    if meters <= 0:
                        continue
                    pct = (meters / total) if total > 0 else 0.0
                    if pct < 0.005:
                        continue
                    color_str = zone_colors[idx][0 if is_dark else 1]
                    with hd.hbox(gap=0.4, align="center"):
                        hd.image(
                            src=swatch_svg(color_str, size=10, radius=2),
                            width=0.6,
                            height=0.6,
                        )
                        hd.text(
                            f"{pct:.0%}",
                            font_size="x-small",
                            font_weight="bold",
                        )
        with hd.box(align="start", gap=0.2):
            hd.text(f"{score:.0f}", font_size="medium", font_weight="bold")
            if bar_uri:
                hd.image(
                    src=bar_uri,
                    width=_SPREAD_BAR_WIDTH,
                    height=_SPREAD_BAR_HEIGHT,
                )


def render_quality_cell(workout: dict, is_dark: bool) -> None:
    """Colored quality pill with score + top-3 contributing categories tooltip."""
    q = workout.get("_quality")
    if q is None:
        hd.text("—", font_size="small", font_color="neutral-400")
        return
    score = workout.get("_quality_score") or 0.0
    energy = workout.get("_quality_energy") or {}
    style = QUALITY_STYLE[q]
    top_cats = sorted(
        ((cat, e) for cat, e in energy.items() if e > 0),
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
    with hd.tooltip() as tt:
        with hd.box(slot=tt.content_slot, padding=0.4, gap=0.3, max_width=24):
            hd.text(f"{q} quality", font_size="small", font_weight="bold")
            hd.text(headline, font_size="x-small")
            if top_cats:
                total = sum(e for _, e in top_cats) or 1.0
                hd.text(
                    "Top contributions:",
                    font_size="x-small",
                    font_color="neutral-500",
                )
                for cat, e in top_cats:
                    with hd.scope(f"{cat} {e}"):
                        hd.text(
                            f"  • {cat}: {100.0 * e / total:.0f}%",
                            font_size="x-small",
                        )
        with hd.box(
            padding=(0.15, 0.5, 0.15, 0.5),
            border_radius="medium",
            background_color=style["bg"],
            align="center",
            justify="center",
        ):
            hd.text(
                style["label"],
                font_size="x-small",
                font_weight="bold",
                font_color=always_white(is_dark),
            )


def _spread_cell_factory(zone_names, zone_colors, score_field, bar_field, bin_field, skip):
    def _render(w):
        render_spread_cell(
            score=w.get(score_field),
            bar_uri=w.get(bar_field),
            bin_meters=w.get(bin_field),
            zone_names=zone_names,
            zone_colors=zone_colors,
            is_dark=hd.theme().is_dark,
            skip_indices=skip,
        )
    return _render


COL_POWER_SPREAD = ColumnDef(
    key="power_spread",
    header="Power Spread",
    width="8rem",
    render_cell=_spread_cell_factory(
        BIN_NAMES, BIN_COLORS, "_power_spread_score", "_bar_uri", "_bin_meters", (0,)
    ),
    sort_value=lambda w: w.get("_power_spread_score")
    if w.get("_power_spread_score") is not None
    else -1.0,
)

COL_HR_SPREAD = ColumnDef(
    key="hr_spread",
    header="HR Spread",
    width="8rem",
    render_cell=_spread_cell_factory(
        HR_ZONE_NAMES,
        HR_ZONE_COLORS,
        "_hr_spread_score",
        "_hr_bar_uri",
        "_hr_bin_meters",
        (0, 6),
    ),
    sort_value=lambda w: w.get("_hr_spread_score")
    if w.get("_hr_spread_score") is not None
    else -1.0,
)

COL_QUALITY = ColumnDef(
    key="quality",
    header="Quality",
    width="6rem",
    render_cell=lambda w: render_quality_cell(w, hd.theme().is_dark),
    sort_value=lambda w: w.get("_quality_score")
    if w.get("_quality_score") is not None
    else -1.0,
)

COL_SIMILARITY = ColumnDef(
    key="similarity",
    header="Similarity",
    width="6rem",
    render_value=lambda w: f"{w['_similarity']:.0f}"
    if w.get("_similarity") is not None
    else "—",
    sort_value=lambda w: w.get("_similarity")
    if w.get("_similarity") is not None
    else -1.0,
)


# ---------------------------------------------------------------------------
# WorkoutTable
# ---------------------------------------------------------------------------


def WorkoutTable(
    results: list,
    columns: list[ColumnDef],
    *,
    paginate: bool = True,
    rows_per_page: int = _ROWS_PER_PAGE,
    highlight: Callable | None = None,
    default_sort_col: str = "date",
    default_sort_asc: bool = False,
) -> None:
    """
    Render a CSS Grid-based, sortable data table of workout results.

    Parameters
    ----------
    results           List of workout dicts.
    columns           Ordered list of ColumnDef defining which columns to show.
    paginate          Show prev/next pagination controls (default True).
    rows_per_page     Rows per page when paginate=True (default 25).
    highlight         fn(workout) -> bool.  True → row styled with primary-50.
    default_sort_col  Column key for the initial sort (default "date").
    default_sort_asc  Initial sort direction (default False = descending).

    Page reset on filter change
    ---------------------------
    WorkoutTable's internal page lives in hd.state(), which is keyed by
    HyperDiv's component identity.  Wrap the call in hd.scope(filter_key)
    and change filter_key when the data source changes to force a page reset:

        with hd.scope(f"my_filter_{state.some_filter}"):
            WorkoutTable(filtered_list, columns)
    """
    if not results:
        hd.text("No results.", font_color=_HEADER_COLOR, font_size=_TEXT_SIZE)
        return

    tbl = hd.state(col=default_sort_col, asc=default_sort_asc, page=0)

    # ── Sort ─────────────────────────────────────────────────────────────────
    active_col = next((c for c in columns if c.key == tbl.col), None)
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
    col_template = " ".join(c.width for c in columns)

    with grid_box(
        grid_template_columns=col_template,
        width="100%",
        horizontal_scroll=True,
        border="1px solid neutral-200",
        border_radius="medium",
    ):
        # Header cells
        for col in columns:
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
                is_hl = highlight(w) if highlight else False
                row_bg = "primary-50" if is_hl else (_ROW_ALT_BG if i % 2 else None)
                for col in columns:
                    with hd.scope(col.key):
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
