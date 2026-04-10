"""
Chart.js stacked-bar config builder for the volume (meters × intensity zone) chart.

Supports both pace-zone mode (default) and HR-zone mode via optional parameters.
All bin_names / bin_colors / draw_order / z*_bins arguments default to the pace-zone
values so existing callers require no changes.

Exported:
    build_volume_chart_config() — returns a Chart.js config dict for VolumeChart.
    get_period_rows()           — returns table row dicts for the distribution table.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from services.formatters import fmt_meters

from services.volume_bins import (
    BIN_NAMES,
    BIN_COLORS,
    N_BINS,
    Z1_BINS,
    Z2_BINS,
    Z3_BINS,
)
from services.rowing_utils import get_season
import hyperdiv as hd

# ---------------------------------------------------------------------------
# Scope → date-range helpers
# ---------------------------------------------------------------------------


def _current_season_bounds(today: date) -> tuple:
    """Return (start, end) dates for the rowing season containing today."""
    if today.month >= 5:
        return date(today.year, 5, 1), date(today.year + 1, 4, 30)
    return date(today.year - 1, 5, 1), date(today.year, 4, 30)


def _scope_date_range(scope: str, today: date) -> tuple:
    """
    Return (lo, hi) date bounds for the given scope string.
    lo / hi may be None, meaning "no lower / upper bound".
    """
    if scope == "all_time":
        return None, None
    if scope == "this_season":
        return _current_season_bounds(today)
    if scope == "past_year":
        return today - timedelta(days=365), today
    if scope == "past_2_years":
        return today - timedelta(days=730), today
    if scope == "past_5_years":
        return today - timedelta(days=1825), today
    return None, None  # fallback


# ---------------------------------------------------------------------------
# Key → date helpers (inverse of volume_bins bucketing)
# ---------------------------------------------------------------------------


def _week_key_to_monday(week_key: str) -> Optional[date]:
    """'YYYY-Www' → Monday date of that ISO week, or None on error."""
    try:
        year_part, w_part = week_key.split("-W")
        return date.fromisocalendar(int(year_part), int(w_part), 1)
    except Exception:
        return None


def _month_key_to_date(month_key: str) -> Optional[date]:
    """'YYYY-MM' → first day of that month, or None on error."""
    try:
        y, m = month_key.split("-")
        return date(int(y), int(m), 1)
    except Exception:
        return None


def _season_key_to_start(season_key: str) -> Optional[date]:
    """'YYYY-YY' → May 1 of the first year, or None on error."""
    try:
        yr = int(season_key[:4])
        return date(yr, 5, 1)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------


def _filter_and_sort_keys(
    keys: list,
    view: str,
    scope: str,
    today: date,
) -> list:
    """
    Filter period keys to those whose representative date falls within the
    scope range, then return sorted (ascending chronological order).
    """
    lo, hi = _scope_date_range(scope, today)

    def _representative_date(key: str) -> Optional[date]:
        if view == "weekly":
            return _week_key_to_monday(key)
        if view == "monthly":
            return _month_key_to_date(key)
        # seasonal
        return _season_key_to_start(key)

    def _in_range(key: str) -> bool:
        if lo is None:
            return True
        d = _representative_date(key)
        if d is None:
            return True
        return lo <= d <= (hi or date.max)

    return sorted(k for k in keys if _in_range(k))


# ---------------------------------------------------------------------------
# Label formatters
# ---------------------------------------------------------------------------


def _week_label(week_key: str) -> str:
    """'2025-W03' → 'Jan 6' (Monday of that week)."""
    d = _week_key_to_monday(week_key)
    if d is None:
        return week_key
    return d.strftime("%b ") + str(d.day)


def _month_label(month_key: str) -> str:
    """'2025-01' → "Jan '25"."""
    d = _month_key_to_date(month_key)
    if d is None:
        return month_key
    # two-digit year
    return d.strftime("%b '") + d.strftime("%y")


# ---------------------------------------------------------------------------
# Main config builder
# ---------------------------------------------------------------------------


def build_volume_chart_config(
    aggregated: dict,
    *,
    view: str = "weekly",
    scope: str = "past_year",
    today: Optional[date] = None,
    bin_names: Optional[list] = None,
    bin_colors: Optional[list] = None,
    draw_order: Optional[list] = None,
) -> dict:
    """
    Build a Chart.js stacked bar chart config dict for the volume view.

    Parameters
    ----------
    aggregated:
        Output of aggregate_workouts().
    view:
        "weekly" | "monthly" | "seasonal"
    scope:
        "this_season" | "past_year" | "past_2_years" | "past_5_years" | "all_time"
    today:
        Reference date; defaults to date.today().
    bin_names:
        7-element list of bin display names.  Defaults to BIN_NAMES (pace zones).
    bin_colors:
        7-element list of (dark_rgba, light_rgba) pairs.  Defaults to BIN_COLORS.
    draw_order:
        List of bin indices controlling bottom→top stack order.
        Defaults to [6, 5, 4, 3, 2, 1, 0].

    Returns
    -------
    Chart.js config dict, or {} if there is no data to display.
    """
    is_dark = hd.theme().is_dark

    today = today or date.today()
    _bin_names = bin_names if bin_names is not None else BIN_NAMES
    _bin_colors = bin_colors if bin_colors is not None else BIN_COLORS
    _draw_order = draw_order if draw_order is not None else [6, 5, 4, 3, 2, 1, 0]

    if view == "weekly":
        raw_data = aggregated.get("weeks", {})
    elif view == "monthly":
        raw_data = aggregated.get("months", {})
    else:
        raw_data = aggregated.get("seasons", {})

    if not raw_data:
        return {}

    keys = _filter_and_sort_keys(list(raw_data.keys()), view, scope, today)
    if not keys:
        return {}

    # ── Labels ──────────────────────────────────────────────────────────────
    if view == "weekly":
        labels = [_week_label(k) for k in keys]
    elif view == "monthly":
        labels = [_month_label(k) for k in keys]
    else:
        labels = list(keys)

    # ── Datasets ─────────────────────────────────────────────────────────────
    # Chart.js stacks datasets in array order: first → bottom, last → top.
    color_dark_or_light = 0 if is_dark else 1
    datasets = []
    for bin_i in _draw_order:
        bin_data = [raw_data[k]["bins"][bin_i] for k in keys]
        if sum(bin_data) < 1.0:
            continue
        color = _bin_colors[bin_i][color_dark_or_light]
        datasets.append(
            {
                "label": _bin_names[bin_i],
                "data": [round(v) for v in bin_data],
                "backgroundColor": color,
                "borderColor": "rgba(0,0,0,0.12)",
                "borderWidth": 0.5,
                "stack": "volume",
            }
        )

    if not datasets:
        return {}

    # ── Colours driven by theme ──────────────────────────────────────────────
    grid_color = "rgba(180,180,180,0.2)" if is_dark else "rgba(80,80,80,0.15)"
    tick_color = "rgba(200,200,200,0.85)" if is_dark else "rgba(50,50,50,0.85)"
    title_color = "rgba(210,210,210,0.9)" if is_dark else "rgba(35,35,35,0.9)"

    return {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": datasets,
        },
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "animation": False,
            "scales": {
                "x": {
                    "stacked": True,
                    "grid": {"color": grid_color},
                    "ticks": {
                        "color": tick_color,
                        "maxRotation": 45,
                        "autoSkip": True,
                        "maxTicksLimit": 26,
                    },
                },
                "y": {
                    "stacked": True,
                    "beginAtZero": True,
                    "grid": {"color": grid_color},
                    "ticks": {"color": tick_color},
                    "title": {
                        "display": True,
                        "text": "Meters",
                        "color": title_color,
                        "font": {"size": 12},
                    },
                },
            },
            "plugins": {
                "legend": {
                    "display": True,
                    "position": "top",
                    "labels": {
                        "color": tick_color,
                        "boxWidth": 14,
                        "padding": 12,
                    },
                },
            },
            "layout": {
                "padding": {"top": 4, "right": 12, "bottom": 4, "left": 4},
            },
        },
    }


# ---------------------------------------------------------------------------
# Distribution classification + period-row helper
# ---------------------------------------------------------------------------


def _classify_distribution(
    z1_pct: float,
    z2_pct: float,
    z3_pct: float,
) -> str:
    """
    Classify a period's training distribution using precomputed zone percentages.

    Expects z1/z2/z3 as 0–100 floats (percentage of work metres).

    Reference literature thresholds (generous for real-world data):
      Polarized     : Z1 ≥ 65 %, Z3 ≥ 15 %, Z3 > Z2
      Pyramidal     : Z1 ≥ 65 %, Z2 > Z3,   Z2 ≥ 10 %
      Threshold     : Z2 ≥ 20 %  (Z1 < 75 %)
      High Intensity: Z3 ≥ 35 %
      Easy / LSD    : Z1 ≥ 90 %, Z2 < 5 %,  Z3 < 5 %
      Mixed         : does not fit any pattern above
    """
    if z1_pct >= 90 and z2_pct < 5 and z3_pct < 5:
        return "Easy / LSD"
    if z1_pct >= 65 and z3_pct >= 15 and z3_pct > z2_pct:
        return "Polarized"
    if z1_pct >= 65 and z2_pct > z3_pct and z2_pct >= 10:
        return "Pyramidal"
    if z2_pct >= 20:
        return "Threshold"
    if z3_pct >= 35:
        return "High Intensity"
    return "Mixed"


def _pct(part: float, total: float) -> str:
    """Return percentage string like '42%', or '—' if total is zero."""
    if total < 1:
        return "—"
    return f"{round(part / total * 100)}%"


def get_period_rows(
    aggregated: dict,
    view: str,
    scope: str,
    today: Optional[date] = None,
    *,
    bin_names: Optional[list] = None,
    z1_bins: Optional[frozenset] = None,
    z2_bins: Optional[frozenset] = None,
    z3_bins: Optional[frozenset] = None,
    z3a_bins: Optional[frozenset] = None,
    z3b_bins: Optional[frozenset] = None,
    no_data_bins: Optional[frozenset] = None,
) -> list:
    """
    Return a list of row dicts (newest first) for the distribution table.

    Parameters
    ----------
    aggregated, view, scope, today:
        Same as build_volume_chart_config().
    bin_names:
        7-element list of bin display names.  Defaults to BIN_NAMES (pace zones).
        Currently unused in row output but kept for symmetry / future column headers.
    z1_bins, z2_bins, z3_bins:
        frozensets of bin indices that constitute the easy / moderate / hard zones.
        Default to the pace-zone Z1_BINS, Z2_BINS, Z3_BINS from volume_bins.
    z3a_bins, z3b_bins:
        Optional frozensets splitting Z3 into two sub-zones (e.g. Threshold vs Max in
        HR mode).  When provided, the returned rows include z3a_m/z3a_pct and
        z3b_m/z3b_pct alongside z3_m/z3_pct.
    no_data_bins:
        frozenset of bin indices to exclude from the work denominator when computing
        zone percentages for the distribution classification.  Use frozenset({6}) in
        HR mode so "No HR" metres don't dilute the classification fractions.

    Each returned dict has:
        label        — human-readable period (e.g. "Jan 6", "Jan '25", "2025-26")
        total        — formatted total meters (work + rest)
        rest         — formatted rest meters
        z1_pct       — Z1 easy % of classified work meters  (formatted "42%")
        z2_pct       — Z2 moderate % of classified work meters
        z3_pct       — Z3 hard % of classified work meters
        z1_m         — formatted Z1 absolute meters
        z2_m         — formatted Z2 absolute meters
        z3_m         — formatted Z3 absolute meters
        z3a_m/z3a_pct, z3b_m/z3b_pct  — (present only when z3a_bins/z3b_bins given)
        distribution — classification string
    """
    today = today or date.today()
    _z1 = z1_bins if z1_bins is not None else Z1_BINS
    _z2 = z2_bins if z2_bins is not None else Z2_BINS
    _z3 = z3_bins if z3_bins is not None else Z3_BINS
    _no_data = no_data_bins if no_data_bins is not None else frozenset()

    if view == "weekly":
        raw_data = aggregated.get("weeks", {})
    elif view == "monthly":
        raw_data = aggregated.get("months", {})
    else:
        raw_data = aggregated.get("seasons", {})

    keys = _filter_and_sort_keys(list(raw_data.keys()), view, scope, today)
    if not keys:
        return []

    rows = []
    for k in reversed(keys):  # newest first
        entry = raw_data[k]
        b = entry["bins"]

        if view == "weekly":
            label = _week_label(k)
        elif view == "monthly":
            label = _month_label(k)
        else:
            label = k

        rest = b[0]
        work = sum(b[1:])
        total = rest + work

        z1_m = sum(b[i] for i in _z1)
        z2_m = sum(b[i] for i in _z2)
        z3_m = sum(b[i] for i in _z3)

        # For classification: exclude no_data bins (e.g. "No HR") from denominator
        # so the zone fractions reflect only classified metres.
        classified_work = sum(b[i] for i in range(1, len(b)) if i not in _no_data)
        denom = classified_work if classified_work >= 1 else 1.0
        z1_pct_f = z1_m / denom * 100.0
        z2_pct_f = z2_m / denom * 100.0
        z3_pct_f = z3_m / denom * 100.0

        if classified_work < 500:
            dist = "—"
        else:
            dist = _classify_distribution(z1_pct_f, z2_pct_f, z3_pct_f)

        row = {
            "label": label,
            "total": fmt_meters(total),
            "rest": fmt_meters(rest),
            "z1_m": fmt_meters(z1_m),
            "z2_m": fmt_meters(z2_m),
            "z3_m": fmt_meters(z3_m),
            "z1_pct": _pct(z1_m, work),
            "z2_pct": _pct(z2_m, work),
            "z3_pct": _pct(z3_m, work),
            "distribution": dist,
        }

        if z3a_bins is not None and z3b_bins is not None:
            z3a_m = sum(b[i] for i in z3a_bins)
            z3b_m = sum(b[i] for i in z3b_bins)
            row["z3a_m"] = fmt_meters(z3a_m)
            row["z3a_pct"] = _pct(z3a_m, work)
            row["z3b_m"] = fmt_meters(z3b_m)
            row["z3b_pct"] = _pct(z3b_m, work)

        rows.append(row)
    return rows
