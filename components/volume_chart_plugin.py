"""
VolumeChart — a HyperDiv Plugin that wraps Chart.js for the stacked-bar
volume (meters × pace zone) chart on the Workouts page.

Usage:
    from components.volume_chart_plugin import VolumeChart
    with hd.box(height="40vh"):
        VolumeChart(config=chart_config)

The `config` prop is the Chart.js dict produced by volume_chart_builder.
The JS layer (volume_chart.js) attaches meter formatters and a rich tooltip.
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)


class VolumeChart(hd.Plugin):
    _name = "VolumeChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        # Same Chart.js CDN as RowingChart — HyperDiv deduplicates identical URLs.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        ("js-link", os.path.join(_HERE, "chart_assets", "volume_chart_plugin.js")),
    ]

    # Full Chart.js config dict produced by build_volume_chart_config().
    config = hd.Prop(hd.Any, None)
