"""
Volume tab — stacked spread-zone bar chart + distribution data table.

Volume chart:
  - Stacked bar chart showing meters per zone per week / month / season.
  - Zone mode toggle: Power Spread | HR Spread | Workout Severity
      Power Spread mode: zones derived from time-aware reference watts (each
                  workout classified against the rower's fitness at the
                  workout's own date via services/reference_watts.py →
                  volume_bins.py).
      HR Spread mode:    zones derived from % of HRmax (heartrate_utils.py).
      Workout Severity:  one bucket per workout's overall Severity category
                  (Low/Moderate/High/Maximal) plus an Unrated bucket for
                  workouts whose ESS metrics can't be computed.  Interval
                  rest_distance is still counted as Rest, never coloured
                  with the workout's severity.
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
from components.app_context import AppContext, your
from services.formatters import fmt_meters
from services.rowing_utils import profile_complete
from components.add_metrics import add_metrics

from services.volume_bins import (
    SEVERITY_BIN_NAMES,
    aggregate_workouts,
    workout_severity_bin_meters,
    workout_zone_meters,
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
from services.erg_stress import SEVERITY_STYLE
from components.app_context import get_profile
from components.volume_chart_builder import build_volume_chart_config, get_period_rows
from components.volume_chart_plugin import VolumeChart
from components.hyperdiv_extensions import grid_box
from components.shared_ui import global_filter_ui

from datetime import timedelta
from services.erg_stress import STIMULUS_T_THRESH, ZONE_BANDS_S
from services.training_load import (
    compute_ctl_atl_tsb,
    daily_loads_from_workouts,
)
from services.volume_bins import BAND_TO_BIN, BIN_COLORS, BIN_NAMES
from components.training_load_chart_builder import build_training_load_chart_config
from components.training_load_chart_plugin import TrainingLoadChart

# HR Z3 sub-zones: bin 2 = Z4 Threshold (80–90 %), bin 1 = Z5 Max (> 90 %)
_HR_Z3A_BINS = frozenset({2})  # Threshold
_HR_Z3B_BINS = frozenset({1})  # Max
_HR_NO_DATA_BINS = frozenset({6})  # "No HR" — excluded from classification denominator


# ---------------------------------------------------------------------------
# Severity-mode bin colors
# ---------------------------------------------------------------------------
# SEVERITY_BIN_NAMES = ["Rest", "Low", "Moderate", "High", "Maximal", "Unrated"]
# build_volume_chart_config wants (dark_rgba, light_rgba) tuples.  Severity
# styles are single-themed (only one rgba), so we use the same colour for
# both themes.  Rest re-uses the standard Rest grey; Unrated picks a neutral
# grey that visually reads as "no signal".


def _severity_rgba(category: str) -> str:
    r, g, b, a = SEVERITY_STYLE[category]["bg"]
    return f"rgba({r},{g},{b},{a})"


_SEVERITY_BIN_COLORS: list[tuple[str, str]] = [
    ("rgba(120,120,120,1)", "rgba(155,155,155,1)"),  # 0 Rest
    (_severity_rgba("Low"), _severity_rgba("Low")),  # 1 Low
    (_severity_rgba("Moderate"), _severity_rgba("Moderate")),  # 2 Moderate
    (_severity_rgba("High"), _severity_rgba("High")),  # 3 High
    (_severity_rgba("Maximal"), _severity_rgba("Maximal")),  # 4 Maximal
    ("rgba(195,195,195,0.45)", "rgba(210,210,210,0.55)"),  # 5 Unrated
]
# Bottom → top stacking order: Maximal, High, Moderate, Low, Rest, Unrated.
# Highest severity at the base so the "hardest" colours anchor the bar.
_SEVERITY_DRAW_ORDER: list[int] = [4, 3, 2, 1, 0, 5]


def _severity_period_rows(aggregated: dict, view: str) -> list:
    """Return one dict per period for the Workout Severity distribution table.

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
        rest, low_m, mod_m, high_m, max_m, unrated_m = b[:6]
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
                "mod_m": fmt_meters(mod_m),
                "mod_pct": _pct(mod_m),
                "high_m": fmt_meters(high_m),
                "high_pct": _pct(high_m),
                "max_m": fmt_meters(max_m),
                "max_pct": _pct(max_m),
                "unrated_m": fmt_meters(unrated_m),
                "unrated_pct": _pct(unrated_m),
                "total_raw": total,
                "rest_raw": rest,
                "low_raw": low_m,
                "mod_raw": mod_m,
                "high_raw": high_m,
                "max_raw": max_m,
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


def _distribution_table(rows: list, view: str, zone_mode: str = "power_spread") -> None:
    """
    Render a sortable CSS Grid table with one row per period showing zone
    breakdowns and a training distribution classification.

    Power Spread mode columns:
      Period | Total | Rest | Z1 Easy | Z2 Threshold | Z3 Hard | Distribution

    HR Spread mode columns:
      Period | Total | Rest | Easy (<70%) | Tempo (70–80%)
      | Threshold (80–90%) | Max (90%+) | Distribution

    Workout Severity mode columns:
      Period | Total | Rest | Low | Moderate | High | Maximal | Unrated

    Sort state resets when view or zone_mode changes (via scope key).
    """
    period_col = _PERIOD_HEADERS.get(view, "Period")

    # Column definitions: (header_label, sort_key, css_width, render_fn)
    # render_fn=None → Distribution badge rendered specially
    if zone_mode == "severity":
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
                "Moderate",
                "mod",
                "minmax(8rem,1fr)",
                lambda r: f"{r['mod_m']}  ({r['mod_pct']})",
            ),
            (
                "High",
                "high",
                "minmax(8rem,1fr)",
                lambda r: f"{r['high_m']}  ({r['high_pct']})",
            ),
            (
                "Maximal",
                "max",
                "minmax(8rem,1fr)",
                lambda r: f"{r['max_m']}  ({r['max_pct']})",
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
                "Easy",
                "z1",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z1_m']}  ({r['z1_pct']})",
            ),
            (
                "Threshold",
                "z2",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z2_m']}  ({r['z2_pct']})",
            ),
            (
                "Hard",
                "z3",
                "minmax(9rem,1fr)",
                lambda r: f"{r['z3_m']}  ({r['z3_pct']})",
            ),
            ("Distribution", "dist", "9rem", None),
        ]

    col_template = " ".join(w for _, _, w, _ in col_defs)
    n_cols = len(col_defs)

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
        "mod": lambda i, r: r.get("mod_raw", 0),
        "high": lambda i, r: r.get("high_raw", 0),
        "max": lambda i, r: r.get("max_raw", 0),
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
                            font_color="neutral-700" if is_sorted else "neutral-500",
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


# Page state — connection-wide so view/mode toggles survive a round-trip
# through ``/workout/<id>``.
@hd.global_state
class VolumePageState(hd.BaseState):
    view = hd.Prop(hd.String, "monthly")
    # "power_spread" | "hr" | "severity"
    zone_mode = hd.Prop(hd.String, "power_spread")
    # "meters" | "percent"
    value_mode = hd.Prop(hd.String, "meters")


def _volume_section(
    all_workouts: list,
    profile: dict,
    is_owner: bool = True,
) -> None:
    """Render the volume controls + stacked bar chart."""

    state = VolumePageState()
    view = state.view
    zone_mode = state.zone_mode  # captured before any mid-render mutations
    value_mode = state.value_mode

    with hd.box(gap=1, align="center"):
        with hd.box(gap=0.2, align="center"):
            hd.h1(f"How Does {your()} Work Stack Up?")
            global_filter_ui()

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
                value_mode=value_mode,
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
        elif zone_mode == "severity":
            # Severity comes from add_metrics — needs the reference-watts
            # index too (for the multi-band intensity model).
            if not reference_watts_loader(all_workouts):
                return
            add_metrics(all_workouts, with_timeline=False)
            unrated_count = sum(1 for w in all_workouts if w.get("_severity") is None)
            if unrated_count:
                # An unrated workout is generally a bug — flag it once per
                # render so the underlying ESS gap stays visible.
                print(
                    f"[volume_page] {unrated_count} workouts have no severity "
                    "rating (ESS computation failed for their session)",
                    file=sys.stderr,
                )
            aggregated = aggregate_workouts(
                all_workouts,
                bin_fn=lambda w: workout_severity_bin_meters(w, w.get("_severity")),
            )
            chart_config = build_volume_chart_config(
                aggregated,
                view=view,
                scope="all_time",
                today=date.today(),
                bin_names=SEVERITY_BIN_NAMES,
                bin_colors=_SEVERITY_BIN_COLORS,
                draw_order=_SEVERITY_DRAW_ORDER,
                value_mode=value_mode,
            )
            rows = _severity_period_rows(aggregated, view)
        else:
            # Power Spread mode: classify each work-second to the duration
            # band whose reference watts is closest to that second's power,
            # then distribute meters by the resulting time-fractions.
            # Gate on the reference-watts loader so the first-time index
            # build shows a progress bar instead of blocking the render.
            if not reference_watts_loader(all_workouts):
                return

            # Populate ``_zone_time_fractions`` / ``_zone_bin_fractions`` on
            # each workout via the central metrics cache; subsequent renders
            # are O(1).
            add_metrics(all_workouts, with_timeline=False)

            aggregated = aggregate_workouts(
                all_workouts,
                bin_fn=workout_zone_meters,
            )
            chart_config = build_volume_chart_config(
                aggregated,
                view=view,
                scope="all_time",
                today=date.today(),
                value_mode=value_mode,
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
            # Y-axis units (meters vs % of period)
            with hd.radio_buttons(
                value=state.value_mode,
                font_size="small",
            ) as val_rg:
                hd.radio_button("Meters", value="meters")
                hd.radio_button("%", value="percent")
            if val_rg.changed:
                state.value_mode = val_rg.value

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
                hd.radio_button("Workout Severity", value="severity")

            if mode_rg.changed:
                state.zone_mode = mode_rg.value

        if zone_mode == "hr":
            _hr_callout(all_workouts, profile, is_owner=is_owner)

        # ── Distribution table ───────────────────────────────────────────────────
        if rows:
            _distribution_table(rows, view, zone_mode=zone_mode)


# ---------------------------------------------------------------------------
# Training-load section: stimulus rollup + days-since + CTL/ATL/TSB chart
# ---------------------------------------------------------------------------


def _stimulus_window_counts(workouts: list, days: int, today: date) -> dict[int, int]:
    """Per-band count of workouts that fully stimulated that system within
    the last ``days`` days (inclusive of today)."""
    cutoff = today - timedelta(days=days - 1)
    counts: dict[int, int] = {int(d): 0 for d in ZONE_BANDS_S}
    for w in workouts:
        ds = (w.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        if wdate < cutoff:
            continue
        for d in w.get("_stimulus_systems") or []:
            counts[int(d)] = counts.get(int(d), 0) + 1
    return counts


def _days_since_last_stimulus(workouts: list, today: date) -> dict[int, int | None]:
    """Per-band days since the last full-or-better stimulus.  ``None``
    when the band has never been stimulated in the workout history."""
    last_seen: dict[int, date] = {}
    for w in workouts:
        ds = (w.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        for d in w.get("_stimulus_systems") or []:
            d_int = int(d)
            if d_int not in last_seen or wdate > last_seen[d_int]:
                last_seen[d_int] = wdate
    out: dict[int, int | None] = {}
    for d in ZONE_BANDS_S:
        d_int = int(d)
        if d_int in last_seen:
            out[d_int] = (today - last_seen[d_int]).days
        else:
            out[d_int] = None
    return out


def _parse_rgba(rgba_str: str) -> tuple:
    """Parse 'rgba(r,g,b,a)' → (r, g, b, a) tuple."""
    try:
        inner = rgba_str.strip()[5:-1]
        parts = [p.strip() for p in inner.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]))
    except Exception:
        return (128, 128, 128, 0.8)


def _training_load_section(workouts: list, is_dark: bool) -> None:
    """Bottom-of-Volume-Page training-load panel.

    Three sub-panels:
      A. Per-system stimulus counts over 7 / 28-day windows.
      B. Days-since-last-stimulus per system, color-coded against
         adaptation-decay timescales (≤7d green, 8–14d yellow, >14d red).
      C. CTL / ATL / TSB time-series chart (Banister PMC).
    """
    today = date.today()

    with hd.box(
        padding=(2, 1, 2, 1),
        gap=1.5,
        border_top="1px solid neutral-200",
        margin_top=2,
    ):
        hd.h3("Training Load")
        hd.text(
            "Multi-session view of per-system stimulus delivery and "
            "Banister-style fitness/fatigue (CTL / ATL / TSB).",
            font_size="small",
            font_color="neutral-500",
        )

        # ── Panel A + B (stimulus counts + days-since) side-by-side ──
        with hd.hbox(gap=2, wrap="wrap", align="start"):
            # Panel A: stimulus rollup
            with hd.box(gap=0.5, min_width=20):
                hd.text(
                    "Stimulus this week / month",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-700",
                )
                counts_7 = _stimulus_window_counts(workouts, 7, today)
                counts_28 = _stimulus_window_counts(workouts, 28, today)
                with grid_box(grid_template_columns="auto 4rem 4rem", gap=0.4):
                    hd.text("System", font_size="x-small", font_weight="semibold")
                    hd.text("7-day", font_size="x-small", font_weight="semibold")
                    hd.text("28-day", font_size="x-small", font_weight="semibold")
                    for d in ZONE_BANDS_S:
                        bin_idx = BAND_TO_BIN[d]
                        name = BIN_NAMES[bin_idx]
                        color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
                        with hd.scope(f"stim_count_{d}"):
                            with hd.hbox(gap=0.3, align="center"):
                                hd.box(
                                    width=0.7,
                                    height=0.7,
                                    background_color=_parse_rgba(color_str),
                                    border_radius="small",
                                )
                                hd.text(name, font_size="small")
                            hd.text(
                                str(counts_7[d]),
                                font_size="small",
                                font_color=(
                                    "neutral-400" if counts_7[d] == 0 else "neutral-800"
                                ),
                            )
                            hd.text(
                                str(counts_28[d]),
                                font_size="small",
                                font_color=(
                                    "neutral-400"
                                    if counts_28[d] == 0
                                    else "neutral-800"
                                ),
                            )

            # Panel B: days-since-last-stimulus
            with hd.box(gap=0.5, min_width=20):
                hd.text(
                    "Days since last stimulus",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-700",
                )
                days_since = _days_since_last_stimulus(workouts, today)
                with grid_box(grid_template_columns="auto 4rem", gap=0.4):
                    hd.text("System", font_size="x-small", font_weight="semibold")
                    hd.text("Days", font_size="x-small", font_weight="semibold")
                    for d in ZONE_BANDS_S:
                        bin_idx = BAND_TO_BIN[d]
                        name = BIN_NAMES[bin_idx]
                        color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
                        days = days_since[d]
                        if days is None:
                            label = "never"
                            color = "neutral-400"
                        elif days <= 7:
                            label = f"{days}d"
                            color = "success-600"
                        elif days <= 14:
                            label = f"{days}d"
                            color = "warning-600"
                        else:
                            label = f"{days}d"
                            color = "danger-600"
                        with hd.scope(f"days_since_{d}"):
                            with hd.hbox(gap=0.3, align="center"):
                                hd.box(
                                    width=0.7,
                                    height=0.7,
                                    background_color=_parse_rgba(color_str),
                                    border_radius="small",
                                )
                                hd.text(name, font_size="small")
                            hd.text(label, font_size="small", font_color=color)

        # ── Panel C: CTL / ATL / TSB chart ──────────────────────────
        with hd.box(gap=0.5):
            hd.text(
                "Fitness / Fatigue / Form (CTL / ATL / TSB)",
                font_size="small",
                font_weight="bold",
                font_color="neutral-700",
            )
            hd.text(
                "Banister model: CTL (blue) = 42-day exponentially-weighted "
                "ESS (fitness); ATL (red) = 7-day EW ESS (fatigue); TSB "
                "(filled area) = CTL − ATL (form, on the right axis).  "
                "Background bands mark TrainingPeaks PMC zones — drag the "
                "brush below to window the view.",
                font_size="x-small",
                font_color="neutral-500",
            )
            # Pass the full history; the plugin's navigator strip handles
            # windowing client-side without rerunning Python.
            daily = daily_loads_from_workouts(workouts)
            if not daily:
                hd.text(
                    "No ESS data yet.",
                    font_size="small",
                    font_color="neutral-400",
                )
            else:
                pmc = compute_ctl_atl_tsb(daily)
                cfg = build_training_load_chart_config(
                    pmc, is_dark=is_dark, initial_window_days=180
                )
                if cfg is not None:
                    with hd.box():
                        TrainingLoadChart(config=cfg)


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def volume_page() -> None:
    """Top-level component for the Volume tab."""

    ctx = AppContext()
    result = get_all_workouts()

    profile = get_profile()

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
        )

        # Training-load section needs ESS metrics on each workout — attach
        # them now if not already present.  The cache makes repeat renders
        # cheap (and severity mode above already populated them).
        add_metrics(all_workouts, with_timeline=False)

        _training_load_section(all_workouts, is_dark=hd.theme().is_dark)
