"""
components/intervals_page.py

Interval Workouts tab — 2D grid browser + sortable data table.

Browser
-------
A 2D grid replaces the old structure checklist. Both physiologically critical
dimensions of interval training are shown simultaneously:

  X axis (6 cols) — representative work-interval duration (median interval):
      ≤30"  ·  30"–2'  ·  2'–4'  ·  4'–8'  ·  8'–20'  ·  20'+

  Y axis (5 rows) — work:rest time ratio (total work / total rest):
      Continuous (≥10:1)  ·  Short (3–10:1)  ·  Balanced (≈1:1)
      Long (1:2–4)        ·  Very Long (<1:4)

Grid is rendered column-first so all cells in a column share the same width,
avoiding the misalignment that flex row-first causes. Each populated cell is
a full-width button with an hd.tooltip showing a physiological description.
Button variant encodes average Z3 intensity of sessions in that cell:
    neutral  → mostly aerobic (avg Z3 < 25 %)
    warning  → moderate (25–50 % Z3)
    danger   → hard sessions (≥ 50 % Z3)
    primary  → selected (overrides intensity colour)

Empty cells show the stimulus label muted — a training coverage map.
Multi-cell selection = OR union. The pace-zone legend below the grid acts as
a conjunctive (AND) filter on the table — select multiple zones to find
workouts that touched all of them.

Grid placement rules:
- Work duration: median work-interval duration in seconds (all non-rest ivs)
- Work:rest ratio: sum(work times) / sum(rest_time fields + rest-type iv times)
  (internally stored as rest/work; rows represent work:rest as displayed)

Table
-----
WorkoutTable (CSS Grid) with interval-specific ColumnDef objects.
Sortable headers (▲/▼), default sort: date descending.
Columns: Date · Reps · Structure (rep-stripped) · Stimulus · Zones bar
         · Work dist · Avg Split · Time · SPM · HR · ↗

Pace-zone filter (legend below grid): conjunctive AND across selected bins.
A workout appears only when it has > 0 metres in every selected pace zone.

Structure filter: clicking any Structure cell in the table sets a filter
restricting the table to workouts with that same structure key.  Clicking
the same cell again, or the ×-chip above the table, clears it.
"""

from __future__ import annotations

import statistics

import hyperdiv as hd

from services.rowing_utils import INTERVAL_WORKOUT_TYPES, get_season
from components.concept2_sync import concept2_sync
from services.interval_utils import (
    avg_workpace_tenths,
    avg_work_spm,
    interval_structure_key,
)
from services.volume_bins import (
    BIN_NAMES,
    BIN_COLORS,
    Z3_BINS,
    get_reference_sbs,
    compute_bin_thresholds,
    workout_bin_meters,
    bin_bar_svg,
    swatch_svg,
)
from services.formatters import fmt_date, fmt_distance, fmt_hr, fmt_split, format_time
from components.hyperdiv_extensions import aligned_button
from components.workout_table import WorkoutTable, ColumnDef, COL_LINK


# ---------------------------------------------------------------------------
# Colour helpers
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
    ('≤ 30"', 0, 30),
    ("30\" – 2'", 30, 120),
    ("2' – 4'", 120, 240),
    ("4' – 8'", 240, 480),
    ("8' – 20'", 480, 1200),
    ("20'+", 1200, float("inf")),
]
_N_COLS = len(_DUR_COLS)

# Work:rest ratio row boundaries + display label (ratio = rest/work internally)
_RATIO_ROWS = [
    ("Continuous", "≥ 10 : 1", 0.0, 0.10),
    ("Short rest", "3–10 : 1", 0.10, 0.50),
    ("Balanced", "≈ 1 : 1", 0.50, 1.50),
    ("Long rest", "1 : 2–4", 1.50, 4.00),
    ("Very Long", "< 1 : 4", 4.00, float("inf")),
]
_N_ROWS = len(_RATIO_ROWS)

# Physiological stimulus labels [row_idx][col_idx]
# Reviewed for accuracy: work duration is median interval length; rest:work
# is total rest / total work.  Cells marked "—" are rare or don't occur in
# practice (e.g. ≤30" continuous doesn't really exist as programmed rowing).
_STIMULI = [
    # Continuous (<0.10)
    ["—", "Fartlek", "Sustained", "Steady state", "Aerobic base", "LSD"],
    # Short (0.10–0.50)
    [
        "—",
        "Lactic capacity",
        "VO₂max stress",
        "Threshold+",
        "Threshold accum.",
        "Tempo",
    ],
    # Balanced (0.50–1.50)
    [
        "Sprint reps",
        "Anaerobic endur.",
        "VO₂max (2k)",
        "VO₂max (5k)",
        "Lact. threshold",
        "Aerobic blocks",
    ],
    # Long (1.50–4.00)
    ["Speed power", "Speed endur.", "VO₂max quality", "5k quality", "Extensive", "—"],
    # Very Long (>4.00)
    ["Max sprint", "Alactic/PCr", "Race pieces", "Race sims", "—", "—"],
]

# Tooltip text for each cell [row_idx][col_idx].  Empty string = no tooltip.
# Aim: enough physiological context to be useful, plus a note on classification
# fuzziness where applicable.
_TOOLTIPS = [
    # Continuous (work:rest ≥ 10:1)
    [
        "",  # ≤30" continuous — n/a
        "Fartlek: Continuous aerobic effort with internal pace variations. "
        "Pace changes are brief enough that lactate never significantly accumulates. "
        "Develops aerobic efficiency without hard recovery demands.  "
        "E.g. 10× 1' easy / 1' mod with no stop.",
        "Sustained: 2–4 min continuous work blocks with minimal transition time. "
        "Primarily mitochondrial and fat-oxidation adaptation.  "
        "E.g. 3× 3' at aerobic pace.",
        "Steady state: Classic moderate-duration continuous aerobic work. "
        "Develops cardiac stroke volume and capillary density; "
        "typically below the first ventilatory threshold.  "
        "E.g. 4× 5' at rate 18–20.",
        "Aerobic base: Long continuous aerobic effort at conversational intensity. "
        "The cornerstone of base-building phases.  "
        "E.g. 2× 15' / 1' rest, or a single 20'.",
        "LSD (Long Slow Distance): Extended low-intensity rowing. "
        "Develops economy, mental endurance, and fat utilisation.  "
        "E.g. single 60' or 2× 30'.",
    ],
    # Short (work:rest 3–10:1)
    [
        "",  # ≤30" — n/a
        "Lactic capacity: Short high-intensity intervals with very brief recovery. "
        "Lactate accumulates rep-to-rep; builds lactate tolerance and buffer capacity.  "
        'E.g. 10× 1\'/12"r, 15× 30"/8"r.',
        "VO₂max stress: 2–4 min intervals with short rest keeps heart rate "
        "continuously elevated near VO₂max — high total VO₂max stimulus per session.  "
        "E.g. 6× 3'/1'r, 8× 2'/40\"r.",
        "Threshold+: Work near or slightly above LT2 with incomplete recovery. "
        "Lactate accumulates gradually across reps.  "
        "E.g. 4× 6'/2'r, 4× 8'/2'r.",
        "Threshold accumulation: Extended work near threshold with short rest. "
        "Accumulates substantial threshold time per session; "
        "late-rep quality may decline.  "
        "E.g. 3× 12'/4'r, 4× 10'/3'r.",
        "Tempo: Long work intervals with brief recovery at moderate-to-threshold intensity. "
        "Essentially fractioned tempo work.  "
        "E.g. 2× 20'/5'r.",
    ],
    # Balanced (work:rest ≈ 1:1)
    [
        "Sprint repeats: Very short efforts with roughly equal recovery. "
        "Develops repeated power output and ATP-PCr resynthesis under partial recovery.  "
        'E.g. 10× 20"/20"r at max power.',
        "Anaerobic endurance: Sub-2-minute efforts with near-equal rest. "
        "Rep begins before lactate clears; trains lactic acid tolerance.  "
        "E.g. 8× 1'/1'r, 10× 45\"/45\"r.",
        "VO₂max (2k prep): THE canonical VO₂max interval. "
        "Work reaches VO₂max; equal rest allows partial recovery while keeping HR elevated.  "
        "E.g. 6× 2'/2'r, 8× 2'/2'r.",
        "VO₂max (5k prep): Longer VO₂max intervals with adequate recovery. "
        "Extends time at VO₂max per rep while maintaining quality — the 'Norwegian' format.  "
        "E.g. 4× 4'/4'r, 5× 1000m/4'r.",
        "Lactate threshold: Long intervals with roughly equal recovery at controlled intensity. "
        "Accumulates extended time at threshold pace with manageable fatigue.  "
        "E.g. 3× 10'/10'r, 2× 15'/15'r.",
        "Aerobic blocks: Very long aerobic intervals with roughly equal recovery. "
        "Uncommon in periodized programs; may arise in low-intensity adaptation phases.  "
        "E.g. 2× 30'/30'r, 3× 20'/20'r.",
    ],
    # Long (work:rest 1:2–4)
    [
        "Speed power: Very short maximal efforts with generous recovery. "
        "Targets the PCr system and peak power output.  "
        'E.g. 8× 15"/45"r, 6× 20"/1\'r.',
        "Speed endurance: Sub-2-minute high-intensity intervals with substantial recovery. "
        "Develops ability to repeat near-maximal efforts with partial PCr recovery.  "
        "E.g. 5× 1'/3'r, 6× 500m/3'r.",
        "VO₂max quality: High-quality VO₂max intervals with full recovery. "
        "Prioritises peak power per rep over total VO₂max stress; "
        "preferred for in-season maintenance.  "
        "E.g. 4× 2'/8'r, 4× 500m/6'r.",
        "5k quality: Extended race-pace efforts with generous recovery. "
        "Develops race-pace efficiency and neuromuscular patterns.  "
        "E.g. 3× 5'/15'r, 4× 1000m/8'r.",
        "Extensive: Long work intervals with even longer rest. "
        "May represent coach-prescribed race pieces or block training with full recovery.  "
        "E.g. 3× 10'/20'r.",
        "",  # 20'+ with long rest — n/a
    ],
    # Very Long (work:rest < 1:4)
    [
        "Max sprint: True maximum-effort sprints with full PCr recovery (work:rest < 1:4). "
        "Each rep should be maximally explosive.  "
        "E.g. 6× 10\"/2'r, 8× 15\"/3'r.",
        "Alactic/PCr: Near-maximal efforts with near-complete PCr resynthesis between reps. "
        "Develops repeated sprint capacity and peak neuromuscular power.  "
        "E.g. 6× 1'/5'r, 8× 500m/4'r at near-max effort.",
        "Race pieces: 2–4 min near-maximal efforts with very long recovery (>4× work time). "
        "Full quality on every rep; used for race-pace familiarisation and power development.  "
        "E.g. 4× 2'/16'r, 3× 3'/15'r at race pace.",
        "Race simulation: 4–8 min efforts at race intensity with very long recovery (>4× work time). "
        "Full recovery ensures each rep is maximally race-representative; develops pace confidence.  "
        "E.g. 3× 5'/25'r, 2× 6'/30'r at 2k race pace.",
        "",  # 8'–20' with very long rest — n/a
        "",  # 20'+ with very long rest — n/a
    ],
]

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


def _enrich_workouts(workouts: list[dict], thresholds) -> list[dict]:
    """
    Filter to interval workout types (excluding single-rep sessions) and
    attach computed fields:

      _bin_meters    list[float]    Per-bin metre counts (index 0 = Rest)
      _bar_uri       str            Data-URI SVG stacked pace-zone bar
      _z3            float          Fraction of work metres in Z3 (bins 1–3)
      _structure_key str            Rep-stripped structure label, e.g. "500m / 2'r"
      _reps          int            Number of work intervals
      _work_pace     float | None   Avg work pace (tenths/500m)
      _work_spm      float | None   Work-weighted avg stroke rate
      _grid_col      int            Column index in 2D grid
      _grid_row      int            Row index in 2D grid
      _stimulus      str            Physiological stimulus label from grid
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
        r["_structure_key"] = interval_structure_key(r, compact=True)
        r["_reps"] = reps
        r["_work_pace"] = avg_workpace_tenths(r)
        r["_work_spm"] = avg_work_spm(r)
        col, row = _compute_grid_placement(r)
        r["_grid_col"] = col
        r["_grid_row"] = row
        r["_stimulus"] = _STIMULI[row][col]
        result.append(r)
    result.sort(key=lambda x: x.get("date", ""), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Filtering & sorting
# ---------------------------------------------------------------------------


def _bin_passes(bm: list, bin_idx: int) -> bool:
    """
    Return True if a workout's bin-meter vector passes the threshold for
    the given bin index to count as an active zone in that workout.

    Thresholds (fraction of total work metres, bins 1–6):
      1 Fast        ≥ 5%  of work
      2 2k          ≥ 10% of work
      3 5k          ≥ 15% of work
      4 Threshold   ≥ 25% of work
      5 Fast Aero   (fast+slow aero) ≥ 50% of work
      6 Slow Aero   slow aero > 30% of work  AND  (fast+slow aero) > 50% of work
    """
    work_total = sum(bm[1:])
    if not work_total:
        return False
    if bin_idx == 1:
        return bm[1] / work_total >= 0.05
    if bin_idx == 2:
        return bm[2] / work_total >= 0.10
    if bin_idx == 3:
        return bm[3] / work_total >= 0.15
    if bin_idx == 4:
        return bm[4] / work_total >= 0.25
    if bin_idx == 5:
        return (bm[5] + bm[6]) / work_total >= 0.50
    if bin_idx == 6:
        return (bm[6] / work_total > 0.30) and ((bm[5] + bm[6]) / work_total > 0.50)
    return False


def _filter_by_bins(workouts: list[dict], active_bins: set[int]) -> list[dict]:
    """
    Conjunctive (AND) filter: keep workouts that pass the threshold for EVERY
    selected bin index.  Empty selection → all workouts returned.
    """
    if not active_bins:
        return workouts
    return [
        r
        for r in workouts
        if all(_bin_passes(r["_bin_meters"], b) for b in active_bins)
    ]


def _zones_tooltip(bm: list) -> str:
    """
    Build a short breakdown string for the zones bar tooltip.
    Shows each bin's percentage of total work metres; omits bins at 0%.
    E.g. "Fast 8%  2k 15%  Threshold 22%  Fast Aero 55%"
    """
    work_total = sum(bm[1:])
    if not work_total:
        return "No work metres recorded"
    parts = []
    for i, name in enumerate(BIN_NAMES[1:], start=1):
        pct = bm[i] / work_total
        if pct >= 0.005:
            parts.append(f"{name} {pct:.0%}")
    return "  ".join(parts) if parts else "—"


def _filter_by_cells(workouts: list[dict], cells: frozenset[str]) -> list[dict]:
    if not cells:
        return workouts
    return [r for r in workouts if f"{r['_grid_col']},{r['_grid_row']}" in cells]


# ---------------------------------------------------------------------------
# Grid browser
# ---------------------------------------------------------------------------


def _cell_key(col: int, row: int) -> str:
    return f"{col},{row}"


def _cell_variant(avg_z3: float) -> str:
    """
    Return a Shoelace button variant based on Z3 intensity fraction.
    Selection state is communicated via outline=True/False rather than
    a colour change, so the border always reflects intensity.
    """
    if avg_z3 >= 0.50:
        return "danger"
    if avg_z3 >= 0.25:
        return "warning"
    return "neutral"


def _zone_filter_legend(state) -> None:
    """
    Clickable pace-zone legend that acts as a conjunctive (AND) filter.

    Each pace zone (Fast … Slow Aerobic) can be toggled on/off.  With one or
    more zones active the table shows only workouts that have at least some
    metres in EVERY selected zone simultaneously.

    Thresholds (see _bin_passes): Fast ≥5%, 2k ≥10%, 5k ≥15%, Threshold ≥25%,
    Fast Aero (fast+slow)≥50%, Slow Aero slow>30% AND combined>50%.

    Active buttons fill with the zone's own colour (via base_style override on
    ::part(base)); inactive buttons show a subtle outline with the swatch inside.
    Active state in state.active_bins (tuple[int]).
    """
    is_dark = hd.theme().is_dark
    active_bins: set[int] = set(state.active_bins)

    with hd.hbox(
        gap=0.75, align="center", padding=(2.5, 0), wrap="wrap", justify="center"
    ):
        # hd.text("Filter by pace zone:", font_size="small", font_color="neutral-500")
        for i, name in enumerate(BIN_NAMES[1:], start=1):
            with hd.scope(name):
                color_str = BIN_COLORS[i][0 if is_dark else 1]
                color_rgba = _parse_rgba(color_str)
                is_active = i in active_bins

                if is_active:
                    # Filled button using the zone colour as background.
                    with hd.button(
                        size="small",
                        padding=(0.2, 0.6, 0.2, 0.6),
                        border="none",
                        base_style=hd.style(
                            background_color=color_rgba,
                        ),
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
                                font_color="neutral-900",
                            )
                else:
                    # Outline button — subtle border so inactive filters recede.
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
                            hd.text(
                                name,
                                font_size="small",
                                font_color="neutral-600",
                            )

                if btn.clicked:
                    sel = set(state.active_bins)
                    if is_active:
                        sel.discard(i)
                    else:
                        sel.add(i)
                    state.active_bins = tuple(sorted(sel))


def _grid_browser(zone_workouts: list[dict], state) -> None:
    """
    Render the 2D work-duration × rest:work grid.

    Layout is column-first: all cells in a column share one parent box, so
    column widths are naturally uniform regardless of cell content length.

    Each populated cell is a full-width button wrapped in an hd.tooltip.
    Clicking a cell toggles it in state.active_cells (multi-select = OR union).
    """
    # Pre-compute per-cell data
    cell_workouts: dict[str, list[dict]] = {}
    for r in zone_workouts:
        k = _cell_key(r["_grid_col"], r["_grid_row"])
        cell_workouts.setdefault(k, []).append(r)

    active_cells: frozenset[str] = frozenset(state.active_cells)

    with hd.box(margin_top=1):
        # Axis label row (small arrows above the grid)
        with hd.hbox(gap=0, align="center", padding=(0, 0, 0.25, 0)):
            # Corner area — vertical axis label pointing downward
            with hd.box(
                width=_ROW_LABEL_W,
                align="start",
                justify="end",
                padding=(0, 0.5, 0, 0),
            ):
                pass
            # Horizontal axis label pointing right — spans the 6 data columns
            with hd.hbox(gap=0.4, align="center", grow=True):
                hd.text(
                    "Work duration",
                    font_size="small",
                    font_color="neutral-400",
                    font_style="italic",
                )
                hd.icon("arrow-right", font_size="small", font_color="neutral-400")

        # Main grid — column-first layout
        with hd.hbox(gap=0, align="stretch"):
            # Row-labels column
            with hd.box(width=_ROW_LABEL_W, border_right="1px solid neutral-200"):
                with hd.hbox(
                    gap=0.4,
                    align="center",
                    justify="end",
                    height=_HEADER_H,
                    padding=(0.4, 0.6),
                ):
                    hd.text(
                        "Work : rest",
                        font_size="small",
                        font_color="neutral-400",
                        font_style="italic",
                    )
                    hd.icon("arrow-down", font_size="small", font_color="neutral-400")

                for ri, (row_label, ratio_range, _, _) in enumerate(_RATIO_ROWS):
                    with hd.scope(f"rl_{ri}"):
                        with hd.box(
                            height=_CELL_H,
                            padding=(0.4, 0.6),
                            align="end",
                            justify="center",
                            border_top="1px solid neutral-200",
                            gap=0.1,
                        ):
                            hd.text(
                                row_label,
                                font_size="small",
                                font_weight="bold",
                                font_color="neutral-600",
                                # text_align="right",
                            )
                            hd.text(
                                ratio_range,
                                font_size="small",
                                font_color="neutral-400",
                                # text_align="right",
                            )

            # Data columns
            for ci, (col_label, _, _) in enumerate(_DUR_COLS):
                with hd.scope(f"col_{ci}"):
                    with hd.box(grow=True, border_left="1px solid neutral-200"):
                        # Column header
                        with hd.box(
                            height=_HEADER_H,
                            padding=(0.3, 0.3),
                            align="center",
                            justify="center",
                            border_bottom="1px solid neutral-200",
                        ):
                            hd.text(
                                col_label,
                                font_size="small",
                                font_weight="bold",
                                font_color="neutral-600",
                                text_align="center",
                            )

                        # Row cells
                        for ri in range(_N_ROWS):
                            k = _cell_key(ci, ri)
                            workouts_in_cell = cell_workouts.get(k, [])
                            count = len(workouts_in_cell)
                            stimulus = _STIMULI[ri][ci]
                            tooltip_text = _TOOLTIPS[ri][ci]
                            is_sel = k in active_cells
                            has_data = count > 0

                            avg_z3 = (
                                sum(r["_z3"] for r in workouts_in_cell) / count
                                if has_data
                                else 0.0
                            )

                            with hd.scope(f"r{ri}"):
                                display_label = stimulus if stimulus != "—" else "Other"
                                if has_data:
                                    tip = (
                                        tooltip_text if tooltip_text else display_label
                                    )
                                    # Thin border wrapper; button fills width+height.
                                    with hd.box(
                                        border_top="1px solid neutral-200",
                                        padding=0,
                                        line_height="normal",
                                        align="center",
                                        justify="center",
                                        height="100%",
                                        width="100%",
                                        # overflow="hidden",
                                    ):
                                        with hd.tooltip(tip, width="100%", distance=20):
                                            with aligned_button(
                                                variant=_cell_variant(avg_z3),
                                                outline=not is_sel,
                                                width="100%",
                                                height=_CELL_H,
                                                padding=(0, 0.2),
                                                line_height="normal",
                                                align="center",
                                            ) as cell_btn:
                                                hd.text(
                                                    str(count),
                                                    font_size="medium",
                                                    font_weight="bold",
                                                )
                                                hd.text(
                                                    display_label,
                                                    font_size="x-small",
                                                    text_align="center",
                                                )
                                    if cell_btn.clicked:
                                        sel = set(state.active_cells)
                                        if is_sel:
                                            sel.discard(k)
                                        else:
                                            sel.add(k)
                                        state.active_cells = tuple(sorted(sel))
                                else:
                                    # Empty cell — muted coverage map, same
                                    # size as data cells via explicit height.
                                    with hd.box(
                                        border_top="1px solid neutral-200",
                                        padding=(0, 0.2),
                                        align="center",
                                        justify="center",
                                        background_color="neutral-0",
                                        height="100%",
                                        width="100%",
                                    ):
                                        if tooltip_text:
                                            with hd.tooltip(tooltip_text, distance=20):
                                                hd.text(
                                                    display_label,
                                                    font_size="x-small",
                                                    font_color="neutral-200",
                                                    text_align="center",
                                                )
                                        else:
                                            hd.text(
                                                display_label,
                                                font_size="x-small",
                                                font_color="neutral-200",
                                                text_align="center",
                                            )


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def intervals_page(client, user_id: str, excluded_seasons=(), machine="All") -> None:
    """Top-level HyperDiv component for the Interval Workouts tab."""

    result = concept2_sync(client)
    if result is None:
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

    ref_sbs = get_reference_sbs(all_workouts)
    thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
    all_intervals = _enrich_workouts(all_workouts, thresholds)

    if not all_intervals:
        with hd.box(padding=4, align="center"):
            hd.text("No interval workouts found.", font_color="neutral-500")
        return

    state = hd.state(
        active_cells=tuple(),  # tuple[str] — "col,row" keys of selected cells
        active_bins=tuple(),  # tuple[int] — pace bin indices (1–6) for AND filter
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

    def _render_zones_cell(w):
        with hd.box(align="start"):
            with hd.tooltip(_zones_tooltip(w["_bin_meters"])):
                hd.image(src=w["_bar_uri"], width=10, height=0.75)

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
            "zones",
            "Intensity zones",
            "10rem",
            render_cell=_render_zones_cell,
            sort_value=lambda w: w.get("_z3", 0.0),
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
        ColumnDef(
            "hr",
            "HR",
            "6rem",
            render_value=lambda w: fmt_hr(w.get("heart_rate")),
            sort_value=lambda w: (w.get("heart_rate") or {}).get("average") or 0,
        ),
        COL_LINK,
    ]

    with hd.box(align="center", gap=1, padding=(2, 2, 2, 2)):
        hd.h1("Review Your Fondest Interval Sessions")

        with hd.box():
            # Pre-compute non-cell filters so the grid counts stay in sync with
            # the active pace-zone and structure filters.
            pre_filtered = _filter_by_bins(all_intervals, set(state.active_bins))
            if state.structure_filter:
                pre_filtered = [
                    r
                    for r in pre_filtered
                    if r["_structure_key"] == state.structure_filter
                ]

            # 2D grid browser — counts reflect pace-zone + structure filters
            _grid_browser(pre_filtered, state)

            hd.divider()

            # Pace-zone legend / conjunctive filter
            _zone_filter_legend(state)

            # Apply cell filter on top of already pace/structure filtered workouts
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
                f"_{sorted(list(state.active_cells))}"
            )
            with hd.scope(filter_key):
                WorkoutTable(
                    filtered,
                    interval_columns,
                    rows_per_page=_ROWS_PER_PAGE,
                    default_sort_col="date",
                )
