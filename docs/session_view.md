# Session Detail View — Design & UI Reference

This document covers how the per-session detail view works in Erg Nerd:
how it is opened, what it displays, how the stroke chart is built, how
custom splits work, and how similar sessions are found.

---

## 1. Overview

Any result table in the app (Performance, Sessions, or Intervals tab) has a
small **⬡ view icon** at the right edge of each row.  Clicking it opens a
full-screen detail view for that workout.  The detail view replaces the tab
content until the user clicks **← Back**.

The detail view is composed of five sections rendered top-to-bottom:

1. **Header bar** — back button, date, machine type, workout type
2. **Summary cards** — wrapping grid of key metrics
3. **Stroke chart** — pace/watts vs. elapsed time with SPM and HR overlays
4. **Splits / intervals table** — per-split or per-interval breakdown, with an optional custom-split editor
5. **Similar sessions** — a clickable result table of the most similar past workouts

---

## 2. Navigation and State

### How a session is opened

Each tab (`ranked_tab`, `sessions_tab`, `interval_tab`) receives an
`on_session_click(workout_id)` callback from `app.py`.  Clicking the view
icon calls this callback, which sets `app_state.selected_session_id`.

`_dashboard_view()` in `app.py` checks this value on every render.  When
set, it renders `session_detail(...)` instead of the normal tab content.
When cleared, the tab view reappears.

### Closing the view

The **← Back** button calls `on_close()`, which sets
`app_state.selected_session_id = None`.  The tab the user was on is still
the active tab — they return exactly where they left off.

Switching tabs (clicking the tab bar) also clears the overlay automatically,
so the user cannot be left in a "session detail on the wrong tab" state.

### Chaining sessions

The **Similar sessions** table at the bottom uses the same `on_session_click`
callback, so clicking a row there navigates directly to that workout without
returning to the tab first.

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

## 4. Stroke Chart

### When it appears

The stroke chart is only shown when `workout.stroke_data == True`.  Workouts
recorded without a PM5 or with stroke data unavailable show a grey notice
instead.

### Data source

Stroke data is fetched on-demand when the detail view opens via
`client.get_strokes(user_id, result_id)` — `GET /api/users/{user}/results/{id}/strokes`.
The endpoint returns a JSON array directly (one object per stroke):

| Field | Unit | Description |
|---|---|---|
| `t` | tenths of a second | Elapsed time |
| `d` | decimeters | Elapsed distance |
| `p` | tenths of a sec/500m | Pace (divide by 10 for display) |
| `spm` | strokes/min | Stroke rate |
| `hr` | bpm | Heart rate (`0` when no HR monitor was worn) |

Stroke data is not cached — it is fetched fresh each time a session is opened
and held only for the lifetime of that view.

### Chart axes

| Axis | Content | Notes |
|---|---|---|
| X | Elapsed time (seconds) | Tick labels formatted as M:SS |
| Primary Y | Pace (sec/500m) or Watts | Inverted when showing pace so faster = higher; toggle between modes |
| Secondary Y left | SPM | Range 0–50; dashed amber line |
| Secondary Y right | HR (bpm) | Range 40–220; dotted red line; hidden if no HR data |

### Controls

| Control | What it does |
|---|---|
| **Pace / Watts** radio | Switch the primary Y-axis between pace (sec/500m) and watts |
| **SPM** switch | Show or hide the stroke rate overlay |
| **HR** switch | Show or hide the heart rate overlay (only present when HR data exists) |
| **Reset zoom** button | Appears when an interval band is zoomed; resets to full x-axis |

### Interval bands

The chart draws shaded background bands for each split or interval:

- **Amber tint** — work intervals/splits
- **Neutral tint** — rest intervals

Clicking a band zooms the x-axis to just that interval's time range.
Clicking **Reset zoom** restores the full view.

For interval workouts, bands come from `workout.workout.intervals` (duration
in tenths of a second, accumulated to elapsed seconds on the x-axis).
For split workouts, bands come from `workout.workout.splits`.
JustRow workouts with no splits have no bands.

### Code

| File | Responsibility |
|---|---|
| `components/session_chart_builder.py` | `build_stroke_chart_config()` — converts strokes + workout to a Chart.js config dict |
| `components/stroke_chart.py` | `StrokeChart` HyperDiv plugin — wraps the config in a `<canvas>` |
| `components/rowing_chart_assets/stroke_chart.js` | Chart.js rendering, band shading, click-to-zoom, tooltips |

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
`session_detail.py` interpolates elapsed time from the stroke data at each
cumulative split boundary (binary search + linear interpolation on the `d`
field), then computes pace, SPM, and HR for each window.

**Persistence:** Custom split configurations are saved to the browser's
`localStorage` under the key `"custom_splits"` as a JSON object
`{str(workout_id): [dist_m, ...]}`.  They survive page refreshes and are
loaded back automatically the next time that session is opened.

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
| `components/session_detail.py` | Top-level overlay component; all sections; custom-split recalculation; similar-session logic |
| `components/session_chart_builder.py` | `build_stroke_chart_config()` — pure Python Chart.js config builder |
| `components/stroke_chart.py` | `StrokeChart` HyperDiv plugin class |
| `components/rowing_chart_assets/stroke_chart.js` | Chart.js rendering, band click-to-zoom, dual Y-axis setup |
| `components/ranked_formatters.py` | `result_table()` — now accepts `on_click` for clickable rows + manual pagination |
| `services/concept2.py` | `Concept2Client.get_strokes()` — fetches the `/strokes` list for a result |
| `app.py` | `selected_session_id` in `app_state`; overlay dispatch in `_dashboard_view()` |

### Entry point

```python
# app.py — _dashboard_view()
if app_state.selected_session_id is not None:
    wo = _workouts_dict.get(str(app_state.selected_session_id))
    session_detail(wo, client, user_id, all_workouts,
                   on_session_click=_open_session)
    return
```

### Clickable result tables

`result_table()` in `ranked_formatters.py` accepts an optional `on_click` callback:

```python
result_table(workouts, on_click=lambda wid: app_state.__setattr__("selected_session_id", wid))
```

When `on_click=None` (the default), the function renders the original read-only
`hd.data_table()`.  When provided, it switches to a custom row renderer using
`hd.scope(r["id"])` per row, with a view icon button at the right edge of each row
and manual prev/next pagination (25 rows per page).

---

## 8. Known Limitations

- **Stroke data not cached.** Each time a session is opened, a fresh API call
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
