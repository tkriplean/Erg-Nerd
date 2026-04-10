# Interval Workouts Tab

## Overview

The Intervals tab is a dedicated browser for Concept2 interval workouts. Interval workouts are excluded from the Rankings tab (pace is not comparable to steady-state efforts) and appear only as dots in the Sessions chart, so this tab is their primary home.

The tab has two main regions:

1. **2D grid browser** — maps every interval workout onto a physiologically meaningful grid so training coverage gaps are immediately visible.
2. **Sortable data table** — lists individual workouts with full detail; filtered by the grid and by the pace-zone legend.

---

## Implicated Files

| File | Role |
|---|---|
| `components/intervals_page.py` | All HyperDiv UI: grid, legend, table, entry point `intervals_page()` |
| `services/interval_utils.py` | Pure-Python helpers: structure label generation, pace computation, SPM weighting |
| `services/volume_bins.py` | Pace-zone infrastructure: `workout_bin_meters()`, `bin_bar_svg()`, `BIN_NAMES`, `BIN_COLORS`, `Z1/Z2/Z3_BINS`, `get_reference_sbs()`, `compute_bin_thresholds()` |
| `services/rowing_utils.py` | `INTERVAL_WORKOUT_TYPES` — the set of `workout_type` strings that qualify as interval sessions |
| `services/concept2.py` | Data loading: `get_client()`, `load_local_workouts()` |
| `components/ranked_formatters.py` | Shared formatters: `_fmt_date`, `_fmt_distance`, `_fmt_hr`, `fmt_split` |
| `app.py` | Tab declaration (`hd.tab("Intervals")`) and dispatch (`elif tabs.active == "Intervals": intervals_page()`) |

---

## Data Flow

```
concept2.get_all_results() / load_local_workouts()
    ↓
_enrich_workouts(all_workouts, thresholds)
    → filters to INTERVAL_WORKOUT_TYPES
    → calls workout_bin_meters() per workout   (from volume_bins.py)
    → calls bin_bar_svg()                      (from volume_bins.py)
    → calls interval_structure_label/key()     (from interval_utils.py)
    → calls avg_work_pace_tenths()             (from interval_utils.py)
    → calls avg_work_spm()                     (from interval_utils.py)
    → calls _compute_grid_placement()          (local)
    → attaches _bin_meters, _bar_uri, _z1/_z2/_z3,
               _structure, _structure_key, _reps,
               _work_pace, _work_spm,
               _grid_col, _grid_row, _stimulus
    ↓
_grid_browser(all_intervals, state)    — always uses full enriched list
_zone_filter_legend(state)             — clickable pace-zone AND filter
_filter_by_cells(all_intervals, ...)   — grid-cell OR selection
_filter_by_bins(cell_filtered, ...)    — pace-zone AND filter
_interval_table(filtered, state)       — sorted, paginated rows
```

The thresholds used for pace-zone classification come from `compute_bin_thresholds(get_reference_sbs(all_workouts), all_workouts)`, identical to how the Volume tab classifies workouts. Pace zones are relative to the user's recent personal bests, not absolute splits.

---

## Pace Zones

Pace zones are defined in `services/volume_bins.py`. There are 7 bins (index 0 = Rest):

| Index | Name | Description |
|---|---|---|
| 0 | Rest | Interval rest distance only |
| 1 | Fast | Above 1k race pace |
| 2 | 2k | 1k–2k pace range |
| 3 | 5k | 2k–5k pace range |
| 4 | Threshold | 5k–60min pace range (LT1–LT2) |
| 5 | Fast Aerobic | 60min–marathon pace range |
| 6 | Slow Aerobic | Below marathon pace |

The three-zone model (`Z3_BINS = {1,2,3}`, `Z2_BINS = {4}`, `Z1_BINS = {5,6}`) is imported but used only to pre-compute `_z1/_z2/_z3` fractions on each enriched workout for sorting purposes (Zones column sorts by `_z3`).

---

## 2D Grid Browser

The grid is the primary navigation tool. It answers "what kinds of interval sessions have I done, and are there gaps?"

### Axes

**X axis — Work duration (6 columns)**
Representative duration = median work-interval duration in seconds across all non-rest intervals in the session.

| Column | Range |
|---|---|
| ≤30" | 0–30 s |
| 30"–2' | 30–120 s |
| 2'–4' | 120–240 s |
| 4'–8' | 240–480 s |
| 8'–20' | 480–1200 s |
| 20'+ | > 1200 s |

**Y axis — Work:rest ratio (5 rows)**
Computed as `total_rest_s / total_work_s` internally; displayed as work:rest.

| Row | Display | Internal ratio (rest/work) |
|---|---|---|
| Continuous | ≥ 10 : 1 | < 0.10 |
| Short | 3–10 : 1 | 0.10–0.50 |
| Balanced | ≈ 1 : 1 | 0.50–1.50 |
| Long | 1 : 2–4 | 1.50–4.00 |
| Very Long | < 1 : 4 | > 4.00 |

Rest is summed from two sources: the `rest_time` field attached to each work interval (C2 API convention — rest is always stored on the interval preceding it), plus the `time` field of any intervals whose `type == "rest"`.

If a workout has no `workout.intervals` data, the full session duration is used and it is placed in the Continuous row.

### Physiological Stimulus Labels

`_STIMULI[row][col]` — a 5×6 table of short labels (e.g. "VO₂max (2k)", "Speed endur."). Cells marked `"—"` represent rare or physiologically implausible combinations.

`_TOOLTIPS[row][col]` — a matching 5×6 table of longer descriptions, each ending with canonical example workouts (`E.g. 6× 2'/2'r`). Shown on hover for both data cells and empty (coverage-map) cells.

### Cell Rendering

- **Populated cells**: rendered as `hd.button` inside `hd.tooltip`. Button variant encodes average Z3 intensity of sessions in that cell: `neutral` (< 25% Z3), `warning` (25–50%), `danger` (≥ 50%), `primary` (selected).
- **Empty cells**: muted label text — the coverage-map function. Still shows tooltip on hover.
- **Selection**: clicking a cell toggles it in `state.active_cells` (tuple of `"col,row"` strings). Multi-cell selection is OR: a workout appears in the table if it belongs to any selected cell.

### Layout

The grid is rendered **column-first** (one `hd.box` per data column, containing all row cells in that column). This ensures all cells in a column share the same width regardless of label length, avoiding the misalignment that a row-first flex layout causes. The row-label column is a fixed-width sidebar.

---

## Pace-Zone Filter Legend

Below the grid, a row of 6 toggle buttons — one per pace zone — acts as a **conjunctive (AND)** filter on the table. Selecting multiple zones shows only workouts that touched all of them simultaneously.

State is in `state.active_bins` (tuple of bin indices 1–6).

### Filter Thresholds (`_bin_passes`)

Selecting a zone does not merely require any metres in that zone. Each zone has a meaningful minimum fraction of total work metres:

| Zone | Passes when |
|---|---|
| Fast (1) | fast ≥ 5% of work |
| 2k (2) | 2k ≥ 10% of work |
| 5k (3) | 5k ≥ 15% of work |
| Threshold (4) | threshold ≥ 25% of work |
| Fast Aerobic (5) | fast aero + slow aero ≥ 50% of work |
| Slow Aerobic (6) | slow aero > 30% of work **AND** combined aero > 50% |

The Slow Aerobic / Fast Aerobic distinction reflects the use case: "Fast Aerobic" catches all predominantly-aerobic sessions, while "Slow Aerobic" specifically isolates true base/recovery work.

---

## Data Table

Custom row renderer (not `hd.data_table`, which cannot host SVG cells). All column headers are sortable via `_sort_header()`.

### Columns (left to right)

| Column | Source | Notes |
|---|---|---|
| Date | `r["date"]` | Formatted by `_fmt_date` |
| Reps | `r["_reps"]` | Count of non-rest intervals |
| Structure | `r["_structure_key"]` | Rep-stripped (e.g. `"500m / 2'r"` not `"6 × 500m / 2'r"`) |
| Stimulus | `r["_stimulus"]` | Grid cell label, italic |
| Zones | `r["_bar_uri"]` | SVG stacked pace-zone bar; tooltip shows full breakdown (e.g. `"Fast 8%  2k 15%  Threshold 22%  Fast Aero 55%"`) |
| Work | `r["distance"]` | Work-only metres (C2 API excludes rest from `distance` on interval workouts) |
| Avg Split | `r["_work_pace"]` | `r["time"] * 500 / r["distance"]`; work-only (C2 API also excludes rest from `time`) |
| Time | `r["time_formatted"]` | Work-only time, pre-formatted by C2 API |
| SPM | `r["_work_spm"]` | Work-weighted average; top-level `stroke_rate` is not used (it averages rest periods where SPM = 0) |
| HR | `r["heart_rate"]["average"]` | Formatted by `_fmt_hr` |

Default sort: date descending. Sort direction flips on repeated clicks; split defaults ascending (fastest = lowest number).

### Pagination

`_ROWS_PER_PAGE = 200`. Page is clamped when filters change total count.

---

## Structure Label Generation (`interval_utils.py`)

`build_interval_lines(r, compact=True)` is the core function. It handles:

1. **Uniform distance + uniform rest** → `"N × Xm / Yr"` (most common)
2. **Variable distance + uniform rest** → `"600–500–400m / 2'r"`
3. **Variable rest or distance** → inline per-interval: `"800m/3'  –  600m/3'  –  400m/2'"`
4. **Complex (multi-interval blocks)** → one line per block: `"800+250+200m / 8'"`

`interval_structure_key()` strips the leading `"N × "` so "3 × 2000m / 5'r" and "5 × 2000m / 5'r" both display as "2000m / 5'r" in the Structure column (reps are shown separately in the Reps column).

The C2 API attaches rest to the interval that precedes it (`rest_time` field on the work interval). An interval with `rest_time == 0` flows directly into the next with no recovery.

---

## Avg Work Pace Note

Both `r["time"]` and `r["distance"]` on interval workouts are **work-only** values in the Concept2 API — rest time and rest distance are excluded. The average work pace is therefore simply:

```
avg_work_pace = r["time"] * 500 / r["distance"]   (tenths-of-sec per 500m)
```

This differs from steady-state workouts where `time` includes everything. No rest-subtraction is needed.

---

## State Variables

All reactive state lives in a single `hd.state()` call in `intervals_page()`:

| Variable | Type | Meaning |
|---|---|---|
| `active_cells` | `tuple[str]` | Selected grid cells, e.g. `("2,1", "3,1")`. OR filter. |
| `active_bins` | `tuple[int]` | Selected pace bins 1–6. AND filter. |
| `sort_col` | `str` | Active sort column id |
| `sort_asc` | `bool` | Sort direction |
| `page` | `int` | Current table page (0-indexed) |

Pace-bin filter is applied after cell filter:
`all_intervals → _filter_by_cells → _filter_by_bins → table`

The grid always displays the full enriched list regardless of `active_bins`.

---

## Adding or Changing Things

**To adjust grid cell labels or tooltips**: edit `_STIMULI` and `_TOOLTIPS` in `intervals_page.py`. Both are 5×6 lists indexed `[row_idx][col_idx]`.

**To change axis boundaries**: edit `_DUR_COLS` (X axis, seconds) or `_RATIO_ROWS` (Y axis, internal rest/work ratio). Changing boundaries does not require changing `_STIMULI` — cells just shift.

**To change pace-zone filter thresholds**: edit `_bin_passes()` in `intervals_page.py`.

**To change pace-zone definitions** (what split qualifies as "2k pace" etc.): see `compute_bin_thresholds()` in `services/volume_bins.py`. These thresholds are derived from the user's recent personal bests; all tabs share them.

**To add a table column**: add a header call in `_interval_table()`, add a data cell in the row loop, and add a sort key in `_sort_workouts()` if sortable. Column widths are HyperDiv units passed as `width=N`.
