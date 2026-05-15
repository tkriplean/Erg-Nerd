"""
Render-time application context, kept in a per-connection ``@hd.global_state``.

The dashboard has two modes:

- **Owner mode** — the logged-in user browsing their own data. Boot-time
  ``_main_body`` reads a single combined localStorage key (see
  ``_SESSION_LS_KEY``) carrying both ``user_id`` and the user's profile dict,
  then calls ``populate_owner(user_id, profile)``. ``app.py:_dashboard_view``
  fires ``concept2_sync(ctx.client)`` once per session; the sync result is
  cached on ``ctx.workouts_dict`` / ``ctx.sorted_workouts``.

- **Public mode** — anyone (no login required) viewing someone else's
  opt-in public profile at ``/u/{user_id}``. ``app.py`` calls
  ``populate_public()`` which pre-loads workouts + profile from
  ``services.public_profiles`` into ``ctx.workouts_dict`` /
  ``ctx.sorted_workouts`` / ``ctx.profile``. There is no ``Concept2Client``;
  pages call ``sync_workouts()`` / ``get_profile()`` which short-circuit
  to the pre-loaded values with no I/O.

``AppContext`` is a hyperdiv ``@hd.global_state`` BaseState. Each websocket
connection (i.e. each user session) has its own AppRunner, so different
viewers do not share state. Pages call ``AppContext()`` from anywhere to
read the active mode, user_id, client, profile, etc. — no need to thread
``ctx`` through every signature.

The ``client``, ``profile``, and workout snapshot props are stored as
``hd.Any`` because they are arbitrary Python objects that live purely
server-side (BaseState is ``collect=False``).

Combined profile key: ``c2_user_id`` and ``profile`` used to be two
separate localStorage keys, each requiring its own async fetch on every
boot. They are now packed into a single JSON object under ``app_session``
so the boot flow makes one round-trip. The helpers ``write_profile_ls``
and ``clear_profile_ls`` keep the on-disk shape in sync with AppContext.

Profile flow
------------
``get_profile()`` is the single canonical accessor for profile data.
Components import it from this module (not from ``profile_page``) and
treat it as a synchronous read — no ``hd.task`` required mid-session.

The profile dict carries ``fetched_at`` (unix ts of last ``/users/me``)
and ``user_edited`` (list of field names the user has explicitly edited
in the Profile tab).  ``refresh_profile_if_stale`` consults these on each
render: when the cached profile is older than ``_PROFILE_REFRESH_TTL``
it spawns one keyed ``hd.task`` to re-fetch, then merges the result via
``merge_refreshed_profile`` — which protects ``user_edited`` fields from
being overwritten by Concept2.
"""

import json
import time
from typing import Optional

import hyperdiv as hd


_SESSION_LS_KEY = "app_session"

# Re-fetch ``/users/me`` and refresh non-edited profile fields after this
# many seconds (~30 days).  The user's profile is otherwise sourced from
# localStorage and never touched by the server.
_PROFILE_REFRESH_TTL = 30 * 24 * 3600

_PROFILE_DEFAULTS: dict = {
    "gender": "",  # "" = not set; "Male" | "Female" when set
    "dob": "",  # "YYYY-MM-DD" date of birth; "" = not set
    "weight": 0.0,  # 0.0 = not set
    "weight_unit": "kg",  # "kg" | "lbs"
    "weight_class": "",  # "" = not set; "Heavyweight" | "Lightweight"
    "max_heart_rate": None,  # None = not set
    # The Concept2 user-profile HRmax preserved from /users/me; mirrored at
    # OAuth time so the profile page can flag a mismatch when the user has
    # overridden ``max_heart_rate``.  Read-only in the app.
    "concept2_max_heart_rate": None,
    "season_start_month": 5,  # 1–12; Power Curve uses this for "Season Best" framing
    "public": False,  # True = profile + workouts published at /u/{user_id}
    "display_name": "",  # Concept2 first_name (read-only in app)
    "profile_image": "",  # Concept2 avatar URL (read-only in app)
    "fetched_at": 0.0,  # Unix ts of last /users/me fetch (0 = never)
    "user_edited": [],  # Field names the user has explicitly edited
}

# Fields the user can edit in the Profile tab.  Mirrored in
# ``components.profile_page``; kept here for ``merge_refreshed_profile``.
_USER_EDITABLE_FIELDS = (
    "gender",
    "dob",
    "weight",
    "weight_unit",
    "weight_class",
    "max_heart_rate",
    "season_start_month",
)


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
    # User profile dict — uniform shape across modes. Owner mode: stored
    # under the combined ``app_session`` localStorage key. Public mode:
    # adapted from the scrubbed server-side public profile via
    # ``_public_profile_to_local_shape``.
    profile = hd.Prop(hd.Any, None)
    # Workout snapshot — populated once per session.
    # Owner mode: written by ``concept2_sync`` after the API sync completes.
    # Public mode: written by ``populate_public`` from disk-stored snapshots.
    workouts_dict = hd.Prop(hd.Any, None)
    sorted_workouts = hd.Prop(hd.Any, None)
    # Session records — ``{session_id: session_record}``.  Owner mode:
    # written by ``concept2_sync`` from the IDB ``sessions`` store after
    # cluster assignment.  Public mode: written by ``populate_public`` from
    # the disk-stored ``sessions.zb64`` mirror, or rebuilt in memory from
    # the workouts dict when that file is absent (older publishes).
    sessions_dict = hd.Prop(hd.Any, None)
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


# ---------------------------------------------------------------------------
# Combined profile localStorage helpers
# ---------------------------------------------------------------------------


def write_profile_ls(user_id: str, profile: Optional[dict] = None) -> None:
    """Persist ``user_id`` + ``profile`` to the combined profile key.

    Call after any change to the user's profile so the next boot reads
    fresh values without an extra round-trip.
    """
    payload = {"user_id": str(user_id), "profile": profile or {}}
    hd.local_storage.set_item(_SESSION_LS_KEY, json.dumps(payload))


def clear_profile_ls() -> None:
    """Remove the combined profile key (used on Disconnect / token loss)."""
    hd.local_storage.remove_item(_SESSION_LS_KEY)


# ---------------------------------------------------------------------------
# Profile accessor + refresh helpers
# ---------------------------------------------------------------------------


def get_profile() -> dict:
    """
    Return the active profile dict, with ``_PROFILE_DEFAULTS`` merged in so
    callers can rely on every key being present. Reads from
    ``AppContext.profile`` — populated at boot for both owner and public
    modes, so this is a pure synchronous read.
    """
    return {**_PROFILE_DEFAULTS, **(AppContext().profile or {})}


def is_profile_stale(profile: Optional[dict]) -> bool:
    """True when the cached profile has no ``fetched_at`` or it has aged
    past ``_PROFILE_REFRESH_TTL``."""
    if not profile:
        return True
    fetched_at = float(profile.get("fetched_at") or 0)
    if fetched_at <= 0:
        return True
    return (time.time() - fetched_at) > _PROFILE_REFRESH_TTL


def merge_refreshed_profile(local: dict, fresh: dict) -> dict:
    """
    Merge a freshly-fetched Concept2 profile into the locally-cached one.

    Fields listed in ``local["user_edited"]`` are protected — the local
    value wins.  Everything else is overwritten by the C2 value, including
    the always-fresh display fields (``display_name``, ``profile_image``)
    and refresh metadata (``fetched_at``).

    ``user_edited`` and ``public`` are state owned by the app, never by
    Concept2; they are preserved from ``local`` unconditionally.
    """
    edited = set(local.get("user_edited") or [])
    merged = {**_PROFILE_DEFAULTS, **(local or {})}
    for key, fresh_val in (fresh or {}).items():
        if key in ("user_edited", "public"):
            continue
        if key in _USER_EDITABLE_FIELDS and key in edited:
            continue
        merged[key] = fresh_val
    merged["user_edited"] = sorted(edited)
    merged["public"] = bool(local.get("public", False))
    return merged


def refresh_profile_if_stale() -> None:
    """
    Owner-mode boot helper.  When the cached profile is stale, spawn one
    keyed ``hd.task`` per session to fetch ``/users/me``, merge the result
    via ``merge_refreshed_profile``, persist to localStorage, and update
    AppContext.

    No-op in public mode and when the profile is fresh.  Renders nothing.
    """
    ctx = AppContext()
    if ctx.mode != "owner" or ctx.client is None or not ctx.user_id:
        return
    profile = ctx.profile or {}
    if not is_profile_stale(profile):
        return

    from services.concept2 import extract_c2_profile

    task = hd.task()
    if not task.running and not task.done:
        task.run(lambda: ctx.client.get_user().get("data", {}))
    if task.done and not task.error and task.result:
        fresh = extract_c2_profile(task.result)
        merged = merge_refreshed_profile(profile, fresh)
        # Avoid a write loop: only persist when something actually changed.
        if merged != profile:
            ctx.profile = merged
            ctx.display_name = merged.get("display_name") or ctx.display_name
            write_profile_ls(ctx.user_id, merged)


# ---------------------------------------------------------------------------
# Public-profile shape adapter
# ---------------------------------------------------------------------------


def _public_profile_to_local_shape(pub: dict) -> dict:
    """
    Adapt a scrubbed server-side public profile dict into the localStorage
    profile shape that the rest of the app consumes.

    The public profile stores ``yob`` (year-of-birth) rather than raw DOB —
    same approximate PII as age, but stable as years pass.  Downstream code
    asks for ``dob`` to derive age, so synthesize a mid-year ``dob``
    (``{yob}-07-01``) that yields the correct age via ``age_from_dob``.

    Schema-v1 profiles (pre-yob) fall back to the snapshot ``age`` field.
    """
    from datetime import date

    out = {**_PROFILE_DEFAULTS}
    yob = pub.get("yob")
    if isinstance(yob, int) and 1900 <= yob <= date.today().year:
        out["dob"] = f"{yob}-07-01"
    else:
        age = pub.get("age")
        if isinstance(age, int) and age > 0:
            out["dob"] = f"{date.today().year - age}-07-01"
    out["gender"] = pub.get("gender", "") or ""
    out["weight"] = pub.get("weight") or 0.0
    out["weight_unit"] = pub.get("weight_unit", "kg") or "kg"
    out["weight_class"] = pub.get("weight_class", "") or ""
    out["max_heart_rate"] = pub.get("max_heart_rate")
    out["display_name"] = pub.get("display_name", "") or ""
    return out


# ---------------------------------------------------------------------------
# Population helpers
# ---------------------------------------------------------------------------


def populate_owner(user_id: str, profile: Optional[dict] = None) -> bool:
    """Populate AppContext for owner mode.  Returns True on success; False if
    no valid token exists for ``user_id`` (caller should clear the session
    key and show the login view).

    Fast path: when the same user is already populated and the cached
    ``ctx.client``'s token has not yet expired, this is an O(1) timestamp
    compare — no disk read, no httpx Client construction. ``profile`` is
    refreshed in place so callers can re-call after editing the profile.

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
        if profile is not None:
            ctx.profile = profile
            ctx.display_name = (profile.get("display_name") or "").strip()
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
        ctx.workouts_dict = None
        ctx.sorted_workouts = None
        ctx.sessions_dict = None
        ctx.all_seasons = []
        ctx.all_machines = []
        ctx.public_profile = None
    ctx.profile = profile or {}
    ctx.display_name = ((profile or {}).get("display_name") or "").strip()
    ctx.client = Concept2Client(token_data["access_token"], user_id=uid)
    ctx.token_expires_at = expires_at
    return True


def populate_public(user_id: str) -> bool:
    """
    Load server-side public data for ``user_id`` and write it into AppContext.
    Returns True on success, False when no published data exists (caller
    should render the 404 view).

    Idempotent: skips writes when (mode, user_id) already match.

    Sessions: when ``sessions.zb64`` is present we load the mirrored cluster
    records directly.  When it's missing (publishes predating the sessions
    feature) we rebuild from the workouts dict in memory so consumers that
    rely on ``ctx.sessions_dict`` keep working — there's no write-back; the
    next owner-side sync will produce the file.
    """
    from services.public_profiles import (
        exists as pp_exists,
        load_public_profile,
        load_public_sessions,
        load_public_workouts,
    )
    from services.rowing_utils import derive_filter_metadata
    from services.sessions import build_sessions_from_scratch
    from services.workout_enrichment import enrich_all

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
    sessions_dict = load_public_sessions(user_id)
    if not sessions_dict:
        # Fallback: rebuild in memory.  We also stamp session_id onto each
        # workout so add_metrics can resolve sessions via the same lookup
        # path used in owner mode.
        sessions_dict, mutations = build_sessions_from_scratch(workouts_dict)
        for wid, sid in mutations.items():
            if wid in workouts_dict:
                workouts_dict[wid]["session_id"] = sid
    # Stage-2 enrichment matches the owner-mode pipeline so consumers can
    # rely on pace/watts/season/etc. being present regardless of mode.
    enrich_all(workouts_dict)
    sorted_workouts = sorted(
        workouts_dict.values(), key=lambda w: w["date"], reverse=True
    )
    ctx.mode = "public"
    ctx.user_id = uid
    ctx.display_name = profile.get("display_name") or "Rower"
    ctx.client = None
    ctx.token_expires_at = 0.0
    ctx.workouts_dict = workouts_dict
    ctx.sorted_workouts = sorted_workouts
    ctx.sessions_dict = sessions_dict
    ctx.all_seasons, ctx.all_machines = derive_filter_metadata(workouts_dict)
    ctx.public_profile = profile
    ctx.profile = _public_profile_to_local_shape(profile)
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
