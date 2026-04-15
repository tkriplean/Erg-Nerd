"""
Volume tab — stacked intensity-zone bar chart + distribution data table.

Volume chart:
  - Stacked bar chart showing meters per intensity zone per week / month / season.
  - Zone mode toggle: Pace | HR
      Pace mode: zones derived from personal-best pace thresholds (volume_bins.py).
      HR mode:   zones derived from % of HRmax (heartrate_utils.py).
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

from components.concept2_sync import concept2_sync
from services.formatters import machine_label
from services.rowing_utils import get_season, profile_complete

from services.volume_bins import (
    get_reference_sbs,
    compute_bin_thresholds,
    aggregate_workouts,
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
from components.profile_page import get_profile
from components.volume_chart_builder import build_volume_chart_config, get_period_rows
from components.volume_chart_plugin import VolumeChart
from components.hyperdiv_extensions import grid_box

# HR Z3 sub-zones: bin 2 = Z4 Threshold (80–90 %), bin 1 = Z5 Max (> 90 %)
_HR_Z3A_BINS = frozenset({2})  # Threshold
_HR_Z3B_BINS = frozenset({1})  # Max
_HR_NO_DATA_BINS = frozenset({6})  # "No HR" — excluded from classification denominator


# ---------------------------------------------------------------------------
# Distribution data table
# ---------------------------------------------------------------------------

_PERIOD_HEADERS = {
    "weekly": "Week",
    "monthly": "Month",
    "seasonal": "Season",
}


def _distribution_table(
    rows: list, view: str, zone_mode: str = "pace_intensity"
) -> None:
    """
    Render a sortable CSS Grid table with one row per period showing zone
    breakdowns and a training distribution classification.

    Pace mode columns:
      Period | Total | Rest | Z1 Easy | Z2 Threshold | Z3 Hard | Distribution

    HR mode columns:
      Period | Total | Rest | Easy (<70%) | Tempo (70–80%)
      | Threshold (80–90%) | Max (90%+) | Distribution

    Sort state resets when view or zone_mode changes (via scope key).
    """
    period_col = _PERIOD_HEADERS.get(view, "Period")

    # Column definitions: (header_label, sort_key, css_width, render_fn)
    # render_fn=None → Distribution badge rendered specially
    if zone_mode == "hr":
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


def _hr_callout(all_workouts: list, profile: dict) -> tuple:
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

        # ── Inline edit ────────────────────────────────────────────────────
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

        # ── Coverage ──────────────────────────────────────────────────────
        hd.text(
            f"HR data in {with_hr} of {total} workouts.",
            font_size="small",
            font_color="neutral-400",
        )


def _volume_section(all_workouts: list, profile: dict, machine: str = "All") -> None:
    """Render the volume controls + stacked bar chart."""

    state = hd.state(
        view="monthly",
        zone_mode="pace_intensity",  # "pace_intensity" | "hr"
    )
    view = state.view
    machine_filter = None if machine == "All" else {machine}

    with hd.box(gap=1, align="center"):
        hd.h1("How Does Your Work Stack Up?")

        # ── HR callout (only in HR mode) — must come before chart to resolve max_hr ──
        max_hr, is_estimated = resolve_max_hr(profile, all_workouts)

        # ── Compute chart data ────────────────────────────────────────────────────
        if state.zone_mode == "hr" and not max_hr:
            # No max HR — skip chart and table; callout already rendered above.
            return
        elif state.zone_mode == "hr":
            aggregated = aggregate_workouts(
                all_workouts,
                machine_filter=machine_filter,
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
        else:
            ref_sbs = get_reference_sbs(all_workouts)
            thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
            aggregated = aggregate_workouts(all_workouts, thresholds, machine_filter)
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

            # Zone mode radio group (Pace / HR)
            with hd.radio_buttons(
                value=state.zone_mode,
                font_size="small",
            ) as mode_rg:
                hd.radio_button("Pace Intensity", value="pace_intensity")
                hd.radio_button("HR Intensity", value="hr")

            if mode_rg.changed:
                state.zone_mode = mode_rg.value
                print(state.zone_mode)

        if state.zone_mode == "hr":
            _hr_callout(all_workouts, profile)

        # ── Distribution table ───────────────────────────────────────────────────
        if rows:
            _distribution_table(rows, view, zone_mode=state.zone_mode)


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def volume_page(client, user_id: str, excluded_seasons=(), machine="All") -> None:
    """Top-level component for the Volume tab."""

    result = concept2_sync(client)
    if result is None:
        return
    _workouts_dict, all_workouts = result

    # Apply global filters
    if excluded_seasons:
        all_workouts = [
            w
            for w in all_workouts
            if get_season(w.get("date", "")) not in set(excluded_seasons)
        ]
    if machine != "All":
        all_workouts = [w for w in all_workouts if w.get("type") == machine]

    profile = get_profile()
    if not profile:
        return

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    with hd.box(padding=(2, 2, 2, 2)):
        _volume_section(all_workouts, profile, machine=machine)
