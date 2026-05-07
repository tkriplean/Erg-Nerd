"""
TrainingLoadChart — HyperDiv Plugin wrapping the Banister CTL/ATL/TSB
fitness-fatigue chart on the Volume page.

Wrapper for ``components/chart_assets/training_load_chart_plugin.js``.
The JS layer renders Chart.js with:
  - CTL / ATL as smooth lines on the primary y-axis
  - TSB as a filled area on a secondary axis (green above 0, red below)
  - Background bands marking TSB interpretation zones
  - Date x-axis (Chart.js time scale)
  - Navigator strip with draggable brush for windowing the main chart

Usage:
    from components.training_load_chart_plugin import TrainingLoadChart
    with hd.box(height="35vh"):
        TrainingLoadChart(config=cfg)
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)


class TrainingLoadChart(hd.Plugin):
    _name = "TrainingLoadChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        # Chart.js core (deduplicated by HyperDiv against other charts).
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        # date-fns adapter for Chart.js's time scale.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js"
        ),
        ("js-link", os.path.join(_HERE, "chart_assets", "training_load_chart_plugin.js")),
    ]

    # Full Chart.js config dict produced by build_training_load_chart_config.
    config = hd.Prop(hd.Any, None)
