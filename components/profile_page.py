"""
Profile tab — user's personal data used for RowingLevel predictions and
heart-rate zone calculations.

Profile lives on ``AppContext.profile``. The combined ``app_session``
localStorage key (see ``components.app_context``) carries it across browser
sessions; ``write_session_ls`` re-serializes the dict whenever a field is
edited here.

Editable values are buffered in ``hd.state()`` so re-renders during typing
do not steal focus. Changes are persisted (LS write + ``ctx.profile``
update) when the user clicks "Update"; radio-group fields (Gender, Weight
Unit, Weight Class) save immediately on change since they never hold
keyboard focus.
"""

import hyperdiv as hd

from services.rowing_utils import age_from_dob
from services import public_profiles
from components.app_context import AppContext, write_session_ls


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

_PROFILE_DEFAULTS: dict = {
    "gender": "",  # "" = not set; "Male" | "Female" when set
    "dob": "",  # "YYYY-MM-DD" date of birth; "" = not set
    "weight": 0.0,  # 0.0 = not set
    "weight_unit": "kg",  # "kg" | "lbs"
    "weight_class": "",  # "" = not set; "Heavyweight" | "Lightweight"
    "max_heart_rate": None,  # None = not set
    "public": False,  # True = profile + workouts published at /u/{user_id}
}


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
    return out


def get_profile() -> dict:
    """
    Return the active profile dict, with ``_PROFILE_DEFAULTS`` merged in so
    callers can rely on every key being present. Reads from
    ``AppContext.profile`` — populated at boot for both owner and public
    modes, so this is a pure synchronous read.
    """
    return {**_PROFILE_DEFAULTS, **(AppContext().profile or {})}


def profile_page() -> None:
    ctx = AppContext()
    # ── One-time buffer init from ctx.profile ───────────────────────────────
    state = hd.state(
        loaded=False,
        # Buffered field values
        gender="",
        dob="",
        weight="",
        weight_unit="kg",
        weight_class="",
        max_heart_rate="",
        public=False,
        # Dirty flag — True when text fields have unsaved changes
        dirty=False,
        # Confirmation dialog for make-private
        confirm_private_open=False,
        # Most recent publish task status (for transient UI feedback)
        publish_status="",  # "", "publishing", "published", "error", "need_sync"
        # action_key of the last publish task whose completion we processed,
        # so we don't re-roll-back state on subsequent renders.
        task_processed_key="",
    )

    if not state.loaded:
        p = get_profile()
        state.gender = p.get("gender", "")
        state.dob = p.get("dob", "")
        state.weight = str(p["weight"]) if p.get("weight") else ""
        state.weight_unit = p.get("weight_unit", "kg")
        state.weight_class = p.get("weight_class", "")
        state.max_heart_rate = (
            str(p["max_heart_rate"]) if p.get("max_heart_rate") else ""
        )
        state.public = bool(p.get("public", False))
        state.loaded = True

    # ── Display-name lookup (owner only) ─────────────────────────────────────
    # Concept2 first name is used as the published display name. Public-mode
    # profile_page is blocked upstream, so ctx.client is guaranteed non-None.
    display_name = ""
    if ctx.client is not None:
        user_task = hd.task()

        def _fetch_user():
            return ctx.client.get_user().get("data", {})

        user_task.run(_fetch_user)
        if user_task.done and user_task.result:
            u = user_task.result
            display_name = (u.get("first_name") or u.get("username") or "").strip()

    # ── Build the profile dict from buffered state ───────────────────────────
    def _current_profile() -> dict:
        try:
            weight_val = float(state.weight) if state.weight else 0.0
        except ValueError:
            weight_val = 0.0
        try:
            mhr = int(state.max_heart_rate) if state.max_heart_rate else None
            if mhr is not None and not (50 <= mhr <= 220):
                mhr = None
        except ValueError:
            mhr = None
        return {
            "gender": state.gender,
            "dob": state.dob,
            "weight": weight_val,
            "weight_unit": state.weight_unit,
            "weight_class": state.weight_class,
            "max_heart_rate": mhr,
            "public": state.public,
        }

    # ── Save helper — updates ctx.profile + the combined session LS key ──────
    def _save():
        new_profile = _current_profile()
        ctx.profile = new_profile
        write_session_ls(ctx.user_id, new_profile)
        state.dirty = False

    # ── Publish tasks (scoped by action key so rerenders don't refire) ───────
    publish_action = hd.state(
        action_key=""
    )  # "publish_all_<ts>" | "publish_profile_<ts>" | "unpublish_<ts>"

    if publish_action.action_key and ctx.user_id:
        with hd.scope(publish_action.action_key):
            pt = hd.task()
            if publish_action.action_key.startswith("publish_all_"):

                def _do_publish_all(uid, prof, dn, wkts):
                    public_profiles.publish_all(uid, prof, dn, wkts)

                if not pt.running and not pt.done:
                    pt.run(
                        _do_publish_all,
                        ctx.user_id,
                        _current_profile(),
                        display_name or "Rower",
                        ctx.workouts_dict or {},
                    )
            elif publish_action.action_key.startswith("publish_profile_"):

                def _do_publish_profile(uid, prof, dn):
                    public_profiles.publish_profile(uid, prof, dn)

                if not pt.running and not pt.done:
                    pt.run(
                        _do_publish_profile,
                        ctx.user_id,
                        _current_profile(),
                        display_name or "Rower",
                    )
            elif publish_action.action_key.startswith("unpublish_"):

                def _do_unpublish(uid):
                    public_profiles.unpublish(uid)

                if not pt.running and not pt.done:
                    pt.run(_do_unpublish, ctx.user_id)

            if pt.running:
                state.publish_status = "publishing"
            elif pt.done and state.task_processed_key != publish_action.action_key:
                state.task_processed_key = publish_action.action_key
                if pt.error:
                    state.publish_status = "error"
                    # Roll back optimistic state changes so the switch
                    # reflects reality (and ctx/LS are consistent).
                    if publish_action.action_key.startswith("publish_all_"):
                        if state.public:
                            state.public = False
                            _save_via_state(state)
                    print(f"[profile] publish task failed: {pt.error}")
                else:
                    state.publish_status = "published"

    # ── Render form ──────────────────────────────────────────────────────────
    with hd.box(gap=1.5, padding=3):
        with hd.box():
            # Gender — radio group; saves immediately (no keyboard focus to lose)
            hd.text("Gender", font_weight="semibold", font_size="small")
            with hd.scope("gender"):
                with hd.radio_group(value=state.gender) as rg:
                    hd.radio_button("Male")
                    hd.radio_button("Female")
                if rg.changed:
                    state.gender = rg.value
                    _save()

        with hd.box():
            # Date of birth — text input (YYYY-MM-DD); buffered
            hd.text("Date of Birth", font_weight="semibold", font_size="small")
            with hd.scope("dob"):
                computed_age = age_from_dob(state.dob)
                dob_input = hd.text_input(
                    value=state.dob,
                    placeholder="YYYY-MM-DD",
                    width=14,
                )
                if dob_input.changed:
                    state.dob = dob_input.value
                    state.dirty = True

        with hd.box():
            # Bodyweight — text input; buffered
            hd.text("Bodyweight", font_weight="semibold", font_size="small")
            with hd.hbox(gap=2, align="center"):
                with hd.scope("weight"):
                    weight_input = hd.text_input(
                        value=state.weight,
                        input_type="number",
                        placeholder="e.g. 75",
                        width=10,
                    )
                    if weight_input.changed:
                        state.weight = weight_input.value
                        state.dirty = True
                with hd.scope("weight_unit"):
                    with hd.radio_group(value=state.weight_unit) as rg:
                        hd.radio_button("kg")
                        hd.radio_button("lbs")
                    if rg.changed:
                        state.weight_unit = rg.value
                        _save()
        with hd.box():
            # Weight class — radio group; saves immediately
            hd.text("Weight Class", font_weight="semibold", font_size="small")
            with hd.scope("weight_class"):
                with hd.radio_group(value=state.weight_class) as rg:
                    hd.radio_button("Heavyweight")
                    hd.radio_button("Lightweight")
                if rg.changed:
                    state.weight_class = rg.value
                    _save()
        with hd.box():
            # Max heart rate — text input; buffered
            suggested_hr = max(100, 220 - computed_age) if computed_age else None
            hd.text("Max Heart Rate", font_weight="semibold", font_size="small")
            with hd.scope("max_hr"):
                mhr_input = hd.text_input(
                    value=state.max_heart_rate,
                    input_type="number",
                    placeholder=f"e.g. {suggested_hr}" if suggested_hr else "e.g. 185",
                    width=10,
                )
                if mhr_input.changed:
                    state.max_heart_rate = mhr_input.value
                    state.dirty = True

        # Update button — only visible when text fields have unsaved changes
        if state.dirty:
            with hd.box(padding=(1, 0, 0, 0)):
                if hd.button("Update", variant="primary", size="small").clicked:
                    _save()
                    # If public, push the updated profile server-side.
                    if state.public and ctx.user_id:
                        import time as _t

                        publish_action.action_key = f"publish_profile_{_t.time()}"

        # ── Public profile toggle ───────────────────────────────────────────
        hd.divider()

        _public_profile_section(state, publish_action, display_name)


def _public_profile_section(state, publish_action, display_name: str) -> None:
    ctx = AppContext()
    """Render the 'Public profile' controls: explainer, toggle, share URL,
    and the make-private confirmation dialog."""

    from services.concept2 import get_server_url

    with hd.box(gap=1):
        hd.text("Public profile", font_weight="semibold", font_size="medium")

        with hd.box(gap=0.5, max_width=20):
            with hd.hbox(gap=0.3, wrap="wrap"):
                hd.text(
                    "When private, none of your profile or workout data is stored on the Erg "
                    "Nerd server. Switching from public to private deletes everything we've stored.",
                    font_color="neutral-600",
                    font_size="small",
                )

            with hd.hbox(gap=0.3, wrap="wrap"):
                hd.text(
                    "When public, a copy of your profile (year of birth, gender, weight, weight class, "
                    "max HR) and your workout history are "
                    "stored on the Erg Nerd server so anyone with your profile link can view "
                    "them.",
                    font_color="neutral-600",
                    font_size="small",
                )

        # Toggle
        with hd.hbox(gap=1, align="center"):
            sw = hd.switch(checked=state.public)
            hd.text(
                "Make my profile public" if not state.public else "Profile is public",
                font_size="small",
                font_weight="semibold",
            )
            if state.publish_status == "publishing":
                hd.spinner()
            elif state.publish_status == "error":
                hd.text(
                    "Publish failed — check server logs.",
                    font_color="danger",
                    font_size="small",
                )
            elif state.publish_status == "need_sync":
                hd.text(
                    "Wait for your workout history to finish syncing, then try again.",
                    font_color="warning-600",
                    font_size="small",
                )

        # Confirmation dialog
        confirm_private_open = hd.dialog("Make profile private?")

        with confirm_private_open:
            with hd.box(gap=1, padding=1):
                hd.text(
                    "Switching to private deletes the profile and workout data we "
                    "have on file for you, and the public link will stop working "
                    "immediately. Nothing will be stored on the Erg Nerd server.",
                    font_color="neutral-700",
                )
                with hd.hbox(gap=1, justify="end", padding_top=1):
                    if hd.button("Cancel", size="small").clicked:
                        confirm_private_open.opened = False
                    if hd.button(
                        "Make private", variant="danger", size="small"
                    ).clicked:
                        import time as _t

                        state.public = False
                        _save_via_state(state)
                        publish_action.action_key = f"unpublish_{_t.time()}"
                        confirm_private_open.opened = False
                        sw.checked = False

        if sw.changed and ctx.user_id:
            import time as _t

            if sw.checked and not state.public:
                # OFF → ON: validate workouts are available first. Toggling
                # before the initial sync completes would publish an empty
                # dict, which publish_workouts rejects with ValueError.
                if not (ctx.workouts_dict or {}):
                    state.publish_status = "need_sync"
                else:
                    state.publish_status = "publishing"
                    state.public = True
                    _save_via_state(state)
                    publish_action.action_key = f"publish_all_{_t.time()}"
            elif not sw.checked and state.public:
                # ON → OFF: open confirmation dialog; keep flag until confirmed.
                sw.checked = True
                confirm_private_open.opened = True

        # Share URL when public
        if state.public and ctx.user_id:
            share_url = f"{get_server_url()}/u/{ctx.user_id}"
            with hd.hbox(gap=1, align="center", padding_top=0.5, wrap="wrap"):
                hd.text(
                    share_url,
                    font_size="small",
                    background_color="neutral-100",
                    padding=(0.25, 0.5),
                    border_radius="small",
                )
                hd.link(
                    "Open",
                    href=f"/u/{ctx.user_id}",
                    target="_blank",
                    font_size="small",
                )


def _save_via_state(state) -> None:
    """Persist the current buffered state to ``ctx.profile`` and the combined
    session LS key. Mirrors the inner ``_save`` closure in ``profile_page``
    but usable from helper scope."""
    ctx = AppContext()
    try:
        weight_val = float(state.weight) if state.weight else 0.0
    except ValueError:
        weight_val = 0.0
    try:
        mhr = int(state.max_heart_rate) if state.max_heart_rate else None
        if mhr is not None and not (50 <= mhr <= 220):
            mhr = None
    except ValueError:
        mhr = None
    new_profile = {
        "gender": state.gender,
        "dob": state.dob,
        "weight": weight_val,
        "weight_unit": state.weight_unit,
        "weight_class": state.weight_class,
        "max_heart_rate": mhr,
        "public": bool(state.public),
    }
    ctx.profile = new_profile
    write_session_ls(ctx.user_id, new_profile)
    state.dirty = False
