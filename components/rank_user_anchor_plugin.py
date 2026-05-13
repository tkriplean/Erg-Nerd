"""
RankUserAnchor — invisible HyperDiv plugin embedded inside the user's row in
the Rank Page rankings modal. On mount and whenever its `signature` prop
changes, the plugin scrolls its host element into the center of the nearest
scrollable ancestor (the rows container with max-height: 60vh), so the
highlighted user row is visible when the modal opens or re-opens on a new
ranking.

Usage: see `render_rankings_modal` in components/rank_ranking_modal.py.
"""

import os

import hyperdiv as hd

_HERE = os.path.dirname(__file__)


class RankUserAnchor(hd.Plugin):
    _name = "RankUserAnchor"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        ("js-link", os.path.join(_HERE, "chart_assets", "rank_user_anchor_plugin.js"))
    ]

    signature = hd.Prop(hd.String, "")
