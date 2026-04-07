"""
Chart.js stacked-bar config builder for the volume (meters × pace zone) chart.

Exported:
    build_volume_chart_config() — returns a Chart.js config dict for VolumeChart.
    get_period_rows()           — returns table row dicts for the distribution table.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from services.volume_bins import BIN_NAMES, BIN_COLORS, N_BINS
from services.rowing_utils import get_season


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
    is_dark: bool = False,
    today: Optional[date] = None,
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
    is_dark:
        True = dark theme colours.
    today:
        Reference date; defaults to date.today().

    Returns
    -------
    Chart.js config dict, or {} if there is no data to display.
    """
    today = today or date.today()

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
    # Desired visual stack (bottom → top):
    #   Slow Aerobic, Fast Aerobic, Threshold, 5k, 2k, Fast, Rest
    # That corresponds to bin indices: 6, 5, 4, 3, 2, 1, 0
    _BIN_DRAW_ORDER = [6, 5, 4, 3, 2, 1, 0]

    color_dark_or_light = 0 if is_dark else 1
    datasets = []
    for bin_i in _BIN_DRAW_ORDER:
        bin_data = [raw_data[k]["bins"][bin_i] for k in keys]
        if sum(bin_data) < 1.0:
            continue
        color = BIN_COLORS[bin_i][color_dark_or_light]
        datasets.append(
            {
                "label": BIN_NAMES[bin_i],
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


def _classify_distribution(bins: list) -> str:
    """
    Classify a period's training distribution using a 3-zone model:
      Z1 Easy       = Fast Aerobic (bin 5) + Slow Aerobic (bin 6)
      Z2 Threshold  = Threshold (bin 4)
      Z3 Hard       = 5k (bin 3) + 2k (bin 2) + Fast (bin 1)

    Reference literature thresholds (generous for real-world data):
      Polarized   : Z1 ≥ 65 %, Z3 ≥ 15 %, Z3 > Z2
      Pyramidal   : Z1 ≥ 65 %, Z2 > Z3,   Z2 ≥ 10 %
      Threshold   : Z2 ≥ 20 %  (Z1 < 75 %)
      High Intensity: Z3 ≥ 35 %
      Easy / LSD  : Z1 ≥ 90 %, Z2 < 5 %,  Z3 < 5 %
      Mixed       : does not fit any pattern above
    """
    work = sum(bins[1:])  # exclude rest (bin 0)
    if work < 500:
        return "—"

    z1 = (bins[5] + bins[6]) / work * 100.0
    z2 = bins[4] / work * 100.0
    z3 = (bins[1] + bins[2] + bins[3]) / work * 100.0

    if z1 >= 90 and z2 < 5 and z3 < 5:
        return "Easy / LSD"
    if z1 >= 65 and z3 >= 15 and z3 > z2:
        return "Polarized"
    if z1 >= 65 and z2 > z3 and z2 >= 10:
        return "Pyramidal"
    if z2 >= 20:
        return "Threshold"
    if z3 >= 35:
        return "High Intensity"
    return "Mixed"


def _fmt_meters(m: float) -> str:
    """Format a meter count: ≥1000 → '10.5k', else '500m'."""
    v = round(m)
    if v >= 1000:
        k = v / 1000
        return (str(k) if k == int(k) else f"{k:.1f}") + "k"
    return f"{v}m"


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
) -> list:
    """
    Return a list of row dicts (newest first) for the distribution table.

    Each dict has:
        label        — human-readable period (e.g. "Jan 6", "Jan '25", "2025-26")
        total        — formatted total meters (work + rest)
        rest         — formatted rest meters
        z1_pct       — Z1 easy aerobic % of work meters  (formatted "42%")
        z2_pct       — Z2 threshold % of work meters
        z3_pct       — Z3 hard % of work meters
        z1_m         — formatted Z1 absolute meters
        z2_m         — formatted Z2 absolute meters
        z3_m         — formatted Z3 absolute meters
        distribution — classification string
    """
    today = today or date.today()

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

        z1 = b[5] + b[6]
        z2 = b[4]
        z3 = b[1] + b[2] + b[3]

        rows.append(
            {
                "label": label,
                "total": _fmt_meters(total),
                "rest": _fmt_meters(rest),
                "z1_m": _fmt_meters(z1),
                "z2_m": _fmt_meters(z2),
                "z3_m": _fmt_meters(z3),
                "z1_pct": _pct(z1, work),
                "z2_pct": _pct(z2, work),
                "z3_pct": _pct(z3, work),
                "distribution": _classify_distribution(b),
            }
        )
    return rows
