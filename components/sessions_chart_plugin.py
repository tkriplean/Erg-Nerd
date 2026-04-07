"""
SessionsChart — HyperDiv Plugin for the pace-vs-date scatter with brush navigator.

Usage:
    from components.sessions_chart_plugin import SessionsChart
    chart = SessionsChart(
        points=pts,
        target_window_start=start_ms,
        target_window_end=end_ms,
        is_dark=hd.theme().is_dark,
        height="52vh",
    )
    if chart.change_id != state.last_change_id:
        state.last_change_id = chart.change_id
        state.window_end_ms  = chart.brush_end

See sessions_chart.js for the full rendering and interaction logic.
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "rowing_chart_assets", "sessions_chart.js")) as _f:
    _SESSIONS_CHART_JS = _f.read()


class SessionsChart(hd.Plugin):
    _name       = "SessionsChart"
    _assets_root = os.path.join(_HERE, "rowing_chart_assets")
    _assets = [
        # Same Chart.js CDN copy as RowingChart / VolumeChart.
        # HyperDiv deduplicates identical URLs so it's only loaded once.
        hd.Plugin.js_link(
            "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
        ),
        hd.Plugin.js(_SESSIONS_CHART_JS),
    ]

    # ── Python → JS (never written by JS) ────────────────────────────────────

    # Pre-computed point data — list of dicts with fields:
    #   x, y, r, r2, c, c33, c70, ivl, sb, dist, date_str, dist_str
    # Sorted largest-dist-first by the builder so big dots render behind small ones.
    points = hd.Prop(hd.Any, [])

    # Window bounds sent from Python (e.g. ◄/► buttons, window-size selector).
    # JS uses these to position the brush but never writes back to them.
    target_window_start = hd.Prop(hd.Int, 0)   # Unix ms
    target_window_end   = hd.Prop(hd.Int, 0)   # Unix ms

    is_dark = hd.Prop(hd.Bool, False)

    # ── JS → Python (never written by Python after initial instantiation) ─────

    # Last brush position after a user drag or jump-click.
    # Python reads these to sync its state after a brush interaction.
    brush_start = hd.Prop(hd.Int, 0)   # Unix ms
    brush_end   = hd.Prop(hd.Int, 0)   # Unix ms

    # Monotonically incremented by JS on every genuine user interaction.
    # Python gates state updates on this changing (same pattern as DateSlider).
    change_id   = hd.Prop(hd.Int, 0)
