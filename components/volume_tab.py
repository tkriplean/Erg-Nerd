"""
Volume tab — stacked pace-zone bar chart + distribution data table.

Volume chart:
  - Stacked bar chart showing meters per pace zone per week / month / season.
  - Toggle: Weekly | Monthly | Seasonal
  - Scope dropdown (per-view, remembers last selection independently):
      Weekly / Monthly:  Past Year  |  This Season  |  Past 2 Years
                         Past 5 Years  |  All Time
      Seasonal:  All Time  (other scopes available but less useful)
  - Machine filter dropdown: All Machines | Rower | SkiErg | Bike | …
"""

from datetime import date

import hyperdiv as hd

from services.concept2 import get_client, load_local_workouts
from services.volume_bins import (
    get_reference_sbs,
    compute_bin_thresholds,
    aggregate_workouts,
)
from components.volume_chart_builder import build_volume_chart_config, get_period_rows
from components.volume_chart import VolumeChart


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _machine_label(type_str: str) -> str:
    """'rower' → 'Rower', 'skierg' → 'SkiErg', etc."""
    _LABELS = {
        "rower": "Rower",
        "skierg": "SkiErg",
        "bike": "Bike",
        "dynamic": "Dynamic",
        "slides": "Slides",
    }
    return _LABELS.get(type_str.lower(), type_str.capitalize())


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


def _distribution_table(rows: list, view: str) -> None:
    """
    Render a data table with one row per period showing zone breakdowns
    and a training distribution classification.

    Columns:
      Period | Total | Rest | Z1 Easy (m / %) | Z2 Threshold (m / %)
             | Z3 Hard (m / %) | Distribution

    Zone definitions (3-zone model):
      Z1 Easy      — Fast Aerobic + Slow Aerobic
      Z2 Threshold — Threshold
      Z3 Hard      — 5k + 2k + Fast
    """
    is_dark = hd.theme().mode == "dark"
    period_col = _PERIOD_HEADERS.get(view, "Period")

    with hd.box(padding=(1, 0, 0, 0)):
        # ── Legend row ──────────────────────────────────────────────────────
        with hd.hbox(gap=1.5, align="center", padding=(0, 0, 1, 0), wrap="wrap"):
            hd.text("Zone key:", font_size="small", font_color="neutral-500")
            hd.text(
                "Z1 = easy aerobic (Fast Aerobic + Slow Aerobic)",
                font_size="small",
                font_color="neutral-500",
            )
            hd.text("·", font_size="small", font_color="neutral-300")
            hd.text(
                "Z2 = threshold",
                font_size="small",
                font_color="neutral-500",
            )
            hd.text("·", font_size="small", font_color="neutral-300")
            hd.text(
                "Z3 = high intensity (5k + 2k + Fast)",
                font_size="small",
                font_color="neutral-500",
            )

        # ── Table ────────────────────────────────────────────────────────────
        col_period = tuple(r["label"] for r in rows)
        col_total = tuple(r["total"] for r in rows)
        col_rest = tuple(r["rest"] for r in rows)

        col_z1 = tuple(f"{r['z1_m']}  ({r['z1_pct']})" for r in rows)
        col_z2 = tuple(f"{r['z2_m']}  ({r['z2_pct']})" for r in rows)
        col_z3 = tuple(f"{r['z3_m']}  ({r['z3_pct']})" for r in rows)
        col_dist = tuple(r["distribution"] for r in rows)

        hd.data_table(
            {
                period_col: col_period,
                "Total": col_total,
                "Rest": col_rest,
                "Z1 Easy": col_z1,
                "Z2 Threshold": col_z2,
                "Z3 Hard": col_z3,
                "Distribution": col_dist,
            },
            rows_per_page=20,
        )


# ---------------------------------------------------------------------------
# Volume chart section
# ---------------------------------------------------------------------------


def _volume_section(all_workouts: list) -> None:
    """Render the volume controls + stacked bar chart."""

    # Per-view scope state so switching views doesn't reset each other's scope.
    state = hd.state(
        weekly_scope="past_year",
        monthly_scope="past_year",
        seasonal_scope="all_time",
        machine="All",
    )

    # ── Controls row ─────────────────────────────────────────────────────────
    with hd.hbox(gap=3, align="center", padding=(0, 0, 1, 0), wrap="wrap"):
        # View toggle (tab group acts as a segmented button)
        view_tabs = hd.tab_group("Weekly", "Monthly", "Seasonal")
        view = view_tabs.active.lower()  # "weekly" | "monthly" | "seasonal"

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

        # Machine filter — derived from data so it reflects what's actually logged.
        machine_types = sorted({w.get("type") for w in all_workouts if w.get("type")})
        with hd.scope("machine_filter"):
            machine_sel = hd.select(value=state.machine, size="small")
            with machine_sel:
                hd.option("All Machines", value="All")
                for mt in machine_types:
                    hd.option(_machine_label(mt), value=mt)
            if machine_sel.changed:
                state.machine = machine_sel.value

    # ── Compute chart data ────────────────────────────────────────────────────
    machine_filter = None if state.machine == "All" else {state.machine}

    ref_sbs = get_reference_sbs(all_workouts)
    thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
    aggregated = aggregate_workouts(all_workouts, thresholds, machine_filter)

    theme = hd.theme()
    is_dark = theme.mode == "dark"

    chart_config = build_volume_chart_config(
        aggregated,
        view=view,
        scope=current_scope,
        is_dark=is_dark,
        today=date.today(),
    )

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
    rows = get_period_rows(aggregated, view, current_scope, today=date.today())
    if rows:
        _distribution_table(rows, view)


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def volume_tab() -> None:
    """Top-level component for the Volume tab."""

    task = hd.task()

    def _fetch():
        client = get_client()
        if client is None:
            local = load_local_workouts()
            workouts = list(local.values())
            workouts.sort(key=lambda r: r.get("date", ""), reverse=True)
            return workouts
        return client.get_all_results()

    task.run(_fetch)

    if task.running:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return

    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return

    all_workouts = task.result or []

    if not all_workouts:
        with hd.box(padding=4, align="center"):
            hd.text("No workouts found.", font_color="neutral-500")
        return

    with hd.box(padding=(2, 2, 2, 2)):
        hd.h3("Volume")
        _volume_section(all_workouts)
