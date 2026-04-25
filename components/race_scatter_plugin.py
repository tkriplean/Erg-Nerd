"""
RaceScatterChart — thin HyperDiv Plugin around Chart.js for the Race Page's
pace-vs-date scatter.

This wrapper exists because HyperDiv's built-in ``hd.chart`` JSON-encodes the
config dict, dropping function values.  The Race scatter needs custom tick
and tooltip callbacks (formatted ``m:ss`` pace, date-aware tooltip lines), so
we ship them as JS-source strings inside the config and re-eval them
client-side.

Usage::

    from components.race_scatter import build_race_scatter_config
    from components.race_scatter_plugin import RaceScatterChart

    cfg = build_race_scatter_config(racing_workouts, metric="pace", is_dark=is_dark)
    RaceScatterChart(config=cfg, height="40vh")

Props
-----
config : dict
    Full Chart.js config produced by ``build_race_scatter_config``.
height : int | str
    Canvas height — px (int) or CSS length (string like ``"40vh"``).
"""

import os

import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "chart_assets", "race_scatter_plugin.js")) as _f:
    _RACE_SCATTER_JS = _f.read()


class RaceScatterChart(hd.Plugin):
    _name = "RaceScatterChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        hd.Plugin.js(_RACE_SCATTER_JS),
    ]

    config = hd.Prop(hd.Any, None)
    height = hd.Prop(hd.Any, "40vh")
