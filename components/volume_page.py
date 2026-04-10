"""
Volume tab — stacked intensity-zone bar chart + distribution data table.

Volume chart:
  - Stacked bar chart showing meters per intensity zone per week / month / season.
  - Zone mode toggle: Pace | HR
      Pace mode: zones derived from personal-best pace thresholds (volume_bins.py).
      HR mode:   zones derived from % of HRmax (heartrate_utils.py).
  - Toggle: Weekly | Monthly | Seasonal  (radio button group)
  - Scope dropdown (per-view, remembers last selection independently):
      Weekly / Monthly:  Past Year  |  This Season  |  Past 2 Years
                         Past 5 Years  |  All Time
      Seasonal:  All Time  (other scopes available but less useful)
  - Machine filter dropdown: All Machines | Rower | SkiErg | Bike | …
    (hidden when only one machine type is present)

HR mode details:
  - Max HR is read from .profile.json (explicit) or estimated at the 98th
    percentile of all valid HR readings (is_estimated=True).
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
from services.rowinglevel import _PROFILE_DEFAULTS
from components.volume_chart_builder import build_volume_chart_config, get_period_rows
from components.volume_chart_plugin import VolumeChart

# HR Z3 sub-zones: bin 2 = Z4 Threshold (80–90 %), bin 1 = Z5 Max (> 90 %)
_HR_Z3A_BINS = frozenset({2})  # Threshold
_HR_Z3B_BINS = frozenset({1})  # Max
_HR_NO_DATA_BINS = frozenset({6})  # "No HR" — excluded from classification denominator

# ---------------------------------------------------------------------------
# Distribution colour helpers
# ---------------------------------------------------------------------------

_DIST_COLORS = {
    "Polarized": ("rgba(50,130,220,0.9)", "rgba(20,105,195,0.9)"),
    "Pyramidal": ("rgba(55,180,80,0.9)", "rgba(25,150,50,0.9)"),
    "Threshold": ("rgba(225,125,35,0.9)", "rgba(205,95,15,0.9)"),
    "High Intensity": ("rgba(215,55,55,0.9)", "rgba(195,35,35,0.9)"),
    "Easy / LSD": ("rgba(115,170,230,0.9)", "rgba(80,140,205,0.9)"),
    "Mixed": ("rgba(150,150,150,0.9)", "rgba(120,120,120,0.9)"),
}


def _dist_badge(label: str, is_dark: bool) -> None:
    """Render a small coloured pill for a distribution classification."""
    colors = _DIST_COLORS.get(label)
    if colors is None:
        hd.text(label, font_size="small", font_color="neutral-500")
        return
    color = colors[0] if is_dark else colors[1]
    hd.box(
        label,
        background_color=color,
        border_radius="full",
        padding=(0.25, 0.75),
        font_size="small",
        font_color="neutral-0",
        font_weight="semibold",
    )


# ---------------------------------------------------------------------------
# Distribution data table
# ---------------------------------------------------------------------------

_PERIOD_HEADERS = {
    "weekly": "Week",
    "monthly": "Month",
    "seasonal": "Season",
}


def _distribution_table(rows: list, view: str, zone_mode: str = "pace") -> None:
    """
    Render a data table with one row per period showing zone breakdowns
    and a training distribution classification.

    Pace mode columns:
      Period | Total | Rest
      | Z1 Easy\n(Fast & Slow Aerobic) | Z2 Threshold
      | Z3 Hard\n(5k + 2k + Fast) | Distribution

    HR mode columns:
      Period | Total | Rest
      | Easy (<70%) | Tempo (70–80%)
      | Threshold (80–90%) | Max (90%+) | Distribution
    """
    period_col = _PERIOD_HEADERS.get(view, "Period")

    col_period = tuple(r["label"] for r in rows)
    col_total = tuple(r["total"] for r in rows)
    col_rest = tuple(r["rest"] for r in rows)
    col_dist = tuple(r["distribution"] for r in rows)

    if zone_mode == "hr":
        col_z1 = tuple(f"{r['z1_m']}  ({r['z1_pct']})" for r in rows)
        col_z2 = tuple(f"{r['z2_m']}  ({r['z2_pct']})" for r in rows)
        col_z3a = tuple(f"{r['z3a_m']}  ({r['z3a_pct']})" for r in rows)
        col_z3b = tuple(f"{r['z3b_m']}  ({r['z3b_pct']})" for r in rows)
        table_data = {
            period_col: col_period,
            "Total": col_total,
            "Rest": col_rest,
            "Easy (<70%)": col_z1,
            "Tempo (70–80%)": col_z2,
            "Threshold (80–90%)": col_z3a,
            "Max (90%+)": col_z3b,
            "Distribution": col_dist,
        }
    else:
        col_z1 = tuple(f"{r['z1_m']}  ({r['z1_pct']})" for r in rows)
        col_z2 = tuple(f"{r['z2_m']}  ({r['z2_pct']})" for r in rows)
        col_z3 = tuple(f"{r['z3_m']}  ({r['z3_pct']})" for r in rows)
        table_data = {
            period_col: col_period,
            "Total": col_total,
            "Rest": col_rest,
            "Z1 Easy\n(Fast & Slow Aerobic)": col_z1,
            "Z2 Threshold": col_z2,
            "Z3 Hard\n(5k + 2k + Fast)": col_z3,
            "Distribution": col_dist,
        }

    with hd.box(padding=(1, 0, 0, 0)):
        hd.data_table(table_data, rows_per_page=20)


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

    return max_hr, max_hr is not None


def _volume_section(all_workouts: list, profile: dict) -> None:
    """Render the volume controls + stacked bar chart."""

    # Per-view scope state so switching views doesn't reset each other's scope.
    state = hd.state(
        view="weekly",
        weekly_scope="past_year",
        monthly_scope="past_year",
        seasonal_scope="all_time",
        machine="All",
        zone_mode="pace",  # "pace" | "hr"
    )

    # ── Controls row ─────────────────────────────────────────────────────────
    with hd.hbox(gap=3, align="center", padding=(0, 0, 1, 0), wrap="wrap"):
        # View radio group (Weekly / Monthly / Seasonal)
        view_rg = hd.radio_buttons(
            "Weekly",
            "Monthly",
            "Seasonal",
            value=state.view.capitalize(),
            font_size="small",
        )
        if view_rg.changed:
            state.view = view_rg.value.lower()
        view = state.view

        # Scope dropdown — per-view state.
        current_scope = getattr(state, f"{view}_scope")
        with hd.scope(f"scope_{view}"):
            scope_sel = hd.select(value=current_scope, size="small")
            with scope_sel:
                hd.option("Past Year", value="past_year")
                hd.option("This Season", value="this_season")
                hd.option("Past 2 Years", value="past_2_years")
                hd.option("Past 5 Years", value="past_5_years")
                hd.option("All Time", value="all_time")
            if scope_sel.changed:
                if view == "weekly":
                    state.weekly_scope = scope_sel.value
                elif view == "monthly":
                    state.monthly_scope = scope_sel.value
                else:
                    state.seasonal_scope = scope_sel.value
                current_scope = scope_sel.value

        # Machine filter — only show when the user has more than one machine type.
        machine_types = sorted({w.get("type") for w in all_workouts if w.get("type")})
        if len(machine_types) > 1:
            with hd.scope("machine_filter"):
                machine_sel = hd.select(value=state.machine, size="small")
                with machine_sel:
                    hd.option("All Machines", value="All")
                    for mt in machine_types:
                        hd.option(machine_label(mt), value=mt)
                if machine_sel.changed:
                    state.machine = machine_sel.value
        else:
            # Single machine type — force filter off
            state.machine = "All"

        # Zone mode radio group (Pace / HR)
        mode_rg = hd.radio_buttons(
            "Pace",
            "HR",
            value="Pace" if state.zone_mode == "pace" else "HR",
            font_size="small",
        )
        if mode_rg.changed:
            state.zone_mode = mode_rg.value.lower()

    # ── HR callout (only in HR mode) ─────────────────────────────────────────
    max_hr = None
    hr_ok = True
    if state.zone_mode == "hr":
        max_hr, hr_ok = _hr_callout(all_workouts, profile)

    # ── Compute chart data ────────────────────────────────────────────────────
    machine_filter = None if state.machine == "All" else {state.machine}

    if state.zone_mode == "hr" and hr_ok:
        aggregated = aggregate_workouts(
            all_workouts,
            machine_filter=machine_filter,
            bin_fn=lambda w: workout_hr_meters(w, max_hr),
        )
        chart_config = build_volume_chart_config(
            aggregated,
            view=view,
            scope=current_scope,
            today=date.today(),
            bin_names=HR_ZONE_NAMES,
            bin_colors=HR_ZONE_COLORS,
            draw_order=HR_ZONE_DRAW_ORDER,
        )
        rows = get_period_rows(
            aggregated,
            view,
            current_scope,
            today=date.today(),
            z1_bins=HR_Z1_BINS,
            z2_bins=HR_Z2_BINS,
            z3_bins=HR_Z3_BINS,
            z3a_bins=_HR_Z3A_BINS,
            z3b_bins=_HR_Z3B_BINS,
            no_data_bins=_HR_NO_DATA_BINS,
        )
    elif state.zone_mode == "hr" and not hr_ok:
        # No max HR — skip chart and table; callout already rendered above.
        return
    else:
        ref_sbs = get_reference_sbs(all_workouts)
        thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
        aggregated = aggregate_workouts(all_workouts, thresholds, machine_filter)
        chart_config = build_volume_chart_config(
            aggregated,
            view=view,
            scope=current_scope,
            today=date.today(),
        )
        rows = get_period_rows(aggregated, view, current_scope, today=date.today())

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

    # ── Distribution table ───────────────────────────────────────────────────
    if rows:
        _distribution_table(rows, view, zone_mode=state.zone_mode)


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def volume_page(client, user_id: str) -> None:
    """Top-level component for the Volume tab."""

    result = concept2_sync(client)
    if result is None:
        return
    _workouts_dict, all_workouts = result

    # Load profile from localStorage
    ls_profile = hd.local_storage.get_item("profile")
    if not ls_profile.done:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return
    profile = {**_PROFILE_DEFAULTS}
    if ls_profile.result:
        try:
            profile = {**_PROFILE_DEFAULTS, **json.loads(ls_profile.result)}
        except Exception:
            pass

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    with hd.box(padding=(2, 2, 2, 2)):
        _volume_section(all_workouts, profile)
