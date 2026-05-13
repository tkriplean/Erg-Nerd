"""
Workout filter bar — shared by the Workouts and Intervals pages.

A single segmented bar of four multi-select dropdowns sits above the
workout table, with a funnel icon at the right end:

    [ Severity ▼ │ Power Zones Engaged ▼ │ Trained System ▼ │ HR Zone ▼ ] ⏷

The buttons are joined edge-to-edge (no border radius between them, no
gap).  When any chip in a dropdown is selected, that button switches to
``primary`` variant and shows the active count, e.g.
``Power Zones Engaged (2) ▼``.

Each dropdown opens a *selector* panel:

    ┌────────────────────────────────────────┐
    │ Filter by overall workout severity —   │
    │ peak intensity + W' debt + glycogen.   │   (header description)
    │ ────────────────────────────────────── │
    │ ✓ ⬛ High      Threshold-grade peak…   │
    │   ⬛ Maximal   Race-pace or PB…        │   (option rows)
    │                                        │
    └────────────────────────────────────────┘

Each option is a row with: a checkmark slot (visible when active), a
coloured swatch, the option name, and a brief inline description.
Selected rows get a subtle background fill plus the check icon.  Rows
are click-targets — clicking toggles inclusion.

Below the bar, an *active-chip row* lists each selection as a removable
× chip prefixed with its dimension — e.g. ``Engaged · Sprint ×``,
``Trained · VO2max ×``.  Clicking a chip removes just that filter; a
``Clear all`` button appears when ≥2 chips are active.

**Power Zones Engaged** filters workouts with ≥10% work-time in a band's
EMA.  **Trained System** filters by the stricter dose threshold
(≥0.50× Partial+).  Both are independent filters; Trained is usually a
subset of Engaged but they compose freely.  Trained rows use a circular
dose-pip swatch (`radius=5`) in place of the square swatch.

The HR Zone dropdown is replaced by an inert disabled button when no
max HR is resolvable.

Filtering is **disjunctive within** a dimension (multi-select = OR) and
**conjunctively across** dimensions (AND).

Active state lives on a shared ``SpreadSeverityFilters`` instance:
    state.active_bins              tuple[int]    Engaged bin indices (1–6)
    state.active_hr_bins           tuple[int]    HR zone indices (1–5)
    state.active_severity          tuple[str]    Severity buckets
    state.active_stimulus_bands    tuple[int]    Trained band-seconds
"""

from __future__ import annotations

import hyperdiv as hd

from services.heartrate_utils import (
    HR_ZONE_COLORS,
    HR_ZONE_DEFINITION_TEXT,
    HR_ZONE_FILTER_TEXT,
    HR_ZONE_NAMES,
)
from services.erg_stress import (
    SEVERITY_DEFINITION_TEXT,
    SEVERITY_FILTER_TEXT,
    SEVERITY_ORDER,
    SEVERITY_STYLE,
    SEVERITY_THRESHOLDS,
    STIMULUS_BAND_DEFINITIONS,
    STIMULUS_FILTER_TEXT,
    ZONE_BANDS_S,
)
from services.volume_bins import (
    BAND_TO_BIN,
    BIN_COLORS,
    BIN_NAMES,
    POWER_ZONE_DEFINITION_TEXT,
    POWER_ZONE_FILTER_TEXT,
    swatch_svg,
)
from components.workout_table import always_white


# ───────────────────────────────────────────────────────────────────────────
# Inline copy for the selector dropdowns.
# ───────────────────────────────────────────────────────────────────────────

# Top-of-dropdown header — one sentence describing what each filter does.
_HEADER_DESC = {
    "severity": (
        "Filter by overall workout severity — peak rolling intensity, "
        "W' depletion, and glycogen drain combined."
    ),
    "engaged": (
        "Filter to workouts that engaged these power systems "
        "(≥10% of work time in the band's EMA)."
    ),
    "trained": (
        "Filter to workouts that delivered at least partial training "
        "stimulus to these systems (dose ≥ 0.50×)."
    ),
    "hr": ("Filter to workouts with meaningful time in these heart-rate " "zones."),
}

# Brief per-option descriptions shown inline next to the swatch + name.
_SEVERITY_BRIEF = {
    "Low": "Below tempo; reservoirs largely intact",
    "Moderate": "Meaningful intensity but sub-threshold",
    "High": "Threshold-grade peak, W' debt, or glycogen drain",
    "Maximal": "Race-pace or PB territory; high recovery demand",
}

_POWER_BRIEF = {
    1: "Very short maximal efforts (~20s)",
    2: "1–2 min supra-threshold efforts (~90s)",
    3: "3–8 min VO₂max efforts (~5min)",
    4: "10–30 min sustained efforts (~20min)",
    5: "40+ min sub-threshold work (~60min)",
    6: "60+ min low-intensity base (~2h)",
}

_TRAINED_BRIEF = {
    20: "Neuromuscular / PCr power",
    90: "Anaerobic capacity (fast glycolysis)",
    300: "Maximal aerobic power (VO₂max)",
    1200: "Lactate threshold / MLSS",
    3600: "Sustained sub-threshold tempo",
    7200: "Aerobic base / long Z2 volume",
}


# ───────────────────────────────────────────────────────────────────────────
# Shared helpers.
# ───────────────────────────────────────────────────────────────────────────


def _parse_rgba(rgba_str: str) -> tuple:
    """Parse 'rgba(r,g,b,a)' → (r, g, b, a) tuple."""
    try:
        inner = rgba_str.strip()[5:-1]
        parts = [p.strip() for p in inner.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]))
    except Exception:
        return (128, 128, 128, 0.8)


def _chip_tooltip_content(tt, heading: str, definition: str, filter_rule: str) -> None:
    with hd.box(slot=tt.content_slot, padding=0.3, gap=0.25, max_width=40):
        hd.text(heading, font_size="medium", font_weight="bold")
        hd.text(definition, font_size="small")
        hd.text(filter_rule, font_size="small", font_style="italic")


def legend_chip(
    *,
    name: str,
    color_str: str,
    is_active: bool,
    definition: str,
    filter_rule: str,
    swatch_radius: int = 2,
) -> bool:
    """Render one single-state toggle chip with hover tooltip.

    Used by the prior-training severity row on the workout detail page.
    The new filter-bar dropdowns use ``_selector_row`` instead (inline
    description, no tooltip).
    """
    color_rgba = _parse_rgba(color_str)
    with hd.tooltip() as tt:
        _chip_tooltip_content(tt, name, definition, filter_rule)
        if is_active:
            with hd.button(
                size="small",
                padding=(0.2, 0.6, 0.2, 0.6),
                border="none",
                base_style=hd.style(background_color=color_rgba),
            ) as btn:
                with hd.hbox(gap=0.4, align="center", justify="center"):
                    hd.image(
                        src=swatch_svg(color_str, size=10, radius=swatch_radius),
                        width=0.65,
                        height=0.65,
                    )
                    hd.text(
                        name,
                        font_size="small",
                        font_color=always_white(hd.theme().is_dark),
                    )
        else:
            with hd.button(
                variant="neutral",
                size="small",
                border="none",
                background_color="neutral-50",
                padding=(0.2, 0.6, 0.2, 0.6),
            ) as btn:
                with hd.hbox(gap=0.4, align="center", justify="center"):
                    hd.image(
                        src=swatch_svg(color_str, size=10, radius=swatch_radius),
                        width=0.65,
                        height=0.65,
                    )
                    hd.text(name, font_size="small", font_color="neutral-600")
    return btn.clicked


@hd.global_state
class SpreadSeverityFilters(hd.BaseState):
    active_bins = hd.Prop(hd.List(hd.Any), [])
    active_hr_bins = hd.Prop(hd.List(hd.Any), [])
    active_severity = hd.Prop(hd.List(hd.Any), [])
    # Training Stimulus filter — list of band-seconds (20, 90, 300, 1200,
    # 3600, 7200).  Driven by the Trained dropdown.
    active_stimulus_bands = hd.Prop(hd.List(hd.Any), [])


def _toggle_in_set(current, key, sort_key=None) -> tuple:
    """Add/remove ``key`` from ``current``; return a new sorted tuple."""
    sel = set(current)
    if key in sel:
        sel.discard(key)
    else:
        sel.add(key)
    if sort_key is None:
        return tuple(sorted(sel))
    return tuple(sorted(sel, key=sort_key))


# ───────────────────────────────────────────────────────────────────────────
# Selector-row + dropdown shell.
# ───────────────────────────────────────────────────────────────────────────


def _selector_row(
    *,
    name: str,
    description: str,
    color_str: str,
    is_active: bool,
    swatch_radius: int,
) -> bool:
    """One selector option — checkmark slot + swatch + name + brief description.

    Returns True if the row was clicked.  Rendered as a full-width button so
    the entire row is a click target; ``label_style`` stretches the label
    slot to 100% so the inner hbox can left-align.
    """
    bg_color = "primary-100" if is_active else "neutral-0"
    with hd.button(
        width="100%",
        variant="text",
        border_radius=0,
        base_style=hd.style(background_color=bg_color),
        label_style=hd.style(
            width="100%",
            # display="flex",
            # justify="flex-start",
            padding=(0.4, 0.6),
        ),
    ) as row_btn:
        with hd.hbox(gap=0.6, align="center", width="100%", justify="start"):
            # Check-mark slot — reserves space whether active or not so
            # rows stay column-aligned.
            with hd.box(width=1.2, align="center", justify="center"):
                if is_active:
                    hd.icon(
                        "check-lg",
                        font_size="medium",
                        font_color="primary-600",
                    )
            hd.image(
                src=swatch_svg(color_str, size=12, radius=swatch_radius),
                width=0.95,
                height=0.95,
            )
            with hd.box(gap=0.05, align="start"):
                hd.text(
                    name,
                    font_size="small",
                    font_weight="semibold",
                    font_color="neutral-800",
                )
                hd.text(
                    description,
                    font_size="x-small",
                    font_color="neutral-500",
                )
    return row_btn.clicked


def _filter_dropdown(
    *,
    label: str,
    header_desc: str,
    items: list,
    active_keys: set,
    on_toggle,
    swatch_radius: int = 2,
    key_prefix: str = "filter",
) -> None:
    """One filter dropdown — trigger button + selector panel.

    ``items`` is a list of (key, name, color_str, description) tuples.
    """
    count = len(active_keys)
    label_text = f"{label} ({count})" if count > 0 else label

    with hd.dropdown() as dd:
        if count > 0:
            btn = hd.button(
                label_text,
                caret=True,
                size="small",
                variant="primary",
                slot=dd.trigger,
            )
        else:
            btn = hd.button(
                label_text,
                caret=True,
                size="small",
                slot=dd.trigger,
            )
        if btn.clicked:
            dd.opened = not dd.opened

        # Selector panel
        with hd.box(
            background_color="neutral-0",
            min_width=22,
            max_width=32,
            gap=0,
            padding=0,
        ):
            with hd.box(padding=(0.6, 0.75, 0.5, 0.75)):
                hd.text(
                    header_desc,
                    font_size="x-small",
                    font_color="neutral-500",
                    font_style="italic",
                )
            hd.divider(spacing=0)

            for key, name, color_str, description in items:
                with hd.scope(f"{key_prefix}_{key}"):
                    clicked = _selector_row(
                        name=name,
                        description=description,
                        color_str=color_str,
                        is_active=key in active_keys,
                        swatch_radius=swatch_radius,
                    )
                    if clicked:
                        on_toggle(key)


# ───────────────────────────────────────────────────────────────────────────
# Per-dimension item lists.
# ───────────────────────────────────────────────────────────────────────────


def _severity_items() -> list:
    items = []
    for label, _upper in SEVERITY_THRESHOLDS:
        c = SEVERITY_STYLE[label]["bg"]
        color_str = f"rgba({c[0]},{c[1]},{c[2]},{c[3]})"
        items.append((label, label, color_str, _SEVERITY_BRIEF.get(label, "")))
    return items


def _engaged_items(is_dark: bool) -> list:
    items = []
    for band_s in ZONE_BANDS_S:
        bin_idx = BAND_TO_BIN[band_s]
        name = BIN_NAMES[bin_idx]
        color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
        items.append((bin_idx, name, color_str, _POWER_BRIEF.get(bin_idx, "")))
    return items


def _trained_items(is_dark: bool) -> list:
    items = []
    for band_s in ZONE_BANDS_S:
        bin_idx = BAND_TO_BIN[band_s]
        name = BIN_NAMES[bin_idx]
        color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
        items.append((band_s, name, color_str, _TRAINED_BRIEF.get(band_s, "")))
    return items


def _hr_items(is_dark: bool) -> list:
    items = []
    for i in range(1, 6):
        name = HR_ZONE_NAMES[i]
        color_str = HR_ZONE_COLORS[i][0 if is_dark else 1]
        # HR definitions are already brief ("Above 90% of your max HR.").
        desc = HR_ZONE_DEFINITION_TEXT.get(i, "").rstrip(".")
        items.append((i, name, color_str, desc))
    return items


# ───────────────────────────────────────────────────────────────────────────
# Active-chip row — removable × chip per active filter.
# ───────────────────────────────────────────────────────────────────────────


def _active_chip(
    *,
    label_text: str,
    color_str: str,
    on_clear,
    key: str,
    swatch_radius: int = 2,
) -> None:
    """A single active-filter chip with ×; click clears just this selection."""
    with hd.scope(f"act_{key}"):
        is_dark = hd.theme().is_dark
        color_rgba = _parse_rgba(color_str)
        with hd.button(
            size="small",
            padding=(0.2, 0.5, 0.2, 0.6),
            border="none",
            base_style=hd.style(background_color=color_rgba),
        ) as btn:
            with hd.hbox(gap=0.4, align="center", justify="center"):
                hd.image(
                    src=swatch_svg(color_str, size=10, radius=swatch_radius),
                    width=0.65,
                    height=0.65,
                )
                hd.text(
                    label_text,
                    font_size="small",
                    font_color=always_white(is_dark),
                )
                hd.text(
                    "×",
                    font_size="medium",
                    font_color=always_white(is_dark),
                    font_weight="bold",
                )
        if btn.clicked:
            on_clear()


def _active_filter_chips(state: SpreadSeverityFilters, is_dark: bool) -> None:
    """Render the active-filter chip row — only when ≥1 filter is active."""
    active_sev = list(state.active_severity)
    active_eng = list(state.active_bins)
    active_trn = list(state.active_stimulus_bands)
    active_hr = list(state.active_hr_bins)

    total = len(active_sev) + len(active_eng) + len(active_trn) + len(active_hr)
    if total == 0:
        return

    with hd.hbox(
        gap=0.5,
        align="center",
        wrap="wrap",
        padding=(0.5, 0, 0, 0),
    ):
        for label in active_sev:
            c = SEVERITY_STYLE[label]["bg"]
            color_str = f"rgba({c[0]},{c[1]},{c[2]},{c[3]})"

            def _on_clear(_label=label):
                state.active_severity = _toggle_in_set(
                    state.active_severity,
                    _label,
                    sort_key=lambda q: SEVERITY_ORDER[q],
                )

            _active_chip(
                label_text=f"Severity · {label}",
                color_str=color_str,
                on_clear=_on_clear,
                key=f"sev_{label}",
            )

        for bin_idx in active_eng:
            name = BIN_NAMES[bin_idx]
            color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]

            def _on_clear(_bi=bin_idx):
                state.active_bins = _toggle_in_set(state.active_bins, _bi)

            _active_chip(
                label_text=f"Engaged · {name}",
                color_str=color_str,
                on_clear=_on_clear,
                key=f"eng_{bin_idx}",
                swatch_radius=2,
            )

        for band_s in active_trn:
            bin_idx = BAND_TO_BIN[band_s]
            name = BIN_NAMES[bin_idx]
            color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]

            def _on_clear(_bs=band_s):
                state.active_stimulus_bands = _toggle_in_set(
                    state.active_stimulus_bands, _bs
                )

            _active_chip(
                label_text=f"Trained · {name}",
                color_str=color_str,
                on_clear=_on_clear,
                key=f"trn_{band_s}",
                swatch_radius=5,
            )

        for zi in active_hr:
            name = HR_ZONE_NAMES[zi]
            color_str = HR_ZONE_COLORS[zi][0 if is_dark else 1]

            def _on_clear(_zi=zi):
                state.active_hr_bins = _toggle_in_set(state.active_hr_bins, _zi)

            _active_chip(
                label_text=f"HR · {name}",
                color_str=color_str,
                on_clear=_on_clear,
                key=f"hr_{zi}",
            )

        if total >= 2:
            if hd.button(
                "Clear all",
                size="small",
            ).clicked:
                state.active_severity = ()
                state.active_bins = ()
                state.active_stimulus_bands = ()
                state.active_hr_bins = ()


# ───────────────────────────────────────────────────────────────────────────
# HR placeholder — inert button when no max HR is resolvable.
# ───────────────────────────────────────────────────────────────────────────


def _hr_placeholder() -> None:
    with hd.tooltip() as tt:
        with hd.box(slot=tt.content_slot, padding=0.3, gap=0.2, max_width=30):
            hd.text("HR Zone filter", font_size="small", font_weight="bold")
            hd.text(
                "Set max HR in Profile to filter by HR zone.",
                font_size="x-small",
                font_color="neutral-500",
            )
        hd.button(
            "HR Zone",
            caret=True,
            size="small",
            disabled=True,
        )


# ───────────────────────────────────────────────────────────────────────────
# Top-level renderer — Workouts & Intervals pages call this.
# ───────────────────────────────────────────────────────────────────────────


@hd.cached
def workout_filter_bar(max_hr: int | None) -> None:
    """Render the segmented filter bar above the workout table.

    Single bar of four multi-select dropdowns + funnel icon on the right.
    Active-filter chip row below, shown only when ≥1 filter is active.
    """
    is_dark = hd.theme().is_dark
    state = SpreadSeverityFilters()

    with hd.box(gap=0.25, padding=(0.5, 0, 0.25, 0)):
        with hd.hbox(align="center", gap=0.5):
            with hd.button_group():
                # ── Severity ─────────────────────────────────────────────
                def _sev_toggle(key):
                    state.active_severity = _toggle_in_set(
                        state.active_severity,
                        key,
                        sort_key=lambda q: SEVERITY_ORDER[q],
                    )

                _filter_dropdown(
                    label="Severity",
                    header_desc=_HEADER_DESC["severity"],
                    items=_severity_items(),
                    active_keys=set(state.active_severity),
                    on_toggle=_sev_toggle,
                    swatch_radius=2,
                    key_prefix="sev",
                )

                # ── Power Zones Engaged ──────────────────────────────────
                def _eng_toggle(key):
                    state.active_bins = _toggle_in_set(state.active_bins, key)

                _filter_dropdown(
                    label="Power Zones Engaged",
                    header_desc=_HEADER_DESC["engaged"],
                    items=_engaged_items(is_dark),
                    active_keys=set(state.active_bins),
                    on_toggle=_eng_toggle,
                    swatch_radius=2,
                    key_prefix="eng",
                )

                # ── Trained System ───────────────────────────────────────
                def _trn_toggle(key):
                    state.active_stimulus_bands = _toggle_in_set(
                        state.active_stimulus_bands, key
                    )

                _filter_dropdown(
                    label="Trained System",
                    header_desc=_HEADER_DESC["trained"],
                    items=_trained_items(is_dark),
                    active_keys=set(state.active_stimulus_bands),
                    on_toggle=_trn_toggle,
                    swatch_radius=5,
                    key_prefix="trn",
                )

                # ── HR Zone ──────────────────────────────────────────────
                if max_hr:

                    def _hr_toggle(key):
                        state.active_hr_bins = _toggle_in_set(state.active_hr_bins, key)

                    _filter_dropdown(
                        label="HR Zone",
                        header_desc=_HEADER_DESC["hr"],
                        items=_hr_items(is_dark),
                        active_keys=set(state.active_hr_bins),
                        on_toggle=_hr_toggle,
                        swatch_radius=2,
                        key_prefix="hr",
                    )
                else:
                    _hr_placeholder()

            # Filter icon on the right
            hd.icon("funnel", font_size="medium", font_color="neutral-400")

        # Active-filter chip row
        _active_filter_chips(state, is_dark)
