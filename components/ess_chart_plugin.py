"""
EffortStressChart — HyperDiv plugin for the per-workout Effort & Stress chart.

Renders a Chart.js line chart with two y-axes:

  * Left axis  — IF_eff(t), the recency-weighted intensity factor (services/erg_stress.py).
                 Severity-bucket bands sit as subtle background fills.
  * Right axis — W'bal(t) as a percentage of W' (Skiba's anaerobic reserve).
                 Hidden when no W' / CP estimate is available.

X axis is session-time seconds, formatted as ``M:SS``.  A shaded band on the
chart marks the *current* workout's window within the session timeline so the
user can see how their workout sat in the larger session.

The chart is read-only — no click-to-zoom, no toolbar.  The companion
``StrokeChart`` already covers per-workout zoom and is rendered above this
one on the Workout Page.

Chart.js is loaded as a shared CDN asset; HyperDiv deduplicates the link
when StrokeChart has already registered it.
"""

import os

import hyperdiv as hd

_HERE = os.path.dirname(__file__)


class EffortStressChart(hd.Plugin):
    _name = "EffortStressChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3/dist/chartjs-plugin-annotation.min.js"
        ),
        ("js-link", os.path.join(_HERE, "chart_assets", "ess_chart_plugin.js")),
    ]

    # Python → JS: full Chart.js-shaped config built by build_effort_stress_chart_config().
    config = hd.Prop(hd.Any, None)

    # Canvas height (px or CSS string).  Defaults to a compact value because the
    # chart is rendered below the StrokeChart, not as the page's hero.
    height = hd.Prop(hd.Any, 220)
