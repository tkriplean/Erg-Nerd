"""
components/intervals_page.py

Interval Workouts tab — 2D grid browser + persistent info panel + sortable
data table.

Browser
-------
A 2D grid shows both physiologically critical dimensions of interval
training at once:

  X axis (6 cols) — representative work-interval duration (median interval):
      ≤30"  ·  30"–1'  ·  1'–3'  ·  3'–8'  ·  8'–20'  ·  20'+
      (60s and 180s splits anchor short / medium / long HIIT per
      Buchheit & Laursen 2013.)

  Y axis (5 rows) — work:rest time ratio (total work / total rest):
      Continuous (<9% rest)  ·  Short (9–33% rest)  ·  Balanced (33–60% rest)
      Long (60–80% rest)     ·  Very Long (>80% rest)

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
The Power Spread + HR Spread + Quality legend stack is shared with the
Sessions page; see :mod:`components.spread_quality_legends` for the chip
rendering and tooltip content.  The three legends combine **disjunctively
(OR) within** themselves and **conjunctively across** with each other and
with the grid-cell selection.

The HR legend's chip row is hidden when the user has no max HR resolvable;
a short note points to the Profile page.  The Quality legend always renders
(workouts with no quality score are excluded when any chip is selected).

Table
-----
WorkoutTable (CSS Grid) with interval-specific ColumnDef objects.
Sortable headers (▲/▼), default sort: date descending.

Columns: Date · Reps · Structure (rep-stripped) · Stimulus ·
         Power Spread (score + bar) · HR Spread (score + bar) ·
         Quality (Low/Medium/High/Ultra pill) ·
         Work dist · Avg Split · Time · SPM · ↗

The Power/HR Spread columns each show a 0–100 weighted-average score
above a small stacked zone bar; hovering either cell opens a rich
content-slot tooltip with per-zone swatch + name + percentage.  Weights
come from services (POWER_SPREAD_WEIGHTS / HR_SPREAD_WEIGHTS).  Sort
is descending by score; workouts with no meaningful meters (or no HR)
render as "—" and sort last.

Time-aware thresholds: each row's Power Spread score is computed
against the rower's reference watts **on that row's own date** (via
services/reference_watts.py), so a 2010 session is graded against 2010
fitness, not today's.  The Quality column uses the same time-aware
reference watts — see :mod:`services.workout_enrichment`.

The Quality column scores every work interval against the date-matched
reference watts via :mod:`services.workout_quality`.  Each split accrues
"quality energy" weighted by ``split_watts / category_reference_watts``
and discounted (for interval sessions) by the preceding rest ratio;
per-category energy is normalized by a reference target and summed.  The
scalar is bucketed **Low** (<0.5) / **Medium** (<0.75) / **High** (≥0.75).
Workouts whose reference-watts index is missing anchors show "—"; sort is
by raw score so ordering is continuous within each bucket.

Structure filter: clicking any Structure cell sets a filter restricting
the table to workouts with that same structure key.  Clicking the same
cell again, or the ×-chip above the table, clears it.
"""

from __future__ import annotations

import statistics

import hyperdiv as hd

from services.rowing_utils import INTERVAL_WORKOUT_TYPES, get_season
from components.concept2_sync import get_all_workouts
from components.reference_watts_loader import reference_watts_loader
from components.app_context import your
from services.interval_utils import (
    avg_workpace_tenths,
    avg_work_spm,
    get_rep_count,
    interval_structure_key,
)
from services.threshold_cache import make_thresholds_resolver
from services.workout_enrichment import attach_spread_and_quality
from services.volume_bins import (
    BIN_COLORS,
    Z3_BINS,
    power_bin_passes,
)
from services.heartrate_utils import (
    hr_bin_passes,
    resolve_max_hr,
)
from services.formatters import fmt_date, fmt_distance, fmt_split, format_time
from components.hyperdiv_extensions import aligned_button, grid_box
from components.workout_table import (
    COL_HR_SPREAD,
    COL_LINK,
    COL_POWER_SPREAD,
    COL_QUALITY,
    ColumnDef,
    WorkoutTable,
    always_white,
)
from components.app_context import get_profile
from components.shared_ui import global_filter_ui
from components.spread_quality_legends import spread_quality_legends

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

# Work duration column boundaries (seconds).  60s splits short HIIT from
# medium HIIT (Buchheit & Laursen 2013); 180s splits medium from long.
_DUR_COLS = [
    ("≤ 30s", 0, 30),
    ("30s – 1min", 30, 60),
    ("1 – 3min", 60, 180),
    ("3 – 8min", 180, 480),
    ("8 – 20min", 480, 1200),
    ("20min+", 1200, float("inf")),
]
_N_COLS = len(_DUR_COLS)

# Work:rest ratio row boundaries + display label (ratio = rest/work internally).
# Aligned with Solli et al. 2024 zone work:rest guidance (Z3 ≈ 6:1, Z4 ≈ 3:1,
# Z5 ≈ 2:1, Z6/7 ≈ 1:1 to 1:10).
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
    # Row 0 — Continuous (rest < 9% of cycle, ≈ no formal rest).  Pace-intensity
    # is intentionally low here — the point is volume at easy/steady intensity,
    # not hardness — so expected_score = 0 (or 10 for fartlek) and the Quality
    # column grades purely on work-time accumulation.
    [
        None,  # ≤30" continuous — n/a
        None,  # 30–60" continuous — uncommon; surges this short are noise inside an aerobic piece
        {
            "name": "Fartlek",
            "description": (
                "Continuous aerobic effort with internal pace surges of 1–3 "
                "minutes. The surges are short enough that blood lactate does "
                "not meaningfully accumulate, so the piece remains fundamentally "
                "aerobic."
            ),
            "example": "10× 1' easy / 1' moderate, continuous.",
            "expected_score": 10,
            "expected_work_s": 1500,
        },
        {
            "name": "Sustained aerobic",
            "description": (
                "3–8 minute continuous aerobic blocks with minimal transition. "
                "Targets mitochondrial density and fat oxidation."
            ),
            "example": "3× 5' at aerobic pace, continuous.",
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
    # Row 1 — Short rest (rest 9–33% of cycle, work:rest ≈ 2–10 : 1).
    # Aligns with Solli et al. 2024 Z3 (MIT, 6–4 : 1) and the lower end of Z4.
    [
        None,  # ≤30" with short rest — physiologically uncommon (alactic territory needs longer rest)
        {
            "name": "Lactate production",
            "description": (
                "Short high-intensity reps (30–60s) with very brief recovery. "
                "Work-to-rest accumulates glycolytic demand across reps and "
                'trains tolerance of low muscle pH — the classical "lactate '
                'production" stimulus.'
            ),
            "example": '12× 30" / 8"r, 10× 45" / 12"r.',
            "expected_score": 65,
            "expected_work_s": 300,
        },
        {
            "name": "VO₂max (short rest)",
            "description": (
                "1–3 minute reps with short recovery. The incomplete recovery "
                "keeps oxygen uptake high across reps, producing a large "
                "VO₂max stimulus per session."
            ),
            "example": '6× 2\' / 30"r, 8× 90" / 30"r.',
            "expected_score": 60,
            "expected_work_s": 480,
        },
        {
            "name": "Supra-threshold",
            "description": (
                "3–8 minute reps at or slightly above the second lactate "
                "threshold with incomplete recovery. Lactate accumulates "
                "gradually across reps."
            ),
            "example": "4× 6' / 2'r, 5× 4' / 1'r.",
            "expected_score": 40,
            "expected_work_s": 600,
        },
        {
            "name": "Threshold accumulation",
            "description": (
                "Long reps near threshold with short recovery. Accumulates "
                "substantial time at threshold; late reps may drift as fatigue "
                "builds."
            ),
            "example": "3× 12' / 4'r, 4× 10' / 3'r.",
            "expected_score": 30,
            "expected_work_s": 1500,
        },
        {
            "name": "Tempo",
            "description": (
                "Long reps with brief recovery at moderate-to-threshold "
                "intensity — effectively fractioned tempo work."
            ),
            "example": "2× 20' / 5'r.",
            "expected_score": 25,
            "expected_work_s": 1800,
        },
    ],
    # Row 2 — Balanced (rest 33–60% of cycle, work:rest ≈ 1 : 1).
    # Aligns with Solli et al. 2024 Z4–Z5 (HIT, 3–1 : 1) — the canonical
    # VO₂max-stimulus zone in periodised endurance programmes.
    [
        {
            "name": "Short HIIT (Tabata-like)",
            "description": (
                "Very short reps (≤30s) with roughly equal recovery — the "
                "classic Tabata-style stimulus. Repeated near-maximal output "
                "keeps oxygen demand pinned at VO₂max while glycolytic load "
                "accumulates."
            ),
            "example": '8× 20" / 10"r (Tabata), 10× 30" / 30"r.',
            "expected_score": 80,
            "expected_work_s": 120,
        },
        {
            "name": "Anaerobic capacity",
            "description": (
                "30–60s near-maximal reps with near-equal rest. Each rep "
                "starts before lactate has fully cleared; trains tolerance of "
                "accumulating lactate."
            ),
            "example": '10× 45" / 45"r, 8× 60" / 45"r.',
            "expected_score": 75,
            "expected_work_s": 360,
        },
        {
            "name": "VO₂max (medium intervals)",
            "description": (
                "1–3 minute reps with roughly equal recovery — short HIIT in "
                "Buchheit & Laursen's framing. Work reaches VO₂max; equal rest "
                "allows partial recovery while keeping oxygen uptake elevated "
                "across reps."
            ),
            "example": "6× 2' / 2'r, 8× 90\" / 90\"r.",
            "expected_score": 65,
            "expected_work_s": 480,
        },
        {
            "name": "VO₂max (long intervals)",
            "description": (
                "3–8 minute reps with adequate recovery — long HIIT in "
                "Buchheit & Laursen's framing. Extends time at VO₂max per "
                "rep while keeping quality high."
            ),
            "example": "5× 4' / 4'r, 4× 5' / 4'r, 4× 1000m / 4'r.",
            "expected_score": 55,
            "expected_work_s": 600,
        },
        {
            "name": "Lactate threshold (1:1)",
            "description": (
                "Long reps with roughly equal recovery at controlled "
                "intensity. Accumulates extended threshold time with "
                "manageable fatigue."
            ),
            "example": "3× 10' / 10'r, 2× 15' / 15'r.",
            "expected_score": 35,
            "expected_work_s": 900,
        },
        None,  # 20'+ work with 1:1 rest is just two efforts — not a programmed stimulus
    ],
    # Row 3 — Long rest (rest 60–80% of cycle, work:rest ≈ 1 : 2–4).
    # Aligns with Solli et al. 2024 Z5–Z6 territory and Buchheit & Laursen's
    # repeated-sprint / short-HIIT-with-long-rest formats.
    [
        {
            "name": "Repeated sprints (RST)",
            "description": (
                "Very short maximal efforts with substantial recovery — the "
                "canonical RST format. Targets the phosphocreatine system and "
                "peak neuromuscular power; recovery is long enough for partial "
                "PCr resynthesis between reps."
            ),
            "example": '8× 15" / 45"r, 6× 20" / 60"r.',
            "expected_score": 90,
            "expected_work_s": 60,
        },
        {
            "name": "Speed endurance",
            "description": (
                "30–60s high-intensity reps with substantial recovery. "
                "Develops the ability to repeat near-maximal efforts with "
                "partial PCr recovery."
            ),
            "example": '5× 45" / 2\'r, 6× 30" / 90"r.',
            "expected_score": 70,
            "expected_work_s": 240,
        },
        {
            "name": "VO₂max (recovery-rich)",
            "description": (
                "1–3 minute high-quality VO₂max reps with near-full "
                "recovery. Prioritises peak power per rep over total VO₂max "
                "dose — useful for in-season maintenance."
            ),
            "example": "6× 2' / 4'r, 4× 90\" / 3'r, 4× 500m / 3'r.",
            "expected_score": 70,
            "expected_work_s": 300,
        },
        None,  # 3–8' work with 1:2–4 rest is uncommon
        None,  # 8–20' work with very long rest — n/a
        None,  # 20'+ with long rest — n/a
    ],
    # Row 4 — Very Long rest (rest > 80% of cycle, work:rest < 1 : 4).
    # Aligns with Solli et al. 2024 Z6/7 all-out and the SIT (Sprint Interval
    # Training) literature (Wingate-style, ~30s all-out / ~4 min recovery).
    [
        {
            "name": "Maximal sprints (alactic)",
            "description": (
                "Maximum-effort sprints with full PCr recovery. Every rep "
                "should be maximally explosive; the energy comes almost "
                "entirely from the phosphocreatine system."
            ),
            "example": "6× 10\" / 2'r, 8× 15\" / 3'r.",
            "expected_score": 95,
            "expected_work_s": 60,
        },
        {
            "name": "Sprint interval training (SIT)",
            "description": (
                "30–60s all-out efforts with very long (>4× work) recovery — "
                "the Wingate-style SIT protocol. Each rep is supramaximal and "
                "rest is long enough to keep quality high across the set."
            ),
            "example": "4× 30\" / 4'r (Wingate), 4× 45\" / 5'r.",
            "expected_score": 90,
            "expected_work_s": 180,
        },
        None,  # 1–3' work with very long rest — race-pace pieces, but uncommon as a programmed stimulus
        None,  # 3–8' work with very long rest — n/a
        None,  # 8–20' work with very long rest — n/a
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

# Per-workout cache for the heavy enrichment fields (especially the bin-meters
# pass).  Each render rebuilds the filtered workout list from scratch, so we
# can't memoize at the list level — but the per-workout enrichment is a pure
# function of (workout content, max_hr), so cache by stable workout id.
#
# Toggling a UI chip or grid cell triggers a full page re-render: with this
# cache, every workout is a hit and enrichment drops from ~370ms to ~1ms.
# Bound the cache so a long session with many imports can't grow without
# limit; on overflow we drop everything (simple, no LRU bookkeeping).
_workout_enrich_cache: dict[tuple, dict] = {}
_WORKOUT_ENRICH_CACHE_MAX = 5000


def _enrich_workouts(
    workouts: list[dict],
    thresholds_for,
    ref_watts_for,
    max_hr: int | None,
) -> list[dict]:
    """
    Filter to interval workout types (excluding single-rep sessions) and
    attach computed fields used by the grid, info panel, and table.

    ``thresholds_for(workout) -> dict | None`` resolves a workout to its
    own-date power thresholds — time-aware, so a 2010 row is classified
    against 2010 fitness (see services/reference_watts.py).
    ``ref_watts_for(workout) -> dict | None`` resolves the same date's
    reference-watts dict, used by the Quality metric.

    Fields attached:

      _bin_meters         list[float]    Per-power-bin meter counts (index 0 = Rest)
      _bar_uri            str            Data-URI SVG stacked power-zone bar
      _z3                 float          Fraction of work meters in Z3 (grid colour)
      _power_spread_score float | None   0–100 weighted power spread
      _hr_bin_meters      list[float] | None  Per-HR-bin meter counts, or None
                                              when max_hr is unknown
      _hr_bar_uri         str | None     Data-URI SVG stacked HR-zone bar, or None
      _hr_spread_score    float | None   0–100 weighted HR spread
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
        # Skip single-rep sessions (e.g. 1×500m / 3'r).  Keep workouts with
        # multiple intervals that share no rest — they form legitimate multi-block
        # sessions even though every rest_time == 0.
        reps = get_rep_count(r)
        if reps == 1:
            continue

        wid = r.get("id")
        cache_key = (wid, max_hr) if wid is not None else None
        if cache_key is not None and cache_key in _workout_enrich_cache:
            result.append(_workout_enrich_cache[cache_key])
            continue

        r = dict(r)  # shallow copy

        attach_spread_and_quality(
            [r],
            workouts,
            max_hr,
            thresholds_for=thresholds_for,
            ref_watts_for=ref_watts_for,
        )
        bm = r["_bin_meters"]
        work_total = sum(bm[1:])
        r["_z3"] = sum(bm[i] for i in Z3_BINS) / work_total if work_total else 0.0

        r["_structure_key"] = interval_structure_key(r, compact=True)
        r["_reps"] = reps
        r["_work_pace"] = avg_workpace_tenths(r)
        r["_work_spm"] = avg_work_spm(r)
        col, row = _compute_grid_placement(r)
        r["_grid_col"] = col
        r["_grid_row"] = row
        r["_stimulus"] = _cell_name(row, col)

        if cache_key is not None:
            if len(_workout_enrich_cache) >= _WORKOUT_ENRICH_CACHE_MAX:
                _workout_enrich_cache.clear()
            _workout_enrich_cache[cache_key] = r
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
    test (power_bin_passes / hr_bin_passes).  Workouts with meters_key == None
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
                    white_token = always_white(is_dark)
                    black_token = always_white(not is_dark)

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
                                if count > 0:
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
                                else:
                                    with hd.box(
                                        width="100%",
                                        height=_CELL_H,
                                        line_height="normal",
                                        align="center",
                                        background_color=cell_bg_rgba,
                                        border="none",
                                        padding_bottom=0,
                                        padding_top=2,
                                    ) as cell_btn:
                                        hd.text(
                                            display_label,
                                            font_size="x-small",
                                            text_align="center",
                                            font_color=white_token,
                                        )

                                with hd.tooltip() as tt:
                                    _grid_cell_tooltip_content(tt, ri, ci)
                                    hd.icon(
                                        "question-circle",
                                        font_color=white_token,
                                        padding_right=0.3,
                                        padding_bottom=0.3,
                                    )


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def intervals_page() -> None:
    """Top-level HyperDiv component for the Interval Workouts tab."""

    print("syncing from intervals page")
    result = get_all_workouts()
    if result is None:
        hd.box(padding=2, min_height="80vh")
        return
    _workouts_dict, all_workouts = result

    profile = get_profile() or {}
    max_hr, _max_hr_estimated = resolve_max_hr(profile, all_workouts)

    # Time-aware thresholds: block on the reference-watts loader so the
    # first-time index build shows a progress bar rather than spawning a
    # synchronous build inside _enrich_workouts.
    if not reference_watts_loader(all_workouts):
        return

    # Per-date cache so we don't recompute thresholds when many workouts
    # share a date.  Both the ref-watts dict (for Quality) and the derived
    # bin thresholds are cached.
    _thresholds_for, _ref_watts_for = make_thresholds_resolver(all_workouts)

    all_intervals = _enrich_workouts(
        all_workouts, _thresholds_for, _ref_watts_for, max_hr
    )

    if not all_intervals:
        with hd.box(padding=4, align="center"):
            hd.text("No interval workouts found.", font_color="neutral-500")
        return

    state = hd.state(
        active_cells=tuple(),  # tuple[str] — "col,row" keys of selected cells
        active_bins=tuple(),  # tuple[int] — pace bin indices (1–6) for OR filter
        active_hr_bins=tuple(),  # tuple[int] — HR bin indices (1–5) for OR filter
        active_quality=tuple(),  # tuple[str] — quality buckets (Low/Medium/High/Ultra) for OR filter
        structure_filter=None,  # str | None — filter table to this structure key
    )

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
        COL_POWER_SPREAD,
        COL_HR_SPREAD,
        COL_QUALITY,
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
            hd.h1(f"Review {your()} Fondest Interval Sessions")
            global_filter_ui()

        with hd.box(width="100%", gap=2):
            # Pre-compute non-cell filters so the grid counts stay in sync with
            # the active pace-zone, HR-zone, and structure filters.
            pre_filtered = _filter_disjunctive(
                all_intervals,
                set(state.active_bins),
                power_bin_passes,
                "_bin_meters",
            )
            pre_filtered = _filter_disjunctive(
                pre_filtered,
                set(state.active_hr_bins),
                hr_bin_passes,
                "_hr_bin_meters",
            )
            if state.active_quality:
                sel_quality = set(state.active_quality)
                pre_filtered = [
                    r for r in pre_filtered if r.get("_quality") in sel_quality
                ]
            if state.structure_filter:
                pre_filtered = [
                    r
                    for r in pre_filtered
                    if r["_structure_key"] == state.structure_filter
                ]

            # 2D grid browser — counts reflect pace/HR/structure filters
            _grid_browser(pre_filtered, state)

            # Dual labelled legends (Pace + HR) with rich chip tooltips.
            # ref_watts dates to the most recent workout so the (?) tooltip
            # under "Power Spread" reflects current fitness.
            today_ref_watts = (
                _ref_watts_for(all_intervals[0]) if all_intervals else None
            )
            spread_quality_legends(state, max_hr, today_ref_watts)

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
                f"_{sorted(list(state.active_quality))}"
                f"_{sorted(list(state.active_cells))}"
            )
            WorkoutTable(
                filtered,
                interval_columns,
                rows_per_page=_ROWS_PER_PAGE,
                default_sort_col="date",
            )
