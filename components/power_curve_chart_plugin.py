"""
PowerCurveChart — a HyperDiv Plugin that wraps Chart.js with full JavaScript
callback support, enabling proper axis tick formatting, rich tooltips, and
arbitrary canvas overlays.

Usage:
    from components.power_curve_chart_plugin import PowerCurveChart
    PowerCurveChart(config=chart_cfg, show_watts=False, x_mode="distance", height="75vh")

The `config` prop is the same Chart.js dict produced by build_chart_config.
The JS layer applies pace/watts tick formatters and a custom tooltip on top.

Props:
    config     — full Chart.js config dict from build_chart_config()
    show_watts — True → Y-axis in watts; False → pace (sec/500m)
    x_mode     — "distance" (meters, default) or "duration" (seconds)
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "chart_assets", "power_curve_chart_plugin.js")) as _f:
    _PERFORMANCE_CHART_JS = _f.read()


class PowerCurveChart(hd.Plugin):
    _name = "PowerCurveChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        # Chart.js loaded from CDN — HyperDiv's own Chart.js is bundled/scoped
        # and not exposed as window.Chart, so we need our own copy.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        hd.Plugin.js(_PERFORMANCE_CHART_JS),
    ]

    # The full Chart.js config dict (same structure as hd.chart expects).
    config = hd.Prop(hd.Any, None)

    # Controls Y-axis formatting: True = watts, False = pace (sec/500m).
    show_watts = hd.Prop(hd.Bool, False)

    # X-axis mode: "distance" (meters) or "duration" (seconds).
    x_mode = hd.Prop(hd.String, "distance")
