"""
LazyTooltip — defers tooltip body construction until the user actually hovers.

The Power Spread and HR Spread columns each render an `sl-tooltip` per row.
At ~200 rows × 2 columns that is 400 tooltips per page, every tooltip body
fully built in Python and shipped to the browser even when never viewed.
This plugin replaces those `hd.tooltip()` wrappers with a small custom
element that:

  * draws the visible cell content (score + bar, or info icon) directly in
    the plugin's shadow DOM from a serialisable `config` dict, and
  * arms a single `mouseenter` / `focusin` listener that lazily creates an
    `<sl-tooltip>` with the rich body the first time the user actually hovers.

All Python-side string/colour resolution still happens in the call sites
(see render_spread_cell in workout_table.py).  JS only turns the resolved
config into DOM.

Config shapes (passed as the `config` prop):

    {"kind": "spread",
     "score": "120",                       # already formatted
     "bin_meters": [0, 1200, 800, …],      # raw work-meter counts per zone
     "bar_colors": ["rgba(…)", …],         # theme-resolved zone colours
     "bar_w": 5.0, "bar_h": 0.5,           # rem
     "items": [{"swatch_uri": "…", "pct_text": "42%"}, …]}

    {"kind": "info",
     "color": "rgba(0,0,0,0)",             # icon colour (CSS string), optional
     "title": "Stimulus shape",
     "body":  "…explanatory text…"}
"""

import os

import hyperdiv as hd

_HERE = os.path.dirname(__file__)


class LazyTooltip(hd.Plugin):
    _name = "LazyTooltip"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        ("js-link", os.path.join(_HERE, "chart_assets", "lazy_tooltip_plugin.js"))
    ]

    config = hd.Prop(hd.Any, None)
    placement = hd.Prop(hd.String, "top")
