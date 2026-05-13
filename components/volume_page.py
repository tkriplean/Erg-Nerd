"""
Volume tab — stacked spread-zone bar chart + distribution table + training tabs.

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

Distribution table:
  - Period (newest first) | Total | Rest | zone columns | Distribution badge.
  - Every column carries ``"sortable": True``; the DataTable plugin renders a
    sort button on each header.  Sort state persists across back-button via
    sessionStorage; ``reset_token`` mirrors ``{view}_{zone_mode}`` so
    swapping either resets sort/page.

Bottom tab group (Training Stimulus | Training Load):
  - Styled like the Workout Page's bottom tab group (large, bold, centered);
    active tab persists on ``VolumePageState.active_tab``.
  - Training Stimulus tab:
      Panel A — per-system Solid+ stimulus counts over six horizons
                 (7/28/90/180/365-day + all-time), title "Stimulus over
                 different time horizons".
      Panel B — days-since-last-stimulus per system at three thresholds
                 (Full / Solid / Last=Partial+), color-coded by adaptation
                 decay (≤7d green, 8–14d yellow, >14d red).
  - Training Load tab:
      CTL/ATL/TSB Banister chart with a plain-English description and an
      expandable "Learn more" disclosure that holds the technical PMC text.
      The brush below the chart defaults to the last 180 days.

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
from components.hyperdiv_extensions import grid_box, radio_group
from components.data_table import DataTable
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
    Render a sortable JS-backed table with one row per period showing zone
    breakdowns and a training distribution classification.  Backed by
    :func:`components.data_table.DataTable`.

    Power Spread mode columns:
      Period | Total | Rest | Z1 Easy | Z2 Threshold | Z3 Hard | Distribution

    HR Spread mode columns:
      Period | Total | Rest | Easy (<70%) | Tempo (70–80%)
      | Threshold (80–90%) | Max (90%+) | Distribution

    Workout Severity mode columns:
      Period | Total | Rest | Low | Moderate | High | Maximal | Unrated

    Sort state lives in the JS plugin and persists across back-button via
    sessionStorage; ``reset_token`` (``"{view}_{zone_mode}"``) resets sort
    when the user swaps view or zone mode.
    """
    period_col = _PERIOD_HEADERS.get(view, "Period")

    period_column = {
        "key": "label",
        "header": period_col,
        "width": "9rem",
        "align": "start",
        "value_key": "label",
        "sort_key": "_idx",
        "default_asc": True,
        "sortable": True,
    }
    total_column = {
        "key": "total",
        "header": "Total",
        "width": "7rem",
        "align": "end",
        "value_key": "total",
        "sort_key": "total_raw",
        "sortable": True,
    }
    rest_column = {
        "key": "rest",
        "header": "Rest",
        "width": "7rem",
        "align": "end",
        "value_key": "rest",
        "sort_key": "rest_raw",
        "sortable": True,
    }
    distribution_column = {
        "key": "distribution",
        "header": "Distribution",
        "width": "9rem",
        "align": "center",
        "value_key": "distribution",
        "sort_key": "distribution",
        "default_asc": True,
        "sortable": True,
    }

    def _zone_col(
        key, header, value_key, secondary_key, sort_key, width="minmax(9rem,1fr)"
    ):
        return {
            "key": key,
            "header": header,
            "width": width,
            "align": "end",
            "value_key": value_key,
            "secondary_key": secondary_key,
            "sort_key": sort_key,
            "sortable": True,
        }

    if zone_mode == "severity":
        columns = [
            period_column,
            total_column,
            rest_column,
            _zone_col(
                "low", "Low", "low_m", "low_pct", "low_raw", width="minmax(8rem,1fr)"
            ),
            _zone_col(
                "mod",
                "Moderate",
                "mod_m",
                "mod_pct",
                "mod_raw",
                width="minmax(8rem,1fr)",
            ),
            _zone_col(
                "high",
                "High",
                "high_m",
                "high_pct",
                "high_raw",
                width="minmax(8rem,1fr)",
            ),
            _zone_col(
                "max",
                "Maximal",
                "max_m",
                "max_pct",
                "max_raw",
                width="minmax(8rem,1fr)",
            ),
            _zone_col(
                "unrated",
                "Unrated",
                "unrated_m",
                "unrated_pct",
                "unrated_raw",
                width="minmax(8rem,1fr)",
            ),
        ]
    elif zone_mode == "hr":
        columns = [
            period_column,
            total_column,
            rest_column,
            _zone_col("z1", "Easy (<70%)", "z1_m", "z1_pct", "z1_raw"),
            _zone_col("z2", "Tempo (70–80%)", "z2_m", "z2_pct", "z2_raw"),
            _zone_col("z3a", "Threshold (80–90%)", "z3a_m", "z3a_pct", "z3a_raw"),
            _zone_col("z3b", "Max (90%+)", "z3b_m", "z3b_pct", "z3b_raw"),
            distribution_column,
        ]
    else:
        columns = [
            period_column,
            total_column,
            rest_column,
            _zone_col("z1", "Easy", "z1_m", "z1_pct", "z1_raw"),
            _zone_col("z2", "Threshold", "z2_m", "z2_pct", "z2_raw"),
            _zone_col("z3", "Hard", "z3_m", "z3_pct", "z3_raw"),
            distribution_column,
        ]

    with hd.box(padding=(1, 0, 0, 0)):
        print(len(rows))
        DataTable(
            rows,
            columns,
            default_sort_col="label",
            default_sort_asc=True,
            reset_token=f"{view}_{zone_mode}",
            initial_rows=10,
        )


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
    # Bottom-of-page tabs: "Training Stimulus" | "Training Load"
    active_tab = hd.Prop(hd.String, "Training Stimulus")


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
            with hd.hbox(align="center", width="100%"):
                with hd.box(align="center", gap=0.5):
                    with radio_group(
                        value=state.value_mode,
                        size="small",
                    ) as val_rg:
                        with hd.box(gap=0):
                            hd.radio_button(
                                "Meters",
                                value="meters",
                                width="100%",
                                button_style=hd.style(border_radius="0px"),
                            )
                            hd.radio_button(
                                "%",
                                value="percent",
                                width="100%",
                                button_style=hd.style(border_radius="0px"),
                            )
                    if val_rg.changed:
                        state.value_mode = val_rg.value

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


def _stimulus_window_counts(
    workouts: list, days: int | None, today: date
) -> dict[int, int]:
    """Per-band count of workouts that delivered Solid+ stimulus
    (``_stimulus_systems`` membership, dose ≥ 0.80) to that system within
    the last ``days`` days (inclusive of today).  ``days=None`` means
    no cutoff — count every workout in history (all-time horizon)."""
    cutoff = None if days is None else today - timedelta(days=days - 1)
    counts: dict[int, int] = {int(d): 0 for d in ZONE_BANDS_S}
    for w in workouts:
        ds = (w.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        if cutoff is not None and wdate < cutoff:
            continue
        for d in w.get("_stimulus_systems") or []:
            counts[int(d)] = counts.get(int(d), 0) + 1
    return counts


#: Days-since thresholds keyed by the panel column.  ``last`` = any
#: Partial+ stimulus (dose ≥ 0.50), ``solid`` = Solid+ (≥ 0.80), ``full`` =
#: Full+ (≥ 0.95).  Centralised here so the renderer below loops over a
#: single source of truth.
_DAYS_SINCE_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("full", 0.95),
    ("solid", 0.80),
    ("partial", 0.50),
)


def _days_since_last_stimulus(
    workouts: list, today: date
) -> dict[int, dict[str, int | None]]:
    """Per-band days since the last stimulus at three thresholds.  Returns
    a dict keyed by band-seconds, each value a dict with keys ``"full"``,
    ``"solid"``, ``"last"`` (see ``_DAYS_SINCE_THRESHOLDS``).  Each entry
    is the number of days since the most recent workout whose dose on
    that band met or exceeded the threshold, or ``None`` when the band
    has never reached that threshold in the workout history."""
    last_seen: dict[int, dict[str, date]] = {int(d): {} for d in ZONE_BANDS_S}
    for w in workouts:
        ds = (w.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(ds)
        except (TypeError, ValueError):
            continue
        doses = w.get("_stimulus_doses") or {}
        for d in ZONE_BANDS_S:
            d_int = int(d)
            dose = float(doses.get(d, doses.get(d_int, 0.0)) or 0.0)
            for col, floor in _DAYS_SINCE_THRESHOLDS:
                if dose >= floor:
                    prev = last_seen[d_int].get(col)
                    if prev is None or wdate > prev:
                        last_seen[d_int][col] = wdate
    out: dict[int, dict[str, int | None]] = {}
    for d in ZONE_BANDS_S:
        d_int = int(d)
        row: dict[str, int | None] = {}
        for col, _floor in _DAYS_SINCE_THRESHOLDS:
            wdate = last_seen[d_int].get(col)
            row[col] = (today - wdate).days if wdate else None
        out[d_int] = row
    return out


def _parse_rgba(rgba_str: str) -> tuple:
    """Parse 'rgba(r,g,b,a)' → (r, g, b, a) tuple."""
    try:
        inner = rgba_str.strip()[5:-1]
        parts = [p.strip() for p in inner.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]))
    except Exception:
        return (128, 128, 128, 0.8)


# Stimulus-rollup time horizons (Panel A inside the Training Stimulus tab).
# ``None`` ⇒ no cutoff (all-time count).
_STIMULUS_HORIZONS: list[tuple[str, int | None]] = [
    ("7-day", 7),
    ("28-day", 28),
    ("90-day", 90),
    ("180-day", 180),
    ("365-day", 365),
    ("All-time", None),
]


def _training_stimulus_tab(workouts: list, is_dark: bool) -> None:
    """Training Stimulus tab content.

    Panel A: per-system Solid+ stimulus counts over six horizons (7/28/90/
    180/365-day + all-time).
    Panel B: days-since-last-stimulus per system at three category thresholds
    (Full / Solid / Last=Partial+), color-coded against adaptation-decay
    timescales (≤7d green, 8–14d yellow, >14d red).
    """
    today = date.today()

    with hd.box(gap=2, wrap="wrap", align="center", justify="center"):
        # Panel: days-since-last-stimulus (3 thresholds)
        with hd.box(gap=0.5, min_width=36, align="center"):
            hd.h3(
                "Days since last stimulus",
                # font_size="small",
                # font_weight="bold",
                # font_color="neutral-700",
            )
            days_since = _days_since_last_stimulus(workouts, today)
            col_labels = {"full": "Full", "solid": "Solid", "partial": "Partial"}
            with grid_box(
                grid_template_columns="auto 3.5rem 3.5rem 3.5rem", gap="0.5rem 2rem"
            ):
                hd.text("System", font_size="x-small", font_weight="semibold")
                for col, _floor in _DAYS_SINCE_THRESHOLDS:
                    with hd.scope(f"{col} {_floor}"):
                        hd.text(
                            col_labels[col],
                            # font_size="small",
                            font_weight="semibold",
                        )
                for d in ZONE_BANDS_S:
                    bin_idx = BAND_TO_BIN[d]
                    name = BIN_NAMES[bin_idx]
                    color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
                    row = days_since[d]
                    with hd.scope(f"days_since_{d}"):
                        with hd.hbox(gap=0.5, align="center"):
                            hd.box(
                                width=0.7,
                                height=0.7,
                                background_color=_parse_rgba(color_str),
                                border_radius="small",
                            )
                            hd.text(
                                name,
                                # font_size="small"
                            )
                        for col, _floor in _DAYS_SINCE_THRESHOLDS:
                            days = row[col]
                            if days is None:
                                label = "—"
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
                            with hd.scope(f"days_since_{d}_{col}"):
                                hd.text(
                                    label,
                                    # font_size="small",
                                    font_color=color,
                                )

        # Panel: stimulus rollup over multiple horizons
        with hd.box(gap=0.5, min_width=30, align="center"):
            hd.h3(
                "Stimulus over different time horizons",
                # font_size="small",
                # font_weight="bold",
                # font_color="neutral-700",
            )
            counts_by_horizon = [
                _stimulus_window_counts(workouts, days, today)
                for _, days in _STIMULUS_HORIZONS
            ]
            with grid_box(
                grid_template_columns="auto repeat(6, 5rem)", gap="0.5rem 2rem"
            ):
                hd.text(
                    "",
                    # font_size="small",
                    font_weight="semibold",
                )
                for label, _ in _STIMULUS_HORIZONS:
                    with hd.scope(f"stim_hdr_{label}"):
                        with hd.box(text_align="end"):
                            hd.text(
                                label,
                                # font_size="small",
                                font_weight="semibold",
                            )
                for idx, d in enumerate(ZONE_BANDS_S):
                    bin_idx = BAND_TO_BIN[d]
                    name = BIN_NAMES[bin_idx]
                    color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]

                    with hd.scope(f"stim_row_{d}"):
                        with hd.hbox(gap=0.5, align="center"):
                            hd.box(
                                width=0.7,
                                height=0.7,
                                background_color=_parse_rgba(color_str),
                                border_radius="small",
                            )
                            hd.text(
                                name,
                                # font_size="small"
                            )
                        for (label, _), counts in zip(
                            _STIMULUS_HORIZONS, counts_by_horizon
                        ):
                            with hd.scope(f"stim_cell_{d}_{label}"):
                                with hd.box(text_align="end"):
                                    hd.text(
                                        str(counts[d]),
                                        # font_size="small",
                                        font_color=(
                                            "neutral-400"
                                            if counts[d] == 0
                                            else "neutral-800"
                                        ),
                                    )


def _training_load_tab(workouts: list, is_dark: bool) -> None:
    """Training Load tab content — CTL / ATL / TSB chart with an accessible
    plain-English description and an expandable "Learn more" disclosure that
    holds the Banister-model technical details."""
    with hd.box(gap=0.8, width="100%", align="center"):
        with hd.box(gap=0.5, width="100%", max_width=70):
            hd.text(
                "How your fitness, fatigue, and form have changed over time. "
                "Blue is fitness (how much training your body has absorbed). "
                "Red is fatigue (how much recent training is still weighing "
                "on you). The green/red shading is form — positive when "
                "you're fresh, negative when you're tired. Drag the brush "
                "below the chart to zoom into a date range.",
                font_size="small",
                font_color="neutral-600",
            )
            with hd.details("Learn more"):
                hd.text(
                    "This is the Banister Performance Management Chart "
                    "(PMC) model. CTL (fitness, blue) is a 42-day "
                    "exponentially-weighted average of your daily ESS "
                    "(Erg Stress Score). ATL (fatigue, red) is the same "
                    "thing with a 7-day window. TSB (form, filled area) "
                    "= CTL − ATL, plotted against the right axis. The "
                    "faint background bands mark TrainingPeaks PMC zones "
                    "— high-risk fatigue through fresh/peak.",
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
                with hd.box(width="100%"):
                    TrainingLoadChart(config=cfg)


def _training_bottom_tabs(workouts: list, is_dark: bool) -> None:
    """Bottom-of-Volume-Page tab group: Training Stimulus | Training Load.

    Styling mirrors the Workout Page's bottom tab group (large, bold,
    centered).  Active-tab selection persists on the page state so
    swapping between Volume tab views doesn't reset it.
    """
    state = VolumePageState()

    with hd.box(
        width="100%",
        padding=(2, 1, 2, 1),
        border_top="1px solid neutral-200",
        margin_top=2,
    ):
        with hd.hbox(gap=2, align="center", justify="center"):
            tabs = hd.tab_group(
                "Training Stimulus",
                "Training Load",
                font_size="x-large",
                font_weight="bold",
            )
            if tabs.active and tabs.active != state.active_tab:
                state.active_tab = tabs.active

        with hd.box(padding_top=2, gap=2, width="100%", align="center"):
            active = tabs.active or state.active_tab or "Training Stimulus"
            if active == "Training Stimulus":
                _training_stimulus_tab(workouts, is_dark)
            else:
                _training_load_tab(workouts, is_dark)


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

        _training_bottom_tabs(all_workouts, is_dark=hd.theme().is_dark)
