"""
Volume tab — stacked spread-zone bar chart + distribution data table.

Volume chart:
  - Stacked bar chart showing meters per zone per week / month / season.
  - Zone mode toggle: Power Spread | HR Spread | Workout Quality
      Power Spread mode: zones derived from time-aware reference watts (each
                  workout classified against the rower's fitness at the
                  workout's own date via services/reference_watts.py →
                  volume_bins.py).
      HR Spread mode:    zones derived from % of HRmax (heartrate_utils.py).
      Workout Quality:   one bucket per workout's overall Quality category
                  (Low/Medium/High/Ultra) plus an Unrated bucket for
                  workouts whose reference-watts index can't resolve them.
                  Interval rest_distance is still counted as Rest, never
                  coloured with the workout's quality.
  - Toggle: Weekly | Monthly | Seasonal  (radio button group)
  - Season and machine filters are applied globally (from app.py gfilter)
    before this component receives workouts; no page-level filter UI for these.

HR mode details:
  - Max HR is read from browser localStorage (key "profile", explicit value) or
    estimated at the 98th percentile of all valid HR readings (is_estimated=True).
  - An inline callout below the controls row shows the active max HR and
    allows in-situ editing which persists to browser localStorage via profile key.
  - If no max HR can be determined, the chart is replaced by a prompt to
    enter max HR manually.
  - Coverage line shows how many workouts have HR data.
"""

from datetime import date

import hyperdiv as hd

import json
import sys

from components.concept2_sync import get_all_workouts
from components.reference_watts_loader import reference_watts_loader
from components.view_context import your
from services.formatters import fmt_meters
from services.rowing_utils import get_season, profile_complete
from services.threshold_cache import make_thresholds_resolver
from services.workout_enrichment import attach_spread_and_quality

from services.volume_bins import (
    QUALITY_BIN_NAMES,
    aggregate_workouts,
    workout_bin_meters,
    workout_quality_bin_meters,
)
from services.heartrate_utils import (
    resolve_max_hr,
    workout_hr_meters,
    hr_coverage,
    HR_ZONE_NAMES,
    HR_ZONE_COLORS,
    HR_ZONE_DRAW_ORDER,
    HR_Z1_BINS,
    HR_Z2_BINS,
    HR_Z3_BINS,
    is_valid_hr,
)
from services.workout_quality import QUALITY_STYLE
from components.profile_page import get_profile_from_context
from components.volume_chart_builder import build_volume_chart_config, get_period_rows
from components.volume_chart_plugin import VolumeChart
from components.hyperdiv_extensions import grid_box
from components.shared_ui import global_filter_ui

# HR Z3 sub-zones: bin 2 = Z4 Threshold (80–90 %), bin 1 = Z5 Max (> 90 %)
_HR_Z3A_BINS = frozenset({2})  # Threshold
_HR_Z3B_BINS = frozenset({1})  # Max
_HR_NO_DATA_BINS = frozenset({6})  # "No HR" — excluded from classification denominator


# ---------------------------------------------------------------------------
# Quality-mode bin colors
# ---------------------------------------------------------------------------
# QUALITY_BIN_NAMES = ["Rest", "Low", "Medium", "High", "Ultra", "Unrated"]
# build_volume_chart_config wants (dark_rgba, light_rgba) tuples.  Quality
# styles are single-themed (only one rgba), so we use the same colour for
# both themes.  Rest re-uses the standard Rest grey; Unrated picks a neutral
# grey that visually reads as "no signal".

def _quality_rgba(category: str) -> str:
    r, g, b, a = QUALITY_STYLE[category]["bg"]
    return f"rgba({r},{g},{b},{a})"


_QUALITY_BIN_COLORS: list[tuple[str, str]] = [
    ("rgba(120,120,120,1)", "rgba(155,155,155,1)"),  # 0 Rest
    (_quality_rgba("Low"), _quality_rgba("Low")),  # 1 Low
    (_quality_rgba("Medium"), _quality_rgba("Medium")),  # 2 Medium
    (_quality_rgba("High"), _quality_rgba("High")),  # 3 High
    (_quality_rgba("Ultra"), _quality_rgba("Ultra")),  # 4 Ultra
    ("rgba(195,195,195,0.45)", "rgba(210,210,210,0.55)"),  # 5 Unrated
]
# Bottom → top stacking order: Ultra, High, Medium, Low, Rest, Unrated.
# Highest quality at the base so the "good" colours anchor the bar.
_QUALITY_DRAW_ORDER: list[int] = [4, 3, 2, 1, 0, 5]


def _quality_period_rows(aggregated: dict, view: str) -> list:
    """Return one dict per period for the Workout Quality distribution table.

    Each dict has the labels and pre-formatted strings the table needs.
    """
    if view == "weekly":
        raw_data = aggregated.get("weeks", {})
    elif view == "monthly":
        raw_data = aggregated.get("months", {})
    else:
        raw_data = aggregated.get("seasons", {})

    # Reuse the chart builder's existing helpers for label formatting.
    from components.volume_chart_builder import (
        _filter_and_sort_keys,
        _month_label,
        _week_label,
    )

    keys = _filter_and_sort_keys(list(raw_data.keys()), view, "all_time", date.today())
    rows = []
    for k in reversed(keys):
        b = raw_data[k]["bins"]
        rest, low_m, med_m, high_m, ultra_m, unrated_m = b[:6]
        total = sum(b)
        if view == "weekly":
            label = _week_label(k)
        elif view == "monthly":
            label = _month_label(k)
        else:
            label = k

        def _pct(part):
            return f"{round(part / total * 100)}%" if total > 0 else "0%"

        rows.append(
            {
                "label": label,
                "total": fmt_meters(total),
                "rest": fmt_meters(rest),
                "low_m": fmt_meters(low_m),
                "low_pct": _pct(low_m),
                "med_m": fmt_meters(med_m),
                "med_pct": _pct(med_m),
                "high_m": fmt_meters(high_m),
                "high_pct": _pct(high_m),
                "ultra_m": fmt_meters(ultra_m),
                "ultra_pct": _pct(ultra_m),
                "unrated_m": fmt_meters(unrated_m),
                "unrated_pct": _pct(unrated_m),
                "total_raw": total,
                "rest_raw": rest,
                "low_raw": low_m,
                "med_raw": med_m,
                "high_raw": high_m,
                "ultra_raw": ultra_m,
                "unrated_raw": unrated_m,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Distribution data table
# ---------------------------------------------------------------------------

_PERIOD_HEADERS = {
    "weekly": "Week",
    "monthly": "Month",
    "seasonal": "Season",
}


def _distribution_table(
    rows: list, view: str, zone_mode: str = "power_spread"
) -> None:
    """
    Render a sortable CSS Grid table with one row per period showing zone
    breakdowns and a training distribution classification.

    Power Spread mode columns:
      Period | Total | Rest | Z1 Easy | Z2 Threshold | Z3 Hard | Distribution

    HR Spread mode columns:
      Period | Total | Rest | Easy (<70%) | Tempo (70–80%)
      | Threshold (80–90%) | Max (90%+) | Distribution

    Workout Quality mode columns:
      Period | Total | Rest | Low | Medium | High | Ultra | Unrated

    Sort state resets when view or zone_mode changes (via scope key).
    """
    period_col = _PERIOD_HEADERS.get(view, "Period")

    # Column definitions: (header_label, sort_key, css_width, render_fn)
    # render_fn=None → Distribution badge rendered specially
    if zone_mode == "quality":
        col_defs = [
            (period_col, "idx", "9rem", lambda r: r["label"]),
            ("Total", "total", "7rem", lambda r: r["total"]),
            ("Rest", "rest", "7rem", lambda r: r["rest"]),
            (
                "Low",
                "low",
                "minmax(8rem,1fr)",
                lambda r: f"{r['low_m']}  ({r['low_pct']})",
            ),
            (
                "Medium",
                "med",
                "minmax(8rem,1fr)",
                lambda r: f"{r['med_m']}  ({r['med_pct']})",
            ),
            (
                "High",
                "high",
                "minmax(8rem,1fr)",
                lambda r: f"{r['high_m']}  ({r['high_pct']})",
            ),
            (
                "Ultra",
                "ultra",
                "minmax(8rem,1fr)",
                lambda r: f"{r['ultra_m']}  ({r['ultra_pct']})",
            ),
            (
                "Unrated",
                "unrated",
                "minmax(8rem,1fr)",
                lambda r: f"{r['unrated_m']}  ({r['unrated_pct']})",
            ),
        ]
    elif zone_mode == "hr":
        col_defs = [
            (period_col, "idx", "9rem", lambda r: r["label"]),
            ("Total", "total", "7rem", lambda r: r["total"]),
            ("Rest", "rest", "7rem", lambda r: r["rest"]),
            (
                "Easy (<70%)",
                "z1",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z1_m']}  ({r['z1_pct']})",
            ),
            (
                "Tempo (70–80%)",
                "z2",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z2_m']}  ({r['z2_pct']})",
            ),
            (
                "Threshold (80–90%)",
                "z3a",
                "minmax(9rem,1fr)",
                lambda r: f"{r.get('z3a_m', '—')}  ({r.get('z3a_pct', '0%')})",
            ),
            (
                "Max (90%+)",
                "z3b",
                "minmax(9rem,1fr)",
                lambda r: f"{r.get('z3b_m', '—')}  ({r.get('z3b_pct', '0%')})",
            ),
            ("Distribution", "dist", "9rem", None),
        ]
    else:
        col_defs = [
            (period_col, "idx", "9rem", lambda r: r["label"]),
            ("Total", "total", "7rem", lambda r: r["total"]),
            ("Rest", "rest", "7rem", lambda r: r["rest"]),
            (
                "Z1 Easy",
                "z1",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z1_m']}  ({r['z1_pct']})",
            ),
            (
                "Z2 Threshold",
                "z2",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z2_m']}  ({r['z2_pct']})",
            ),
            (
                "Z3 Hard",
                "z3",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z3_m']}  ({r['z3_pct']})",
            ),
            ("Distribution", "dist", "9rem", None),
        ]

    col_template = " ".join(w for _, _, w, _ in col_defs)
    n_cols = len(col_defs)

    # Reset sort when view or zone_mode changes
    with hd.scope(f"{view}_{zone_mode}"):
        # Default: idx asc=False → index 0 (newest) first
        sort = hd.state(col="idx", asc=True)

        # Sort rows (rows are already newest-first at index 0)
        _SORT_KEYS = {
            "idx": lambda i, r: i,
            "total": lambda i, r: r.get("total_raw", 0),
            "rest": lambda i, r: r.get("rest_raw", 0),
            "z1": lambda i, r: r.get("z1_raw", 0),
            "z2": lambda i, r: r.get("z2_raw", 0),
            "z3": lambda i, r: r.get("z3_raw", 0),
            "z3a": lambda i, r: r.get("z3a_raw", 0),
            "z3b": lambda i, r: r.get("z3b_raw", 0),
            "dist": lambda i, r: r.get("distribution", ""),
            "low": lambda i, r: r.get("low_raw", 0),
            "med": lambda i, r: r.get("med_raw", 0),
            "high": lambda i, r: r.get("high_raw", 0),
            "ultra": lambda i, r: r.get("ultra_raw", 0),
            "unrated": lambda i, r: r.get("unrated_raw", 0),
        }
        key_fn = _SORT_KEYS.get(sort.col, _SORT_KEYS["idx"])
        indexed = list(enumerate(rows))
        sorted_rows = sorted(indexed, key=lambda p: key_fn(*p), reverse=not sort.asc)

        with hd.box(padding=(1, 0, 0, 0)):
            with grid_box(
                grid_template_columns=col_template,
                width="100%",
                border="1px solid neutral-200",
                border_radius="medium",
                overflow="hidden",
            ):
                # ── Header row ─────────────────────────────────────────────
                for ci, (header, col_key, _, _) in enumerate(col_defs):
                    with hd.scope(f"hdr_{col_key}"):
                        is_sorted = sort.col == col_key
                        arrow = (" ▲" if sort.asc else " ▼") if is_sorted else ""
                        cell_props = dict(
                            padding=(0.5, 0.75),
                            background_color="neutral-50",
                            border_bottom="1px solid neutral-200",
                            align="center",
                        )
                        if ci < n_cols - 1:
                            cell_props["border_right"] = "1px solid neutral-200"
                        with hd.box(**cell_props):
                            btn = hd.button(
                                f"{header}{arrow}",
                                variant="text",
                                font_size="small",
                                font_weight="semibold",
                                font_color="neutral-700"
                                if is_sorted
                                else "neutral-500",
                            )
                            if btn.clicked:
                                if sort.col == col_key:
                                    sort.asc = not sort.asc
                                else:
                                    sort.col = col_key
                                    # First click: descending for numeric cols, ascending for period/dist
                                    sort.asc = col_key in ("idx", "dist")

                # ── Data rows ──────────────────────────────────────────────
                for orig_i, row in sorted_rows:
                    row_bg = "neutral-50" if orig_i % 2 == 0 else "neutral-0"
                    with hd.scope(f"row_{orig_i}"):
                        for ci, (_, col_key, _, render_fn) in enumerate(col_defs):
                            with hd.scope(f"c{ci}{col_key}"):
                                cell_props = dict(
                                    padding=(0.5, 0.75),
                                    background_color=row_bg,
                                    border_top="1px solid neutral-100",
                                    align="end",
                                    justify="center",
                                )
                                if ci < n_cols - 1:
                                    cell_props["border_right"] = "1px solid neutral-100"
                                with hd.box(**cell_props):
                                    if col_key == "dist":
                                        hd.text(row["distribution"])
                                    else:
                                        hd.text(render_fn(row), font_size="small")


# ---------------------------------------------------------------------------
# Volume chart section
# ---------------------------------------------------------------------------


def _hr_callout(all_workouts: list, profile: dict, is_owner: bool = True) -> tuple:
    """
    Render the HR mode info bar.  Returns (max_hr, ok) where ok=False means
    there is no usable max HR and the chart should be suppressed.

    Shows:
      • "Max HR:" label, current value (or placeholder)
      • Inline edit field; Save button appears only when the field value
        differs from the stored max HR.
      • HR coverage: "HR data in N of M workouts."
    """
    max_hr, is_estimated = resolve_max_hr(profile, all_workouts)
    with_hr, total = hr_coverage(all_workouts)

    with hd.hbox(
        border="1px solid neutral-200",
        border_radius="medium",
        background_color="neutral-50",
        padding=1,
        gap=1,
        align="center",
        wrap="wrap",
    ):
        # ── Max HR label + source note ─────────────────────────────────────
        hd.text("Max HR:", font_size="small", font_color="neutral-600")

        # ── Inline edit (owner only) ────────────────────────────────────────
        if is_owner:
            with hd.scope("hr_edit"):
                hr_input = hd.text_input(
                    placeholder="e.g. 185",
                    value=str(max_hr) if max_hr else "",
                    size="small",
                    width=6,
                )
                # Save button only when the field value differs from what's stored
                stored_str = str(max_hr) if max_hr else ""
                if hr_input.value != stored_str:
                    save_btn = hd.button("Save", size="small", variant="primary")
                    if save_btn.clicked and hr_input.value:
                        try:
                            new_val = int(hr_input.value)
                            if is_valid_hr(new_val):
                                hd.local_storage.set_item(
                                    "profile",
                                    json.dumps({**profile, "max_heart_rate": new_val}),
                                )
                                max_hr = new_val
                                is_estimated = False
                        except ValueError:
                            pass
        else:
            hd.text(
                str(max_hr) if max_hr else "—",
                font_size="small",
                font_weight="semibold",
            )

        # ── Coverage ──────────────────────────────────────────────────────
        hd.text(
            f"HR data in {with_hr} of {total} workouts.",
            font_size="small",
            font_color="neutral-400",
        )


def _volume_section(
    all_workouts: list,
    profile: dict,
    is_owner: bool = True,
    ctx=None,
) -> None:
    """Render the volume controls + stacked bar chart."""

    state = hd.state(
        view="monthly",
        zone_mode="power_spread",  # "power_spread" | "hr" | "quality"
    )
    view = state.view
    zone_mode = state.zone_mode  # captured before any mid-render mutations

    with hd.box(gap=1, align="center"):
        with hd.box(gap=0.2, align="center"):
            hd.h1(f"How Does {your(ctx)} Work Stack Up?")
            global_filter_ui(ctx)

        # ── HR callout (only in HR mode) — must come before chart to resolve max_hr ──
        max_hr, is_estimated = resolve_max_hr(profile, all_workouts)

        # ── Compute chart data ────────────────────────────────────────────────────
        if zone_mode == "hr" and not max_hr:
            # No max HR — skip chart and table; callout already rendered above.
            return
        elif zone_mode == "hr":
            aggregated = aggregate_workouts(
                all_workouts,
                bin_fn=lambda w: workout_hr_meters(w, max_hr),
            )
            chart_config = build_volume_chart_config(
                aggregated,
                view=view,
                scope="all_time",
                today=date.today(),
                bin_names=HR_ZONE_NAMES,
                bin_colors=HR_ZONE_COLORS,
                draw_order=HR_ZONE_DRAW_ORDER,
            )
            rows = get_period_rows(
                aggregated,
                view,
                "all_time",
                today=date.today(),
                z1_bins=HR_Z1_BINS,
                z2_bins=HR_Z2_BINS,
                z3_bins=HR_Z3_BINS,
                z3a_bins=_HR_Z3A_BINS,
                z3b_bins=_HR_Z3B_BINS,
                no_data_bins=_HR_NO_DATA_BINS,
            )
        elif zone_mode == "quality":
            # Quality requires per-date reference watts + thresholds.
            if not reference_watts_loader(all_workouts):
                return
            # Attach _quality to each workout so we can read it from the bin_fn.
            attach_spread_and_quality(all_workouts, all_workouts, max_hr)
            unrated_count = sum(
                1 for w in all_workouts if w.get("_quality") is None
            )
            if unrated_count:
                # An unrated workout is generally a bug — flag it once per
                # render so the underlying reference-watts gap stays visible.
                print(
                    f"[volume_page] {unrated_count} workouts have no quality "
                    "rating (missing reference watts at their date)",
                    file=sys.stderr,
                )
            aggregated = aggregate_workouts(
                all_workouts,
                bin_fn=lambda w: workout_quality_bin_meters(w, w.get("_quality")),
            )
            chart_config = build_volume_chart_config(
                aggregated,
                view=view,
                scope="all_time",
                today=date.today(),
                bin_names=QUALITY_BIN_NAMES,
                bin_colors=_QUALITY_BIN_COLORS,
                draw_order=_QUALITY_DRAW_ORDER,
            )
            rows = _quality_period_rows(aggregated, view)
        else:
            # Time-aware thresholds: each workout is classified against the
            # rower's fitness at the workout's own date.  Gate on the
            # reference-watts loader so the first-time index build shows a
            # progress bar instead of blocking the render.
            if not reference_watts_loader(all_workouts):
                return

            _thresholds_for, _ = make_thresholds_resolver(all_workouts)

            aggregated = aggregate_workouts(
                all_workouts,
                bin_fn=lambda w: workout_bin_meters(w, _thresholds_for(w)),
            )
            chart_config = build_volume_chart_config(
                aggregated,
                view=view,
                scope="all_time",
                today=date.today(),
            )
            rows = get_period_rows(aggregated, view, "all_time", today=date.today())

        # ── Chart ────────────────────────────────────────────────────────────────
        if chart_config:
            with hd.box(height="42vh", width="100%"):
                VolumeChart(config=chart_config)
        else:
            with hd.box(padding=3, align="center"):
                hd.text(
                    "Not enough data for the selected scope.",
                    font_color="neutral-500",
                    font_size="small",
                )

        # ── Controls row ─────────────────────────────────────────────────────────
        with hd.hbox(gap=3, align="center", padding=(0, 0, 1, 0), wrap="wrap"):
            # View radio group (Weekly / Monthly / Seasonal)
            with hd.radio_buttons(
                value=state.view,
                font_size="small",
            ) as view_rg:
                hd.radio_button("Weekly", value="weekly")
                hd.radio_button("Monthly", value="monthly")
                hd.radio_button("Seasonal", value="seasonal")

            if view_rg.changed:
                state.view = view_rg.value.lower()

            # Zone mode radio group
            with hd.radio_buttons(
                value=state.zone_mode,
                font_size="small",
            ) as mode_rg:
                hd.radio_button("Power Spread", value="power_spread")
                hd.radio_button("HR Spread", value="hr")
                hd.radio_button("Workout Quality", value="quality")

            if mode_rg.changed:
                state.zone_mode = mode_rg.value

        if zone_mode == "hr":
            _hr_callout(all_workouts, profile, is_owner=is_owner)

        # ── Distribution table ───────────────────────────────────────────────────
        if rows:
            _distribution_table(rows, view, zone_mode=zone_mode)


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def volume_page(ctx) -> None:
    """Top-level component for the Volume tab."""

    result = get_all_workouts(ctx)

    profile = get_profile_from_context(ctx)

    if result is None or not profile:
        hd.box(padding=2, min_height="80vh")
        return

    _, all_workouts = result

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    with hd.box(padding=2, min_height="80vh"):
        _volume_section(
            all_workouts,
            profile,
            is_owner=ctx.is_owner,
            ctx=ctx,
        )
