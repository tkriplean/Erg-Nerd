"""
components/interval_tab.py

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
Custom row renderer (hd.data_table lacks SVG cells). All sortable column
headers show ▲/▼. Default sort: date descending.
Columns: Date · Reps · Structure (rep-stripped) · Stimulus · Zones bar
         · Work dist · Avg Split · Time · SPM · HR

Pace-zone filter (legend below grid): conjunctive AND across selected bins.
A workout appears only when it has > 0 metres in every selected pace zone.
"""

from __future__ import annotations

import statistics

import hyperdiv as hd

from services.concept2 import get_client, load_local_workouts
from services.rowing_utils import INTERVAL_WORKOUT_TYPES
from services.interval_utils import (
    avg_work_pace_tenths,
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
from components.ranked_formatters import (
    _fmt_date,
    _fmt_distance,
    _fmt_hr,
    fmt_split,
)


# ---------------------------------------------------------------------------
# Grid axis definitions
# ---------------------------------------------------------------------------

# Work duration column boundaries (seconds)
_DUR_COLS = [
    ('≤30"', 0, 30),
    ("30\"–2'", 30, 120),
    ("2'–4'", 120, 240),
    ("4'–8'", 240, 480),
    ("8'–20'", 480, 1200),
    ("20'+", 1200, float("inf")),
]
_N_COLS = len(_DUR_COLS)

# Work:rest ratio row boundaries + display label (ratio = rest/work internally)
_RATIO_ROWS = [
    ("Continuous", "≥ 10 : 1", 0.0, 0.10),
    ("Short", "3–10 : 1", 0.10, 0.50),
    ("Balanced", "≈ 1 : 1", 0.50, 1.50),
    ("Long", "1 : 2–4", 1.50, 4.00),
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
    ["—", "Lactic cap.", "VO₂max stress", "Threshold+", "Threshold accum.", "Tempo"],
    # Balanced (0.50–1.50)
    [
        "Sprint reps",
        "Anaerobic end.",
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
        "Lactic capacity: Short high-intensity intervals with brief recovery. "
        "Lactate accumulates rep-to-rep; builds lactate tolerance and buffer capacity.  "
        'E.g. 10× 1\'/30"r, 12× 30"/20"r.',
        "VO₂max stress: 2–4 min intervals with short rest keeps heart rate "
        "continuously elevated near VO₂max — high total VO₂max stimulus per session.  "
        "E.g. 6× 3'/1'r, 8× 2'/1'r.",
        "Threshold+: Work near or slightly above LT2 with incomplete recovery. "
        "Lactate accumulates gradually across reps.  "
        "E.g. 4× 6'/2'r, 5× 5'/90\"r.",
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
        "Aerobic blocks: Extended aerobic intervals with substantial recovery. "
        "Fletcher/block training overlap is common.  "
        "E.g. 2× 30'/30'r.",
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
        "Race pieces: 2–4 min race-pace efforts with very long recovery. "
        "Full quality on every rep; used for pace familiarisation.  "
        "E.g. 3× 2000m/15'r, 5× 1'/10'r.",
        "Race simulation: 4–8 min efforts at or near competition intensity with very long recovery. "
        "Develops race-specific fitness and pace judgement.  "
        "E.g. 2× 5k/20'r, 3× 2000m/20'r.",
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
        r["_work_pace"] = avg_work_pace_tenths(r)
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


def _sort_workouts(workouts: list[dict], col: str, asc: bool) -> list[dict]:
    key_fns = {
        "date": lambda r: r.get("date", ""),
        "reps": lambda r: r.get("_reps") or 0,
        "work": lambda r: r.get("distance") or 0,
        "split": lambda r: r.get("_work_pace") or float("inf"),
        "zones": lambda r: r.get("_z3", 0.0),
        "time": lambda r: r.get("time") or 0,
        "spm": lambda r: r.get("_work_spm") or 0.0,
        "hr": lambda r: (r.get("heart_rate") or {}).get("average") or 0,
    }
    return sorted(
        workouts,
        key=key_fns.get(col, key_fns["date"]),
        reverse=not asc,
    )


# ---------------------------------------------------------------------------
# Grid browser
# ---------------------------------------------------------------------------


def _cell_key(col: int, row: int) -> str:
    return f"{col},{row}"


def _cell_variant(avg_z3: float, is_sel: bool) -> str:
    if is_sel:
        return "primary"
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
    Swatches use hd.image (data-URI SVG) so raw rgba() values stay out of
    HyperDiv's colour prop system.  Active state in state.active_bins (tuple[int]).
    """
    is_dark = hd.theme().mode == "dark"
    active_bins: set[int] = set(state.active_bins)

    with hd.hbox(gap=1, align="center", padding=(0.5, 0), wrap="wrap"):
        hd.text("Filter by pace zone:", font_size="small", font_color="neutral-500")
        for i, name in enumerate(BIN_NAMES[1:], start=1):
            with hd.scope(name):
                color = BIN_COLORS[i][0 if is_dark else 1]
                is_active = i in active_bins
                with hd.hbox(gap=0.5, align="center"):
                    hd.image(
                        src=swatch_svg(color, size=12, radius=2),
                        width=0.75,
                        height=0.75,
                    )
                    btn = hd.button(
                        name,
                        variant="primary" if is_active else "neutral",
                        size="small",
                        outline=not is_active,
                    )
                if btn.clicked:
                    sel = set(state.active_bins)
                    if is_active:
                        sel.discard(i)
                    else:
                        sel.add(i)
                    state.active_bins = tuple(sorted(sel))
                    state.page = 0


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
                with hd.hbox(gap=0.4, align="center"):
                    hd.icon("arrow-down", font_size="small", font_color="neutral-400")
                    hd.text(
                        "Work:rest",
                        font_size="x-small",
                        font_color="neutral-400",
                        font_style="italic",
                    )
            # Horizontal axis label pointing right — spans the 6 data columns
            with hd.hbox(gap=0.4, align="center", grow=True):
                hd.text(
                    "Work duration",
                    font_size="x-small",
                    font_color="neutral-400",
                    font_style="italic",
                )
                hd.icon("arrow-right", font_size="small", font_color="neutral-400")

        # Main grid — column-first layout
        with hd.hbox(gap=0, align="stretch"):
            # Row-labels column
            with hd.box(width=_ROW_LABEL_W, border_right="1px solid neutral-200"):
                # Spacer aligning with column headers
                hd.box(height=_HEADER_H)
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
                                font_size="x-small",
                                font_weight="bold",
                                font_color="neutral-600",
                                # text_align="right",
                            )
                            hd.text(
                                ratio_range,
                                font_size="x-small",
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
                                font_size="x-small",
                                font_weight="bold",
                                font_color="neutral-500",
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
                                        line_height="normal"
                                        # overflow="hidden",
                                    ):
                                        with hd.tooltip(tip, width="100%"):
                                            with hd.button(
                                                variant=_cell_variant(avg_z3, is_sel),
                                                outline=not is_sel,
                                                width="100%",
                                                height=_CELL_H,
                                                # padding=(0, 0.2),
                                                # # align="center",
                                            ) as cell_btn:
                                                with hd.box(
                                                    gap=0.15,
                                                    align="center",
                                                    justify="center",
                                                ):
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
                                        state.page = 0
                                else:
                                    # Empty cell — muted coverage map, same
                                    # size as data cells via explicit height.
                                    with hd.box(
                                        height=_CELL_H,
                                        border_top="1px solid neutral-200",
                                        padding=(0.25, 0.2),
                                        align="center",
                                        justify="center",
                                        background_color="neutral-0",
                                    ):
                                        if stimulus != "—":
                                            if tooltip_text:
                                                with hd.tooltip(tooltip_text):
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

    # Active-filter summary chips
    if active_cells:
        with hd.hbox(gap=0.75, wrap="wrap", align="center", padding=(0.75, 0, 0, 0)):
            n = len(active_cells)
            hd.text(
                f"Filtered to {n} cell{'s' if n != 1 else ''}:",
                font_size="small",
                font_color="neutral-500",
            )
            for k in sorted(active_cells):
                ci, ri = (int(x) for x in k.split(","))
                label = f"{_DUR_COLS[ci][0]} / {_RATIO_ROWS[ri][0]}"
                with hd.scope(f"rm_{k}"):
                    if hd.button(
                        f"{label}  ×", variant="primary", size="small"
                    ).clicked:
                        state.active_cells = tuple(
                            c for c in state.active_cells if c != k
                        )
                        state.page = 0
            if hd.button("Clear all", variant="neutral", size="small").clicked:
                state.active_cells = tuple()
                state.page = 0


# ---------------------------------------------------------------------------
# Sortable table
# ---------------------------------------------------------------------------


def _sort_header(label: str, col_id: str, width, state) -> None:
    """Render a sortable column header button."""
    is_active = state.sort_col == col_id
    indicator = (" ▲" if state.sort_asc else " ▼") if is_active else ""
    btn = hd.button(
        f"{label}{indicator}",
        variant="text",
        size="small",
        font_weight="bold" if is_active else "normal",
        font_color="neutral-600" if is_active else "neutral-500",
        width=width,
    )
    if btn.clicked:
        if state.sort_col == col_id:
            state.sort_asc = not state.sort_asc
        else:
            state.sort_col = col_id
            # Sensible default directions
            state.sort_asc = col_id == "split"  # fastest split = ascending
        state.page = 0


def _interval_table(workouts: list[dict], state) -> tuple[int, int]:
    """
    Render a custom row-based table with sortable column headers.
    Returns (total_rows, total_pages).
    """
    sorted_wk = _sort_workouts(workouts, state.sort_col, state.sort_asc)
    total = len(sorted_wk)
    total_pages = max(1, (total + _ROWS_PER_PAGE - 1) // _ROWS_PER_PAGE)
    page_rows = sorted_wk[
        state.page * _ROWS_PER_PAGE : (state.page + 1) * _ROWS_PER_PAGE
    ]

    if not page_rows:
        with hd.box(padding=3, align="center"):
            hd.text("No workouts match the selected filters.", font_color="neutral-500")
        return total, total_pages

    # Header row
    with hd.hbox(
        gap=1,
        padding=(0.25, 1),
        border_bottom="1px solid neutral-200",
        align="center",
    ):
        _sort_header("Date", "date", 9, state)
        _sort_header("Reps", "reps", 4, state)
        hd.text(
            "Structure",
            grow=True,
            font_size="small",
            font_weight="bold",
            font_color="neutral-500",
        )
        hd.text(
            "Stimulus",
            width=10,
            font_size="small",
            font_weight="bold",
            font_color="neutral-500",
        )
        _sort_header("Zones", "zones", 10, state)
        _sort_header("Work", "work", 6, state)
        _sort_header("Avg Split", "split", 7, state)
        _sort_header("Time", "time", 7, state)
        _sort_header("SPM", "spm", 4, state)
        _sort_header("HR", "hr", 6, state)

    # Data rows
    for i, r in enumerate(page_rows):
        with hd.scope(i):
            with hd.hbox(
                gap=1,
                padding=(0.5, 1),
                align="center",
                background_color="neutral-50" if i % 2 else "neutral-0",
            ):
                hd.text(
                    _fmt_date(r.get("date", "")),
                    width=9,
                    font_size="small",
                    font_color="neutral-700",
                )
                hd.text(
                    str(r["_reps"]) if r["_reps"] else "—",
                    width=4,
                    font_size="small",
                    font_color="neutral-500",
                )
                with hd.box(grow=True):
                    # _structure_key strips the leading "N × " rep count
                    hd.text(r["_structure_key"], font_size="small")
                # Stimulus label (from grid classification)
                stimulus = r.get("_stimulus", "")
                with hd.box(width=10):
                    if stimulus and stimulus != "—":
                        hd.text(
                            stimulus,
                            font_size="x-small",
                            font_color="neutral-500",
                            font_style="italic",
                        )
                with hd.box(width=10, align="start"):
                    with hd.tooltip(_zones_tooltip(r["_bin_meters"])):
                        hd.image(src=r["_bar_uri"], width=10, height=0.75)
                hd.text(
                    _fmt_distance(r.get("distance")),
                    width=6,
                    font_size="small",
                    font_color="neutral-700",
                )
                hd.text(
                    fmt_split(r["_work_pace"]) if r["_work_pace"] else "—",
                    width=7,
                    font_size="small",
                )
                hd.text(
                    r.get("time_formatted", "—"),
                    width=7,
                    font_size="small",
                    font_color="neutral-500",
                )
                spm = r.get("_work_spm")
                hd.text(
                    f"{spm:.0f}" if spm else "—",
                    width=4,
                    font_size="small",
                    font_color="neutral-500",
                )
                hd.text(
                    _fmt_hr(r.get("heart_rate")),
                    width=6,
                    font_size="small",
                    font_color="neutral-500",
                )

    return total, total_pages


def _pagination(state, total: int, total_pages: int) -> None:
    if total_pages <= 1:
        return
    with hd.hbox(gap=1, align="center", padding=(1, 0)):
        if state.page > 0:
            if hd.button("← Prev", variant="neutral", size="small").clicked:
                state.page -= 1
        hd.text(
            f"Page {state.page + 1} of {total_pages}  ({total} workouts)",
            font_size="small",
            font_color="neutral-500",
        )
        if state.page < total_pages - 1:
            if hd.button("Next →", variant="neutral", size="small").clicked:
                state.page += 1


# ---------------------------------------------------------------------------
# Tab entry point
# ---------------------------------------------------------------------------


def interval_tab() -> None:
    """Top-level HyperDiv component for the Interval Workouts tab."""

    task = hd.task()

    def _fetch():
        client = get_client()
        if client is None:
            local = load_local_workouts()
            workouts = list(local.values())
            workouts.sort(key=lambda r: r.get("date", ""), reverse=True)
            return workouts
        return client.get_all_results()

    task.run(_fetch)

    if task.running:
        with hd.box(align="center", padding=4):
            hd.spinner()
        return

    if task.error:
        hd.alert(
            f"Error loading workouts: {task.error}",
            variant="danger",
            opened=True,
        )
        return

    all_workouts = task.result or []

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
        sort_col="date",
        sort_asc=False,
        page=0,
    )

    with hd.box(padding=(2, 2, 2, 2)):
        hd.h3(f"Interval Workouts  ({len(all_intervals)})")

        # 2D grid browser (always shows all intervals, unaffected by bin filter)
        _grid_browser(all_intervals, state)

        hd.divider()

        # Pace-zone legend / conjunctive filter
        _zone_filter_legend(state)

        # Apply grid-cell selection, then pace-bin filter
        active_cells = frozenset(state.active_cells)
        cell_filtered = _filter_by_cells(all_intervals, active_cells)
        filtered = _filter_by_bins(cell_filtered, set(state.active_bins))

        # Clamp page if filter changed total
        total_filtered = len(filtered)
        total_pages = max(1, (total_filtered + _ROWS_PER_PAGE - 1) // _ROWS_PER_PAGE)
        if state.page >= total_pages:
            state.page = max(0, total_pages - 1)

        with hd.hbox(align="center", justify="space-between", padding=(0.5, 0)):
            hd.text(
                f"{total_filtered} workout{'s' if total_filtered != 1 else ''}",
                font_size="small",
                font_color="neutral-500",
            )
            if active_cells or state.active_bins:
                hd.text(
                    f"(filtered from {len(all_intervals)} total)",
                    font_size="small",
                    font_color="neutral-400",
                )

        total, total_pages = _interval_table(filtered, state)
        _pagination(state, total, total_pages)
