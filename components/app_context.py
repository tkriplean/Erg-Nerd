"""
Render-time application context, kept in a per-connection ``@hd.global_state``.

The dashboard has two modes:

- **Owner mode** — the logged-in user browsing their own data. ``app.py``
  calls ``concept2_sync(ctx.client)`` once per session inside
  ``_dashboard_view``; the result is cached on ``ctx.workouts_dict`` /
  ``ctx.sorted_workouts``. ``get_profile()`` pulls the profile from
  localStorage.

- **Public mode** — anyone (no login required) viewing someone else's
  opt-in public profile at ``/u/{user_id}``. There is no ``Concept2Client``;
  ``app.py`` pre-loads workouts and profile from
  ``services.public_profiles`` into the same ``ctx.workouts_dict`` /
  ``ctx.sorted_workouts`` slots before rendering the dashboard. Pages call
  ``sync_workouts()`` / ``get_profile()`` which short-circuit to the
  pre-loaded values with no I/O.

``AppContext`` is a hyperdiv ``@hd.global_state`` BaseState. Each websocket
connection (i.e. each user session) has its own AppRunner, so different
viewers do not share state. Pages call ``AppContext()`` from anywhere to
read the active mode, user_id, client, etc. — no need to thread ``ctx``
through every signature.

The ``client`` and the workout snapshot props are stored as ``hd.Any``
because they are arbitrary Python objects that live purely server-side
(BaseState is ``collect=False``).
"""

import time
from typing import Optional

import hyperdiv as hd


@hd.global_state
class AppContext(hd.BaseState):
    mode = hd.Prop(hd.String, "owner")  # "owner" | "public"
    user_id = hd.Prop(hd.String, "")
    display_name = hd.Prop(hd.String, "")
    client = hd.Prop(hd.Any, None)  # Concept2Client | None
    # Unix timestamp at which ``client``'s access token expires.  Owned by
    # ``populate_owner`` so it can short-circuit per-render Concept2Client
    # construction when the cached token is still valid.
    token_expires_at = hd.Prop(hd.Float, 0.0)
    # Workout snapshot — populated once per session.
    # Owner mode: written by ``concept2_sync`` after the API sync completes.
    # Public mode: written by ``populate_public`` from disk-stored snapshots.
    workouts_dict = hd.Prop(hd.Any, None)
    sorted_workouts = hd.Prop(hd.Any, None)
    # Filter-UI metadata derived from ``workouts_dict`` at population time;
    # consumed by ``components.shared_ui.global_filter_ui``.
    all_seasons = hd.Prop(hd.List(hd.String), [])
    all_machines = hd.Prop(hd.List(hd.String), [])
    public_profile = hd.Prop(hd.Any, None)

    @property
    def is_owner(self) -> bool:
        return self.mode == "owner"

    @property
    def is_public(self) -> bool:
        return self.mode == "public"


def populate_owner(user_id: str, display_name: str = "") -> bool:
    """Populate AppContext for owner mode.  Returns True on success; False if
    no valid token exists for ``user_id`` (caller should clear localStorage
    and show the login view).

    Fast path: when the same user is already populated and the cached
    ``ctx.client``'s token has not yet expired, this is an O(1) timestamp
    compare — no disk read, no httpx Client construction.

    Cold path: loads (and refreshes if expired) the OAuth token from disk
    via ``get_valid_token`` and constructs a fresh ``Concept2Client``.  The
    new token's expiry is recorded on ``ctx.token_expires_at`` so subsequent
    renders take the fast path until the access token actually rotates.
    """
    from services.concept2 import Concept2Client, get_valid_token

    ctx = AppContext()
    uid = str(user_id)
    same_user = ctx.mode == "owner" and ctx.user_id == uid

    # Fast path: cached client whose token is still valid (60s safety margin
    # mirrors ``services.concept2.is_token_expired``).
    if same_user and ctx.client is not None and time.time() < ctx.token_expires_at - 60:
        return True

    token_data = get_valid_token(uid)
    if token_data is None:
        return False

    expires_at = float(token_data.get("saved_at", 0)) + float(
        token_data.get("expires_in", 0)
    )
    ctx.mode = "owner"
    ctx.user_id = uid
    if not same_user:
        ctx.display_name = display_name or ""
        ctx.workouts_dict = None
        ctx.sorted_workouts = None
        ctx.all_seasons = []
        ctx.all_machines = []
        ctx.public_profile = None
    ctx.client = Concept2Client(token_data["access_token"], user_id=uid)
    ctx.token_expires_at = expires_at
    return True


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
    from services.rowing_utils import derive_filter_metadata

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
    ctx.token_expires_at = 0.0
    ctx.workouts_dict = workouts_dict
    ctx.sorted_workouts = sorted_workouts
    ctx.all_seasons, ctx.all_machines = derive_filter_metadata(workouts_dict)
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
