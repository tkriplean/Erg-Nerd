"""
RaceChart — HyperDiv Plugin for the regatta-style event race animation.

Usage:
    from components.race_chart_plugin import RaceChart

    chart = RaceChart(
        races=races_data,       # list of boat dicts from stroke_utils.build_races_data()
        event_type="dist",      # "dist" | "time"
        event_value=2000,       # meters (dist) or tenths-of-sec (time)
        is_dark=hd.theme().is_dark,
        height="58vh",
    )

Props — Python → JS (Python-owned, never written by JS):
    races         List of boat dicts: [{id, label, color, strokes, is_pb, season}, …]
                  Each strokes list is [{t: secs, d: meters}, …] sorted ascending by t.
    event_type    "dist" (distance event) or "time" (timed event).
    event_value   For distance events: target distance in meters.
                  For time events: event duration in tenths of a second.
    is_dark       Boolean dark-mode flag — drives canvas color scheme.

Props — JS → Python (JS-owned, written by JS on user interaction):
    change_id       Monotonically incremented when the user seeks.
    current_time_ms Current race position in milliseconds (as of last seek).
    wr_requested    True when the user has ticked the "Include World Record
                    boat" checkbox in the ghost lane.

Plus two WR-ghost-lane props:
    wr_available    Python → JS. When True, the plugin renders an extra
                    "ghost lane" beneath the last real boat with a Shoelace
                    checkbox labelled "Include World Record boat".  The lane
                    is only visible while the race is NOT in progress
                    (``!playing && currentTimeMs === 0``).

All animation is handled internally by the JS plugin using requestAnimationFrame.
Python does not drive a tick loop for this plugin.

See race_chart_plugin.js for full rendering and interaction logic.
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "chart_assets", "race_chart_plugin.js")) as _f:
    _RACE_CHART_JS = _f.read()


class RaceChart(hd.Plugin):
    _name = "RaceChart"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        hd.Plugin.js(_RACE_CHART_JS),
    ]

    # ── Python → JS (never written by JS) ────────────────────────────────────

    # List of boat dicts (see stroke_utils.build_races_data for schema).
    races = hd.Prop(hd.Any, [])

    # "dist" for distance events (e.g. 2000m), "time" for timed events (e.g. 30 min).
    event_type = hd.Prop(hd.String, "dist")

    # For dist events: target distance in meters.
    # For time events: event duration in tenths of a second.
    event_value = hd.Prop(hd.Int, 2000)

    is_dark = hd.Prop(hd.Bool, False)

    # When True, the plugin renders a bottom "ghost lane" with a checkbox
    # labelled "Include World Record boat".  Hidden while a race is running.
    wr_available = hd.Prop(hd.Bool, False)

    # ── JS → Python (never written by Python after initial instantiation) ─────

    # Monotonically incremented by JS whenever the user seeks via the slider.
    change_id = hd.Prop(hd.Int, 0)

    # Race clock position at the time of the last user seek (milliseconds).
    current_time_ms = hd.Prop(hd.Int, 0)

    # JS-owned: True when the user has ticked the "Include World Record boat"
    # checkbox in the ghost lane.  Python reads this into state.show_wr_boat.
    wr_requested = hd.Prop(hd.Bool, False)
