"""
Shared filter-legend component for Power Spread, HR Spread, and Quality.

Both the Intervals and Sessions pages render this same stack of three
labelled legends.  Each legend is a row of clickable chips; clicking a chip
toggles its membership in the page's active-set.  Filtering is **disjunctive
within** a legend (selecting two chips matches workouts in EITHER zone) and
**conjunctive across** legends.

Active state lives on the caller's ``state`` object as:
    state.active_bins        tuple[int]    pace bin indices (1–6)
    state.active_hr_bins     tuple[int]    HR bin indices (1–5)
    state.active_quality     tuple[str]    quality buckets

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
from services.volume_bins import (
    BIN_COLORS,
    BIN_NAMES,
    POWER_ZONE_DEFINITION_TEXT,
    POWER_ZONE_FILTER_TEXT,
    swatch_svg,
)
from services.workout_quality import (
    QUALITY_DEFINITION_TEXT,
    QUALITY_FILTER_TEXT,
    QUALITY_ORDER,
    QUALITY_STYLE,
    QUALITY_THRESHOLDS,
)
from components.workout_table import always_white


# Power Spread reference events for the scale tooltip — one per bin index 1–6.
_POWER_SCALE_ANCHORS: list[tuple[str | None, tuple | None]] = [
    ("1k", ("dist", 1000)),  # Fast
    ("2k", ("dist", 2000)),  # 2k
    ("5k", ("dist", 5000)),  # 5k
    ("60'", ("time", 36000)),  # Threshold
    ("Mara", ("dist", 42195)),  # Fast Aerobic
    (None, None),  # Slow Aerobic — no anchor
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
        hd.text("Power Spread zones", font_size="small", font_weight="bold")
        hd.text(
            "Each band is one zone; the label below names the PR event that "
            "sits inside it. Boundaries are midpoints between adjacent "
            "reference watts.",
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
                        evt_name, evt_key = _POWER_SCALE_ANCHORS[i - 1]
                        if evt_name is None:
                            hd.text(
                                "below",
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
class SpreadQualityFilters(hd.BaseState):
    active_bins = hd.Prop(hd.List(hd.Any), [])
    active_hr_bins = hd.Prop(hd.List(hd.Any), [])
    active_quality = hd.Prop(hd.List(hd.Any), [])


@hd.cached
def spread_quality_legends(
    max_hr: int | None,
) -> None:
    """Render Power Spread + HR Spread + Quality filter chips.

    Toggles ``state.active_bins`` / ``state.active_hr_bins`` /
    ``state.active_quality`` on click.
    """
    is_dark = hd.theme().is_dark

    state = SpreadQualityFilters()

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

        # ── Quality legend ───────────────────────────────────────────────
        active_quality: set[str] = set(state.active_quality)
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(0.25, 0, 0.5, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "Quality",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            for label, _upper in QUALITY_THRESHOLDS:
                with hd.scope(f"quality_{label}"):
                    color_rgba = QUALITY_STYLE[label]["bg"]
                    color_str = (
                        f"rgba({color_rgba[0]},{color_rgba[1]},{color_rgba[2]},"
                        f"{color_rgba[3]})"
                    )
                    clicked = legend_chip(
                        name=label,
                        color_str=color_str,
                        is_active=label in active_quality,
                        definition=QUALITY_DEFINITION_TEXT[label],
                        filter_rule=QUALITY_FILTER_TEXT[label],
                    )
                    if clicked:
                        sel = set(state.active_quality)
                        if label in sel:
                            sel.discard(label)
                        else:
                            sel.add(label)
                        state.active_quality = tuple(
                            sorted(sel, key=lambda q: QUALITY_ORDER[q])
                        )
