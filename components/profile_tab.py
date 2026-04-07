"""
Profile tab — user's personal data used for RowingLevel predictions and
future heart-rate zone calculations.

Saved to .profile.json on every change (auto-save, no explicit Save button).
"""

import hyperdiv as hd

from services.rowinglevel import load_profile, save_profile


def profile_tab() -> None:
    p = load_profile()

    state = hd.state(
        gender=p["gender"],  # "" = not yet set
        age=p["age"],  # 0  = not yet set
        weight=p["weight"],  # 0.0 = not yet set
        weight_unit=p["weight_unit"],
        max_heart_rate=p.get("max_heart_rate") or 0,
    )

    def _save():
        save_profile(
            {
                "gender": state.gender,
                "age": state.age,
                "weight": state.weight,
                "weight_unit": state.weight_unit,
                "max_heart_rate": state.max_heart_rate or None,
            }
        )

    with hd.box(gap=1, padding=3):
        # hd.text("Personal Profile", font_weight="bold", font_size="large")
        hd.text(
            "Used for RowingLevel predictions and heart-rate analysis.",
            font_color="neutral-500",
            font_size="small",
        )

        # Gender — no default; stays blank until the user picks one
        hd.text("Gender", font_weight="semibold", font_size="small")
        with hd.scope("gender"):
            with hd.radio_group(value=state.gender) as rg:
                hd.radio_button("Male")
                hd.radio_button("Female")
            if rg.changed:
                state.gender = rg.value
                _save()

        # Age — blank when 0 (not yet set)
        hd.text("Age", font_weight="semibold", font_size="small")
        with hd.scope("age"):
            ti = hd.text_input(
                value=str(state.age) if state.age else "",
                input_type="number",
                placeholder="e.g. 35",
                width=10,
            )
            if ti.changed:
                try:
                    state.age = max(1, min(120, int(ti.value)))
                    _save()
                except ValueError:
                    state.age = 0
                    _save()

        # Weight — blank when 0.0 (not yet set)
        hd.text("Bodyweight", font_weight="semibold", font_size="small")
        with hd.hbox(gap=2, align="center"):
            with hd.scope("weight"):
                ti = hd.text_input(
                    value=str(state.weight) if state.weight else "",
                    input_type="number",
                    placeholder="e.g. 75",
                    width=10,
                )
                if ti.changed:
                    try:
                        state.weight = max(0.1, float(ti.value))
                        _save()
                    except ValueError:
                        state.weight = 0.0
                        _save()
            with hd.scope("weight_unit"):
                with hd.radio_group(value=state.weight_unit) as rg:
                    hd.radio_button("kg")
                    hd.radio_button("lbs")
                if rg.changed:
                    state.weight_unit = rg.value
                    _save()

        # Max heart rate — optional, blank when 0 (not yet set)
        suggested_hr = max(100, 220 - state.age) if state.age else None
        hd.text("Max Heart Rate", font_weight="semibold", font_size="small")
        with hd.hbox(gap=2, align="center"):
            with hd.scope("max_hr"):
                ti = hd.text_input(
                    value=str(state.max_heart_rate) if state.max_heart_rate else "",
                    input_type="number",
                    placeholder=f"e.g. {suggested_hr}" if suggested_hr else "e.g. 185",
                    width=10,
                )
                if ti.changed:
                    try:
                        v = int(ti.value)
                        state.max_heart_rate = max(1, min(300, v))
                        _save()
                    except ValueError:
                        state.max_heart_rate = 0
                        _save()
