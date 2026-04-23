"""
components/intervals_page.py

Interval Workouts tab — 2D grid browser + persistent info panel + sortable
data table.

Browser
-------
A 2D grid shows both physiologically critical dimensions of interval
training at once:

  X axis (6 cols) — representative work-interval duration (median interval):
      ≤30"  ·  30"–2'  ·  2'–4'  ·  4'–8'  ·  8'–20'  ·  20'+

  Y axis (5 rows) — work:rest time ratio (total work / total rest):
      Continuous (≥10:1)  ·  Short (3–10:1)  ·  Balanced (≈1:1)
      Long (1:2–4)        ·  Very Long (<1:4)

Grid is rendered with CSS Grid (row-first) so column widths are set globally
via grid_template_columns.  Every cell is a full-width button — populated
cells show the session count on top of the stimulus label, empty cells
show only the label.

Each populated cell carries its own ``expected_score`` (on the stimulus
entry in ``_STIMULUS_INFO``) that determines its background colour via
``_cell_background_rgba`` — continuous-row cells read as aerobic blue,
row-4 sprints as red, etc.  "Other" cells (uncommon combinations) fall
back to a neutral grey.  Text is forced white in both themes.  Selection
rides on a thick white border rather than a colour change, so the cell's
expected-intensity colour stays visible.  Multi-cell selection = OR
union — the table filters to workouts in any selected cell.

Info panel (below the grid)
---------------------------
No per-cell tooltips.  The info panel iterates over `state.active_cells`
and renders one stimulus entry per selected cell (name, axis coordinates,
physiological description, example workout), separated by thin dividers.
When nothing is selected, a muted placeholder invites the user to click.
Empty cells toggle selection the same as populated ones (they just don't
contribute workouts to the table filter).

Grid placement rules:
- Work duration: median work-interval duration in seconds (all non-rest ivs)
- Work:rest ratio: sum(work times) / sum(rest_time fields + rest-type iv times)
  (internally stored as rest/work; rows represent work:rest as displayed)

Legends & filters
-----------------
Two labelled legend rows below the info panel:
  • "Pace Intensity" — 6 chips (Fast · 2k · 5k · Threshold · Fast Aero · Slow Aero)
  • "HR Intensity"  — 5 chips (Z5 Max · Z4 Threshold · Z3 Tempo · Z2 Aerobic · Z1 Recovery)

Both legends are **disjunctive (OR)** within themselves: selecting two chips
shows workouts touching _either_.  The three filter groups (grid cells,
pace chips, HR chips) combine conjunctively with each other.  Each chip has
a rich tooltip (content_slot) explaining zone definition and filter rule.

The HR legend is hidden entirely when the user has no max HR resolvable;
a short note points to the Profile page.

Table
-----
WorkoutTable (CSS Grid) with interval-specific ColumnDef objects.
Sortable headers (▲/▼), default sort: date descending.

Columns: Date · Reps · Structure (rep-stripped) · Stimulus ·
         Pace Intensity (score + bar) · HR Intensity (score + bar) ·
         Quality (Low/Medium/High pill) ·
         Work dist · Avg Split · Time · SPM · ↗

The Pace/HR Intensity columns each show a 0–100 weighted-average score
above a small stacked zone bar; hovering either cell opens a rich
content-slot tooltip with per-zone swatch + name + percentage.  Weights
come from services (PACE_INTENSITY_WEIGHTS / HR_INTENSITY_WEIGHTS).  Sort
is descending by score; workouts with no meaningful meters (or no HR)
render as "—" and sort last.

The Quality column compares each workout against its cell's own
``expected_score`` and ``expected_work_s`` in ``_STIMULUS_INFO``.
**Low** = pace score below expected (session wasn't hard enough).
**Medium** = pace score meets/exceeds expected but total work time is
below the cell's dose target.  **High** = both meet/exceed.  Cells
classified as "Other" (uncommon combinations) show "—".  The cell shows
a small coloured pill whose tooltip explains the grade with the
workout's own numbers alongside the targets.

Structure filter: clicking any Structure cell sets a filter restricting
the table to workouts with that same structure key.  Clicking the same
cell again, or the ×-chip above the table, clears it.
"""

from __future__ import annotations

import statistics

import hyperdiv as hd

from services.rowing_utils import INTERVAL_WORKOUT_TYPES, get_season
from components.concept2_sync import sync_from_context
from components.view_context import your
from services.interval_utils import (
    avg_workpace_tenths,
    avg_work_spm,
    interval_structure_key,
)
from services.volume_bins import (
    BIN_NAMES,
    BIN_COLORS,
    Z3_BINS,
    PACE_ZONE_DEFINITION_TEXT,
    PACE_ZONE_FILTER_TEXT,
    get_reference_sbs,
    compute_bin_thresholds,
    workout_bin_meters,
    pace_intensity_score,
    pace_bin_passes,
    bin_bar_svg,
    swatch_svg,
)
from services.heartrate_utils import (
    HR_ZONE_NAMES,
    HR_ZONE_COLORS,
    HR_ZONE_DEFINITION_TEXT,
    HR_ZONE_FILTER_TEXT,
    resolve_max_hr,
    workout_hr_meters,
    hr_intensity_score,
    hr_bin_passes,
)
from services.formatters import fmt_date, fmt_distance, fmt_split, format_time
from components.hyperdiv_extensions import aligned_button, grid_box
from components.workout_table import WorkoutTable, ColumnDef, COL_LINK
from components.profile_page import get_profile_from_context
from components.shared_ui import global_filter_ui

# ---------------------------------------------------------------------------
# color helpers
# ---------------------------------------------------------------------------


def _parse_rgba(rgba_str: str) -> tuple:
    """
    Parse an 'rgba(r,g,b,a)' string → (r, g, b, a) tuple for use with
    HyperDiv's background_color / border_color props (which accept raw tuples).
    """
    try:
        inner = rgba_str.strip()[5:-1]  # strip "rgba(" and ")"
        parts = [p.strip() for p in inner.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3]))
    except Exception:
        return (128, 128, 128, 0.8)


# ---------------------------------------------------------------------------
# Grid axis definitions
# ---------------------------------------------------------------------------

# Work duration column boundaries (seconds)
_DUR_COLS = [
    ("≤ 30s", 0, 30),
    ("30s – 2min", 30, 120),
    ("2 – 4min", 120, 240),
    ("4 – 8min", 240, 480),
    ("8 – 20min'", 480, 1200),
    ("20min+", 1200, float("inf")),
]
_N_COLS = len(_DUR_COLS)

# Work:rest ratio row boundaries + display label (ratio = rest/work internally)
_RATIO_ROWS = [
    ("Continuous", "≥ 10w : 1r", 0.0, 0.10),
    ("Short rest", "2–10w : 1r", 0.10, 0.50),
    ("Balanced", "≈ 1w : 1r", 0.50, 1.50),
    ("Long rest", "1w : 1.5–4r", 1.50, 4.00),
    ("Very Long", "< 1w : 4r", 4.00, float("inf")),
]
_RATIO_ROWS = [
    ("Continuous", "< 9% rest", 0.0, 0.10),
    ("Short rest", "9-33% rest", 0.10, 0.50),
    ("Balanced", "33-60% rest", 0.50, 1.50),
    ("Long rest", "60–80% rest", 1.50, 4.00),
    ("Very Long", "> 80% rest", 4.00, float("inf")),
]


_N_ROWS = len(_RATIO_ROWS)

# ---------------------------------------------------------------------------
# Per-cell quality expectations
# ---------------------------------------------------------------------------
#
# Each populated cell in _STIMULUS_INFO carries two numbers describing what a
# well-executed quality session of that specific stimulus looks like:
#
#   expected_score  — 0–100 pace-intensity score we'd expect.  Drives:
#                     (a) the cell's background colour in the grid, and
#                     (b) the "was this hard enough?" check for the Quality
#                         column.
#
#   expected_work_s — rough lower bound (seconds of work) for a genuine dose
#                     at that stimulus.  Used with expected_score to grade
#                     each session Low / Medium / High in the Quality column.
#
# Values are conservative ballparks — they shape a 3-state colour chip, not
# a prescription.  Cells left as None in _STIMULUS_INFO use no expectations:
# the grid paints them a neutral grey and their Quality column shows "—".


def _intensity_to_bin(score: float) -> int:
    """Map a 0–100 pace-intensity score to the pace bin (1–6) whose colour
    best represents it."""
    if score >= 90:
        return 1  # Fast (red)
    if score >= 70:
        return 2  # 2k (orange)
    if score >= 50:
        return 3  # 5k (yellow-green)
    if score >= 30:
        return 4  # Threshold (green)
    if score >= 10:
        return 5  # Fast Aerobic (blue)
    return 6  # Slow Aerobic (light blue)


# Neutral grey fallback for cells that have no stimulus info (the "Other"
# uncommon combinations).  Same value in both themes — no intensity signal
# to convey.
_OTHER_CELL_RGBA_LIGHT: tuple = (180, 185, 190, 1)
_OTHER_CELL_RGBA_DARK: tuple = (110, 115, 120, 1)


def _cell_background_rgba(row_idx: int, col_idx: int, is_dark: bool) -> tuple:
    """RGBA tuple for a grid cell's background.

    Populated cells are coloured by the stimulus's own ``expected_score``;
    "Other" (uncommon) cells fall back to a neutral grey so they don't falsely
    imply an intensity.
    """
    info = _STIMULUS_INFO[row_idx][col_idx]
    if info is None:
        return _OTHER_CELL_RGBA_DARK if is_dark else _OTHER_CELL_RGBA_LIGHT
    bin_idx = _intensity_to_bin(info.get("expected_score", 0))
    return _parse_rgba(BIN_COLORS[bin_idx][0 if is_dark else 1])


def _always_white(is_dark: bool) -> str:
    """Return a Shoelace neutral token that renders as white in either theme."""
    # In light theme neutral-0 is white; in dark theme it flips to dark, so
    # we swap to neutral-1000 which ends up white under the dark palette.
    return "neutral-1000" if is_dark else "neutral-0"


# ---------------------------------------------------------------------------
# Session quality
# ---------------------------------------------------------------------------
#
# Each session is rated Low / Medium / High against the row's quality
# expectations.  The rule is intentionally lenient:
#
#   • Low     — pace intensity is below the row's expected intensity.
#               (The session wasn't actually hard enough to count as quality
#                at this work:rest ratio, regardless of volume.)
#   • Medium  — pace intensity ≥ expected, but total work time is below
#               the row's expected dose.  (Right intensity, short dose.)
#   • High    — pace intensity ≥ expected AND total work time ≥ expected.
#
# Sessions with no meaningful meters (score is None) return None.


def _compute_quality(r: dict) -> str | None:
    score = r.get("_pace_score")
    if score is None:
        return None
    row = r.get("_grid_row")
    col = r.get("_grid_col")
    if row is None or col is None:
        return None
    info = (
        _STIMULUS_INFO[row][col] if 0 <= row < _N_ROWS and 0 <= col < _N_COLS else None
    )
    if info is None:
        return None
    expected_score = info.get("expected_score")
    expected_work_s = info.get("expected_work_s")
    if expected_score is None or expected_work_s is None:
        return None
    work_s = (r.get("time") or 0) / 10.0
    if score < expected_score:
        return "Low"
    if work_s < expected_work_s:
        return "Medium"
    return "High"


_QUALITY_ORDER = {"Low": 0, "Medium": 1, "High": 2}

_QUALITY_STYLE: dict[str, dict] = {
    "Low": {
        "label": "Low",
        "bg": (215, 55, 55, 0.85),  # BIN_COLORS[1] dark (red)
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "Medium": {
        "label": "Medium",
        "bg": (225, 125, 35, 0.85),  # orange
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
    "High": {
        "label": "High",
        "bg": (25, 150, 50, 0.90),  # green
        "fg_on_dark_theme": "neutral-1000",
        "fg_on_light_theme": "neutral-0",
    },
}

# ---------------------------------------------------------------------------
# Stimulus matrix (grid + info panel source of truth)
# ---------------------------------------------------------------------------
#
# One entry per grid cell, indexed [row_idx][col_idx] where the outer index
# runs over _RATIO_ROWS (Continuous → Very Long) and the inner over _DUR_COLS
# (≤30" → 20'+).  Each populated entry is a dict with:
#
#   name         — short label shown on the cell button + info-panel heading
#   description  — one-to-two-sentence physiological description, plain
#                  rowing-literature terminology (Seiler, Daniels, Billat)
#   example      — "E.g. …" worked prescription, rendered on its own line
#
# Cells left as None are physiologically uncommon or unprogrammed (e.g. a
# continuous ≤30" piece).  The grid labels those "Other" and the info panel
# explains they are unusual combinations.
_STIMULUS_INFO: list[list[dict | None]] = [
    # Row 0 — Continuous (work:rest ≥ 10:1)
    # Aerobic sessions.  Pace-intensity scores here are intentionally low —
    # the point is volume at easy/steady intensity, not hardness — so we
    # set expected_score = 0 and grade purely on work-time accumulation.
    [
        None,  # ≤30" continuous — n/a
        {
            "name": "Fartlek",
            "description": (
                "Continuous aerobic effort with internal pace variations. "
                "The surges are short enough that blood lactate does not "
                "meaningfully accumulate, so the piece remains fundamentally "
                "aerobic."
            ),
            "example": "10× 1' easy / 1' moderate, continuous.",
            "expected_score": 10,
            "expected_work_s": 1500,
        },
        {
            "name": "Sustained aerobic",
            "description": (
                "2–4 minute continuous aerobic blocks with minimal "
                "transition. Targets mitochondrial density and fat oxidation."
            ),
            "example": "3× 3' at aerobic pace, continuous.",
            "expected_score": 0,
            "expected_work_s": 1500,
        },
        {
            "name": "Steady state",
            "description": (
                "Moderate-duration continuous work below the first lactate "
                "threshold. Develops stroke volume and capillarisation."
            ),
            "example": "4× 5' at rate 18–20, continuous.",
            "expected_score": 0,
            "expected_work_s": 1500,
        },
        {
            "name": "Aerobic base",
            "description": (
                "Long continuous effort at conversational intensity — the "
                "cornerstone of base-building phases."
            ),
            "example": "2× 15' / 1'r, or a single 20'.",
            "expected_score": 0,
            "expected_work_s": 1800,
        },
        {
            "name": "Long slow distance",
            "description": (
                "Extended low-intensity rowing. Builds economy, mental "
                "endurance, and fat utilisation."
            ),
            "example": "Single 60', or 2× 30'.",
            "expected_score": 0,
            "expected_work_s": 2700,
        },
    ],
    # Row 1 — Short rest (work:rest 3–10:1)
    [
        None,  # ≤30" — n/a
        {
            "name": "Glycolytic capacity",
            "description": (
                "Short high-intensity intervals with very brief recovery. "
                "Work-to-rest accumulates glycolytic demand across reps and "
                "trains tolerance of low muscle pH."
            ),
            "example": '10× 1\' / 12"r, 15× 30" / 8"r.',
            "expected_score": 65,
            "expected_work_s": 300,
        },
        {
            "name": "VO₂max intervals",
            "description": (
                "2–4 minute intervals with short recovery. The incomplete "
                "recovery keeps oxygen uptake high across reps, producing a "
                "large VO₂max stimulus per session."
            ),
            "example": "6× 3' / 1'r, 8× 2' / 40\"r.",
            "expected_score": 55,
            "expected_work_s": 480,
        },
        {
            "name": "Supra-threshold",
            "description": (
                "Work at or slightly above the second lactate threshold "
                "with incomplete recovery. Lactate accumulates gradually "
                "across reps."
            ),
            "example": "4× 6' / 2'r, 4× 8' / 2'r.",
            "expected_score": 40,
            "expected_work_s": 600,
        },
        {
            "name": "Threshold accumulation",
            "description": (
                "Long intervals near threshold with short recovery. "
                "Accumulates substantial time at threshold; late reps may "
                "drift as fatigue builds."
            ),
            "example": "3× 12' / 4'r, 4× 10' / 3'r.",
            "expected_score": 30,
            "expected_work_s": 1500,
        },
        {
            "name": "Tempo",
            "description": (
                "Long work intervals with brief recovery at moderate-to-"
                "threshold intensity — effectively fractioned tempo work."
            ),
            "example": "2× 20' / 5'r.",
            "expected_score": 25,
            "expected_work_s": 1800,
        },
    ],
    # Row 2 — Balanced (work:rest ≈ 1:1)
    [
        {
            "name": "Neuromuscular sprints",
            "description": (
                "Very short efforts with roughly equal recovery. Develops "
                "repeated peak power as the phosphocreatine system partly "
                "replenishes between reps."
            ),
            "example": '10× 20" / 20"r at maximal power.',
            "expected_score": 80,
            "expected_work_s": 120,
        },
        {
            "name": "Anaerobic endurance",
            "description": (
                "Sub-2-minute efforts with near-equal rest. Each rep starts "
                "before lactate has fully cleared; trains tolerance of "
                "accumulating lactate."
            ),
            "example": '8× 500m / 2\'r, 10× 45" / 45"r.',
            "expected_score": 75,
            "expected_work_s": 360,
        },
        {
            "name": "VO₂max (2k-prep)",
            "description": (
                "The canonical VO₂max interval. Work reaches VO₂max; equal "
                "rest allows partial recovery while keeping oxygen uptake "
                "elevated across reps."
            ),
            "example": "6× 2' / 2'r, 8× 2' / 2'r.",
            "expected_score": 65,
            "expected_work_s": 480,
        },
        {
            "name": "VO₂max (5k-prep)",
            "description": (
                "Longer VO₂max intervals with adequate recovery. Extends "
                "time at VO₂max per rep while keeping quality high."
            ),
            "example": "4× 4' / 4'r, 5× 1000m / 4'r.",
            "expected_score": 55,
            "expected_work_s": 600,
        },
        {
            "name": "Lactate threshold",
            "description": (
                "Long intervals with roughly equal recovery at controlled "
                "intensity. Accumulates extended threshold time with "
                "manageable fatigue."
            ),
            "example": "3× 10' / 10'r, 2× 15' / 15'r.",
            "expected_score": 35,
            "expected_work_s": 900,
        },
        # {
        #     "name": "Aerobic blocks",
        #     "description": (
        #         "Very long aerobic intervals with roughly equal recovery. "
        #         "Uncommon in periodised programmes; often arises in low-"
        #         "intensity adaptation phases."
        #     ),
        #     "example": "2× 30' / 30'r, 3× 20' / 20'r.",
        #     "expected_score": 15,
        #     "expected_work_s": 1500,
        # },
        None,
    ],
    # Row 3 — Long rest (work:rest 1:2–4)
    [
        {
            "name": "Speed / power",
            "description": (
                "Very short maximal efforts with generous recovery. "
                "Targets the phosphocreatine system and peak neuromuscular "
                "power."
            ),
            "example": '8× 15" / 45"r, 6× 20" / 1\'r.',
            "expected_score": 90,
            "expected_work_s": 60,
        },
        {
            "name": "Speed endurance",
            "description": (
                "Sub-2-minute high-intensity intervals with substantial "
                "recovery. Develops the ability to repeat near-maximal "
                "efforts with partial PCr recovery."
            ),
            "example": "5× 1' / 3'r, 6× 500m / 3'r.",
            "expected_score": 70,
            "expected_work_s": 240,
        },
        {
            "name": "VO₂max (long intervals)",
            "description": (
                "High-quality VO₂max intervals with near-full recovery. "
                "Prioritises peak power per rep over total VO₂max dose — "
                "useful for in-season maintenance."
            ),
            "example": "4× 2' / 8'r, 4× 500m / 6'r.",
            "expected_score": 70,
            "expected_work_s": 300,
        },
        # {
        #     "name": "5k quality",
        #     "description": (
        #         "Extended race-pace efforts with generous recovery. "
        #         "Refines race-pace efficiency and pacing."
        #     ),
        #     "example": "3× 5' / 15'r, 4× 1000m / 8'r.",
        #     "expected_score": 55,
        #     "expected_work_s": 480,
        # },
        # {
        #     "name": "Extensive endurance",
        #     "description": (
        #         "Long work intervals with even longer rest. Typically coach-"
        #         "prescribed race pieces or block training with full recovery."
        #     ),
        #     "example": "3× 10' / 20'r.",
        #     "expected_score": 30,
        #     "expected_work_s": 900,
        # },
        None,
        None,
        None,  # 20'+ with long rest — n/a
    ],
    # Row 4 — Very Long rest (work:rest < 1:4)
    [
        {
            "name": "Maximal sprints",
            "description": (
                "True maximum-effort sprints with full PCr recovery. Every "
                "rep should be maximally explosive."
            ),
            "example": "6× 10\" / 2'r, 8× 15\" / 3'r.",
            "expected_score": 95,
            "expected_work_s": 60,
        },
        {
            "name": "Alactic (PCr)",
            "description": (
                "Near-maximal efforts with near-complete PCr resynthesis "
                "between reps. Builds repeated sprint capacity and peak "
                "neuromuscular power."
            ),
            "example": "4× 45\" / 5'r",
            "expected_score": 90,
            "expected_work_s": 180,
        },
        # {
        #     "name": "Race-pace intervals",
        #     "description": (
        #         "2–4 minute near-maximal efforts with very long recovery "
        #         "(>4× work time). Full quality on every rep; used for race-"
        #         "pace familiarisation and power development."
        #     ),
        #     "example": "4× 2' / 8'r, 3× 3' / 12'r at 2k pace.",
        #     "expected_score": 75,
        #     "expected_work_s": 240,
        # },
        # {
        #     "name": "Race simulations",
        #     "description": (
        #         "4–8 minute efforts at race intensity with very long "
        #         "recovery (>4× work time). Full recovery keeps each rep "
        #         "maximally race-representative."
        #     ),
        #     "example": "3× 5' / 25'r, 2× 6' / 30'r at 2k race pace.",
        #     "expected_score": 75,
        #     "expected_work_s": 360,
        # },
        None,
        None,
        None,  # 8'–20' with very long rest — n/a
        None,  # 20'+ with very long rest — n/a
    ],
]


def _cell_info(row_idx: int, col_idx: int) -> dict | None:
    """Return the stimulus dict for a (row, col) or None if the cell is n/a."""
    return _STIMULUS_INFO[row_idx][col_idx]


def _cell_name(row_idx: int, col_idx: int) -> str:
    """Return the short stimulus name for a cell, or "Other" when n/a."""
    info = _cell_info(row_idx, col_idx)
    return info["name"] if info else "Other"


_ROWS_PER_PAGE = 200

# Width (in HyperDiv units) of the small zone bar rendered inside each of
# the Pace/HR Intensity cells — half the full zone-bar width so the score
# reads as the dominant signal.
_INTENSITY_BAR_WIDTH = 5.0
_INTENSITY_BAR_HEIGHT = 0.5

# Grid cell sizing
_CELL_H = 4.0  # HyperDiv units per data cell
_HEADER_H = 2.0  # HyperDiv units for column header
_ROW_LABEL_W = 10  # HyperDiv units for row label column


# ---------------------------------------------------------------------------
# Grid placement helpers
# ---------------------------------------------------------------------------


def _dur_col(seconds: float) -> int:
    """Map a work duration (seconds) to a column index."""
    for i, (_, lo, hi) in enumerate(_DUR_COLS):
        if lo <= seconds < hi:
            return i
    return _N_COLS - 1


def _ratio_row(ratio: float) -> int:
    """Map a rest:work ratio to a row index."""
    for i, (_, _, lo, hi) in enumerate(_RATIO_ROWS):
        if lo <= ratio < hi:
            return i
    return _N_ROWS - 1


def _compute_grid_placement(r: dict) -> tuple[int, int]:
    """
    Return (col_idx, row_idx) for placing r in the 2D grid.

    Work duration  = median work-interval duration in seconds.
    Rest:work ratio = sum(rest_time) / sum(work_time).
    Times in the C2 API are stored in tenths of seconds.
    """
    ivs = (r.get("workout") or {}).get("intervals") or []
    work_ivs = [iv for iv in ivs if (iv.get("type") or "").lower() != "rest"]

    if not work_ivs:
        total_s = (r.get("time") or 0) / 10
        return _dur_col(total_s), 0  # Continuous row

    work_times_s = [(iv.get("time") or 0) / 10 for iv in work_ivs]
    total_work_s = sum(work_times_s)
    rep_work_s = statistics.median(work_times_s) if work_times_s else 0.0

    rest_ivs = [iv for iv in ivs if (iv.get("type") or "").lower() == "rest"]
    total_rest_s = sum((iv.get("rest_time") or 0) / 10 for iv in work_ivs) + sum(
        (iv.get("time") or 0) / 10 for iv in rest_ivs
    )

    ratio = total_rest_s / total_work_s if total_work_s > 0 else 0.0
    return _dur_col(rep_work_s), _ratio_row(ratio)


# ---------------------------------------------------------------------------
# Data enrichment
# ---------------------------------------------------------------------------


def _enrich_workouts(
    workouts: list[dict],
    thresholds,
    max_hr: int | None,
) -> list[dict]:
    """
    Filter to interval workout types (excluding single-rep sessions) and
    attach computed fields used by the grid, info panel, and table.

    Fields attached:

      _bin_meters       list[float]    Per-pace-bin meter counts (index 0 = Rest)
      _bar_uri          str            Data-URI SVG stacked pace-zone bar
      _z3               float          Fraction of work meters in Z3 (grid colour)
      _pace_score       float | None   0–100 weighted pace intensity
      _hr_bin_meters    list[float] | None  Per-HR-bin meter counts, or None
                                         when max_hr is unknown
      _hr_bar_uri       str | None     Data-URI SVG stacked HR-zone bar, or None
      _hr_score         float | None   0–100 weighted HR intensity
      _structure_key    str            Rep-stripped structure label
      _reps             int            Number of work intervals
      _work_pace        float | None   Avg work pace (tenths/500m)
      _work_spm         float | None   Work-weighted avg stroke rate
      _grid_col         int            Column index in the 2D grid
      _grid_row         int            Row index in the 2D grid
      _stimulus         str            Short stimulus name for the cell
                                         ("Other" when cell is n/a)
    """
    result = []
    for r in workouts:
        if r.get("workout_type") not in INTERVAL_WORKOUT_TYPES:
            continue
        ivs = (r.get("workout") or {}).get("intervals") or []
        work_ivs = [iv for iv in ivs if (iv.get("type") or "").lower() != "rest"]

        # Skip single-rep sessions (e.g. 1×500m / 3'r).  Keep workouts with
        # multiple intervals that share no rest — they form legitimate multi-block
        # sessions even though every rest_time == 0.
        reps = len(work_ivs) or len(ivs)
        if reps == 1:
            continue

        r = dict(r)  # shallow copy

        bm = workout_bin_meters(r, thresholds)
        work_total = sum(bm[1:])

        r["_bin_meters"] = bm
        r["_bar_uri"] = bin_bar_svg(bm)
        r["_z3"] = sum(bm[i] for i in Z3_BINS) / work_total if work_total else 0.0
        r["_pace_score"] = pace_intensity_score(bm)

        if max_hr:
            hrm = workout_hr_meters(r, max_hr)
            r["_hr_bin_meters"] = hrm
            # Render the HR bar using only classified meters (bins 0–5); drop
            # bin 6 so "no HR" doesn't dilute the colour signal.  bin_bar_svg
            # takes a 7-element list and skips index 0 internally, so pad
            # bins 1–5 with a 0 for the "No HR" slot.
            hr_for_bar = list(hrm)
            hr_for_bar[6] = 0
            r["_hr_bar_uri"] = bin_bar_svg(hr_for_bar)
            r["_hr_score"] = hr_intensity_score(hrm)
        else:
            r["_hr_bin_meters"] = None
            r["_hr_bar_uri"] = None
            r["_hr_score"] = None

        r["_structure_key"] = interval_structure_key(r, compact=True)
        r["_reps"] = reps
        r["_work_pace"] = avg_workpace_tenths(r)
        r["_work_spm"] = avg_work_spm(r)
        col, row = _compute_grid_placement(r)
        r["_grid_col"] = col
        r["_grid_row"] = row
        r["_stimulus"] = _cell_name(row, col)
        r["_quality"] = _compute_quality(r)
        result.append(r)
    result.sort(key=lambda x: x.get("date", ""), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _filter_disjunctive(
    workouts: list[dict],
    active_bins: set[int],
    passes_fn,
    meters_key: str,
) -> list[dict]:
    """
    Keep any workout whose bin-meters (under meters_key) pass the threshold
    for ANY of the selected bins — i.e. disjunctive (OR) combination.
    Empty selection → pass through unchanged.

    passes_fn(bin_meters, bin_idx) → bool is the services-layer threshold
    test (pace_bin_passes / hr_bin_passes).  Workouts with meters_key == None
    (no HR data) never match any HR bin and are dropped from a non-empty HR
    selection.
    """
    if not active_bins:
        return workouts
    return [
        r
        for r in workouts
        if r.get(meters_key) is not None
        and any(passes_fn(r[meters_key], b) for b in active_bins)
    ]


def _filter_by_cells(workouts: list[dict], cells: frozenset[str]) -> list[dict]:
    if not cells:
        return workouts
    return [r for r in workouts if f"{r['_grid_col']},{r['_grid_row']}" in cells]


# ---------------------------------------------------------------------------
# Grid browser
# ---------------------------------------------------------------------------


def _cell_key(col: int, row: int) -> str:
    return f"{col},{row}"


def _grid_cell_tooltip_content(tt, row_idx: int, col_idx: int) -> None:
    info = _cell_info(row_idx, col_idx)
    col_label = _DUR_COLS[col_idx][0]
    row_label, ratio_range, _, _ = _RATIO_ROWS[row_idx]

    with hd.box(slot=tt.content_slot, gap=0.4, max_width=40):
        with hd.hbox(gap=0.5, align="center", wrap="wrap"):
            hd.text(
                info["name"] if info else f"Other ({row_label})",
                font_weight="bold",
                font_size="medium",
            )
            hd.text(
                f"{col_label} work · {ratio_range}",
                font_size="small",
                font_color="neutral-300",
            )
        if info:
            hd.text(info["description"], font_size="small")
            hd.text(
                f"E.g. {info['example']}",
                font_size="small",
                font_color="neutral-300",
                font_style="italic",
            )
        else:
            hd.text(
                "This combination of work duration and work:rest ratio is "
                "uncommon in structured training.  Workouts that land here "
                "are shown for completeness.",
                font_size="small",
                font_color="neutral-300",
            )


def _chip_tooltip_content(tt, heading: str, definition: str, filter_rule: str) -> None:
    """Rich chip tooltip body: bold zone heading, definition, filter rule."""
    with hd.box(slot=tt.content_slot, padding=0.3, gap=0.25, max_width=40):
        hd.text(heading, font_size="medium", font_weight="bold")
        hd.text(definition, font_size="small")
        hd.text(filter_rule, font_size="small", font_style="italic")


def _intensity_chip(
    *,
    name: str,
    color_str: str,
    is_active: bool,
    definition: str,
    filter_rule: str,
) -> bool:
    """
    Render a single intensity filter chip (pace or HR) with a rich tooltip.
    Returns True if the chip was clicked this render cycle.
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
                        src=swatch_svg(color_str, size=10, radius=2),
                        width=0.65,
                        height=0.65,
                    )
                    hd.text(
                        name,
                        font_size="small",
                        font_color=_always_white(hd.theme().is_dark),
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


def _zone_filter_legends(state, max_hr: int | None) -> None:
    """
    Two stacked labelled legends: Pace Intensity (always) + HR Intensity
    (only when max_hr is resolvable).  Both combine **disjunctively** within
    themselves — selecting two chips shows workouts touching EITHER zone.

    Each chip has a rich content-slot tooltip with the zone's definition and
    the filter rule.  Active state lives in state.active_bins (pace) and
    state.active_hr_bins (HR); chip colour reflects the zone colour when on.
    """
    is_dark = hd.theme().is_dark

    # ── Pace Intensity legend ───────────────────────────────────────────
    active_bins: set[int] = set(state.active_bins)
    with hd.box(gap=0.3):
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(1, 0, 0.25, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "Pace Intensity",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            for i, name in enumerate(BIN_NAMES[1:], start=1):
                with hd.scope(f"pace_{name}"):
                    color_str = BIN_COLORS[i][0 if is_dark else 1]
                    clicked = _intensity_chip(
                        name=name,
                        color_str=color_str,
                        is_active=i in active_bins,
                        definition=PACE_ZONE_DEFINITION_TEXT.get(i, ""),
                        filter_rule=PACE_ZONE_FILTER_TEXT.get(i, ""),
                    )
                    if clicked:
                        sel = set(state.active_bins)
                        if i in sel:
                            sel.discard(i)
                        else:
                            sel.add(i)
                        state.active_bins = tuple(sorted(sel))

        # ── HR Intensity legend ─────────────────────────────────────────────
        if not max_hr:
            with hd.hbox(
                gap=0.75,
                align="center",
                padding=(0.25, 0, 0.5, 0),
                wrap="wrap",
                justify="center",
            ):
                hd.text(
                    "HR Intensity",
                    font_size="small",
                    font_weight="bold",
                    font_color="neutral-300",
                    min_width=7,
                )
                hd.text(
                    "Set max HR in Profile to filter by HR intensity.",
                    font_size="x-small",
                    font_color="neutral-400",
                    font_style="italic",
                )
            return

        active_hr_bins: set[int] = set(state.active_hr_bins)
        with hd.hbox(
            gap=0.75,
            align="center",
            padding=(0.25, 0, 0.5, 0),
            wrap="wrap",
            justify="center",
        ):
            hd.text(
                "HR Intensity",
                font_size="small",
                font_weight="bold",
                font_color="neutral-600",
                min_width=7,
            )
            # HR_ZONE_NAMES indices 1..5 are the classifiable zones.
            for i in range(1, 6):
                name = HR_ZONE_NAMES[i]
                with hd.scope(f"hr_{name}"):
                    color_str = HR_ZONE_COLORS[i][0 if is_dark else 1]
                    clicked = _intensity_chip(
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


def _grid_browser(zone_workouts: list[dict], state) -> None:
    """
    Render the 2D work-duration × rest:work grid using CSS Grid.

    Single grid_box with grid_template_columns = row-label col + N data cols.
    All cells are direct grid children (row-first order), so CSS Grid guarantees
    uniform column widths without column-first nesting.

    Each populated cell's background is coloured by the zone corresponding
    to that stimulus's own ``expected_score`` (see `_STIMULUS_INFO`).
    "Other" cells — physiologically uncommon combinations — fall back to a
    neutral grey.  Cell text is forced white in both themes; selected cells
    get a thick white border so the cell colour stays visible.  Every cell
    (populated or empty) toggles the selection; the info panel below the
    grid explains each selected cell.
    """
    is_dark = hd.theme().is_dark

    # Pre-compute per-cell data
    cell_workouts: dict[str, list[dict]] = {}
    for r in zone_workouts:
        k = _cell_key(r["_grid_col"], r["_grid_row"])
        cell_workouts.setdefault(k, []).append(r)

    active_cells: frozenset[str] = frozenset(state.active_cells)

    # CSS Grid template: fixed row-label column + equal-width data columns
    col_template = f"{_ROW_LABEL_W}rem " + " ".join(["1fr"] * _N_COLS)

    with hd.box(margin_top=1):
        # Axis label row (small arrows above the grid)
        with hd.hbox(gap=0, align="center", padding=(0, 0, 0.25, 0)):
            # Corner area — spacer aligned with row-label column
            with hd.box(
                width=_ROW_LABEL_W,
                align="start",
                justify="end",
                padding=(0, 0.5, 0, 0),
            ):
                pass
            # Horizontal axis label pointing right — spans the data columns
            with hd.hbox(gap=0.4, align="center", grow=True):
                hd.text(
                    "Work duration",
                    font_size="small",
                    font_color="neutral-400",
                    font_style="italic",
                )
                hd.icon("arrow-right", font_size="small", font_color="neutral-400")

        # Main grid — CSS Grid (row-first; column widths set globally)
        with grid_box(
            grid_template_columns=col_template,
            border_radius="medium",
            overflow="hidden",
        ):
            # ── Header row ────────────────────────────────────────────────
            # Corner cell
            with hd.scope("corner"):
                with hd.hbox(
                    gap=0.4,
                    align="center",
                    justify="end",
                    height=_HEADER_H,
                    padding=(0.4, 0.6),
                ):
                    hd.text(
                        "Work : rest ratio",
                        font_size="small",
                        font_color="neutral-400",
                        font_style="italic",
                    )
                    hd.icon("arrow-down", font_size="small", font_color="neutral-400")

            # Column header cells
            for ci, (col_label, _, _) in enumerate(_DUR_COLS):
                with hd.scope(f"hdr_{ci}"):
                    cell_props = dict(
                        height=_HEADER_H,
                        padding=(0.3, 0.3),
                        align="center",
                        justify="center",
                        # border_bottom="1px solid neutral-200",
                    )
                    with hd.box(**cell_props):
                        hd.text(
                            col_label,
                            font_size="small",
                            font_weight="bold",
                            font_color="neutral-600",
                            text_align="center",
                        )

            # ── Data rows ─────────────────────────────────────────────────
            for ri, (row_label, ratio_range, _, _) in enumerate(_RATIO_ROWS):
                with hd.scope(f"row_{ri}"):
                    # Row label cell
                    with hd.scope("lbl"):
                        with hd.box(
                            # height=_CELL_H,
                            padding=(0.4, 0.6),
                            align="end",
                            justify="center",
                            # border_top="1px solid neutral-200",
                            # border_right="1px solid neutral-200",
                            gap=0.1,
                        ):
                            hd.text(
                                row_label,
                                font_size="small",
                                font_weight="bold",
                                font_color="neutral-600",
                            )
                            hd.text(
                                ratio_range,
                                font_size="small",
                                font_color="neutral-400",
                            )

                    # Data cells — each cell is coloured by its own stimulus's
                    # expected pace-intensity score.  "Other" cells fall back
                    # to a neutral grey.  Selection state is a thick white
                    # border rather than a colour change, so the cell colour
                    # stays legible.
                    white_token = _always_white(is_dark)
                    black_token = _always_white(not is_dark)

                    for ci in range(_N_COLS):
                        with hd.scope(f"c{ci}"):
                            k = _cell_key(ci, ri)
                            workouts_in_cell = cell_workouts.get(k, [])
                            count = len(workouts_in_cell)
                            display_label = _cell_name(ri, ci)
                            is_sel = k in active_cells
                            cell_bg_rgba = (
                                black_token
                                if is_sel
                                else _cell_background_rgba(ri, ci, is_dark)
                            )

                            sel_border = (
                                f"5px solid {black_token}"
                                if is_sel
                                else "1px solid neutral-0"
                            )

                            with hd.box(
                                align="end",
                                gap=0,
                                background_color=cell_bg_rgba,
                                border="1px solid neutral-0",
                            ):
                                with aligned_button(
                                    width="100%",
                                    height=_CELL_H,
                                    line_height="normal",
                                    align="center",
                                    background_color=cell_bg_rgba,
                                    border="none",
                                    padding_bottom=0,
                                    padding_top=1.5,
                                ) as cell_btn:
                                    if count > 0:
                                        hd.text(
                                            str(count),
                                            font_size="large",
                                            font_weight="bold",
                                            font_color=white_token,
                                        )
                                    hd.text(
                                        display_label,
                                        font_size="x-small",
                                        text_align="center",
                                        font_color=white_token,
                                    )
                                if cell_btn.clicked:
                                    sel = set(state.active_cells)
                                    if is_sel:
                                        sel.discard(k)
                                    else:
                                        sel.add(k)
                                    state.active_cells = tuple(sorted(sel))

                                with hd.tooltip() as tt:
                                    _grid_cell_tooltip_content(tt, ri, ci)
                                    hd.icon(
                                        "question-circle",
                                        font_color=white_token,
                                        padding_right=0.3,
                                        padding_bottom=0.3,
                                    )


# ---------------------------------------------------------------------------
# Info panel
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Intensity column rendering (Pace + HR)
# ---------------------------------------------------------------------------


def _render_intensity_cell(
    score: float | None,
    bar_uri: str | None,
    bin_meters: list | None,
    zone_names: list[str],
    zone_colors: list[tuple[str, str]],
    is_dark: bool,
    *,
    skip_indices: tuple[int, ...] = (0,),
) -> None:
    """
    Shared cell renderer for the Pace Intensity and HR Intensity columns.

    Layout: score (bold) on top, a small stacked zone bar (half-width)
    underneath, and a rich content-slot tooltip listing each non-empty zone
    with its swatch, name, and percentage.  Workouts with no meaningful
    meters (score is None) render as a single "—" with no bar.

    skip_indices — zone indices to exclude entirely from the tooltip (e.g.
    Rest, or Rest + No HR).  They are also excluded from the percentage
    denominator so the zone percentages sum to 100%.
    """
    if score is None or bin_meters is None:
        hd.text("—", font_size="medium", font_color="neutral-400")
        return

    total = sum(m for idx, m in enumerate(bin_meters) if idx not in set(skip_indices))

    with hd.tooltip() as tt:
        with hd.box(slot=tt.content_slot, padding=0.4, gap=0.2, min_width=12):
            for idx, name in enumerate(zone_names):
                with hd.scope(f"{idx} {name}"):
                    if idx in skip_indices:
                        continue
                    meters = bin_meters[idx] if idx < len(bin_meters) else 0
                    if meters <= 0:
                        continue
                    pct = (meters / total) if total > 0 else 0.0
                    if pct < 0.005:
                        continue
                    color_str = zone_colors[idx][0 if is_dark else 1]
                    with hd.hbox(gap=0.4, align="center"):
                        hd.image(
                            src=swatch_svg(color_str, size=10, radius=2),
                            width=0.6,
                            height=0.6,
                        )
                        hd.text(name, font_size="x-small", min_width=6)
                        hd.text(
                            f"{pct:.0%}",
                            font_size="x-small",
                            font_weight="bold",
                        )
        with hd.box(align="start", gap=0.2):
            hd.text(f"{score:.0f}", font_size="medium", font_weight="bold")
            if bar_uri:
                hd.image(
                    src=bar_uri,
                    width=_INTENSITY_BAR_WIDTH,
                    height=_INTENSITY_BAR_HEIGHT,
                )


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def intervals_page(ctx, global_state, excluded_seasons=(), machine="All") -> None:
    """Top-level HyperDiv component for the Interval Workouts tab."""

    print("syncing from intervals page")
    result = sync_from_context(ctx)
    if result is None:
        hd.box(padding=2, min_height="80vh")
        return
    _workouts_dict, all_workouts = result

    # Apply global filters
    if excluded_seasons:
        all_workouts = [
            w
            for w in all_workouts
            if get_season(w.get("date", "")) not in set(excluded_seasons)
        ]
    if machine != "All":
        all_workouts = [w for w in all_workouts if w.get("type") == machine]

    profile = get_profile_from_context(ctx) or {}
    max_hr, _max_hr_estimated = resolve_max_hr(profile, all_workouts)

    ref_sbs = get_reference_sbs(all_workouts)
    thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
    all_intervals = _enrich_workouts(all_workouts, thresholds, max_hr)

    if not all_intervals:
        with hd.box(padding=4, align="center"):
            hd.text("No interval workouts found.", font_color="neutral-500")
        return

    state = hd.state(
        active_cells=tuple(),  # tuple[str] — "col,row" keys of selected cells
        active_bins=tuple(),  # tuple[int] — pace bin indices (1–6) for OR filter
        active_hr_bins=tuple(),  # tuple[int] — HR bin indices (1–5) for OR filter
        structure_filter=None,  # str | None — filter table to this structure key
    )

    is_dark = hd.theme().is_dark

    # ── Interval-specific column definitions (capture state for filter button) ──
    def _render_structure_cell(w):
        is_active = state.structure_filter == w["_structure_key"]
        btn = hd.button(
            w["_structure_key"],
            variant="text",
            size="medium",
            padding=(0, 0),
            font_weight="semibold" if is_active else "normal",
            font_color="primary-500" if is_active else "neutral-700",
        )
        if btn.clicked:
            state.structure_filter = None if is_active else w["_structure_key"]

    def _render_stimulus_cell(w):
        s = w.get("_stimulus", "")
        if s and s != "—":
            hd.text(
                s, font_size="x-small", font_color="neutral-500", font_style="italic"
            )

    def _render_pace_intensity_cell(w):
        _render_intensity_cell(
            score=w.get("_pace_score"),
            bar_uri=w.get("_bar_uri"),
            bin_meters=w.get("_bin_meters"),
            zone_names=BIN_NAMES,
            zone_colors=BIN_COLORS,
            is_dark=is_dark,
            skip_indices=(0,),
        )

    def _render_hr_intensity_cell(w):
        _render_intensity_cell(
            score=w.get("_hr_score"),
            bar_uri=w.get("_hr_bar_uri"),
            bin_meters=w.get("_hr_bin_meters"),
            zone_names=HR_ZONE_NAMES,
            zone_colors=HR_ZONE_COLORS,
            is_dark=is_dark,
            skip_indices=(0, 6),
        )

    def _render_quality_cell(w):
        q = w.get("_quality")
        if q is None:
            hd.text("—", font_size="small", font_color="neutral-400")
            return
        row = w.get("_grid_row", 0)
        col = w.get("_grid_col", 0)
        info = (
            _STIMULUS_INFO[row][col]
            if 0 <= row < _N_ROWS and 0 <= col < _N_COLS
            else None
        )
        expected_score = (info or {}).get("expected_score", 0)
        expected_work_s = (info or {}).get("expected_work_s", 0)
        stim_name = (info or {}).get("name", "this stimulus")
        score = w.get("_pace_score")
        work_s = (w.get("time") or 0) / 10.0
        style = _QUALITY_STYLE[q]
        if q == "Low":
            explanation = (
                f"Pace intensity {score:.0f} is below the ~{expected_score:.0f} "
                f"expected of a {stim_name} session — the session wasn't hard "
                f"enough to count as a quality dose."
            )
        elif q == "Medium":
            explanation = (
                f"Pace intensity {score:.0f} meets or exceeds the ~"
                f"{expected_score:.0f} expected for {stim_name}, but total "
                f"work time ({format_time(int(work_s * 10))}) is below the "
                f"~{format_time(expected_work_s * 10)} dose typical of a full "
                f"quality session."
            )
        else:  # High
            explanation = (
                f"Pace intensity {score:.0f} meets or exceeds the ~"
                f"{expected_score:.0f} expected for {stim_name}, and total "
                f"work time ({format_time(int(work_s * 10))}) clears the "
                f"~{format_time(expected_work_s * 10)} dose expected of a full "
                f"quality session."
            )
        with hd.tooltip() as tt:
            with hd.box(slot=tt.content_slot, padding=0.4, gap=0.25, max_width=22):
                hd.text(
                    f"{q} quality",
                    font_size="small",
                    font_weight="bold",
                )
                hd.text(explanation, font_size="x-small")
            with hd.box(
                padding=(0.15, 0.5),
                border_radius="medium",
                background_color=style["bg"],
                align="center",
                justify="center",
            ):
                hd.text(
                    style["label"],
                    font_size="x-small",
                    font_weight="bold",
                    font_color=_always_white(is_dark),
                )

    interval_columns = [
        ColumnDef(
            "date",
            "Date",
            "10rem",
            render_value=lambda w: fmt_date(w.get("date", "")),
            sort_value=lambda w: w.get("date", ""),
        ),
        ColumnDef(
            "reps",
            "Reps",
            "4rem",
            render_value=lambda w: str(w["_reps"]) if w.get("_reps") else "—",
            sort_value=lambda w: w.get("_reps") or 0,
        ),
        ColumnDef(
            "structure",
            "Structure",
            "minmax(8rem,1fr)",
            render_cell=_render_structure_cell,
            sortable=False,
        ),
        ColumnDef(
            "stimulus",
            "Stimulus",
            "10rem",
            render_cell=_render_stimulus_cell,
            sortable=False,
        ),
        ColumnDef(
            "pace_intensity",
            "Pace Intensity",
            "8rem",
            render_cell=_render_pace_intensity_cell,
            sort_value=lambda w: w.get("_pace_score")
            if w.get("_pace_score") is not None
            else -1.0,
        ),
        ColumnDef(
            "hr_intensity",
            "HR Intensity",
            "8rem",
            render_cell=_render_hr_intensity_cell,
            sort_value=lambda w: w.get("_hr_score")
            if w.get("_hr_score") is not None
            else -1.0,
        ),
        ColumnDef(
            "quality",
            "Quality",
            "6rem",
            render_cell=_render_quality_cell,
            sort_value=lambda w: _QUALITY_ORDER.get(w.get("_quality"), -1),
        ),
        ColumnDef(
            "work",
            "Work",
            "6rem",
            render_value=lambda w: fmt_distance(w.get("distance")),
            sort_value=lambda w: w.get("distance") or 0,
        ),
        ColumnDef(
            "split",
            "Avg Split",
            "7rem",
            render_value=lambda w: fmt_split(w["_work_pace"])
            if w.get("_work_pace")
            else "—",
            sort_value=lambda w: w.get("_work_pace") or float("inf"),
            default_asc=True,
        ),
        ColumnDef(
            "time",
            "Time",
            "7rem",
            render_value=lambda w: w.get("time_formatted")
            or (format_time(w["time"]) if w.get("time") else "—"),
            sort_value=lambda w: w.get("time") or 0,
        ),
        ColumnDef(
            "spm",
            "SPM",
            "4rem",
            render_value=lambda w: f"{w['_work_spm']:.0f}"
            if w.get("_work_spm")
            else "—",
            sort_value=lambda w: w.get("_work_spm") or 0,
        ),
        COL_LINK,
    ]

    with hd.box(align="center", gap=1, padding=2, min_height="80vh"):
        with hd.box(gap=0.2, align="center"):
            hd.h1(f"Review {your(ctx)} Fondest Interval Sessions")
            global_filter_ui(global_state, ctx)

        with hd.box(width="100%", gap=2):
            # Pre-compute non-cell filters so the grid counts stay in sync with
            # the active pace-zone, HR-zone, and structure filters.
            pre_filtered = _filter_disjunctive(
                all_intervals,
                set(state.active_bins),
                pace_bin_passes,
                "_bin_meters",
            )
            pre_filtered = _filter_disjunctive(
                pre_filtered,
                set(state.active_hr_bins),
                hr_bin_passes,
                "_hr_bin_meters",
            )
            if state.structure_filter:
                pre_filtered = [
                    r
                    for r in pre_filtered
                    if r["_structure_key"] == state.structure_filter
                ]

            # 2D grid browser — counts reflect pace/HR/structure filters
            _grid_browser(pre_filtered, state)

            # Dual labelled legends (Pace + HR) with rich chip tooltips
            _zone_filter_legends(state, max_hr)

            # Apply cell filter on top of already pace/HR/structure filtered
            active_cells = frozenset(state.active_cells)
            filtered = _filter_by_cells(pre_filtered, active_cells)

            total_filtered = len(filtered)

            # Structure filter chip
            if state.structure_filter:
                with hd.hbox(
                    gap=0.75, wrap="wrap", align="center", padding=(0.5, 0, 0, 0)
                ):
                    hd.text("Structure:", font_size="small", font_color="neutral-500")
                    if hd.button(
                        f"{state.structure_filter}  ×",
                        variant="primary",
                        size="small",
                    ).clicked:
                        state.structure_filter = None

            with hd.hbox(align="center", justify="space-between", padding=(0.5, 0)):
                hd.text(
                    f"{total_filtered} workout{'s' if total_filtered != 1 else ''}",
                    font_size="small",
                    font_color="neutral-500",
                )

            # Scope-reset trick: changing filter_key forces WorkoutTable's
            # internal hd.state to reinitialise, resetting page to 0.
            filter_key = (
                f"{state.structure_filter or 'all'}"
                f"_{sorted(list(state.active_bins))}"
                f"_{sorted(list(state.active_hr_bins))}"
                f"_{sorted(list(state.active_cells))}"
            )
            with hd.scope(filter_key):
                WorkoutTable(
                    filtered,
                    interval_columns,
                    rows_per_page=_ROWS_PER_PAGE,
                    default_sort_col="date",
                )
