"""
Render-time application context, kept in a per-connection ``@hd.global_state``.

The dashboard has two modes:

- **Owner mode** — the logged-in user browsing their own data. Pages call
  ``concept2_sync(ctx.client)`` to load and sync workouts; ``get_profile()``
  pulls the profile from localStorage.

- **Public mode** — anyone (no login required) viewing someone else's
  opt-in public profile at ``/u/{user_id}``. There is no ``Concept2Client``;
  ``app.py`` pre-loads workouts and profile from
  ``services.public_profiles`` before constructing the context. Pages call
  ``sync_workouts()`` / ``get_profile()`` which short-circuit to the
  pre-loaded values with no I/O.

``AppContext`` is a hyperdiv ``@hd.global_state`` BaseState. Each websocket
connection (i.e. each user session) has its own AppRunner, so different
viewers do not share state. Pages call ``AppContext()`` from anywhere to
read the active mode, user_id, client, etc. — no need to thread ``ctx``
through every signature.

The ``client`` and the public-mode pre-loaded snapshots are stored as
``hd.Any`` props because they are arbitrary Python objects that live purely
server-side (BaseState is ``collect=False``).
"""

from typing import Optional

import hyperdiv as hd


@hd.global_state
class AppContext(hd.BaseState):
    mode = hd.Prop(hd.String, "owner")  # "owner" | "public"
    user_id = hd.Prop(hd.String, "")
    display_name = hd.Prop(hd.String, "")
    client = hd.Prop(hd.Any, None)  # Concept2Client | None
    # Public-mode only: pre-loaded from services.public_profiles.
    public_workouts_dict = hd.Prop(hd.Any, None)
    public_sorted_workouts = hd.Prop(hd.Any, None)
    public_profile = hd.Prop(hd.Any, None)

    @property
    def is_owner(self) -> bool:
        return self.mode == "owner"

    @property
    def is_public(self) -> bool:
        return self.mode == "public"


def _client_auth(client) -> str:
    """Extract a Concept2Client's Authorization header for equality checks.

    ``get_client`` returns a fresh ``Concept2Client`` instance every render;
    comparing object identity would always differ and trip an infinite
    re-render loop. The auth header changes only on token refresh.
    """
    if client is None:
        return ""
    try:
        return str(client._http.headers.get("Authorization", ""))
    except Exception:
        return ""


def populate_owner(client, user_id: str, display_name: str = "") -> None:
    """Populate AppContext for owner mode. Idempotent — only writes props
    when the owning user changes or the access token has been refreshed."""
    ctx = AppContext()
    uid = str(user_id)
    same_user = ctx.mode == "owner" and ctx.user_id == uid
    same_token = same_user and _client_auth(ctx.client) == _client_auth(client)
    if same_token:
        return
    ctx.mode = "owner"
    ctx.user_id = uid
    if not same_user:
        ctx.display_name = display_name or ""
        ctx.public_workouts_dict = None
        ctx.public_sorted_workouts = None
        ctx.public_profile = None
    ctx.client = client


def populate_public(user_id: str) -> bool:
    """
    Load server-side public data for ``user_id`` and write it into AppContext.
    Returns True on success, False when no published data exists (caller
    should render the 404 view).

    Idempotent: skips writes when (mode, user_id) already match.
    """
    from services.public_profiles import (
        exists as pp_exists,
        load_public_profile,
        load_public_workouts,
    )

    if not pp_exists(user_id):
        return False
    ctx = AppContext()
    uid = str(user_id)
    if ctx.mode == "public" and ctx.user_id == uid:
        return True
    profile = load_public_profile(user_id)
    workouts_dict = load_public_workouts(user_id)
    if profile is None or workouts_dict is None:
        return False
    sorted_workouts = sorted(
        workouts_dict.values(), key=lambda w: w.get("date", ""), reverse=True
    )
    ctx.mode = "public"
    ctx.user_id = uid
    ctx.display_name = profile.get("display_name") or "Rower"
    ctx.client = None
    ctx.public_workouts_dict = workouts_dict
    ctx.public_sorted_workouts = sorted_workouts
    ctx.public_profile = profile
    return True


def your(capitalize: bool = True) -> str:
    """
    Return the second-person possessive ("Your") in owner mode or the viewed
    rower's possessive ("Hank's") in public mode. Used for headings like
    "Your Quality 2k Efforts" which become "Hank's Quality 2k Efforts" when
    someone else is browsing a public profile.

    Falls back to "Their/their" when no display name is set.
    """
    ctx = AppContext()
    if ctx.mode == "owner":
        return "Your" if capitalize else "your"
    name = (ctx.display_name or "").strip()
    if not name:
        return "Their" if capitalize else "their"
    if name[-1] == "s":
        return f"{name}'"
    return f"{name}'s"
