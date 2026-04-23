# Interval Workouts Tab

## Overview

The Intervals tab is a dedicated browser for Concept2 interval workouts. Interval workouts are excluded from the Rankings tab (pace is not comparable to steady-state efforts) and appear only as dots in the Sessions chart, so this tab is their primary home.

The tab has three regions:

1. **2D grid browser** — maps every interval workout onto a physiologically meaningful grid so training coverage gaps are immediately visible. Each row is coloured by the *expected* pace intensity of a quality session at that work:rest ratio, giving an at-a-glance map of what "hard" should look like.
2. **Persistent info panel** — sits directly below the grid and describes *every* currently selected stimulus (name, axis coordinates, physiological description, example workout). Useful for comparing several stimuli side by side.
3. **Sortable data table** — lists individual workouts with full detail; includes a Quality column grading each session against its row's expected intensity/volume; filtered by the grid cells, the Pace Intensity legend, the HR Intensity legend, and any active Structure filter.

---

## Implicated Files

| File | Role |
|---|---|
| `components/intervals_page.py` | All HyperDiv UI: grid, info panel, legends, table, entry point `intervals_page()` |
| `services/interval_utils.py` | Pure-Python helpers: structure label generation, pace computation, SPM weighting |
| `services/volume_bins.py` | Pace-zone infrastructure: `workout_bin_meters()`, `bin_bar_svg()`, `swatch_svg()`, `BIN_NAMES`, `BIN_COLORS`, `PACE_INTENSITY_WEIGHTS`, `pace_intensity_score()`, `pace_bin_passes()`, `PACE_ZONE_DEFINITION_TEXT`, `PACE_ZONE_FILTER_TEXT`, `get_reference_sbs()`, `compute_bin_thresholds()` |
| `services/heartrate_utils.py` | HR-zone infrastructure: `workout_hr_meters()`, `resolve_max_hr()`, `HR_ZONE_NAMES`, `HR_ZONE_COLORS`, `HR_INTENSITY_WEIGHTS`, `hr_intensity_score()`, `hr_bin_passes()`, `HR_ZONE_DEFINITION_TEXT`, `HR_ZONE_FILTER_TEXT` |
| `services/rowing_utils.py` | `INTERVAL_WORKOUT_TYPES` — the set of `workout_type` strings that qualify as interval sessions |
| `services/formatters.py` | Shared formatters: `fmt_date`, `fmt_distance`, `fmt_split`, `format_time` |
| `components/workout_table.py` | Generic `WorkoutTable` + `ColumnDef` renderer used by the data table |
| `components/profile_page.py` | `get_profile_from_context(ctx)` — reads the user's profile for max HR |
| `app.py` | Tab declaration and dispatch |

---

## Data Flow

```
sync_from_context(ctx) → all_workouts
    ↓
get_profile_from_context(ctx) → profile
resolve_max_hr(profile, all_workouts) → (max_hr, is_estimated)
    ↓
_enrich_workouts(all_workouts, thresholds, max_hr)
    → filters to INTERVAL_WORKOUT_TYPES (skipping single-rep sessions)
    → workout_bin_meters() per workout           (pace bins)
    → bin_bar_svg() for pace                    (stacked bar SVG)
    → pace_intensity_score()                    (0–100 weighted score)
    → workout_hr_meters() if max_hr set         (HR bins)
    → bin_bar_svg() for HR
    → hr_intensity_score()                      (0–100 weighted score)
    → interval_structure_key(), avg_workpace_tenths(), avg_work_spm()
    → _compute_grid_placement() → (col, row)
    → _cell_name(row, col)                      (stimulus label)
    ↓
_filter_disjunctive(… active_bins, pace_bin_passes, "_bin_meters")
_filter_disjunctive(… active_hr_bins, hr_bin_passes, "_hr_bin_meters")
Structure filter (exact match on _structure_key)
    ↓
_grid_browser(pre_filtered, state)     — grid counts reflect all above filters
_info_panel(state)                     — describes every cell in state.active_cells
_zone_filter_legends(state, max_hr)    — Pace + HR chip legends (OR within each)
_filter_by_cells(pre_filtered, …)      — grid-cell OR selection
WorkoutTable(filtered, interval_columns, …)
```

The thresholds used for pace-zone classification come from `compute_bin_thresholds(get_reference_sbs(all_workouts), all_workouts)`, identical to how the Volume tab classifies workouts. Pace zones are relative to the user's recent personal bests, not absolute splits.

---

## Pace & HR Zones

Pace zones (`services/volume_bins.py`) — 7 bins (index 0 = Rest):

| Index | Name | Description |
|---|---|---|
| 0 | Rest | Interval rest distance only — excluded from intensity |
| 1 | Fast | Above midpoint(1k, 2k) pace |
| 2 | 2k | Between midpoint(1k,2k) and midpoint(2k,5k) |
| 3 | 5k | Between midpoint(2k,5k) and midpoint(5k,60min) |
| 4 | Threshold | Between midpoint(5k,60min) and midpoint(60min,marathon) |
| 5 | Fast Aerobic | Down to ~3 s/500m slower than marathon pace |
| 6 | Slow Aerobic | Below marathon pace |

HR zones (`services/heartrate_utils.py`) — 7 bins (index 0 = Rest, 6 = No HR):

| Index | Name | Description |
|---|---|---|
| 0 | Rest | Interval rest — excluded from intensity |
| 1 | Z5 Max | > 90% of max HR |
| 2 | Z4 Threshold | 80–90% |
| 3 | Z3 Tempo | 70–80% |
| 4 | Z2 Aerobic | 60–70% |
| 5 | Z1 Recovery | < 60% |
| 6 | No HR | No valid HR data — excluded from intensity |

### Intensity Score (0–100)

Both pace and HR intensity are linear weighted averages of the per-bin meter fractions:

```
score = Σ (meters_in_bin / meaningful_meters × weight_per_bin)
```

- Pace weights: `[0, 100, 80, 60, 40, 20, 0]` (Fast → Slow Aerobic)
- HR weights: `[0, 100, 75, 50, 25, 0, 0]` (Z5 Max → Z1 Recovery → No HR)

Meaningful meters exclude Rest (both models) and No HR (HR only). Workouts with zero meaningful meters → score `None`, rendered as "—" in the table and sorted last.

---

## 2D Grid Browser

The grid is the primary navigation tool. It answers "what kinds of interval sessions have I done, and are there gaps?"

### Axes

**X axis — Work duration (6 columns)** — median work-interval duration across all non-rest intervals.

| Column | Range |
|---|---|
| ≤30" | 0–30 s |
| 30"–2' | 30–120 s |
| 2'–4' | 120–240 s |
| 4'–8' | 240–480 s |
| 8'–20' | 480–1200 s |
| 20'+ | > 1200 s |

**Y axis — Work:rest ratio (5 rows)** — `total_rest_s / total_work_s` internally; displayed as work:rest.

| Row | Display | Internal ratio (rest/work) |
|---|---|---|
| Continuous | ≥ 10w : 1r | < 0.10 |
| Short rest | 3–10w : 1r | 0.10–0.50 |
| Balanced | ≈ 1w : 1r | 0.50–1.50 |
| Long rest | 1w : 2–4r | 1.50–4.00 |
| Very Long | < 1w : 4r | > 4.00 |

Rest is summed from two sources: the `rest_time` field on each work interval (C2 API convention — rest is stored on the preceding work interval), plus the `time` field of any intervals whose `type == "rest"`.

If a workout has no `workout.intervals` data, the full session duration is used and it is placed in the Continuous row.

### Stimulus Matrix (`_STIMULUS_INFO`)

`_STIMULUS_INFO[row][col]` is a 5×6 table; each populated entry is a dict:

```python
{
    "name":             "VO₂max (2k-prep)",
    "description":      "The canonical VO₂max interval. …",
    "example":          "6× 2' / 2'r, 8× 2' / 2'r.",
    "expected_score":   65,    # target pace-intensity score
    "expected_work_s":  480,   # target total work seconds for a full dose
}
```

Cells left as `None` represent physiologically uncommon combinations; the grid labels them "Other", paints them neutral grey, and the info panel notes they are unusual. "Other" cells have no expectations, so their Quality column shows "—".

Terminology follows standard endurance literature (Seiler's polarized model, Daniels' training paces, Billat's HIIT taxonomy). Short labels appear on the grid button; the description and example are rendered structurally in the info panel.

### Cell Rendering & Click Semantics

- **Per-cell background colour**: every populated cell carries its own `expected_score` (on the stimulus entry in `_STIMULUS_INFO`) which maps to a pace-zone colour via `_cell_background_rgba`. Fartlek reads as aerobic blue; VO₂max intervals as yellow-green; race-pace intervals as red. "Other" (uncommon) cells fall back to a neutral grey so they don't falsely imply an intensity. Colour is independent of the workouts in the cell — empty cells still read as "if you did a quality session here, this is the intensity it would be."
- **Text colour is always white** — both the rep count and the stimulus label use whichever `neutral-*` Shoelace token currently renders white (`neutral-0` in light mode, `neutral-1000` in dark mode), resolved via `hd.theme().is_dark`. Row colours are chosen to maintain contrast with white in both themes.
- **Selection indicator**: selected cells get a thick (`3px solid white`) border; unselected cells get a hairline translucent border. No colour change on selection, so the row's expected intensity remains legible.
- **Empty cells** are rendered the same way (row-coloured background, just no rep count), and clicking them toggles selection just like populated cells — selecting an "empty" cell adds nothing to the table but tells the info panel to describe that stimulus.
- **Click behaviour**: toggle the cell's key in `state.active_cells`. Multi-cell selection is an OR union for table filtering; the info panel shows one entry per selected cell.

### Layout

The grid uses CSS Grid (`grid_box`) with `grid_template_columns = "{label_w}rem 1fr 1fr 1fr 1fr 1fr 1fr"`. All cells are direct grid children in row-first order, so column widths are globally uniform.

---

## Info Panel

Directly below the grid, above the legends. Fixed `min-height: 6rem` prevents layout jumps.

- No cells selected → muted placeholder: *"Click any cell to learn about that training stimulus. Columns = median work-interval length; rows = work:rest time ratio."*
- One or more cells selected → one entry per selected cell, separated by thin dividers. Each entry shows: bold stimulus name, axis coordinates (e.g. *"2' – 4' work · ≈ 1w : 1r"*), the description paragraph, and an italicised *"E.g. …"* example line. When more than one cell is selected, entries render in a compact form (smaller heading) so several fit at once.
- Selected cell with no stimulus info (uncommon combination) → short note explaining the combination is unusual.

---

## Pace & HR Intensity Legends

Below the info panel, two labelled chip rows:

- **Pace Intensity** — 6 chips: Fast · 2k · 5k · Threshold · Fast Aerobic · Slow Aerobic.
- **HR Intensity** — 5 chips: Z5 Max · Z4 Threshold · Z3 Tempo · Z2 Aerobic · Z1 Recovery. Rendered only when `resolve_max_hr(profile, all_workouts)` returns a usable value. Otherwise the row shows a short note pointing at the Profile page.

### Filter Logic — Disjunctive (OR) Within Each Group

Selecting multiple chips in the Pace legend shows workouts whose pace distribution passes the zone threshold for **any** selected zone (union). Same for HR. The three filter groups combine conjunctively with each other:

```
visible = grid_cells_filter(w) AND pace_chips_filter(w) AND hr_chips_filter(w)
```

This is implemented by `_filter_disjunctive(workouts, active_bins, passes_fn, meters_key)` applied once per legend.

### Per-Zone Thresholds

Pace (`pace_bin_passes` — `services/volume_bins.py`):

| Zone | Passes when |
|---|---|
| Fast (1) | fast ≥ 5% of work |
| 2k (2) | 2k ≥ 10% of work |
| 5k (3) | 5k ≥ 15% of work |
| Threshold (4) | threshold ≥ 25% of work |
| Fast Aerobic (5) | fast aero + slow aero ≥ 50% of work |
| Slow Aerobic (6) | slow aero > 30% of work **AND** combined aero > 50% |

HR (`hr_bin_passes` — `services/heartrate_utils.py`) — thresholds are fractions of **HR-classified** meters (Rest and No-HR excluded):

| Zone | Passes when |
|---|---|
| Z5 Max (1) | Z5 ≥ 5% |
| Z4 Threshold (2) | Z4 ≥ 10% |
| Z3 Tempo (3) | Z3 ≥ 20% |
| Z2 Aerobic (4) | Z2 ≥ 40% |
| Z1 Recovery (5) | Z1 ≥ 40% |

### Rich Chip Tooltips

Each chip carries an `hd.tooltip` whose `content_slot` box contains: bold zone heading, one-line definition (`PACE_ZONE_DEFINITION_TEXT` / `HR_ZONE_DEFINITION_TEXT`), and one-line filter rule (`PACE_ZONE_FILTER_TEXT` / `HR_ZONE_FILTER_TEXT`).

---

## Data Table

`WorkoutTable` (CSS Grid) with interval-specific `ColumnDef` objects. Sortable headers (▲/▼); default sort is date descending.

### Columns

| Column | Source | Notes |
|---|---|---|
| Date | `r["date"]` | Formatted by `fmt_date` |
| Reps | `r["_reps"]` | Count of non-rest intervals |
| Structure | `r["_structure_key"]` | Rep-stripped; click to toggle structure filter |
| Stimulus | `r["_stimulus"]` | Grid cell label, italic |
| Pace Intensity | `r["_pace_score"]` + `r["_bar_uri"]` | Bold 0–100 score above a small stacked pace-zone bar. Rich tooltip lists each non-empty zone (swatch · name · percentage). "—" when no work meters. |
| HR Intensity | `r["_hr_score"]` + `r["_hr_bar_uri"]` | Same layout as Pace Intensity, using HR zones. "—" when the workout has no usable HR data or the user has no max HR. |
| Quality | `r["_quality"]` | Low / Medium / High pill, compared against the row's expected intensity and work-time targets. Rich tooltip explains the grade. "—" for workouts without a pace score. |
| Work | `r["distance"]` | Work-only meters (C2 API excludes rest from `distance` on interval workouts) |
| Avg Split | `r["_work_pace"]` | `r["time"] * 500 / r["distance"]`; work-only |
| Time | `r["time_formatted"]` | Work-only time |
| SPM | `r["_work_spm"]` | Work-weighted average |
| ↗ | — | Open-workout link (`COL_LINK`) |

Sort direction flips on repeated header clicks; split defaults ascending (fastest = lowest number). Pace / HR Intensity sort descending by score, with `None` sorting last. Quality sorts by `_QUALITY_ORDER` (High > Medium > Low > None).

### Quality Grade

`_compute_quality(r)` assigns Low / Medium / High per workout by comparing its pace intensity score and total work time against **the cell's own targets** (not the row's).  Each populated entry in `_STIMULUS_INFO` carries two fields:

- `expected_score` — the 0–100 pace-intensity score a good session of that specific stimulus should reach (e.g. 40 for Supra-threshold, 75 for Race-pace intervals, 0 for Aerobic base).
- `expected_work_s` — a rough lower bound on total work seconds needed to count as a full dose.

Why per-cell rather than per-row: the same work:rest ratio can mean very different sessions at different durations. A 30"–2' piece in the "Short rest" row is Glycolytic capacity (expect ~65 score); a 20'+ piece on the same row is Tempo (expect ~25 score). One expectation per row forced the colour map and the quality grade to average across these — per-cell targets fix both.

Continuous-row cells (Fartlek, Steady state, Aerobic base, LSD) set `expected_score` to 0 or a very low value so pure low-intensity Z2 work doesn't grade as Low; quality on those rows is governed almost entirely by whether the session accumulated enough volume.

Rules:
- **Low** — pace score below the cell's expected intensity.
- **Medium** — pace score meets/exceeds expected, but total work time is below the cell's dose target.
- **High** — both pace score and total work time meet/exceed expected.
- **—** — pace score is `None`, the workout sits outside the grid, or its cell is "Other" (no expectations defined).

Each cell is a small coloured pill (red/orange/green). The hover tooltip shows the grade name plus a one-sentence explanation with the workout's score and work-minutes alongside the row's targets, so the user can see *why* it graded the way it did.

### Pagination

`_ROWS_PER_PAGE = 200`. Changing any filter (`filter_key` scope) resets the WorkoutTable's internal page/sort state.

---

## Structure Label Generation (`interval_utils.py`)

`build_interval_lines(r, compact=True)` is the core function. It handles:

1. **Super-block** (nested repetition with a longer outer rest) → `"3 × (5 × 20" / 10"r) / 4'r"`. Detected by `_detect_super_block()`; fires only when the outer rest is strictly longer than any rest within the inner pattern, so uniform sequences like `6 × 500m / 2'r` are not mis-detected as `2 × (3 × 500m / 2'r) / 2'r`.
2. **Uniform distance + uniform rest** → `"N × Xm / Yr"` (most common)
3. **Long arithmetic progression of distances or times + uniform rest** → ladder abbreviation `"1,000–900–...–200–100m / 1'r"`. Triggered by `_abbreviate_arithmetic` when the sequence has ≥ 5 values with a constant non-zero step; otherwise the full list is rendered.
4. **Variable distance + uniform rest** (non-arithmetic) → `"600–500–400m / 2'r"`
5. **Variable rest or distance** → inline per-interval: `"800m/3'  –  600m/3'  –  400m/2'"`
6. **Complex (multi-interval blocks)** → one line per block: `"800+250+200m / 8'"`

`interval_structure_key()` strips the leading `"N × "` so "3 × 2000m / 5'r" and "5 × 2000m / 5'r" both display as "2000m / 5'r" in the Structure column (reps are shown separately in the Reps column).

The C2 API attaches rest to the interval that precedes it (`rest_time` field on the work interval). An interval with `rest_time == 0` flows directly into the next with no recovery. Some programmings (notably super-blocks like `3 × (5 × 20" / 10"r) / 4'r`) also store the long outer rest as a standalone `type == "rest"` interval; `build_interval_lines` folds those into the preceding work interval's `rest_time` up front, so block-splitting and super-block detection both see a single, canonical interval list.

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
| `active_cells` | `tuple[str]` | Selected grid cells, e.g. `("2,1", "3,1")`. Drives both the OR filter on the table and the set of entries in the info panel. |
| `active_bins` | `tuple[int]` | Selected pace bins 1–6. OR filter. |
| `active_hr_bins` | `tuple[int]` | Selected HR bins 1–5. OR filter. |
| `structure_filter` | `str \| None` | Exact-match structure key filter. Toggled by clicking a Structure cell; shows only workouts with that structure key. |

Sort column, sort direction, and page are internal state owned by `WorkoutTable`, not by `intervals_page`.

Filter order:
```
all_intervals
  → _filter_disjunctive (pace chips, _bin_meters)
  → _filter_disjunctive (HR chips, _hr_bin_meters)
  → structure_filter
  → _grid_browser renders pre-filtered counts
  → _filter_by_cells (grid selection)
  → WorkoutTable
```

---

## Adding or Changing Things

**To adjust grid cell labels or descriptions**: edit `_STIMULUS_INFO` in `intervals_page.py`. Each populated cell is a dict with `name`, `description`, `example`; leave a cell `None` to mark it uncommon.

**To change axis boundaries**: edit `_DUR_COLS` (X axis, seconds) or `_RATIO_ROWS` (Y axis, internal rest/work ratio). Stimulus entries stay in place — workouts just shift to different cells.

**To change pace filter thresholds**: edit `pace_bin_passes()` in `services/volume_bins.py`; update `PACE_ZONE_FILTER_TEXT` so chip tooltips stay honest.

**To change HR filter thresholds**: edit `hr_bin_passes()` in `services/heartrate_utils.py`; update `HR_ZONE_FILTER_TEXT`.

**To change pace-zone definitions** (what split qualifies as "2k pace" etc.): see `compute_bin_thresholds()` in `services/volume_bins.py`. These thresholds are derived from the user's recent personal bests; all tabs share them.

**To change intensity weightings**: edit `PACE_INTENSITY_WEIGHTS` / `HR_INTENSITY_WEIGHTS`. Keep the scale 0–100 — the UI labels it as a 0–100 score.

**To add a table column**: append a `ColumnDef` to `interval_columns` inside `intervals_page()`.
