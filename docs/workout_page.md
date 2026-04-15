# Workout Detail View — Design & UI Reference

This document covers how the per-workout detail view works in Erg Nerd:
how it is opened, what it displays, how the workout chart is built, how
custom splits work, and how similar sessions are found.

---

## 1. Overview

Any result table in the app (Performance, Sessions, or Intervals tab) has a
small **⬡ view icon** at the right edge of each row.  Clicking it opens a
full-screen detail view for that workout.  The detail view replaces the tab
content until the user navigates away (e.g. by clicking a nav tab or using
the browser back button).

The detail view is composed of five sections rendered top-to-bottom:

1. **Header bar** — back button, date, machine type, workout type
2. **Summary cards** — wrapping grid of key metrics
3. **Workout chart** — pace/watts vs. elapsed time with SPM and HR overlays
4. **Splits / intervals table** — per-split or per-interval breakdown, with an optional custom-split editor
5. **Similar sessions** — a clickable result table of the most similar past workouts

---

## 2. Navigation and State

### How a workout is opened

Every result table in the app includes a **view** link in its rightmost column
(rendered by `COL_LINK` → `_link_cell()` in `workout_table.py`).  Clicking
it navigates the browser to `/session/{id}`.

`_dashboard_view()` in `app.py` detects `in_session = loc.path.startswith("/session/")`,
extracts the integer ID from the path, and calls `workout_page(session_id, client, user_id)`
instead of the normal tab content.

### Closing the view

Clicking any tab in the nav bar navigates away from the `/session/…` path,
which returns the user to that tab.  The browser's own back button also works.
There is no explicit "Back" button in the UI — it was removed; navigation
relies entirely on tab clicks or browser-native navigation.

### Chaining sessions

The **Similar sessions** table at the bottom uses the same `COL_LINK` column,
so clicking a row there navigates directly to `/session/{id}` for that workout.

---

## 3. Summary Cards

The summary card grid shows whichever of the following are available for
the workout.  Cards are displayed in a wrapping row so they reflow naturally
on narrow windows.

| Card | Source | Notes |
|---|---|---|
| Distance | `workout.distance` | Work-only for intervals (Concept2 excludes rest) |
| Time | `workout.time` | Work-only for intervals |
| Avg Pace | Derived: `time × 500 / distance` | Shown as M:SS.t /500m |
| Avg Watts | Derived from avg pace | Standard Concept2 formula: `2.80 × (500/pace)³` |
| Max Watts | Derived from stroke data | Only shown when `stroke_data=True` |
| Avg Watts/Stroke | Derived from stroke data | Mean watts across all strokes |
| Avg Watts/Heartbeat | Derived from stroke data + HR | Only shown when HR data is present |
| Avg SPM | `workout.stroke_rate` | Average across entire workout including rest |
| Stroke Count | `workout.stroke_count` | As reported by Concept2 |
| Drag Factor | `workout.drag_factor` | |
| Rest Distance | `workout.rest_distance` | Interval workouts only |
| Rest Time | `workout.rest_time` | Interval workouts only |
| Avg HR | `workout.heart_rate.average` | When HR monitor was worn |
| Max HR | `workout.heart_rate.max` | When available |

---

## 4. Workout Chart

### When it appears

The workout chart is only shown when `workout.stroke_data == True`.  Workouts
recorded without a PM5 or with stroke data unavailable show a grey notice
instead.

### Data source

Stroke data is fetched on-demand when the detail view opens via
`client.get_strokes(user_id, result_id)` — `GET /api/users/{user}/results/{id}/strokes`.
The endpoint returns a JSON array (one object per stroke):

| Field | Unit | Description |
|---|---|---|
| `t` | tenths of a second | Elapsed time (resets to 0 at each interval boundary) |
| `d` | decimeters | Elapsed distance |
| `p` | tenths of a sec/500m | Pace (divide by 10 for display) |
| `spm` | strokes/min | Stroke rate |
| `hr` | bpm | Heart rate (`0` when no HR monitor was worn) |

Stroke data is not cached — it is fetched fresh each time a workout is opened
and held only for the lifetime of that view.

#### Upstream sanitisation

`concept2.get_strokes()` passes the raw API data through `_sanitise_strokes()`
before returning it.  This silently drops any stroke where `t` decreases but
the new value is still above 300 tenths (30 s) — a device bug where the PM5
occasionally emits a duplicate stroke with a slightly earlier timestamp
mid-rest.  Genuine interval-boundary resets always drop `t` back near zero.

#### Interval time stitching

For interval workouts the API resets `t` to 0 at the start of each work
interval.  `_stitch_interval_times()` in `workout_chart_builder.py` detects
each backward jump and adds a running offset so the final `t` values are
monotonically increasing across the whole session.  The offset is advanced
by the canonical interval duration (work time + rest time) from the interval
metadata rather than by `prev_t`, so rest periods are correctly represented
even when the last stroke arrives a few tenths before the interval ends.

### Chart axes

| Axis | Content | Notes |
|---|---|---|
| X | Elapsed time (seconds) | Tick labels formatted as M:SS |
| Primary Y (left) | Pace (sec/500m) or Watts | Inverted when showing pace so faster = higher |
| Secondary Y (right, dark blue) | SPM | Min 0, integer bounds; matches series colour |
| Secondary Y (right, red) | HR (bpm) | Range 40–220; hidden if no HR data |

### Controls — normal mode

| Control | What it does |
|---|---|
| **Pace / Watts** radio | Switch the primary Y-axis between pace (sec/500m) and watts |
| **Stack** switch | Enter stacked-intervals mode (available when there are multiple work bands) |
| **Reset zoom** button | Appears when a band is zoomed; resets to full x-axis |

### Controls — stacked mode

Enabled by the **Stack** switch.  Each work interval is overlaid on a shared
x-axis starting at t = 0, coloured with an HSL palette from blue to orange.
A Chart.js legend shows one entry per interval.  Entering stack mode clears
any active zoom.

An additional row of per-series visibility switches appears below the chart:

| Switch | What it controls |
|---|---|
| **Pace** / **Watts** | Show or hide the pace/watts series for all intervals |
| **SPM** | Show or hide the stroke-rate series for all intervals |
| **HR** | Show or hide the heart-rate series (only present when HR data exists) |

### Interval bands (normal mode)

The chart draws shaded background bands for each split or interval:

- **Amber tint** — work intervals/splits
- **Neutral tint** — rest intervals

Clicking a band zooms the x-axis to just that interval's time range.
Clicking **Reset zoom** restores the full view.

For interval workouts, bands come from `workout.workout.intervals` (duration
in tenths of a second, accumulated to elapsed seconds on the x-axis).
For split workouts, bands come from `workout.workout.splits`.
JustRow workouts with no splits have no bands.

### Series colours

| Series | Light mode | Dark mode |
|---|---|---|
| Pace / Watts | `#60a5fa` (light blue, thicker) | same |
| SPM | `#1e40af` (dark blue, dashed) | `#3b82f6` (lighter blue) |
| HR | `#ef4444` (red, dotted) | `#f87171` (lighter red) |

Axis tick labels and titles use the same colour as their series.

### Code

| File | Responsibility |
|---|---|
| `components/workout_chart_builder.py` | `build_stroke_chart_config()` — converts strokes + workout to a Chart.js config dict |
| `components/workout_chart_plugin.py` | `StrokeChart` HyperDiv plugin — wraps the config in a `<canvas>` |
| `components/chart_assets/workout_chart_plugin.js` | Chart.js rendering, band shading, click-to-zoom, stacked mode, tooltips |

---

## 5. Splits / Intervals Table

### Interval workouts

For any workout type in `INTERVAL_WORKOUT_TYPES` (`FixedDistanceInterval`,
`FixedTimeInterval`, `FixedCalorieInterval`, `VariableInterval`,
`VariableIntervalUndefinedRest`), the table shows each **work interval** as a
row.  Rest is shown in the rightmost **Rest** column of the preceding work row.

Columns: `#` · `Type` · `Distance` · `Time` · `Pace` · `Watts` · `SPM` · `Avg HR` · `Rest`

### Split workouts and JustRow

For `FixedDistanceSplits`, `FixedTimeSplits`, and `JustRow`, the table shows
the splits from `workout.workout.splits` (the pre-computed 500m splits from
the PM5).

Columns: `#` · `Distance` · `Time` · `Pace` · `Watts` · `SPM` · `Avg HR` · `Max HR`

### Custom splits

The custom split editor appears when **all** of the following are true:
- The workout is not an interval type
- `stroke_data=True` (stroke data is required to interpolate new boundaries)
- Total workout distance is known

The editor shows a row of editable distance chips.  The default is the
standard 500m splits.  The user can add, remove, or edit chips.

**Validation:** The sum of all chip values must equal the workout's total
distance within ±2 metres.  A warning is shown while the sum is off; the
**Recalculate** button is disabled.

**Recalculation:** When recalculate is clicked, `_recalculate_splits()` in
`workout_page.py` interpolates elapsed time from the stroke data at each
cumulative split boundary (binary search + linear interpolation on the `d`
field), then computes pace, SPM, and HR for each window.

**Persistence:** Custom split configurations are saved to the browser's
`localStorage` under the key `"custom_splits"` as a JSON object
`{str(workout_id): [dist_m, ...]}`.  They survive page refreshes and are
loaded back automatically the next time that workout is opened.

---

## 6. Similar Sessions

The similar sessions table at the bottom of the page shows up to 8 workouts
from the user's history that are most similar to the current session.  Each
row is clickable and navigates directly to that session's detail view.

### Matching logic

**Interval workouts** — matched by `interval_structure_key()` (from
`services/interval_utils.py`), which strips the leading rep count so that
"6 × 500m / 2′r" and "4 × 500m / 2′r" both match the key `"500m / 2′r"`.
Results are sorted by date descending (most recent first).

**Non-interval workouts** — matched by exact `workout_type` and distance
within ±20%.  Results are sorted by |pace delta| ascending (closest pace
first), so the most performance-comparable sessions appear at the top.
When pace cannot be computed, results fall back to date descending.

---

## 7. Code Organisation

| File | Responsibility |
|---|---|
| `components/workout_page.py` | Top-level overlay component; all sections; custom-split recalculation; similar-session logic |
| `components/concept2_sync.py` | Ensures workouts are synced from the API before the detail view loads |
| `components/workout_chart_builder.py` | `build_stroke_chart_config()` — pure Python Chart.js config builder |
| `components/workout_chart_plugin.py` | `StrokeChart` HyperDiv plugin class |
| `components/chart_assets/workout_chart_plugin.js` | Chart.js rendering, band click-to-zoom, stacked mode, dual Y-axis setup |
| `components/workout_table.py` | `WorkoutTable()` — CSS Grid sortable table; `COL_LINK` / `_link_cell()` renders the per-row view link |
| `services/concept2.py` | `Concept2Client.get_strokes()` — fetches and sanitises the `/strokes` list for a result |
| `app.py` | URL routing via `loc.path`; dispatches `workout_page()` for `/session/{id}` paths |

### Entry point

```python
# app.py — _dashboard_view()
if in_session:
    session_id = int(loc.path.split("/")[2])
    workout_page(session_id, client, user_id)
```

### Clickable result tables

Every table that should support drill-in includes `COL_LINK` in its column list.
`COL_LINK` uses `_link_cell()`, which renders an `hd.link("view", href=f"/session/{id}")`.
Navigating to that URL triggers the routing logic in `_dashboard_view()`.

---

## 8. Known Limitations

- **Stroke data not cached.** Each time a workout is opened, a fresh API call
  is made.  On slow connections there may be a brief spinner before the chart
  appears.  Summary cards and the splits table (from the cached workout object)
  are visible immediately.

- **Custom splits for timed events.** The custom split editor currently works
  in terms of metres.  For timed-event workouts (30 min, 60 min, etc.) where
  the user may want splits by elapsed time rather than distance, this is not
  yet supported.  The editor is shown but splits by time boundary must be
  translated to approximate distances manually.

- **1-minute and very short workouts.** Stroke data for very short workouts
  may contain only a handful of strokes, making the chart sparse.

- **Avg Watts/Heartbeat.** This metric is only well-defined when the HR and
  pace arrays are the same length (i.e., every stroke has an associated HR
  reading).  When lengths differ it is omitted.
