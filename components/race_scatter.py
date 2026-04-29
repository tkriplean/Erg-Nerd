"""
race_scatter.py — Pure-Python Chart.js config builder for the Race Page's
pace-vs-date scatter chart.

Renders via :class:`RaceScatterChart` (a thin plugin in
``components/race_scatter_plugin.py``).  We use a *linear* x-axis with
epoch-ms values — rather than Chart.js's ``time`` scale — so the chart works
without bundling ``chartjs-adapter-date-fns``.

Exported:
    build_race_scatter_config(racing_workouts, *, metric, is_dark, pb_id) -> dict

The scatter shows one dot per qualifying workout, colored by season.
Y-axis is pace (sec/500m, inverted) or watts.
"""

from __future__ import annotations

from typing import Optional

from services.rowing_utils import season_color


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
    'return months[d.getUTCMonth()]+" \'"+String(d.getUTCFullYear()).slice(-2);'
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
        if w["pace"] is None or not w["date_ms"]:
            continue
        points.append({"x": w["date_ms"], "y": w["watts"] if show_watts else w["pace"]})
        colors.append(season_color(w["season"], fmt="hex"))
        is_pb = pb_id is not None and w["id"] == pb_id
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
