# Race Page

**File:** `components/race_page.py`
**Entry point:** `race_page(client, user_id, excluded_seasons=(), machine="All")`

A regatta-style animated race that replays all qualifying workouts for a single
ranked Concept2 event side-by-side, one boat per workout, driven by real
stroke-level data fetched from the Concept2 API.  Below the canvas a stroke
graph compares the per-stroke pace/SPM/HR of every boat in the race, and below
the results table a pace-vs-date scatter shows the long-arc trend.

---

## UI Layout

```
A Race Between [Your Season Bests ▾] at [2k ▾]!  ← interactive h1 title
─────────────────────────────────────────────
Fetching stroke data…  3 / 7   ████░░░░       ← progress bar (while loading)
┌──────────────────────────────────────────┐
│  Sort boats by [Date ▾]                  │  ← dropdown lives in canvas overlay
│  Race canvas  (RaceChart plugin)         │  ← auto-height: 26px + 44px × N lanes
│  ▢ Include World Record boat   (ghost)   │  ← phantom lane (idle race only)
└──────────────────────────────────────────┘
[WR caption — when WR boat is enabled]
─────────────────────────────────────────────
Stroke graph (StrokeChart) — primary = PB, others overlaid as compares
[Pace ⌐ Watts]   [pace] [SPM] [HR]   ← chart controls
"Stroke-level data isn't available for N workouts: dates…"  ← footer note
─────────────────────────────────────────────
7 result(s) — 2,000m                        ← results table header
Date  Season  Time  Pace  Watts  SPM  HR
─────────────────────────────────────────────
Pace over time — 2k          ● Pace ○ Watts ← scatter heading + metric toggle
[Pace-vs-date scatter — RaceScatterChart]
```

Season and machine filtering are applied globally (passed in from `app.py`).
The results table and scatter always show **all** qualifying workouts
regardless of the include filter — the filter only affects which boats race
on the canvas and which lines appear on the stroke graph.

---

## Interactive Title

The page title is rendered as an `hd.h1()` containing two inline `hd.dropdown()`
widgets that double as the filter controls:

| Token | Control | Changes |
|---|---|---|
| **[Your Season Bests ▾]** | Include filter dropdown | `state.include_filter` |
| **[2k ▾]** | Event dropdown | `state.event_type` + `state.event_value` |

Include filter options and their state values:

| Label | `state.include_filter` |
|---|---|
| Great Efforts *(default)* | `"All"` |
| Season Bests | `"SBs"` |

---

## State Variables

| Name | Type | Description |
|---|---|---|
| `event_type` | `str` | `"dist"` or `"time"` |
| `event_value` | `int` | meters (dist events) or tenths-of-second (time events) |
| `include_filter` | `str` | `"All"` / `"SBs"` — default `"All"` |
| `show_wr_boat` | `bool` | Whether the age-group WR ghost boat is enabled |
| `wr_records` | `dict` | Cached `{(etype, evalue): {result, name, date, age_category, weight_class, gender}}` from `concept2_records` |
| `wr_records_key` | `str` | `"gender\|age\|weight_kg"` — invalidation key for `wr_records` |
| `chart_metric` | `str` | `"pace"` or `"watts"` — stroke graph y-axis |
| `chart_show_pace` | `bool` | Stroke graph: pace/watts line visible |
| `chart_show_spm` | `bool` | Stroke graph: SPM line visible |
| `chart_show_hr` | `bool` | Stroke graph: HR line visible |
| `scatter_metric` | `str` | `"pace"` or `"watts"` — pace-vs-date scatter y-axis |

The sort mode (`date` / `result`) is owned by the JS plugin — see *Race Canvas*
below.  Stroke fetching is handled uniformly by `concept2_sync.strokes_batch`,
which manages its own progress state internally.

---

## Workout Filtering Pipeline

```
all_workouts (all synced workouts)
  │
  ├─ is_rankable_noninterval()       quality filter (same as Performance page)
  ├─ apply_quality_filters()         removes anomalous entries
  ├─ excluded_seasons  (global)      from app.py gfilter
  ├─ machine           (global)      from app.py gfilter
  │
  └─▶ rankable_efforts
        │
        ├─ _event_workouts(…, "All")
        │    └─▶ all_event_workouts   used for the results table and scatter
        │
        └─ _include_filtered(…, include_filter)
             └─▶ racing_workouts      used for race canvas + stroke graph
```

`_include_filtered()` uses `apply_best_only()` from `services/rowing_utils.py`:
- `"All"` → all qualifying workouts (default)
- `"SBs"` → one best per season
- `"top"` → top 10 overall

---

## Stroke Data Fetching

Stroke data (raw Concept2 stroke samples: tenths-of-seconds, decimetres,
pace, SPM, HR) is fetched uniformly via
`components.concept2_sync.strokes_batch(workout_ids)`.  That helper owns:

- An on-disk cache (per-user JSON) populated one workout at a time.
- A progress bar rendered on the owner's view while fetches are running.
- Synchronous reads from the cache for non-owners and subsequent renders.

`build_races_data` then runs each cached raw-stroke list through
`services.stroke_utils.normalize_strokes`, which:

1. Calls `ensure_raw_stroke_origin(raw)` — if the first sample has `t > 0` or
   `d > 0` (some PM5 short-piece exports drop the catch), prepends a
   synthetic `{t:0, d:0, p:0, spm:0, hr:0}` so the boat starts at the line
   instead of teleporting to its first-stroke distance.
2. Converts units to seconds + metres for the JS animation.

The same origin-patch is applied on the workout-page chart path
(`components.workout_chart_builder._stitch_interval_times`) so both views
benefit from the fix.

When the API returns no stroke data, `synthesize_strokes()` builds sparse
points from split boundaries.  The JS animation detects these via
`boat.has_real_strokes = False` and uses `boat.avg_spm` for cadence.
Synthesised boats are excluded from the stroke graph (their dates are listed
in a small footer note).

---

## World Record Ghost Boat

When the user's profile is complete (gender, date of birth, weight) and the
machine filter is RowErg-compatible, an extra **phantom lane** appears below
the last real boat lane while the race is idle.  It contains a Shoelace
checkbox labelled *"Include World Record boat"*.

- The checkbox is owned by JS — it sets `wr_requested` (JS → Python prop),
  and Python reflects that into `state.show_wr_boat`.
- The phantom lane is hidden during play (any time `currentTimeMs > 0` or
  `playing`), so the canvas shrinks back to N real lanes.
- When checked and Python finds a record for the current event, the WR boat
  is **prepended** to `races` (top lane) and a two-line muted caption
  appears below the canvas:

  > *Joe Smith's M hwt 40-49 record of 6:01.5 (1:30.4/500m) set on 2024-04-12.*
  > *The WR boat rows even splits — stroke-level data isn't public.*

  Caption rendered by `_wr_caption()` in `race_page.py`.

- `state.wr_records` caches `{(etype, evalue): metadata_dict}` where each
  metadata dict carries `result, name, date, age_category, weight_class,
  gender`.  See `services/concept2_records.py::_filter_records`.
- The WR boat itself is built via
  `build_wr_boat(event_type, event_value, record_result)` from
  `services/stroke_utils.py`, which synthesises strokes from the official
  result. `has_real_strokes = False` on WR boats; the JS uses `avg_spm` for
  oar cadence.
- Profile incompleteness (missing gender, DOB, or weight) sets
  `wr_available = False`, suppressing the phantom lane entirely.

---

## Race Canvas — RaceChart Plugin

**Plugin:** `components/race_chart_plugin.py` + `components/chart_assets/race_chart_plugin.js`

### Props

Python → JS:

| Prop | Type | Description |
|---|---|---|
| `races` | `list` | Boat dicts from `stroke_utils.build_races_data()` |
| `event_type` | `str` | `"dist"` or `"time"` |
| `event_value` | `int` | meters or tenths-of-second |
| `is_dark` | `bool` | Dark mode flag for color scheme |
| `wr_available` | `bool` | If True, render the WR phantom lane + checkbox while idle |

JS → Python:

| Prop | Type | Description |
|---|---|---|
| `change_id` | `int` | Increments on user seek |
| `current_time_ms` | `int` | Race-clock position at last seek |
| `wr_requested` | `bool` | True when the user has ticked the phantom-lane checkbox |

### Sort dropdown (JS-owned)

The plugin renders an absolutely-positioned `<sl-select>` in the top-right
corner of `canvasWrap` with options *"Sort boats by date"* / *"Sort boats by
result"*.  On change the JS re-sorts the `races` array in place and rebuilds
the canvas — Python is not re-rendered.  Each boat dict carries `date_iso`
(set by `build_races_data`) so JS can sort by date without parsing labels.

### Boat dict schema (from `build_races_data`)

| Field | Type | Description |
|---|---|---|
| `id` | `int` | Workout ID |
| `label` | `str` | "Jan. 26th, 2019" |
| `date_iso` | `str` | "YYYY-MM-DD" — sort key for the JS sort dropdown |
| `color` | `str` | CSS hex color (season-derived) |
| `strokes` | `list` | `[{t: secs, d: meters, p, spm, hr}]` sorted by t |
| `is_pb` | `bool` | True for the all-time best workout |
| `season` | `str` | e.g. `"2025-26"` |
| `finish_time_s` | `float\|None` | Official finish time (dist events) |
| `finish_dist_m` | `float\|None` | Official final meters (time events) |
| `avg_spm` | `int` | Piece average stroke rate (0 if unknown) |
| `has_real_strokes` | `bool` | False → strokes synthesised from splits |

### Canvas sizing

Height is auto-computed in JS by `updateCanvasHeight()`, called from
`rebuildMaxTime()` and on phantom-lane visibility changes:

```
height = 26px (header) + N × 44px (lanes) + 44px (phantom, if visible) + 6px pad
```

Width is always 100% of the containing block.

### Boat geometry, oars, splits, finish ranks

(Unchanged — see comments in `race_chart_plugin.js` for hull bezier math, oar
phase accumulator, blade rendering, split checkpoint selection, and finish
rank assignment.)

---

## Stroke Graph — StrokeChart (shared with Workout Page)

Below the race canvas, a `StrokeChart` (the same chart used on the workout
page) renders one line per boat in `racing_workouts`:

- **Primary line** = the all-time PB workout.
- **Compare overlays** = every other racing workout, dashed, season-coloured.
- Lane colour matches graph-line colour: each compare-series entry passes the
  boat's season hex through `build_compare_series` (see
  `components.workout_chart_builder.build_compare_series`).
- Synthesised-stroke boats are excluded; their dates are listed in a small
  *"Stroke-level data isn't available for …"* footer when any are skipped.

The shared chart-controls row offers Pace/Watts toggle + Pace/SPM/HR
visibility switches.  Defaults: pace on, SPM/HR off.

### Y-axis capping (shared with workout page)

Pace at the catch is so slow that fitting all data makes interesting
differences invisible.  `build_stroke_chart_config` computes a *capped*
y-range whose slow end is the faster of:

1. The slowest interval/split/workout average + 10% (or watts − 10%), or
2. The slowest pace/power observed.

A small unobtrusive **⤢ full range** button in the top-right of the chart
toggles back to the full data range when needed.  The button is hidden when
the cap and full ranges are identical.  Both `paceYMin/paceYMax` (capped)
and `paceYMinFull/paceYMaxFull` are emitted on the config dict; the JS
chooses based on the toggle state.  See
`components/workout_chart_builder.py::_slow_end_cap` and the `range-btn`
handler in `chart_assets/workout_chart_plugin.js`.

The HR axis is suppressed entirely when the HR switch is off, so an empty
*bpm* axis no longer shows for boats without HR data.

---

## Results Table

Rendered by `_results_table()`. Shows all workouts in `racing_workouts`
(include filter is **not** applied), sorted by result (fastest time or
longest distance). The all-time PB row is highlighted in `primary-50`
background.

Columns: Date · Season · Time (or Distance for time events) · Avg Pace · Avg
Watts · Avg SPM · Avg HR

---

## Pace-vs-Date Scatter — RaceScatterChart

**Plugin:** `components/race_scatter_plugin.py` +
`components/chart_assets/race_scatter_plugin.js`
**Config builder:** `components/race_scatter.py::build_race_scatter_config`

Below the results table, a scatter shows one dot per qualifying workout
(every effort in `all_event_workouts` — the include filter is intentionally
ignored here so the long-arc trend is visible).

- Each dot is coloured by season (`season_color(get_season(...), fmt="hex")`).
- The PB dot is drawn larger (radius 6) with a 2px white border ring.
- A dashed best-fit line spans the visible range, computed in pure Python
  via simple OLS on `(days_from_min_date, y)`.  Skipped when fewer than two
  points or the date range collapses.
- A **Pace / Watts** radio toggle next to the chart heading drives
  `state.scatter_metric`.  The Y-axis is reversed (slow at bottom) for pace
  and normal for watts.
- The X-axis is a *linear* scale with epoch-ms values; tick labels are
  formatted as `"Mon 'YY"` via a JS callback.  This avoids bundling
  `chartjs-adapter-date-fns`.
- Tooltip lines: `"YYYY-MM-DD: m:ss.s/500m"` (pace) or
  `"YYYY-MM-DD: NNN W"` (watts).

The plugin is a thin wrapper that exists solely to re-eval the JS-string
callbacks (HyperDiv's built-in `hd.chart` JSON-encodes the config and drops
function values, so callbacks ship as strings and are restored client-side).

---

## Key Service Dependencies

| Module | Used for |
|---|---|
| `services/stroke_utils.py` | `build_races_data()`, `build_wr_boat()`, `synthesize_strokes()`, `ensure_raw_stroke_origin()`, `normalize_strokes()` |
| `services/concept2_records.py` | `get_age_group_records()` — returns `{key: metadata_dict}` for the WR boat + caption |
| `services/rowing_utils.py` | `is_rankable_noninterval()`, `apply_quality_filters()`, `RANKED_DISTANCES`, `RANKED_TIMES`, `get_season()`, `apply_best_only()`, `compute_pace()`, `compute_watts()`, `age_from_dob()`, `profile_complete()`, `season_color()` |
| `services/formatters.py` | `format_time()`, `fmt_split()` — WR caption formatting |
| `components/race_chart_plugin.py` | `RaceChart` HyperDiv plugin |
| `components/race_scatter.py` + `race_scatter_plugin.py` | Pace-vs-date scatter |
| `components/workout_chart_builder.py` | `build_stroke_chart_config()`, `build_compare_series()` — shared with workout page |
| `components/workout_chart_plugin.py` | `StrokeChart` plugin (race + workout pages) |
| `components/concept2_sync.py` | `get_all_workouts()`, `strokes_batch()` |
| `components/hyperdiv_extensions.py` | `radio_group` — scatter Pace/Watts toggle |
| `components/profile_page.py` | `get_profile_from_context()` — reads user profile for WR boat |
