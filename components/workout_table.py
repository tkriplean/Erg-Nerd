"""
components/workout_table.py — JS-backed sortable, paginated table component.

Exported:
  WorkoutTable()    — public function: instantiates the JS plugin and
                      dispatches events back to the consumer.
  COLUMN_REGISTRY   — dict mapping column key → ColumnDef.  Source of truth
                      for column metadata; compiled into JSON-shaped configs
                      at WorkoutTable call time and shipped to the JS plugin.

Shared cell-renderer helpers (still used outside the table — workout_page
summary cards, spread/severity legends):
  always_white(is_dark)
  render_spread_cell(...)

Design
------
The table is rendered entirely in JavaScript by
``components/chart_assets/workout_table_plugin.js``.  Python's job is:

  1. Maintain ``COLUMN_REGISTRY`` describing every available column.
  2. Compile each consumer's column entry list into JSON-shaped configs
     (no Python callables cross the boundary).
  3. Instantiate the plugin with the full row dataset, the configs, and
     small reactive props (``visible_ids``, ``highlight_ids``,
     ``reset_token``).
  4. Read ``plugin.event_out`` and dispatch to the consumer's
     ``on_event`` map.

Sort and pagination state live in JS — no Python round-trip on header
clicks or page changes.  Filter changes that should reset the page push
a different ``reset_token`` string; JS resets sort + page when the token
changes.

Filter strategy
---------------
For consumers with frequent filter UI (intervals_page), pass the full
dataset to ``results=`` once and an updated ``visible_ids`` list on
filter change — JS hides non-matching rows.  For consumers that
pre-filter in Python (most pages), just pass the already-filtered list;
``visible_ids`` stays None.

Column entry shape (consumer-facing — unchanged from the Python era)
--------------------------------------------------------------------
Each entry is a string key or a dict:

    "date"
    {"key": "distance", "header": "Work", "width": "6rem"}
    {"key": "compare",
     "compared_ids": [...], "stack_active": False}
    {"key": "rank_event", "event_order": _EVENT_ORDER_IDX}

Layout overrides (header/width/align/sortable/default_asc) replace the
registry defaults.  Other keys become ``opts`` shipped to the JS
renderer.

Events
------
Stateful columns emit named events with JSON-serializable payloads.
Pass ``on_event={"name": handler}``; handlers receive the payload dict.

  structure_click  {"structure_key": str}
  compare_toggle   {"workout_id": str, "checked": bool}
  rank_click       {"row_id": str | int}
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass

import hyperdiv as hd

from services.volume_bins import BIN_COLORS, BIN_NAMES, swatch_svg
from services.heartrate_utils import HR_ZONE_COLORS, HR_ZONE_NAMES
from services.erg_stress import (
    SEVERITY_STYLE,
    STIMULUS_T_THRESH,
    ZONE_BANDS_S,
)
from services.volume_bins import BAND_TO_BIN
from services.formatters import fmt_distance


_HERE = os.path.dirname(__file__)


_LAYOUT_KEYS = frozenset({"header", "width", "align", "sortable", "default_asc"})

_SPREAD_BAR_WIDTH = 5.0
_SPREAD_BAR_HEIGHT = 0.5

_DEFAULT_ROWS_PER_PAGE = 25


# ---------------------------------------------------------------------------
# Column definition (metadata only — no Python render/sort closures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnDef:
    """Metadata for one column in the registry.

    `renderer` selects a JS render function in workout_table_plugin.js.
    For renderer="text" (the default), `format` selects a JS format
    function from FORMATS.  `sort_key` (defaults to `key`) selects a
    JS sort-key function from SORT_KEYS.
    """

    key: str
    header: str
    width: str
    align: str = "center"
    sortable: bool = True
    default_asc: bool = False
    renderer: str = "text"
    format: str | None = None
    sort_key: str | None = None


# ---------------------------------------------------------------------------
# Column registry — every column the app can render
# ---------------------------------------------------------------------------


COLUMN_REGISTRY: dict[str, ColumnDef] = {
    # ── Workout columns ─────────────────────────────────────────────────
    "date": ColumnDef("date", "Date", "115px", format="date", align="start"),
    "type": ColumnDef("type", "Type", "7rem", format="type"),
    "distance": ColumnDef(
        "distance", "Distance", "7rem", format="distance", align="end"
    ),
    "time": ColumnDef("time", "Time", "7rem", format="time", align="end"),
    "pace": ColumnDef("pace", "Pace /500m", "7rem", format="pace", default_asc=True),
    # Watts cell renders the watts number on top with a small Zone-Spread
    # stacked bar below — bar widths come from ``_zone_bin_fractions``.
    "watts": ColumnDef("watts", "Watts", "6rem", renderer="watts_zones"),
    "drag": ColumnDef("drag", "Drag", "5rem", format="drag"),
    "spm": ColumnDef("spm", "SPM", "4rem", format="spm"),
    # ── Session-tree columns (only used when tree_mode=True) ────────────
    "main_work": ColumnDef(
        "main_work",
        "Main Work",
        "minmax(10rem,1.5fr)",
        renderer="main_work_lines",
        align="center",
        sortable=False,
    ),
    "time_of_day": ColumnDef(
        "time_of_day",
        "Time",
        "6rem",
        renderer="time_of_day",
        align="center",
        sortable=False,
    ),
    "work_duration": ColumnDef(
        "work_duration",
        "Work Duration",
        "7rem",
        format="work_duration",
        align="center",
    ),
    "distance_combined": ColumnDef(
        "distance_combined",
        "Distance",
        "7rem",
        renderer="combined_distance",
        align="center",
    ),
    "work_distance": ColumnDef(
        "work_distance",
        "Work Distance",
        "7rem",
        format="work_distance",
        align="center",
    ),
    "other_distance": ColumnDef(
        "other_distance",
        "Other Distance",
        "7rem",
        format="other_distance",
        align="center",
    ),
    # "hr": ColumnDef("hr", "HR", "8rem", format="hr"),
    "season": ColumnDef("season", "Season", "6rem", format="season"),
    "link": ColumnDef("link", "", "2.5rem", renderer="link", sortable=False),
    "structure": ColumnDef(
        "structure", "Intervals", "minmax(8rem,1fr)", format="structure", align="start"
    ),
    "reps": ColumnDef("reps", "Reps", "4rem", format="reps"),
    "work_pace": ColumnDef(
        "work_pace", "Avg Split", "7rem", format="work_pace", default_asc=True
    ),
    "work_spm": ColumnDef("work_spm", "SPM", "4rem", format="work_spm"),
    "stimulus": ColumnDef(
        "stimulus", "Stimulus", "10rem", renderer="stimulus", sortable=False
    ),
    "workout_structure": ColumnDef(
        "workout_structure",
        "Intervals",
        "minmax(8rem,1fr)",
        format="workout_structure",
        align="start",
    ),
    "similarity": ColumnDef("similarity", "Similarity", "6rem", format="similarity"),
    "hr": ColumnDef("hr", "HR", "8rem", renderer="hr_spread"),
    # ── ESS family (services/erg_stress.py) ─────────────────────────────
    "ess": ColumnDef("ess", "ESS", "5rem", format="ess"),
    "if_eff": ColumnDef("if_eff", "Intensity", "6rem", format="if_eff"),
    "severity": ColumnDef("severity", "Severity", "7rem", renderer="severity"),
    "anaerobic_strain": ColumnDef(
        "anaerobic_strain", "W' Used", "5rem", format="anaerobic_strain"
    ),
    "glycogen_used": ColumnDef(
        "glycogen_used", "Gly Used", "5rem", format="glycogen_used"
    ),
    "stimulus": ColumnDef(
        "stimulus", "Stimulus", "5.5rem", renderer="stimulus"
    ),
    # ── Stateful columns ─────────────────────────────────────────────────
    "structure_filter": ColumnDef(
        "structure_filter",
        "Structure",
        "minmax(8rem,1fr)",
        renderer="structure_filter",
        sortable=False,
    ),
    "compare": ColumnDef(
        "compare", "Compare", "5.5rem", renderer="compare", sortable=False
    ),
    # ── Rank-page columns ────────────────────────────────────────────────
    "rank_event": ColumnDef(
        "rank_event", "Event", "7rem", format="rank_event", default_asc=True
    ),
    "rank_date": ColumnDef("rank_date", "Date", "9rem", format="rank_date"),
    "rank_age": ColumnDef("rank_age", "Age", "4rem", format="rank_age"),
    "rank_age_group": ColumnDef(
        "rank_age_group", "Age Group", "6rem", format="rank_age_group"
    ),
    "rank_result": ColumnDef(
        "rank_result", "Result", "7rem", format="rank_result", align="end"
    ),
    "rank_pace": ColumnDef(
        "rank_pace", "Pace", "6rem", format="rank_pace", default_asc=True
    ),
    "rank_watts": ColumnDef("rank_watts", "Watts", "5rem", format="rank_watts"),
    "rank_wr_pct_pace": ColumnDef(
        "rank_wr_pct_pace", "% WR Pace", "6rem", format="rank_wr_pct_pace"
    ),
    "rank_wr_pct_watts": ColumnDef(
        "rank_wr_pct_watts", "% WR Watts", "6rem", format="rank_wr_pct_watts"
    ),
    "rank_wr_pace": ColumnDef(
        "rank_wr_pace", "WR Pace", "6rem", format="rank_wr_pace", default_asc=True
    ),
    "rank": ColumnDef("rank", "Rank", "9rem", renderer="rank", default_asc=True),
    "rank_percentile": ColumnDef(
        "rank_percentile", "%ile", "5rem", renderer="rank_percentile"
    ),
    "rank_distribution": ColumnDef(
        "rank_distribution",
        "Watts Distribution",
        "minmax(10rem,1fr)",
        renderer="rank_distribution",
        sortable=False,
    ),
}


# ---------------------------------------------------------------------------
# Theme/color helper used by both the table and external consumers
# ---------------------------------------------------------------------------


def always_white(is_dark: bool) -> str:
    """Return a Shoelace neutral token that renders as white in either theme."""
    return "neutral-1000" if is_dark else "neutral-0"


def _rgba_css(rgba: tuple) -> str:
    r, g, b, a = rgba
    return f"rgba({r},{g},{b},{a})"


# ---------------------------------------------------------------------------
# External-facing cell renderers (used by workout_page summary cards and
# spread/quality legends — NOT by the JS-backed table itself).
# Kept here for backwards compatibility with the previous module surface.
# ---------------------------------------------------------------------------


@hd.cached
def render_spread_cell(
    score: float | None,
    _bin_meters: tuple | None,
    zone_names: tuple[str],
    zone_colors: tuple[tuple[str, str]],
    is_dark: bool,
    *,
    skip_indices: tuple[int, ...] = (0,),
    show_meters: bool = True,
) -> None:
    """
    Score + zone-bar + per-zone breakdown tooltip.  Used by workout_page's
    summary cards (the in-table version is rendered natively by the JS
    plugin).  The stacked zone-bar is built in the LazyTooltip JS plugin
    from ``_bin_meters`` + ``bar_colors`` — the SVG is no longer round-
    tripped through Python as a base64 data URI.

    ``_bin_meters`` may be an array of meter counts (HR-spread case) or
    time fractions summing to 1.0 (Zone-Spread case).  Set
    ``show_meters=False`` for the latter so the tooltip drops the meters
    column and shows only zone-name + percentage.
    """
    from components.lazy_tooltip_plugin import LazyTooltip

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
        item = {
            "swatch_uri": swatch_svg(color_str, size=10, radius=2),
            "name": zone_names[idx],
            "pct_text": f"{pct:.0%}",
        }
        if show_meters:
            item["meters_text"] = fmt_distance(int(round(meters)))
        items.append(item)

    LazyTooltip(
        config={
            "kind": "spread",
            "score": f"{score:.0f}",
            "bin_meters": list(_bin_meters),
            "bar_colors": _theme_bar_colors(zone_colors, is_dark),
            "bar_w": _SPREAD_BAR_WIDTH,
            "bar_h": _SPREAD_BAR_HEIGHT,
            "items": items,
            "show_meters": show_meters,
        },
        placement="top",
    )


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class _WorkoutTablePlugin(hd.Plugin):
    _name = "WorkoutTable"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        ("js-link", os.path.join(_HERE, "chart_assets", "workout_table_plugin.js")),
    ]

    rows = hd.Prop(hd.Any, [])
    column_configs = hd.Prop(hd.Any, [])
    visible_ids = hd.Prop(hd.Any, None)
    highlight_ids = hd.Prop(hd.Any, [])
    default_sort_col = hd.Prop(hd.String, "date")
    default_sort_asc = hd.Prop(hd.Bool, False)
    paginate = hd.Prop(hd.Bool, True)
    rows_per_page = hd.Prop(hd.Int, _DEFAULT_ROWS_PER_PAGE)
    reset_token = hd.Prop(hd.String, "")
    tree_mode = hd.Prop(hd.Bool, False)
    event_out = hd.Prop(hd.Any, None)


# ---------------------------------------------------------------------------
# Compile a column entry into a JSON-shaped config for the JS plugin
# ---------------------------------------------------------------------------


def _theme_swatches(zone_colors, is_dark: bool) -> list[str]:
    return [
        swatch_svg(color_pair[0 if is_dark else 1], size=10, radius=2)
        for color_pair in zone_colors
    ]


def _theme_bar_colors(zone_colors, is_dark: bool) -> list[str]:
    """Resolve a zone-color table to a flat list of CSS color strings for the
    active theme.  The JS plugin builds the stacked spread bar inline from
    these (see ``_buildSpreadBar`` in workout_table_plugin.js)."""
    return [pair[0 if is_dark else 1] for pair in zone_colors]


def _enrich_opts(key: str, opts: dict) -> dict:
    """Add theme-dependent or constant resources to a column's opts."""
    if key == "watts":
        is_dark = hd.theme().is_dark
        return {
            **opts,
            "swatch_uris": _theme_swatches(BIN_COLORS, is_dark),
            "bar_colors": _theme_bar_colors(BIN_COLORS, is_dark),
            "zone_names": list(BIN_NAMES),
            "skip_indices": [0],
            "bar_w": _SPREAD_BAR_WIDTH,
            "bar_h": _SPREAD_BAR_HEIGHT,
        }
    if key == "hr":
        is_dark = hd.theme().is_dark
        return {
            **opts,
            "swatch_uris": _theme_swatches(HR_ZONE_COLORS, is_dark),
            "bar_colors": _theme_bar_colors(HR_ZONE_COLORS, is_dark),
            "zone_names": list(HR_ZONE_NAMES),
            "skip_indices": [0, 6],
            "bar_w": _SPREAD_BAR_WIDTH,
            "bar_h": _SPREAD_BAR_HEIGHT,
        }
    if key == "severity":
        return {
            **opts,
            "severity_styles": {
                s: {"label": v["label"], "bg": _rgba_css(v["bg"])}
                for s, v in SEVERITY_STYLE.items()
            },
        }
    if key == "stimulus":
        is_dark = hd.theme().is_dark
        # Per-band metadata for the JS strip + tooltip.  Bands are in
        # ZONE_BANDS_S order (shortest → longest); colors / names / swatches
        # pull from BIN_COLORS by skipping index 0 (Rest) via BAND_TO_BIN.
        bands_list = list(ZONE_BANDS_S)
        bin_idx_per_band = [BAND_TO_BIN[d] for d in bands_list]
        zone_names = [BIN_NAMES[i] for i in bin_idx_per_band]
        bar_colors = _theme_bar_colors(
            tuple(BIN_COLORS[i] for i in bin_idx_per_band), is_dark
        )
        swatch_uris = _theme_swatches(
            tuple(BIN_COLORS[i] for i in bin_idx_per_band), is_dark
        )
        return {
            **opts,
            "bands": bands_list,
            "colors": bar_colors,
            "zone_names": zone_names,
            "swatch_uris": swatch_uris,
            "t_thresh": {int(d): float(STIMULUS_T_THRESH[d]) for d in bands_list},
        }
    return opts


def _json_safe(v):
    """Recursively coerce non-JSON-serializable values (datetimes, sets,
    tuples) into JSON-friendly equivalents.  Most workout fields pass
    through unchanged."""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, set):
        return [_json_safe(x) for x in v]
    return v


# Heavy enrichment fields that are *only* consumed by Python-side Workout-Page
# rendering (charts, rollup widgets, splits-table builders) — never read by
# the JS table plugin.  Shipping them through ``_row_for_js`` once per row
# was the dominant render cost on the Workouts page once the ESS family
# landed: ``_ess_timeline`` alone is ~1800 timepoints × six per-zone ratios
# per workout.  Skipping them at JSON-serialisation time cut the
# Workouts-page render from ~25 s to ~0.5 s in profiling.
#
# Do NOT add ``_bin_meters`` / ``_hr_bin_meters`` to this set — they're
# small AND the JS plugin reads them (power/HR-spread tooltip bars).
_TABLE_IRRELEVANT_KEYS: frozenset = frozenset(
    {
        "_ess_timeline",
        "_ess_segments",
        "_ess_session_summary",
    }
)


def _row_for_js(r: dict) -> dict:
    """Coerce a row dict into JSON-safe form for the JS plugin.

    Heavy Python-only enrichment fields (see :data:`_TABLE_IRRELEVANT_KEYS`)
    are skipped — they bloat the JS payload without ever being read.  Cost
    is paid once per render.

    Tree-mode parent rows carry an ``_children`` list of workout dicts;
    the same key-strip is applied to each child so per-workout
    ``_ess_segments`` / ``_ess_session_summary`` / ``_ess_timeline``
    don't leak into the JS payload.
    """
    out = {}
    for k, v in r.items():
        if k in _TABLE_IRRELEVANT_KEYS:
            continue
        if k == "_children" and isinstance(v, list):
            out[k] = [
                {
                    ck: _json_safe(cv)
                    for ck, cv in child.items()
                    if ck not in _TABLE_IRRELEVANT_KEYS
                }
                for child in v
            ]
        else:
            out[k] = _json_safe(v)
    return out


def _compile_column(entry, *, tree_mode: bool = False) -> dict:
    if isinstance(entry, str):
        entry = {"key": entry}
    key = entry["key"]
    base = COLUMN_REGISTRY[key]
    layout = {k: entry[k] for k in entry if k in _LAYOUT_KEYS}
    opts = {k: v for k, v in entry.items() if k != "key" and k not in _LAYOUT_KEYS}

    # In tree mode, the date column needs chevron + role-badge rendering;
    # route it through a dedicated JS renderer.  (Time-of-day and total
    # session duration live in the separate ``time_of_day`` column.)
    renderer = base.renderer
    if tree_mode and key == "date":
        renderer = "tree_date"

    return {
        "key": key,
        "header": layout.get("header", base.header),
        "width": layout.get("width", base.width),
        "align": layout.get("align", base.align),
        "sortable": layout.get("sortable", base.sortable),
        "default_asc": layout.get("default_asc", base.default_asc),
        "renderer": renderer,
        "format": base.format or base.key,
        "sort_key": base.sort_key or base.key,
        "opts": _enrich_opts(key, opts),
    }


# ---------------------------------------------------------------------------
# Public component
# ---------------------------------------------------------------------------


def WorkoutTable(
    results: list,
    columns: list,
    *,
    paginate: bool = True,
    rows_per_page: int = _DEFAULT_ROWS_PER_PAGE,
    highlight=None,
    default_sort_col: str = "date",
    default_sort_asc: bool = False,
    on_event: dict | None = None,
    visible_ids: list | None = None,
    reset_token: str = "",
    tree_mode: bool = False,
    sessions_dict: dict | None = None,
) -> None:
    """Render a sortable, paginated table.  See module docstring for column
    entry shape and event names.

    Parameters
    ----------
    results          List of row dicts (workout / rank rows).  Each row
                     should have a stable ``id`` field.
    columns          Ordered list of column entries (string key or dict).
    paginate         Show prev/next pagination controls.
    rows_per_page    Rows per page when paginate=True.
    highlight        Optional callable ``fn(row) -> bool``.  Resolved to a
                     ``highlight_ids`` list once before shipping.
    default_sort_col Column key for the initial sort.
    default_sort_asc Initial sort direction.
    on_event         dict mapping event name to handler.  Each handler is
                     called with a JSON-serializable payload dict.
    visible_ids      Optional list of row ids to display.  None = show all.
                     Use this with filter UIs that change frequently —
                     ship the full dataset once, push only the id list on
                     filter change.
    reset_token      String that, when changed, causes the JS plugin to
                     reset sort + page back to defaults.  Replaces the
                     ``hd.scope(filter_key)`` reset trick.
    tree_mode        Group rows into expandable session blocks (Workouts
                     page).  When True, ``results`` is fed into
                     :func:`services.session_rollup.build_session_rows` and
                     the resulting parent rows (with embedded ``_children``)
                     are shipped to the JS plugin.  ``sessions_dict`` is
                     required.
    sessions_dict    AppContext sessions dict.  Used only when ``tree_mode``.
    """
    if not results:
        hd.text("No results.", font_color="neutral-500", font_size="small")
        return

    if tree_mode:
        from services.session_rollup import build_session_rows

        results = build_session_rows(results, sessions_dict or {})
        if not results:
            hd.text("No results.", font_color="neutral-500", font_size="small")
            return

    column_configs = [_compile_column(c, tree_mode=tree_mode) for c in columns]

    if highlight is not None:
        highlight_ids = [r["id"] for r in results if "id" in r and highlight(r)]
    else:
        highlight_ids = []

    plugin = _WorkoutTablePlugin(
        rows=[_row_for_js(r) for r in results],
        column_configs=column_configs,
        visible_ids=visible_ids,
        highlight_ids=highlight_ids,
        default_sort_col=default_sort_col,
        default_sort_asc=default_sort_asc,
        paginate=paginate,
        rows_per_page=rows_per_page,
        reset_token=reset_token,
        tree_mode=tree_mode,
    )

    # ── Event dispatch ───────────────────────────────────────────────────
    seen = hd.state(seq=0)
    ev = plugin.event_out
    if ev and isinstance(ev, dict):
        seq = ev.get("seq", 0)
        if seq > seen.seq:
            seen.seq = seq
            handler = (on_event or {}).get(ev.get("name"))
            if handler:
                handler(ev.get("payload") or {})
