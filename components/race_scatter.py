"""
race_scatter.py — Pure-Python Chart.js config builder for the Race Page's
pace-vs-date scatter chart.

Renders via :class:`RaceScatterChart` (a thin plugin in
``components/race_scatter_plugin.py``).  We use a *linear* x-axis with
epoch-ms values — rather than Chart.js's ``time`` scale — so the chart works
without bundling ``chartjs-adapter-date-fns``.

Exported:
    build_race_scatter_config(racing_workouts, *, metric, is_dark, pb_id) -> dict

The scatter shows one dot per qualifying workout, colored by season, with
a dashed OLS best-fit line.  Y-axis is pace (sec/500m, inverted) or watts.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from services.rowing_utils import (
    compute_pace,
    compute_watts,
    get_season,
    season_color,
)


# ── JS callback strings ──────────────────────────────────────────────────────
# These are restored to real functions inside the plugin via eval().

_PACE_TICK_JS = (
    "function(v){"
    "var s=Math.abs(v);"
    "var m=Math.floor(s/60);"
    "var sec=String(Math.round(s%60)).padStart(2,'0');"
    "return m+':'+sec;"
    "}"
)

_DATE_TICK_JS = (
    "function(v){"
    "var d=new Date(v);"
    "var months=['Jan','Feb','Mar','Apr','May','Jun',"
    "'Jul','Aug','Sep','Oct','Nov','Dec'];"
    "return months[d.getUTCMonth()]+\" '\"+String(d.getUTCFullYear()).slice(-2);"
    "}"
)

_PACE_TOOLTIP_JS = (
    "function(ctx){"
    "var raw=ctx.raw||{};"
    "var d=new Date(raw.x);"
    "var date=d.toISOString().slice(0,10);"
    "var s=Math.abs(raw.y);"
    "var m=Math.floor(s/60);"
    "var sec=(s%60).toFixed(1).padStart(4,'0');"
    "return date+': '+m+':'+sec+'/500m';"
    "}"
)

_WATTS_TOOLTIP_JS = (
    "function(ctx){"
    "var raw=ctx.raw||{};"
    "var d=new Date(raw.x);"
    "var date=d.toISOString().slice(0,10);"
    "return date+': '+Math.round(raw.y)+' W';"
    "}"
)


def _parse_date_ms(date_str: str) -> Optional[int]:
    """Parse a Concept2 ISO date string to epoch milliseconds (UTC)."""
    if not date_str:
        return None
    try:
        dt = _dt.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = _dt.datetime.fromisoformat(date_str[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def _ols_endpoints(points: list[dict]) -> Optional[list[dict]]:
    """Fit ``y = a*x + b`` and return ``[first, last]`` endpoints for a best-fit
    line.  Returns ``None`` when fewer than two points or x range is zero."""
    if len(points) < 2:
        return None
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    x_min, x_max = min(xs), max(xs)
    if x_max == x_min:
        return None
    n = len(points)
    # Normalise x to days-from-min to keep the slope numerically stable.
    day_ms = 1000 * 60 * 60 * 24
    xn = [(x - x_min) / day_ms for x in xs]
    mean_x = sum(xn) / n
    mean_y = sum(ys) / n
    num = sum((xn[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xn[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = mean_y - slope * mean_x
    x_end_days = (x_max - x_min) / day_ms
    return [
        {"x": x_min, "y": intercept},
        {"x": x_max, "y": intercept + slope * x_end_days},
    ]


def build_race_scatter_config(
    racing_workouts: list[dict],
    *,
    metric: str = "pace",
    is_dark: bool = False,
    pb_id: Optional[int] = None,
) -> dict:
    """
    Return a Chart.js config dict for the pace/watts-vs-date scatter.

    Parameters
    ----------
    racing_workouts : the full qualifying-event workout set (no Include filter)
    metric          : ``"pace"`` (sec/500m, inverted axis) or ``"watts"``
    is_dark         : apply dark-mode tick and grid colours
    pb_id           : workout id to emphasise (larger dot + white ring)
    """
    show_watts = metric == "watts"

    points: list[dict] = []
    colors: list[str] = []
    radii: list[float] = []
    borders: list[str] = []
    border_widths: list[float] = []

    for w in racing_workouts:
        pace = compute_pace(w)
        if pace is None:
            continue
        x_ms = _parse_date_ms(w.get("date") or "")
        if x_ms is None:
            continue
        y = compute_watts(pace) if show_watts else pace
        points.append({"x": x_ms, "y": y})
        colors.append(season_color(get_season(w.get("date", "")), fmt="hex"))
        is_pb = pb_id is not None and w.get("id") == pb_id
        radii.append(6 if is_pb else 4)
        borders.append("#ffffff" if is_pb else "rgba(0,0,0,0)")
        border_widths.append(2 if is_pb else 0)

    tick_color = "#9ca3af" if is_dark else "#6b7280"
    grid_color = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.08)"

    datasets = [
        {
            "type": "scatter",
            "label": "Efforts",
            "data": points,
            "pointBackgroundColor": colors,
            "pointRadius": radii,
            "pointBorderColor": borders,
            "pointBorderWidth": border_widths,
            "showLine": False,
        }
    ]

    fit = _ols_endpoints(points)
    if fit is not None:
        datasets.append(
            {
                "type": "line",
                "label": "Trend",
                "data": fit,
                "borderColor": tick_color,
                "borderWidth": 1.5,
                "borderDash": [4, 4],
                "pointRadius": 0,
                "showLine": True,
                "fill": False,
            }
        )

    y_ticks: dict = {"color": tick_color}
    if not show_watts:
        y_ticks["callback"] = _PACE_TICK_JS

    y_axis = {
        "type": "linear",
        "position": "left",
        "reverse": not show_watts,
        "grid": {"color": grid_color},
        "ticks": y_ticks,
        "title": {
            "display": True,
            "text": "watts" if show_watts else "pace (/500m)",
            "color": tick_color,
            "font": {"size": 11},
        },
    }

    return {
        "type": "scatter",
        "data": {"datasets": datasets},
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "animation": False,
            "plugins": {
                "legend": {"display": False},
                "tooltip": {
                    "callbacks": {
                        "label": _WATTS_TOOLTIP_JS if show_watts else _PACE_TOOLTIP_JS
                    }
                },
            },
            "scales": {
                "x": {
                    "type": "linear",
                    "grid": {"color": grid_color},
                    "ticks": {
                        "color": tick_color,
                        "maxRotation": 0,
                        "autoSkip": True,
                        "maxTicksLimit": 8,
                        "callback": _DATE_TICK_JS,
                    },
                },
                "y": y_axis,
            },
        },
    }
