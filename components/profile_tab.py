"""
Profile tab — user's personal data used for RowingLevel predictions and
heart-rate zone calculations.

Profile is stored in browser localStorage under the key "profile" as a JSON
string.  It is loaded once on first render (initial_loaded guard) to avoid
re-reading after writes, which would cause focus loss on text inputs.

All editable values are buffered in hd.state().  Changes are only persisted
to localStorage when the user clicks "Update" — radio-group fields (Gender,
Weight Unit) save immediately on change since they never hold keyboard focus.
"""

import json

import hyperdiv as hd

from services.rowinglevel import _PROFILE_DEFAULTS, age_from_dob


def profile_tab() -> None:
    # ── One-time load from localStorage ─────────────────────────────────────
    state = hd.state(
        loaded=False,
        # Buffered field values
        gender="",
        dob="",
        weight="",
        weight_unit="kg",
        weight_class="",
        max_heart_rate="",
        # Dirty flag — True when text fields have unsaved changes
        dirty=False,
    )

    if not state.loaded:
        ls_profile = hd.local_storage.get_item("profile")
        if not ls_profile.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return
        p = {**_PROFILE_DEFAULTS}
        if ls_profile.result:
            try:
                p = {**_PROFILE_DEFAULTS, **json.loads(ls_profile.result)}
            except Exception:
                pass
        state.gender = p.get("gender", "")
        state.dob = p.get("dob", "")
        state.weight = str(p["weight"]) if p.get("weight") else ""
        state.weight_unit = p.get("weight_unit", "kg")
        state.weight_class = p.get("weight_class", "")
        state.max_heart_rate = (
            str(p["max_heart_rate"]) if p.get("max_heart_rate") else ""
        )
        state.loaded = True

    # ── Save helper — writes buffered state to localStorage ──────────────────
    def _save():
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
        hd.local_storage.set_item(
            "profile",
            json.dumps(
                {
                    "gender": state.gender,
                    "dob": state.dob,
                    "weight": weight_val,
                    "weight_unit": state.weight_unit,
                    "weight_class": state.weight_class,
                    "max_heart_rate": mhr,
                }
            ),
        )
        state.dirty = False

    # ── Render form ──────────────────────────────────────────────────────────
    with hd.box(gap=1.5, padding=3):
        hd.text(
            "Used for RowingLevel predictions and heart-rate analysis.",
            font_color="neutral-500",
            font_size="small",
        )

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
