"""
Shared filter-legend component for Power Spread, HR Spread, and Severity.

Both the Intervals and Workouts pages render this same stack of three
labelled legends.  Each legend is a row of clickable chips; clicking a chip
toggles its membership in the page's active-set.  Filtering is **disjunctive
within** a legend (selecting two chips matches workouts in EITHER zone) and
**conjunctive across** legends.

Active state lives on the caller's ``state`` object as:
    state.active_bins        tuple[int]    pace bin indices (1–6)
    state.active_hr_bins     tuple[int]    HR bin indices (1–5)
    state.active_severity    tuple[str]    severity buckets

The Power Spread header carries a (?) icon whose tooltip renders a graphical
scale of each zone's reference event and the user's current watts at that
event (dated to the most recent workout).
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


# Zone Spread duration anchors for the scale tooltip — one per bin index 1–6.
# Each label is the EMA time-constant of the corresponding band; the chip
# colour matches the existing six-zone palette in BIN_COLORS.
_POWER_SCALE_ANCHORS: list[tuple[str | None, tuple | None]] = [
    ("20s", None),  # Sprint
    ("1:30", None),  # Anaerobic
    ("5:00", None),  # VO2max
    ("20:00", None),  # Threshold
    ("60:00", None),  # Tempo
    ("2h", None),  # Endurance
]


def _parse_rgba(rgba_str: str) -> tuple:
    """Parse 'rgba(r,g,b,a)' → (r, g, b, a) tuple."""
    try:
        inner = rgba_str.strip()[5:-1]
        parts = [p.strip() for p in inner.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]))
    except Exception:
        return (128, 128, 128, 0.8)


def _power_scale_tooltip_content(tt, is_dark: bool) -> None:
    """Graphical zone-scale explainer rendered into a tooltip's content slot."""
    with hd.box(slot=tt.content_slot, padding=0.5, gap=0.4, min_width=32):
        hd.text("Zone Spread bands", font_size="small", font_weight="bold")
        hd.text(
            "Each band is the EMA of power with a different time constant. "
            "Each second of work is classified to whichever band is most "
            "saturated at that moment; the bar shows the resulting time mix.",
            font_size="x-small",
            font_color="neutral-500",
        )
        with hd.hbox(gap=0.15, padding_top=0.2):
            for i, name in enumerate(BIN_NAMES[1:], start=1):
                with hd.scope(f"scale_{name}"):
                    with hd.box(gap=0.15, align="center"):
                        color_str = BIN_COLORS[i][0 if is_dark else 1]
                        hd.box(
                            width="100%",
                            height=0.7,
                            background_color=_parse_rgba(color_str),
                            border_radius="small",
                        )
                        hd.text(
                            name,
                            font_size="x-small",
                            font_weight="semibold",
                            font_color="neutral-700",
                        )
                        evt_name, _evt_key = _POWER_SCALE_ANCHORS[i - 1]
                        if evt_name is None:
                            hd.text(
                                "—",
                                font_size="x-small",
                                font_color="neutral-400",
                                font_style="italic",
                            )
                        else:
                            hd.text(
                                evt_name,
                                font_size="x-small",
                                font_color="neutral-500",
                            )


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
) -> bool:
    """Render one filter chip; return True if clicked this render."""
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
                        src=swatch_svg(color_str, size=10, radius=2),
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
                        src=swatch_svg(color_str, size=10, radius=2),
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
    # 3600, 7200).  Selected bands pass any workout with Partial+ stimulus
    # (dose ≥ 0.50) on that band.
    active_stimulus_bands = hd.Prop(hd.List(hd.Any), [])


@hd.cached
def spread_severity_legends(
    max_hr: int | None,
) -> None:
    """Render Power Spread + HR Spread + Severity filter chips.

    Toggles ``state.active_bins`` / ``state.active_hr_bins`` /
    ``state.active_severity`` on click.
    """
    is_dark = hd.theme().is_dark

    state = SpreadSeverityFilters()

    # ── Power Spread legend ──────────────────────────────────────────────
    active_bins: set[int] = set(state.active_bins)
    with hd.box(gap=0.3):
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(1, 0, 0.25, 0),
            wrap="wrap",
            justify="center",
        ):
            with hd.hbox(gap=0.25, align="center", min_width=7):
                hd.text(
                    "Power Spread",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-600",
                )
                with hd.tooltip() as tt:
                    _power_scale_tooltip_content(tt, is_dark)
                    hd.icon(
                        "question-circle",
                        font_size="small",
                        font_color="neutral-400",
                    )
            for i, name in enumerate(BIN_NAMES[1:], start=1):
                with hd.scope(f"power_{name}"):
                    color_str = BIN_COLORS[i][0 if is_dark else 1]
                    clicked = legend_chip(
                        name=name,
                        color_str=color_str,
                        is_active=i in active_bins,
                        definition=POWER_ZONE_DEFINITION_TEXT.get(i, ""),
                        filter_rule=POWER_ZONE_FILTER_TEXT.get(i, ""),
                    )
                    if clicked:
                        sel = set(state.active_bins)
                        if i in sel:
                            sel.discard(i)
                        else:
                            sel.add(i)
                        state.active_bins = tuple(sorted(sel))

        # ── HR Spread legend ─────────────────────────────────────────────
        if not max_hr:
            with hd.hbox(
                gap=0.75,
                align="center",
                padding=(0.25, 0, 0.5, 0),
                wrap="wrap",
                justify="center",
            ):
                hd.text(
                    "HR Spread",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-300",
                    min_width=7,
                )
                hd.text(
                    "Set max HR in Profile to filter by HR spread.",
                    font_size="x-small",
                    font_color="neutral-400",
                    font_style="italic",
                )
        else:
            active_hr_bins: set[int] = set(state.active_hr_bins)
            with hd.hbox(
                gap=0.75,
                align="center",
                padding=(0.25, 0, 0.5, 0),
                wrap="wrap",
                justify="center",
            ):
                hd.text(
                    "HR Spread",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-600",
                    min_width=7,
                )
                for i in range(1, 6):
                    name = HR_ZONE_NAMES[i]
                    with hd.scope(f"hr_{name}"):
                        color_str = HR_ZONE_COLORS[i][0 if is_dark else 1]
                        clicked = legend_chip(
                            name=name,
                            color_str=color_str,
                            is_active=i in active_hr_bins,
                            definition=HR_ZONE_DEFINITION_TEXT.get(i, ""),
                            filter_rule=HR_ZONE_FILTER_TEXT.get(i, ""),
                        )
                        if clicked:
                            sel = set(state.active_hr_bins)
                            if i in sel:
                                sel.discard(i)
                            else:
                                sel.add(i)
                            state.active_hr_bins = tuple(sorted(sel))

        # ── Severity legend ──────────────────────────────────────────────
        active_severity: set[str] = set(state.active_severity)
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(0.25, 0, 0.5, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "Severity",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            for label, _upper in SEVERITY_THRESHOLDS:
                with hd.scope(f"severity_{label}"):
                    color_rgba = SEVERITY_STYLE[label]["bg"]
                    color_str = (
                        f"rgba({color_rgba[0]},{color_rgba[1]},{color_rgba[2]},"
                        f"{color_rgba[3]})"
                    )
                    clicked = legend_chip(
                        name=label,
                        color_str=color_str,
                        is_active=label in active_severity,
                        definition=SEVERITY_DEFINITION_TEXT[label],
                        filter_rule=SEVERITY_FILTER_TEXT[label],
                    )
                    if clicked:
                        sel = set(state.active_severity)
                        if label in sel:
                            sel.discard(label)
                        else:
                            sel.add(label)
                        state.active_severity = tuple(
                            sorted(sel, key=lambda q: SEVERITY_ORDER[q])
                        )

        # ── Stimulus legend ──────────────────────────────────────────────
        # One chip per duration band; clicking filters to workouts that
        # delivered Partial+ stimulus (dose ≥ 0.50) to that system.
        # Disjunctive within the legend (multi-select shows workouts
        # hitting *any* of the selected systems), conjunctive across
        # legends.
        active_stimulus: set[int] = set(state.active_stimulus_bands)
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(0.25, 0, 0.5, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "Stimulus",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            for band_s in ZONE_BANDS_S:
                bin_idx = BAND_TO_BIN[band_s]
                name = BIN_NAMES[bin_idx]
                with hd.scope(f"stim_{name}"):
                    color_str = BIN_COLORS[bin_idx][0 if is_dark else 1]
                    clicked = legend_chip(
                        name=name,
                        color_str=color_str,
                        is_active=band_s in active_stimulus,
                        definition=STIMULUS_BAND_DEFINITIONS.get(band_s, ""),
                        filter_rule=STIMULUS_FILTER_TEXT.get(band_s, ""),
                    )
                    if clicked:
                        sel = set(state.active_stimulus_bands)
                        if band_s in sel:
                            sel.discard(band_s)
                        else:
                            sel.add(band_s)
                        state.active_stimulus_bands = tuple(sorted(sel))
