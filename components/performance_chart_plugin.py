"""
PerformanceChart — a HyperDiv Plugin that wraps Chart.js with full JavaScript
callback support, enabling proper axis tick formatting, rich tooltips, and
arbitrary canvas overlays.

Usage:
    from components.performance_chart_plugin import PerformanceChart
    PerformanceChart(config=chart_cfg, show_watts=False, height="75vh")

The `config` prop is the same Chart.js dict produced by build_chart_config.
The JS layer applies pace/watts tick formatters and a custom tooltip on top.
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "chart_assets", "performance_chart_plugin.js")) as _f:
    _PERFORMANCE_CHART_JS = _f.read()


class PerformanceChart(hd.Plugin):
    _name = "PerformanceChart"
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
