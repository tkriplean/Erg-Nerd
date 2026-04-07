"""
DateSlider — timeline scrubber with date tooltip and SB annotation dots.

Prop ownership:
  Python → JS   target_value   authoritative position; always written by Python,
                               never by JS, so HyperDiv's mutated flag never
                               blocks updates (fixes animation thumb not moving).
  JS → Python   value          last position reported by a user drag/seek.
  JS → Python   change_id      monotonically incremented on every user interaction;
                               Python gates sim_week updates on this changing.
  Python → JS   min_value, max_value, step, start_date, annotations

Usage:
    ds = DateSlider(
        min_value=0,
        max_value=total_days - 1,
        target_value=sim_day_idx,   # drives the thumb
        step=1,
        start_date=sim_start.isoformat(),
    )
    if ds.change_id != state.last_ds_change_id:
        state.last_ds_change_id = ds.change_id
        state.sim_week = int(ds.value)   # read user's seek position
"""

import os
import hyperdiv as hd

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "date_slider_assets", "date_slider.js")) as _f:
    _DATE_SLIDER_JS = _f.read()


class DateSlider(hd.Plugin):
    _name = "DateSlider"
    _assets_root = os.path.join(_HERE, "date_slider_assets")
    _assets = [hd.Plugin.js(_DATE_SLIDER_JS)]

    min_value    = hd.Prop(hd.Int, 0)
    max_value    = hd.Prop(hd.Int, 100)
    # Python-owned: drives the visible thumb position every render.
    # Never written by JS, so StoredProp.mutated stays False and init() always
    # updates it — fixing animation frames being ignored after a user drag.
    target_value = hd.Prop(hd.Int, 0)
    # JS-owned: last position reported by a user drag/annotation click.
    # Python reads this to detect seek gestures (gated by change_id).
    value        = hd.Prop(hd.Int, 0)
    step         = hd.Prop(hd.Int, 1)
    start_date   = hd.Prop(hd.String, "")
    # Monotonically incremented by JS on every genuine user interaction.
    change_id    = hd.Prop(hd.Int, 0)
    # [{day, label, color}] — SB dots below the track.
    annotations  = hd.Prop(hd.Any, [])
