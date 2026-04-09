"""
StrokeChart — lightweight HyperDiv Plugin for per-stroke time-series data.

Renders a multi-axis Chart.js line chart with:
  - Primary Y: pace (sec/500m, inverted) or watts
  - Secondary Y left: SPM
  - Secondary Y right: HR (when available)
  - X axis: elapsed seconds, labelled as M:SS
  - Shaded background bands for each split/interval
  - Click-to-band-zoom via clicked_band_idx prop

Usage::

    chart = StrokeChart(
        config=build_stroke_chart_config(strokes, workout, ...),
        height=300,
    )
    if chart.clicked_band_idx != state.last_band_idx:
        state.last_band_idx = chart.clicked_band_idx
        state.focused_interval = chart.clicked_band_idx

Chart.js (from CDN) is loaded as a shared asset — HyperDiv deduplicates
identical URLs so it is only loaded once even when PerformanceChart is
also on the page.
"""

import os

import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "rowing_chart_assets", "stroke_chart.js")) as _f:
    _STROKE_CHART_JS = _f.read()


class StrokeChart(hd.Plugin):
    _name = "StrokeChart"
    _assets_root = os.path.join(_HERE, "rowing_chart_assets")
    _assets = [
        # Shared Chart.js CDN copy — deduplicated by HyperDiv if PerformanceChart
        # is already registered on the same page.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        # Optional annotation plugin for interval background bands.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3/dist/chartjs-plugin-annotation.min.js"
        ),
        hd.Plugin.js(_STROKE_CHART_JS),
    ]

    # ── Python → JS ──────────────────────────────────────────────────────────

    # Full chart config dict produced by session_chart_builder.build_stroke_chart_config()
    config = hd.Prop(hd.Any, None)

    # Canvas height in pixels (or CSS string like "300px").
    height = hd.Prop(hd.Any, 280)

    # ── JS → Python ──────────────────────────────────────────────────────────

    # Index of the interval band most recently clicked by the user.
    # -1 = none clicked. Python reads this to zoom the x-axis to that interval.
    clicked_band_idx = hd.Prop(hd.Int, -1)
