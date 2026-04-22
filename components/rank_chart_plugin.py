"""
RankChart — HyperDiv Plugin wrapping Chart.js for the Rank Page.

Renders a scatter chart with a **categorical** x-axis (one tick per ranked
event in a caller-defined order) and a numeric y-axis showing either
"% of world record" or "percentile of ranking pool". No animation loop,
no timeline, no scrubber — static chart only.

Props (Python → JS):
    event_order   — list of {key: str, label: str} in x-axis order.
                    ``key`` matches the per-point ``x_key``.
    series        — list of series objects:
                      {label, color, border_color,
                       points: [{x_key, y, tooltip}, ...]}
    y_label       — y-axis label (e.g. "% of World Record" or "Percentile").
    y_mode        — "pct" (% of WR; dashed reference line at y=100)
                    or "percentile" (0-100, dashed reference at y=50).
    is_dark       — bool dark-mode flag.
    height_css    — CSS height string for the canvas container.
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "chart_assets", "rank_chart_plugin.js")) as _f:
    _RANK_CHART_JS = _f.read()


class RankChart(hd.Plugin):
    _name = "RankChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        hd.Plugin.js(_RANK_CHART_JS),
    ]

    event_order = hd.Prop(hd.Any, [])
    series = hd.Prop(hd.Any, [])
    y_label = hd.Prop(hd.String, "")
    y_mode = hd.Prop(hd.String, "pct")  # "pct" | "percentile"
    is_dark = hd.Prop(hd.Bool, False)
    height_css = hd.Prop(hd.String, "55vh")
