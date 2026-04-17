# Race Page

**File:** `components/race_page.py`  
**Entry point:** `race_page(client, user_id, excluded_seasons=(), machine="All")`

A regatta-style animated race that replays all qualifying workouts for a single
ranked Concept2 event side-by-side, one boat per workout, driven by real
stroke-level data fetched from the Concept2 API.

---

## UI Layout

```
A Race Between [Your Season Bests ▾] at [2k ▾]!  ← interactive h1 title
─────────────────────────────────────────────
Fetching stroke data…  3 / 7   ████░░░░       ← progress bar (while loading)
┌─────────────────────────────────────────┐
│  Race canvas  (RaceChart plugin)        │   ← auto-height: 26px + 44px × N lanes
└─────────────────────────────────────────┘
Sort lanes by  ● Date  ○ Result             ← sort toggle (below canvas)
─────────────────────────────────────────────
7 result(s) — 2,000m                        ← results table header
Date  Season  Time  Pace  Watts  SPM  HR    ← full results table
```

Season and machine filtering are applied globally (passed in from `app.py`).
The results table always shows **all** qualifying workouts regardless of the
include filter — the filter only affects which boats race on the canvas.

---

## Interactive Title

The page title is rendered as an `hd.h1()` containing two inline `hd.dropdown()`
widgets that double as the filter controls:

| Token | Control | Changes |
|---|---|---|
| **[Your Season Bests ▾]** | Include filter dropdown | `state.include_filter` |
| **[2k ▾]** | Event dropdown | `state.event_type` + `state.event_value`; resets fetch queue |

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
| `event_value` | `int` | metres (dist events) or tenths-of-second (time events) |
| `include_filter` | `str` | `"All"` / `"SBs"` — default `"All"` |
| `sort_mode` | `str` | `"date"` (newest first) or `"result"` (fastest first) |
| `show_wr_boat` | `bool` | Whether the age-group WR ghost boat is enabled |
| `wr_records` | `dict` | Cached `{(etype, evalue): result}` from `concept2_records` |
| `wr_records_key` | `str` | `"gender\|age\|weight_kg"` — invalidation key for `wr_records` |
| `strokes_cache_loaded` | `bool` | True once the localStorage stroke cache has been read |
| `strokes_by_id` | `dict` | `{str(workout_id): [{t, d}, …]}` — in-memory stroke cache |
| `fetch_queue` | `tuple[int]` | Workout IDs still waiting for stroke fetch |
| `fetch_total` | `int` | Total fetches needed for the current batch |
| `fetch_done` | `int` | Completed fetches in the current batch |
| `last_batch_key` | `str` | Sentinel that detects when the qualifying set changes |

---

## Workout Filtering Pipeline

```
all_workouts (all synced workouts)
  │
  ├─ is_rankable_noninterval()         quality filter (same as Performance page)
  ├─ apply_quality_filters()         removes anomalous entries
  ├─ excluded_seasons  (global)      from app.py gfilter
  ├─ machine           (global)      from app.py gfilter
  │
  └─▶ rankable_efforts
        │
        ├─ _event_workouts()         match event_type + event_value + "All" / "SBs" / "top" filter
        │    └─▶ racing_workouts          used for the results table (all pieces) and boats on the canvas
```

`_include_filtered()` uses `apply_best_only()` from `services/rowing_utils.py`:
- `"All"` → all qualifying workouts (default)
- `"SBs"` → one best per season
- `"top"` → top 10 overall

---

## Stroke Data Fetching

Stroke data (1-Hz telemetry: time + distance per stroke) is fetched one workout
at a time via `fetch_one_stroke()` from `services/stroke_utils.py`.

- **Cache**: stored in `localStorage` under key `strokes_cache`, compressed with
  `services/local_storage_compression.py`. Loaded once on first render.
- **Fetch loop**: each render cycle starts one `hd.task()` for the next
  un-cached workout ID (wrapped in `hd.scope(f"fetch_{id}")` for task isolation).
  Progress is shown as a progress bar: `fetch_done / fetch_total`.
- **Batch key**: `last_batch_key` is `"{event_type}_{event_value}_{sorted_ids}"`.
  Changing event or include filter resets the queue for only the newly required IDs.
- **Synthesised strokes**: when the API returns no stroke data, `synthesize_strokes()`
  builds sparse `[{t, d}]` points from split boundaries. The JS animation detects
  these via `boat.has_real_strokes = False` and uses `boat.avg_spm` for cadence.

---

## World Record Ghost Boat

When the user's profile is complete (gender, date of birth, weight), a toggle adds a **WR ghost boat** representing the applicable Concept2 age-group world record for the current event.

- `state.show_wr_boat` — boolean toggle, off by default.
- `state.wr_records` — cached `{(etype, evalue): result}` dict from `get_age_group_records()` in `services/concept2_records.py`. Records are fetched at most once per profile (keyed on `state.wr_records_key = "gender|age|weight_kg"`).
- The WR boat is built via `build_wr_boat(event_type, event_value, record_result)` from `services/stroke_utils.py`, which synthesises strokes from the official result. `has_real_strokes = False` on WR boats; the JS uses `avg_spm` for oar cadence.
- Profile incompleteness (missing gender, DOB, or weight) silently suppresses the toggle.

---

## Race Canvas — RaceChart Plugin

**Plugin:** `components/race_chart_plugin.py` + `components/chart_assets/race_chart_plugin.js`

Python props passed to JS:

| Prop | Type | Description |
|---|---|---|
| `races` | `list` | Boat dicts from `stroke_utils.build_races_data()` |
| `event_type` | `str` | `"dist"` or `"time"` |
| `event_value` | `int` | metres or tenths-of-second |
| `is_dark` | `bool` | Dark mode flag for colour scheme |

JS writes back `change_id` and `current_time_ms` on user seek (not used by Python currently).

### Boat dict schema (from `build_races_data`)

| Field | Type | Description |
|---|---|---|
| `id` | `int` | Workout ID |
| `label` | `str` | "Jan. 26th, 2019" |
| `color` | `str` | CSS hex colour (season-derived) |
| `strokes` | `list` | `[{t: secs, d: metres}]` sorted by t |
| `is_pb` | `bool` | True for the all-time best workout |
| `season` | `str` | e.g. `"2025-26"` |
| `finish_time_s` | `float\|None` | Official finish time (dist events) |
| `finish_dist_m` | `float\|None` | Official final metres (time events) |
| `avg_spm` | `int` | Piece average stroke rate (0 if unknown) |
| `has_real_strokes` | `bool` | False → strokes synthesised from splits |

### Canvas sizing

Height is auto-computed in JS by `updateCanvasHeight()`, called from `rebuildMaxTime()`:

```
height = 26px (header) + N × 44px (lanes) + 6px (bottom pad)
```

Width is always 100% of the containing block.

### Boat geometry

Each boat hull is drawn as a bezier-curve ellipse (pointed bow, rounded stern).
Centre X position ensures **stern touches the start line at dist=0** and **bow
touches the finish line at dist=normDist**:

```
TRACK_INNER = TRACK_W − 2 × hullHL
boatCx      = TRACK_L + hullHL + (dist / normDist) × TRACK_INNER
```

For **distance events**, `normDist = event_value` (metres).  
For **time events**, `normDist = maxDistForTimeEvent` — the furthest official
`finish_dist_m` across all boats, computed once in `rebuildMaxTime()`.

### Oar animation

Each boat has two sculling oars. The oar angle sweeps from catch (~33° bow-ward)
through finish (~23° stern-ward) during the drive, then swings back during recovery.

**Phase accumulator** — stroke phase is maintained as a per-boat float in `phaseAccum`
(a `Map<id, phase>`). Each rAF tick, phase advances by `lastWallDeltaSec × STROKE_SPEED / boatPeriod`.
Using wall-clock time (not race time) keeps the visual cadence correct regardless
of the playback speed multiplier. `STROKE_SPEED = 1.20` speeds up all animations
proportionally without affecting relative SPM differences.

**Period** — `getSmoothedPeriod(boat, timeSec)` returns seconds-per-stroke:
- Real-stroke boats: ±10-stroke windowed mean of actual timestamps, clamped 12–65 SPM.
- Synthesised boats: `60 / boat.avg_spm` (constant for the whole piece).
- Fallback: `fieldBasePeriod` (field-wide mean, set in `rebuildMaxTime()`).

**Blade** — drawn as an ellipse at the tip of the shaft. Semi-major axis (along
shaft) ≈ 28% of oar length; semi-minor ≈ 45% of that, giving a 2:1 elongated
widening. During drive (blade in water): sliver minor axis + dim opacity. During
recovery (blade feathered above water): fat minor axis + full opacity.

**Finish** — once a distance-event boat crosses the line, its phase is locked at
`DRIVE_FRAC` (oars at rest alongside the hull).

### Split checkpoints

`getSplitInterval(targetDist)` picks the largest checkpoint spacing from
`[5000, 2000, 1000, 500, 250, 100]` that yields **at least 3 checkpoints**
before the finish line. Labels appear only after the boat's stern has fully
cleared the checkpoint + padding.

### Finish ranks & medals

- **Distance events**: `finishRanks` is populated as each boat's bow crosses
  the finish line. Rank is assigned in crossing order.
- **Time events**: `finishRanks` is populated at `atEnd` (timeMs ≥ maxTimeMs − 50ms)
  by sorting boats by `finish_dist_m` descending.

---

## Results Table

Rendered by `_results_table()`. Shows all workouts in `racing_workouts` (include
filter is **not** applied), sorted by result (fastest time or longest distance).
The all-time PB row is highlighted in `primary-50` background.

Columns: Date · Season · Time (or Distance for time events) · Avg Pace · Avg Watts · Avg SPM · Avg HR

---

## Key Service Dependencies

| Module | Used for |
|---|---|
| `services/stroke_utils.py` | `build_races_data()`, `fetch_one_stroke()`, `build_wr_boat()`, `synthesize_strokes()` |
| `services/concept2_records.py` | `get_age_group_records()` — fetches/caches Concept2 WR records for WR ghost boat |
| `services/ranked_filters.py` | `is_rankable_noninterval()`, `apply_quality_filters()` |
| `services/rowing_utils.py` | `RANKED_DISTANCES`, `RANKED_TIMES`, `get_season()`, `apply_best_only()`, `compute_pace()`, `compute_watts()`, `age_from_dob()`, `profile_complete()` |
| `services/formatters.py` | `format_time()`, `fmt_split()` |
| `services/local_storage_compression.py` | Compress/decompress stroke cache for localStorage |
| `components/race_chart_plugin.py` | `RaceChart` HyperDiv plugin |
| `components/concept2_sync.py` | `concept2_sync()` — ensures workouts are loaded |
| `components/hyperdiv_extensions.py` | `radio_group` — sort toggle |
| `components/profile_page.py` | `get_profile()` — reads user profile for WR boat |
