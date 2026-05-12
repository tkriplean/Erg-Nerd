"""
components/data_table.py — Generic JS-backed sortable table plugin.

A lightweight sibling to :mod:`components.workout_table`: same visual style
(grid layout, sort-arrow headers, optional pagination) but data-agnostic.
Used for tables whose cells are plain strings — no workout-specific
renderers, no tree mode, no search bar.

The shared visual look comes from ``chart_assets/table_shared.js`` (loaded
before this plugin's main JS so ``window.ErgNerdTable.gridCSS`` is
available at registration time).

Column entry shape (each entry is a dict)::

    {
        "key":          "z1",                  # unique id (state/sort)
        "header":       "Easy",                # display label
        "width":        "minmax(9rem,1fr)",    # CSS grid track size
        "align":        "end",                 # "start" | "center" | "end"
        "sortable":     True,
        "default_asc":  False,                 # sort direction on first click
        "renderer":     "text",                # forward-compat hook
        "value_key":    "z1_m",                # row field shown as text
        "secondary_key": "z1_pct",             # optional; shown as " (val)"
        "sort_key":     "z1_raw",              # optional; defaults to value_key
    }

Rows must include any ``sort_key`` fields (typically ``*_raw`` numeric).
The plugin stamps an ``_idx`` field on each row before shipping so a
column may sort by insertion order with ``sort_key="_idx"``.

Sort and pagination state live in JS and persist to ``sessionStorage``
keyed by the plugin's component id, so a return-trip and back-button
restores the table to where it was.  ``reset_token`` resets sort/page/
show-all when it changes.

The ``initial_rows`` prop (0 = disabled) hides all but the first N rows
on the current page behind a "Show all" overlay button.  Bottom
pagination is suppressed while the overlay is visible (top pagination
stays).  See ``chart_assets/data_table_plugin.js`` for the render rule.
"""

from __future__ import annotations

import datetime as _dt
import os

import hyperdiv as hd


_HERE = os.path.dirname(__file__)


_DEFAULT_ROWS_PER_PAGE = 50


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class _DataTablePlugin(hd.Plugin):
    _name = "DataTable"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        ("js-link", os.path.join(_HERE, "chart_assets", "table_shared.js")),
        ("js-link", os.path.join(_HERE, "chart_assets", "data_table_plugin.js")),
    ]

    rows = hd.Prop(hd.Any, [])
    columns = hd.Prop(hd.Any, [])
    default_sort_col = hd.Prop(hd.String, "")
    default_sort_asc = hd.Prop(hd.Bool, True)
    paginate = hd.Prop(hd.Bool, False)
    rows_per_page = hd.Prop(hd.Int, _DEFAULT_ROWS_PER_PAGE)
    # 0 disables the show-all overlay.  When >0, only the first N rows of
    # the current page are visible until the overlay is clicked.
    initial_rows = hd.Prop(hd.Int, 0)
    # When this string changes, JS resets sort/page/show-all back to
    # defaults.  Use it to mirror filter state from Python.
    reset_token = hd.Prop(hd.String, "")
    # JS → Python event dispatch.  Reserved for future use; current
    # consumers don't emit events.
    event_out = hd.Prop(hd.Any, None)


# ---------------------------------------------------------------------------
# Helpers — JSON coercion
# ---------------------------------------------------------------------------


def _json_safe(v):
    """Recursively coerce non-JSON-serializable values into JSON-friendly
    equivalents.  Mirrors the workout_table helper but kept local so this
    module has no cross-table dependency."""
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, set):
        return [_json_safe(x) for x in v]
    return v


def _row_for_js(r: dict, idx: int) -> dict:
    """Coerce a row into JSON-safe form and stamp ``_idx`` for sort_key='_idx'."""
    out = {k: _json_safe(v) for k, v in r.items()}
    out["_idx"] = idx
    return out


# ---------------------------------------------------------------------------
# Public component
# ---------------------------------------------------------------------------


def DataTable(
    rows: list,
    columns: list,
    *,
    paginate: bool = False,
    rows_per_page: int = _DEFAULT_ROWS_PER_PAGE,
    initial_rows: int = 0,
    default_sort_col: str = "",
    default_sort_asc: bool = True,
    reset_token: str = "",
    on_event: dict | None = None,
) -> None:
    """Render a generic sortable JS table.

    Parameters
    ----------
    rows             List of dicts.  Each row's keys are referenced by the
                     column entries' ``value_key`` / ``secondary_key`` /
                     ``sort_key`` fields.
    columns          List of column dicts (see module docstring).
    paginate         Show prev/next pagination controls.
    rows_per_page    Rows per page when paginate=True.
    initial_rows     0 = disabled.  >0 hides rows past the Nth on the
                     current page behind a "Show all" overlay button.
    default_sort_col Column ``key`` for the initial sort.  Empty = unsorted
                     (rows render in their input order, modulo ``_idx``).
    default_sort_asc Initial sort direction.
    reset_token      Bumping this string resets sort/page/show-all.
    on_event         dict mapping event name → handler.  Reserved; current
                     consumers don't emit events.
    """
    if not rows:
        hd.text("No results.", font_color="neutral-500", font_size="small")
        return

    rows_js = [_row_for_js(r, i) for i, r in enumerate(rows)]
    columns_js = [_json_safe(c) for c in columns]

    plugin = _DataTablePlugin(
        rows=rows_js,
        columns=columns_js,
        default_sort_col=default_sort_col,
        default_sort_asc=default_sort_asc,
        paginate=paginate,
        rows_per_page=rows_per_page,
        initial_rows=initial_rows,
        reset_token=reset_token,
    )

    # Event dispatch — same seq-counter pattern as WorkoutTable.  Kept in
    # place so future stateful renderers can wire handlers without churn.
    seen = hd.state(seq=0)
    ev = plugin.event_out
    if ev and isinstance(ev, dict):
        seq = ev.get("seq", 0)
        if seq > seen.seq:
            seen.seq = seq
            handler = (on_event or {}).get(ev.get("name"))
            if handler:
                handler(ev.get("payload") or {})
