"""
Shared value-objects and registries for the Power Curve feature.

Exported:
    FilterSpec, ChartStyle, AnimationState — frozen dataclasses grouping the
        Power Curve page's state variables by concern.  They are hashable,
        so any of them can stand in for the hand-rolled cache keys that used
        to gate pipeline recomputation.

    Predictor, PREDICTORS, PREDICTORS_BY_KEY — the predictor registry.  Single
        source of truth for predictor name, description, whether it supports
        the "Show components" toggle, the label/description for that toggle,
        and the prediction-table column key.

This module has no dependency on HyperDiv — safe to import from anywhere
(services, components, tests).
"""

from __future__ import annotations

from dataclasses import dataclass


# ───────────────────────────────────────────────────────────────────────────
# State value-objects
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FilterSpec:
    """Data-identity inputs. Changing any of these invalidates the workout
    filtering pipeline (quality filters, event selection, best-filter)."""

    machine: str
    excluded_seasons: tuple  # tuple[str, ...]
    dist_enabled: tuple  # tuple[bool, ...], index-aligned with RANKED_DISTANCES
    time_enabled: tuple  # tuple[bool, ...], index-aligned with RANKED_TIMES
    best_filter: str  # "All" | "PBs" | "SBs"


@dataclass(frozen=True)
class ChartStyle:
    """Render-style inputs. Changing any affects rendering only — not
    the underlying workout filtering."""

    y_metric: str  # "pace" | "watts"
    x_metric: str  # "distance" | "duration"
    log_x: bool
    log_y: bool
    predictor: str  # PREDICTORS_BY_KEY key
    show_components: bool
    overlay_bests: str  # "PBs" | "SBs" | "None"  (was draw_power_curves)
    compare_wc: bool


@dataclass(frozen=True)
class AnimationState:
    """User-controlled animation knobs."""

    timeline_day: int | None  # None = end of timeline ("today")
    speed: str  # "0.5x" | "1x" | "4x" | "16x"
    playing: bool


# ───────────────────────────────────────────────────────────────────────────
# Predictor registry — one source of truth for all predictor metadata.
# Consumed by the prediction dropdown in _chart_settings, the "Show
# components" gear, the prediction-table header, and the per-keyframe
# compute_timeline_snapshot in power_curve_timeline.py.
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Predictor:
    key: str  # "critical_power" | "loglog" | "pauls_law" | "rowinglevel" | "average" | "none"
    name: str  # Display label for dropdown + table header
    desc: str  # Long description for tooltip + dropdown option text
    supports_components: bool  # Whether the "Show components" gear applies
    component_label: str  # Checkbox label in the components dropdown
    component_desc: str  # Tooltip below the components checkbox
    table_column: str | None  # Short key for prediction-table column; None → no table column


_DESC_CP = (
    "Two-component power-duration model (veloclinic). "
    "Requires 5 or more PBs spanning a 10:1 duration ratio. Method from rowsandall.com."
)
_DESC_LL = (
    "Fits a power law (log watts vs log distance) across all scoped PBs. "
    "Similar to the Free Spirits Pace Predictor (freespiritsrowing.com) "
    "but uses all PBs, not just two."
)
_DESC_RL = (
    "Predictions from rowinglevel.com based on your profile (gender, age, bodyweight). "
    "Distance-weighted average across all anchor PBs. Distance events only."
)
_DESC_PL = (
    "Predicts +5.0 s/500m for each doubling of distance "
    "(population default — needs 2 or more PBs to personalise), "
    "applied from each anchor PB and averaged."
)
_DESC_AVG = "Mean of all available predictions for this event."


PREDICTORS: tuple = (
    Predictor(
        key="critical_power",
        name="Critical Power",
        desc=_DESC_CP,
        supports_components=True,
        component_label="Show fast-twitch & slow-twitch components",
        component_desc="Shows the fast-twitch and slow-twitch power components separately.",
        table_column="cp",
    ),
    Predictor(
        key="loglog",
        name="Log-Log Watts Fit",
        desc=_DESC_LL,
        supports_components=False,
        component_label="",
        component_desc="",
        table_column="loglog",
    ),
    Predictor(
        key="pauls_law",
        name="Paul's Law (average)",
        desc=_DESC_PL,
        supports_components=True,
        component_label="Show one curve per anchor",
        component_desc="Shows one curve per PB anchor, before averaging.",
        table_column="pl",
    ),
    Predictor(
        key="rowinglevel",
        name="RowingLevel (average)",
        desc=_DESC_RL,
        supports_components=True,
        component_label="Show one RL curve per anchor",
        component_desc="Shows the RL curve from each PB anchor, before distance-weighted averaging.",
        table_column="rl",
    ),
    Predictor(
        key="average",
        name="Average of all techniques",
        desc=_DESC_AVG,
        supports_components=True,
        component_label="Show individual model curves",
        component_desc="Shows all individual model curves that were averaged.",
        table_column="avg",
    ),
    Predictor(
        key="none",
        name="...actually, don't predict",
        desc="Hide the prediction curve.",
        supports_components=False,
        component_label="",
        component_desc="",
        table_column=None,
    ),
)

PREDICTORS_BY_KEY: dict = {p.key: p for p in PREDICTORS}
