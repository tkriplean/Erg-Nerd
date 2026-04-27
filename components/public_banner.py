"""
Public-mode banner rendered at the top of ``_dashboard_view`` when the viewer
is on ``/u/{user_id}``.

Shows:
  - "Viewing {display_name}'s public profile"
  - A link back to the logged-in dashboard ("/")
  - If the viewer's own user_id (from the combined session LS key) matches
    the public profile id, a small "(This is how others see your profile.)"
    hint — useful for owner QA without needing an incognito window.
"""

import json

import hyperdiv as hd

from components.app_context import AppContext, _SESSION_LS_KEY


def public_banner() -> None:
    ctx = AppContext()
    if ctx.mode != "public":
        return

    ls_session = hd.local_storage.get_item(_SESSION_LS_KEY)
    viewer_uid = ""
    if ls_session.done and ls_session.result:
        try:
            viewer_uid = json.loads(ls_session.result).get("user_id") or ""
        except Exception:
            viewer_uid = ""
    is_self_view = bool(viewer_uid) and str(viewer_uid) == str(ctx.user_id)

    with hd.hbox(
        padding=1,
        background_color="primary-100",
        border_bottom="1px solid primary-200",
        gap=1,
        align="center",
        justify="center",
        wrap="wrap",
    ):
        hd.icon("eye", font_color="primary-700")
        hd.text(
            f"Viewing {ctx.display_name}'s public profile",
            font_color="primary-900",
            font_weight="semibold",
            font_size="small",
        )
        if is_self_view:
            hd.text(
                "(This is how others see your profile.)",
                font_color="primary-700",
                font_size="small",
            )
        with hd.box(grow=True):
            pass
        hd.link(
            "Switch to your Concept2 account",
            href="/",
            target="_self",
            font_size="small",
            font_color="primary-700",
        )
