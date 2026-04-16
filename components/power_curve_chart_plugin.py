"""
PowerCurveChart — a HyperDiv Plugin that wraps Chart.js with full JavaScript
callback support, enabling proper axis tick formatting, rich tooltips, and
arbitrary canvas overlays.  Includes an integrated transport bar (Play/Pause
button, Speed selector) and timeline scrubber so that all playback interaction
is handled entirely in JS with zero Python round-trips.

Usage:
    from components.power_curve_chart_plugin import PowerCurveChart
    PowerCurveChart(
        config=chart_cfg,
        show_watts=False,
        x_mode="distance",
        timeline_min=0,
        timeline_max=total_days,
        timeline_start_date=sim_start.isoformat(),
        timeline_annotations=sb_annotations,
        rewind_day=rewind_day,
        height="75vh",
    )

The `config` prop is the same Chart.js dict produced by build_chart_config.
The JS layer applies pace/watts tick formatters and a custom tooltip on top.

Props (Python → JS):
    config               — full Chart.js config dict from build_chart_config(); used
                           for static rendering.  JS ignores this when sim_bundle is
                           active.
    show_watts           — True → Y-axis in watts; False → pace (sec/500m)
    x_mode               — "distance" (meters, default) or "duration" (seconds)
    sim_bundle           — precomputed animation bundle dict (None until task completes)
    sim_command          — "play" | "pause" | "stop" — Python signals state transitions
                           (triggers play when bundle loads, stop when at_today)
    sim_speed            — "0.5x" | "1x" | "4x" | "16x" — initial playback speed;
                           JS owns speed state after init (no round-trip on change)
    rewind_day           — day to seek to when Play is pressed at end of timeline
    timeline_min         — minimum day value for the scrubber (default 0)
    timeline_max         — maximum day value for the scrubber
    timeline_start_date  — ISO date string for day 0 (used for tooltip labels)
    timeline_annotations — list of {day, label, color} dicts for SB dots

Props (JS → Python):
    sim_playing_out — True/False sent when user clicks the Play/Pause button;
                      Python uses this to gate bundle loading (sim_playing state)
    sim_day_out     — current animation day reported by JS on every tick and after seeks
    sim_done        — monotonically incremented by JS when animation completes
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

    # ── Animation props (Python → JS) ─────────────────────────────────────────

    # Precomputed animation bundle dict.  None until the bundle task completes.
    # JS caches by bundle_key — only re-applied when the key changes.
    sim_bundle = hd.Prop(hd.Any, None)

    # Animation command.
    #   "play"  — start/resume the animation interval
    #   "pause" — stop the interval, preserve currentDay
    #   "stop"  — clear interval, reset currentDay = 0
    # Seeking is handled entirely in JS via the integrated scrubber — no
    # "seek:N" variant; Python never needs to route seeks.
    sim_command = hd.Prop(hd.String, "stop")

    # Initial playback speed string.  JS initializes its speed button from this
    # and owns speed state after that — no round-trip on user speed changes.
    sim_speed = hd.Prop(hd.String, "1x")

    # Day to seek to when the Play button is pressed while at the end of the
    # timeline (typically 30 days before the first qualifying event).
    rewind_day = hd.Prop(hd.Int, 0)

    # ── Back-communication props (JS → Python) ────────────────────────────────

    # Written by JS when the user clicks Play or Pause.  Python uses this to
    # update state.sim_playing, which gates bundle loading.  Not sent from
    # handleSimCommand — only from genuine user interaction.
    sim_playing_out = hd.Prop(hd.Bool, False)

    # Current animation day written by JS on every tick and after user seeks.
    sim_day_out = hd.Prop(hd.Int, -1)

    # Monotonically incremented by JS when the animation reaches total_days.
    # Python gates "animation complete" on this changing.
    sim_done = hd.Prop(hd.Int, 0)

    # ── Timeline scrubber props (Python → JS) ─────────────────────────────────

    # Range for the scrubber track (day offsets from sim_start).
    timeline_min = hd.Prop(hd.Int, 0)
    timeline_max = hd.Prop(hd.Int, 100)

    # ISO date string for day 0 — used to render date tooltips on the scrubber.
    timeline_start_date = hd.Prop(hd.String, "")

    # Annotation dots: list of {day, label, color} — SB/PB events below the track.
    timeline_annotations = hd.Prop(hd.Any, [])
