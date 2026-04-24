"""
Render-time context object threaded through every dashboard page.

The dashboard has two modes:

- **Owner mode** — the logged-in user browsing their own data.  Pages call
  ``concept2_sync(ctx.client)`` to load and sync workouts; ``get_profile()``
  pulls the profile from localStorage.

- **Public mode** — anyone (no login required) viewing someone else's
  opt-in public profile at ``/u/{user_id}``.  There is no ``Concept2Client``;
  ``app.py`` pre-loads workouts and profile from
  ``services.public_profiles`` before constructing the context.  Pages call
  ``sync_from_context(ctx)`` / ``get_profile_from_context(ctx)`` which
  short-circuit to the pre-loaded values with no I/O.

Pages should accept ``ctx`` in place of the old ``(client, user_id)`` pair.
When a page still needs the raw client or user_id, read ``ctx.client`` and
``ctx.user_id`` — ``ctx.client`` is ``None`` in public mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass(frozen=True)
class ViewContext:
    mode: str  # "owner" | "public"
    user_id: str
    client: Optional[Any] = None  # Concept2Client in owner mode; None in public
    display_name: str = ""
    # Public-mode only: pre-loaded from services.public_profiles.
    public_workouts_dict: Optional[dict] = None
    public_sorted_workouts: Optional[list] = None
    public_profile: Optional[dict] = None

    @property
    def is_owner(self) -> bool:
        return self.mode == "owner"

    @property
    def is_public(self) -> bool:
        return self.mode == "public"


def your(ctx: "ViewContext", capitalize: bool = True) -> str:
    """
    Return the second-person possessive ("Your") in owner mode or the viewed
    rower's possessive ("Hank's") in public mode.  Used for headings like
    "Your Quality 2k Efforts" which become "Hank's Quality 2k Efforts" when
    someone else is browsing a public profile.

    Falls back to "Their/their" when no display name is set.
    """
    if ctx is None or ctx.mode == "owner":
        return "Your" if capitalize else "your"
    name = (ctx.display_name or "").strip()
    if not name:
        return "Their" if capitalize else "their"
    if name[-1] == "s":
        return f"{name}'"
    return f"{name}'s"


def build_owner_context(client, user_id: str, display_name: str = "") -> ViewContext:
    return ViewContext(
        mode="owner",
        user_id=str(user_id),
        client=client,
        display_name=display_name or "",
    )


def build_public_context(user_id: str) -> Optional[ViewContext]:
    """
    Load server-side public data for ``user_id`` and build a public ViewContext.
    Returns None if no public data is published.
    """
    from services.public_profiles import (
        exists as pp_exists,
        load_public_profile,
        load_public_workouts,
    )

    if not pp_exists(user_id):
        return None
    profile = load_public_profile(user_id)
    workouts_dict = load_public_workouts(user_id)
    if profile is None or workouts_dict is None:
        return None
    sorted_workouts = sorted(
        workouts_dict.values(), key=lambda w: w.get("date", ""), reverse=True
    )
    return ViewContext(
        mode="public",
        user_id=str(user_id),
        client=None,
        display_name=profile.get("display_name") or "Rower",
        public_workouts_dict=workouts_dict,
        public_sorted_workouts=sorted_workouts,
        public_profile=profile,
    )
