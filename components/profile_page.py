"""
Profile tab — user's personal data used for RowingLevel predictions and
heart-rate zone calculations.

Profile lives on ``AppContext.profile``. The combined ``app_session``
localStorage key (see ``components.app_context``) carries it across browser
workouts; ``write_profile_ls`` re-serializes the dict whenever a field is
edited here.

Editable values are buffered in ``hd.state()`` so re-renders during typing
do not steal focus. Changes are persisted (LS write + ``ctx.profile``
update) when the user clicks "Update"; radio-group fields (Gender, Weight
Unit, Weight Class) save immediately on change since they never hold
keyboard focus.

Edit tracking
-------------
Every save records which user-editable fields actually changed in
``profile["user_edited"]`` (a JSON-friendly list).  The stale-refresh path
in ``components.app_context.refresh_profile_if_stale`` consults that list
and refuses to overwrite any field the user has touched in this UI, so
that periodic Concept2 re-fetches never clobber an in-app override.

Display name
------------
``display_name`` and ``profile_image`` are read-only here — they're populated
from ``/users/me`` at OAuth (and on stale refresh) by
``services.concept2.extract_c2_profile``.  This page reads them directly
from the profile dict (no per-render API call).
"""

import hyperdiv as hd

from services.rowing_utils import age_from_dob
from services.glycogen import RESERVE_KJ_PER_KG, mass_kg_from_profile
from services.heartrate_utils import (
    estimate_max_hr_with_count,
    is_valid_hr,
    stroke_level_candidates,
    stroke_level_peak_hr,
)
from services import public_profiles
from components.app_context import (
    AppContext,
    get_profile,
    write_profile_ls,
    _PROFILE_DEFAULTS,
    _USER_EDITABLE_FIELDS,
)


# Threshold (bpm) for flagging a noticeable HRmax disagreement between the
# user's entered value and the Concept2-profile value.
_HRMAX_MISMATCH_BPM = 5


_MONTH_OPTIONS: tuple[tuple[int, str], ...] = (
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
)


def _diff_user_edits(prev: dict, new: dict) -> list[str]:
    """Return user-editable field names whose value differs between
    ``prev`` and ``new``."""
    diffs: list[str] = []
    for key in _USER_EDITABLE_FIELDS:
        if prev.get(key) != new.get(key):
            diffs.append(key)
    return diffs


def _persist_profile(new_user_inputs: dict) -> dict:
    """
    Merge buffered user inputs onto the existing profile, record any
    changed fields in ``user_edited``, persist to LS + AppContext, and
    return the merged profile.

    ``new_user_inputs`` carries only the values the form controls (the
    seven user-editable fields plus ``public``).  Display fields and
    refresh metadata are preserved from the existing profile.
    """
    ctx = AppContext()
    prev = {**_PROFILE_DEFAULTS, **(ctx.profile or {})}
    edited = list(prev.get("user_edited") or [])
    new_edits = _diff_user_edits(prev, new_user_inputs)
    for key in new_edits:
        if key not in edited:
            edited.append(key)

    merged = {
        **prev,
        **new_user_inputs,
        "user_edited": edited,
    }
    ctx.profile = merged
    write_profile_ls(ctx.user_id, merged)
    return merged


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
        season_start_month="5",
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
        # Stroke-level HRmax re-estimate.  ``hrmax_estimate_active`` toggles
        # the workflow on; the workflow drives a strokes_batch fetch and
        # surfaces the result in the *_estimate_* fields once it lands.  A
        # zero ``hrmax_estimate_result`` means "no result yet".
        hrmax_estimate_active=False,
        hrmax_estimate_result=0,
        hrmax_estimate_result_date="",
        hrmax_estimate_result_readings=0,
        hrmax_estimate_result_workouts=0,
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
        state.season_start_month = str(p.get("season_start_month") or 5)
        state.public = bool(p.get("public", False))
        state.loaded = True

    # ── Display name comes from the cached profile (no per-render fetch) ────
    profile = get_profile()
    display_name = (profile.get("display_name") or ctx.display_name or "").strip()

    # ── Build the user-input slice from buffered state ──────────────────────
    def _current_inputs() -> dict:
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
        try:
            ssm = int(state.season_start_month) if state.season_start_month else 5
            if not (1 <= ssm <= 12):
                ssm = 5
        except ValueError:
            ssm = 5
        return {
            "gender": state.gender,
            "dob": state.dob,
            "weight": weight_val,
            "weight_unit": state.weight_unit,
            "weight_class": state.weight_class,
            "max_heart_rate": mhr,
            "season_start_month": ssm,
            "public": bool(state.public),
        }

    # ── Save helper — diff, mark edits, persist to ctx.profile + LS ─────────
    def _save():
        _persist_profile(_current_inputs())
        state.dirty = False

    # ── Publish tasks (scoped by action key so rerenders don't refire) ───────
    publish_action = hd.state(
        action_key=""
    )  # "publish_all_<ts>" | "publish_profile_<ts>" | "unpublish_<ts>"

    if publish_action.action_key and ctx.user_id:
        pt = hd.task()
        if publish_action.action_key.startswith("publish_all_"):

            def _do_publish_all(uid, prof, dn, wkts):
                public_profiles.publish_all(
                    uid, prof, wkts, ctx.sessions_dict, display_name=dn
                )

            if not pt.running and not pt.done:
                pt.run(
                    _do_publish_all,
                    ctx.user_id,
                    get_profile(),
                    display_name or "Rower",
                    ctx.workouts_dict or {},
                )
        elif publish_action.action_key.startswith("publish_profile_"):

            def _do_publish_profile(uid, prof, dn):
                public_profiles.publish_profile(uid, prof, display_name=dn)

            if not pt.running and not pt.done:
                pt.run(
                    _do_publish_profile,
                    ctx.user_id,
                    get_profile(),
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

            with hd.radio_group(value=state.gender) as rg:
                hd.radio_button("Male")
                hd.radio_button("Female")
            if rg.changed:
                state.gender = rg.value
                _save()

        with hd.box():
            # Date of birth — text input (YYYY-MM-DD); buffered
            hd.text("Date of Birth", font_weight="semibold", font_size="small")
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
                weight_input = hd.text_input(
                    value=state.weight,
                    input_type="number",
                    placeholder="e.g. 75",
                    width=10,
                )
                if weight_input.changed:
                    state.weight = weight_input.value
                    state.dirty = True
                with hd.radio_group(value=state.weight_unit) as rg:
                    hd.radio_button("kg")
                    hd.radio_button("lbs")
                if rg.changed:
                    state.weight_unit = rg.value
                    _save()
            # Implied glycogen reserve — read-only, derived from bodyweight.
            # Drives the Glycogen Used metric on workouts.
            _mass_kg = mass_kg_from_profile(
                {
                    "weight": float(state.weight) if state.weight else 0.0,
                    "weight_unit": state.weight_unit,
                }
            )
            if _mass_kg:
                _reserve_kj = int(round(RESERVE_KJ_PER_KG * _mass_kg))
                hd.text(
                    f"Implied glycogen reserve ≈ {_reserve_kj:,} kJ "
                    f"({int(RESERVE_KJ_PER_KG)} kJ/kg × bodyweight). "
                    "Drives the Glycogen Used metric on the Workouts page.",
                    font_size="x-small",
                    font_color="neutral-500",
                )
        with hd.box():
            # Weight class — radio group; saves immediately
            hd.text("Weight Class", font_weight="semibold", font_size="small")
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
            mhr_input = hd.text_input(
                value=state.max_heart_rate,
                input_type="number",
                placeholder=f"e.g. {suggested_hr}" if suggested_hr else "e.g. 185",
                width=10,
            )
            if mhr_input.changed:
                state.max_heart_rate = mhr_input.value
                state.dirty = True

            _max_hr_provenance(state, profile, ctx)

        with hd.box():
            # Season start month — saves immediately (select; no keyboard focus).
            hd.text("Season starts", font_weight="semibold", font_size="small")
            season_sel = hd.select(
                value=str(state.season_start_month), size="small", max_width=15
            )
            with season_sel:
                for _m, _name in _MONTH_OPTIONS:
                    with hd.scope(f"season_month_{_m}"):
                        hd.option(_name, value=str(_m))
            if season_sel.changed:
                state.season_start_month = season_sel.value
                _save()
            hd.text(
                "Month your competitive season starts. Used to define "
                '"Season Best" framings on the Power Curve. Defaults to '
                "May (northern-hemisphere summer racing).",
                font_size="x-small",
                font_color="neutral-500",
                max_width=24,
            )

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


def _max_hr_provenance(state, profile: dict, ctx) -> None:
    """Render the HRmax provenance hint below the Max Heart Rate input.

    Three cases:

    1. User has typed a valid value → if a Concept2-profile HRmax exists and
       the two disagree by ≥``_HRMAX_MISMATCH_BPM``, surface a side-by-side
       "Review HRmax" prompt so the user can confirm which is correct.
    2. User has typed nothing → resolve to the estimator and show
       "Estimated from N workouts — for better accuracy, enter manually
       after a max test."  N is the count of workouts that contributed at
       least one valid HR reading.
    3. No estimate possible (too little HR data) → silent.
    """
    try:
        user_val = int(state.max_heart_rate) if state.max_heart_rate else None
        if user_val is not None and not (50 <= user_val <= 220):
            user_val = None
    except ValueError:
        user_val = None

    c2_val = profile.get("concept2_max_heart_rate")
    try:
        c2_val = int(c2_val) if c2_val else None
    except (TypeError, ValueError):
        c2_val = None
    if c2_val is not None and not is_valid_hr(c2_val):
        c2_val = None

    if user_val is not None:
        if c2_val is not None and abs(user_val - c2_val) >= _HRMAX_MISMATCH_BPM:
            with hd.box(
                gap=0.4,
                padding=(0.6, 0.8),
                border="1px solid warning-200",
                border_radius="medium",
                background_color="warning-50",
                margin_top=0.4,
                max_width=24,
            ):
                hd.text(
                    "Review HRmax",
                    font_size="small",
                    font_weight="semibold",
                    font_color="warning-800",
                )
                hd.text(
                    f"You entered {user_val} bpm. Your Concept2 profile "
                    f"says {c2_val} bpm. Take a look — one of the two is "
                    "probably stale.",
                    font_size="x-small",
                    font_color="warning-800",
                )
        return

    workouts = list((ctx.sorted_workouts or [])) if ctx else []
    if not workouts and ctx and ctx.workouts_dict:
        workouts = list(ctx.workouts_dict.values())
    if not workouts:
        return

    est_val, n_workouts = estimate_max_hr_with_count(workouts)

    if est_val is None:
        return

    hd.text(
        f"Estimated {est_val} bpm from {n_workouts} workouts — for better "
        "accuracy, enter manually after a max test (e.g. all-out 2k or "
        "6×500m, then read the highest value you saw).",
        font_size="x-small",
        font_color="neutral-500",
        max_width=24,
    )

    _stroke_level_reestimate_workflow(state, workouts)


def _stroke_level_reestimate_workflow(state, workouts: list) -> None:
    """Stroke-level HRmax re-estimate flow rendered under the Max HR input.

    Two-phase: a "Re-estimate from stroke data" button launches the flow,
    which uses :func:`stroke_level_candidates` to pick the top-5 long-effort
    PB-candidate workouts and ``components.concept2_sync.strokes_batch`` to
    fetch (or read from IndexedDB) the per-stroke HR series for each.  The
    survivor with the highest valid stroke-level HR drives the surfaced
    estimate, which the user can then promote into the Max HR input.

    Idempotent: re-fetched workouts come from the IndexedDB cache so a
    repeated click is essentially free.
    """
    if state.hrmax_estimate_result and not state.hrmax_estimate_active:
        _render_estimate_result(state)
        return

    if not state.hrmax_estimate_active:
        with hd.box(padding=(0.4, 0, 0, 0)):
            btn = hd.button(
                "Re-estimate HRmax from stroke data",
                size="small",
                variant="default",
            )
            if btn.clicked:
                state.hrmax_estimate_active = True
                state.hrmax_estimate_result = 0
                state.hrmax_estimate_result_date = ""
                state.hrmax_estimate_result_readings = 0
                state.hrmax_estimate_result_workouts = 0
        return

    # ── Active: drive a stroke-batch fetch over the top-N candidates ──
    candidates = stroke_level_candidates(workouts)
    if not candidates:
        with hd.box(
            gap=0.3,
            padding=(0.5, 0.8),
            border="1px solid neutral-200",
            border_radius="medium",
            background_color="neutral-50",
            margin_top=0.4,
            max_width=24,
        ):
            hd.text(
                "No stroke-level candidates yet — needs at least one workout "
                "longer than six minutes with stroke data and HR readings.",
                font_size="x-small",
                font_color="neutral-600",
            )
            if hd.button("Cancel", size="small", variant="text").clicked:
                state.hrmax_estimate_active = False
        return

    # Lazy import — strokes_batch lives in a HyperDiv-aware module.
    from components.concept2_sync import strokes_batch

    batch = strokes_batch(candidates)
    if batch.get("is_loading"):
        with hd.hbox(gap=0.5, align="center", padding_top=0.4):
            hd.spinner()
            total = batch.get("total") or len(candidates)
            done = batch.get("done") or 0
            hd.text(
                f"Fetching stroke data… ({done}/{total})",
                font_size="x-small",
                font_color="neutral-600",
            )
        return

    peak, meta = stroke_level_peak_hr(candidates, batch.get("by_id") or {})
    if peak is None:
        with hd.box(
            gap=0.3,
            padding=(0.5, 0.8),
            border="1px solid neutral-200",
            border_radius="medium",
            background_color="neutral-50",
            margin_top=0.4,
            max_width=24,
        ):
            hd.text(
                "Stroke data fetched but no valid HR readings surfaced. "
                "Wear an HR monitor on your next long effort to make this "
                "estimator work.",
                font_size="x-small",
                font_color="neutral-600",
            )
            if hd.button("OK", size="small", variant="text").clicked:
                state.hrmax_estimate_active = False
        return

    state.hrmax_estimate_result = int(peak)
    state.hrmax_estimate_result_date = meta.get("from_workout_date") or ""
    state.hrmax_estimate_result_readings = int(meta.get("n_readings") or 0)
    state.hrmax_estimate_result_workouts = int(meta.get("n_with_strokes") or 0)
    state.hrmax_estimate_active = False


def _render_estimate_result(state) -> None:
    """Show the stroke-level peak with Apply / Dismiss controls."""
    peak = int(state.hrmax_estimate_result)
    date = state.hrmax_estimate_result_date or "an unknown date"
    readings = int(state.hrmax_estimate_result_readings)
    workouts = int(state.hrmax_estimate_result_workouts)

    with hd.box(
        gap=0.4,
        padding=(0.6, 0.8),
        border="1px solid primary-200",
        border_radius="medium",
        background_color="primary-50",
        margin_top=0.4,
        max_width=24,
    ):
        hd.text(
            f"Stroke-level peak: {peak} bpm",
            font_size="small",
            font_weight="semibold",
            font_color="primary-800",
        )
        hd.text(
            f"From your effort on {date} — {readings:,} stroke readings "
            f"across {workouts} workouts.",
            font_size="x-small",
            font_color="primary-800",
        )
        with hd.hbox(gap=0.5, padding_top=0.3):
            if hd.button("Use this value", size="small", variant="primary").clicked:
                state.max_heart_rate = str(peak)
                state.dirty = True
                state.hrmax_estimate_result = 0
                state.hrmax_estimate_result_date = ""
                state.hrmax_estimate_result_readings = 0
                state.hrmax_estimate_result_workouts = 0
            if hd.button("Dismiss", size="small", variant="text").clicked:
                state.hrmax_estimate_result = 0
                state.hrmax_estimate_result_date = ""
                state.hrmax_estimate_result_readings = 0
                state.hrmax_estimate_result_workouts = 0


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
    profile LS key. Mirrors the inner ``_save`` closure in ``profile_page``
    but usable from helper scope."""
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
    try:
        ssm = int(state.season_start_month) if state.season_start_month else 5
        if not (1 <= ssm <= 12):
            ssm = 5
    except ValueError:
        ssm = 5
    _persist_profile(
        {
            "gender": state.gender,
            "dob": state.dob,
            "weight": weight_val,
            "weight_unit": state.weight_unit,
            "weight_class": state.weight_class,
            "max_heart_rate": mhr,
            "season_start_month": ssm,
            "public": bool(state.public),
        }
    )
    state.dirty = False
