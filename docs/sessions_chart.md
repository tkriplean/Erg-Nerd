# Sessions Chart — Design Reference

Pace-vs-date scatter chart for the Sessions tab, implemented as a HyperDiv plugin
backed by Chart.js 4. This document records the design decisions and important
implementation details so that future changes can be made with full context.

---

## Table of Contents

1. [Architecture overview](#architecture-overview)
2. [Data pipeline](#data-pipeline)
3. [Outlier filter (Critical Power)](#outlier-filter-critical-power)
4. [Season-best detection](#season-best-detection)
5. [Point preparation](#point-preparation)
6. [Visual encoding](#visual-encoding)
7. [Interval session display](#interval-session-display)
8. [Chart area locking](#chart-area-locking)
9. [Y-axis range calculation](#y-axis-range-calculation)
10. [X-axis adaptive ticks](#x-axis-adaptive-ticks)
11. [Focus+context / brush navigator](#focuscontext--brush-navigator)
12. [Session filters and window controls](#session-filters-and-window-controls)
13. [Workouts-in-view table](#workouts-in-view-table)
14. [Tooltip design](#tooltip-design)
15. [Prop contract (Python ↔ JS)](#prop-contract-python--js)
16. [Key files](#key-files)

---

## Architecture overview

The chart uses the **focus+context** pattern (also called overview+detail):

```
┌─────────────────────────────────────────┐  flex: 1 (grows to fill)
│  Main chart — windowed/focused view     │
├─────────────────────────────────────────┤  1px separator
│  Overview chart — full history  (88px) │
│  ████▓▓▓▓▓▓▓▓▓▓▓▓████ ← brush rect    │
└─────────────────────────────────────────┘
```

Both panels are Chart.js 4 scatter charts living in the same HyperDiv plugin
shadow root. The overview chart hosts the brush rectangle; the main chart shows
only the sessions inside the current window.

**Why two canvases rather than one?**  The brush rectangle must always span the
full dataset extent while the focused view must have independent axis limits.
Two independent Chart.js instances are the cleanest way to achieve this.

**HyperDiv integration:** The component is a `hd.Plugin` subclass
(`SessionsChart`, in `components/sessions_chart_plugin.py`). Data flows
Python → JS via `ctx.initialProps` and `ctx.onPropUpdate`; user interactions
(brush drags) are reported back via `ctx.updateProp`.

---

## Data pipeline

```
all_workouts (raw Concept2 API dicts)
    │
    ▼  _apply_outlier_filter()
filtered workouts  (removes warm-ups / aborted pieces)
    │
    ├──▶  compute_sb_ids()        → set of IDs that are a season best
    │
    └──▶  prepare_points()        → list of point dicts, sorted largest-dist first
              │
              └──▶  SessionsChart(points=…)   HyperDiv plugin → JS
```

Session-level filters (10k+, Intervals Only, No Intervals) are applied in
`sessions_chart()` **after** the outlier filter and **before** SB detection and
point preparation, so the SB set is always consistent with the visible data.

---

## Outlier filter (Critical Power)

**Purpose:** drop warm-up rows, aborted pieces, and erroneous entries that would
otherwise balloon the y-axis or visually mislead.

**Method:** four-parameter veloclinic (Critical Power) model.

1. Collect the athlete's personal best at each ranked event (non-interval only):
   - Distance events ≥ 500m: best = lowest elapsed time.
   - Timed events: best = greatest distance (highest wattage).
2. Fit the CP model via `fit_critical_power()` (≥ 5 ranked bests required).
3. Solve numerically (Brent's method) for the predicted 2 000m time using the
   fitted curve.
4. Drop any session whose pace > 1.75× the predicted 2k pace.

**Fallback:** if the CP fit is unavailable (too few ranked bests, poor R², etc.),
keep every session ≥ 500m.

**Constants** (all in `sessions_chart_builder.py`):

| Name | Value | Meaning |
|---|---|---|
| `_CP_MIN_DIST_M` | 500 | Min distance included in CP fit |
| `_OUTLIER_FACTOR` | 1.75 | Pace must be ≤ this × predicted 2k pace |
| `_MIN_DIST_M` | 500 | Hard floor — nothing shorter is ever plotted |

---

## Season-best detection

Only **non-interval** sessions at a **ranked event** (distance or duration) are
eligible.  Ranked events are defined in `services/rowing_utils.py` as
`RANKED_DIST_SET` and `RANKED_TIME_SET`.

A "season" is determined by `get_season(date_str)` from the same module; seasons
run roughly May–April (the Concept2 logbook season).

For each `(season, event)` pair the fastest pace wins. The function returns a
`set` of workout IDs; the JS plugin receives this information as a boolean `sb`
field on each point dict.

---

## Point preparation

`prepare_points()` converts raw workout dicts to compact point dicts and sorts
them **largest-`dist` first** so that large circles render behind smaller ones.

### Interval distance accounting

The Concept2 API stores distance differently for interval vs non-interval:

| Field | Non-interval | Interval |
|---|---|---|
| `r["distance"]` | total meters | **work meters only** |
| `r["rest_distance"]` | absent / 0 | **rest meters** (top-level) |

This asymmetry was the source of earlier bugs.  The code now explicitly reads:
```python
work_m = r["distance"]          # work meters (API field for intervals)
rest_m = r.get("rest_distance") or 0   # rest meters (top-level API field)
total_m = work_m + rest_m
```

### Dot sizing

Outer radius (px) = `0.25 × √total_meters`  (the "½√m rule" in the code).

This keeps circles area-proportional to distance while keeping them visually
compact. Small sessions (e.g., a 2k at ~16px radius) and marathon rows (large
radius) span a reasonable range.

### Colour assignment

Colour is **deterministic and session-stable**: `hashlib.md5(str(id)).hexdigest()`
mod 12 selects a palette entry.  The same session always gets the same colour
regardless of filter state.

The 12-entry palette uses hand-tuned HSL triples balanced for readability on
both light and dark themes.

### Colour variants

Each point dict carries multiple pre-computed colour strings so the JS layer
never recomputes opacity maths:

| Field | Opacity | Role |
|---|---|---|
| `c` | 1.00 | Outlines (border of regular dots, SB halo label) |
| `c33` | 0.33 | Regular dot fill |
| `c25` | 0.25 | Hatch tile background (work area tint) |
| `c60` | 1.00* | Interval circle border (rest annulus ring) |
| `cHatch` | 0.60 | Hatch stripe colour (independent from border) |
| `c70` | 0.70 | Overview in-window dot fill |

\* `c60` was originally 0.60 opacity but a linter pass changed it to 1.00.
`cHatch` was introduced simultaneously as a separate field so the stripe and
border opacities can be tuned independently in future.

---

## Visual encoding

### Regular (non-interval) sessions

- Filled circle, radius = `r`.
- Fill: `c33` (33% opacity of the session colour).
- Border: `c` (full opacity, 1px).

### Season-best halos

Rendered as a separate dataset (layer 3, drawn first = behind everything else):
- Transparent fill, gold ring (`rgba(255,210,50,0.90)`), 2px border.
- Radius = `r + 4` px (slightly larger than the dot).

### Interval sessions

A single Chart.js circle encodes both **work extent** and **rest extent**:

```
     ← r (outer, total extent) →
  ┌─────────────────────────────┐
  │  border ring (rest area)   │  width = r - r2
  │  ┌──────────────────────┐  │
  │  │  hatch fill (work)   │  │  radius = r2 (inner edge)
  │  └──────────────────────┘  │
  └─────────────────────────────┘
```

- `pointRadius = (r + r2) / 2` — Chart.js strokes **centred** on the circumference,
  so inner edge = `(r+r2)/2 − (r−r2)/2 = r2`, outer edge = `r`.
- `borderWidth = r − r2` — the rest annulus.
- `borderColor = c60` — full-opacity ring.
- `backgroundColor = makeHatchFill(c25, cHatch, restFraction)` — diagonal
  hatch pattern, stripe density ∝ rest fraction.

#### Hatch fill pattern

`makeHatchFill(lightColor, stripeColor, restFraction)` creates a 10×10 px
`CanvasPattern` with a single diagonal line:

```
moveTo(-1, -1)  →  lineTo(p+1, p+1)   lineCap = "square"
```

**Why overshoot?** Drawing `(0,0)→(p,p)` with `lineCap:"butt"` leaves
sub-pixel gaps where tiles meet because the stroke endpoints land exactly on
tile boundaries.  Extending to `(-1,-1)→(p+1,p+1)` ensures the stroke fully
overlaps the tile boundary on both sides, producing seamless solid stripes.

**Stripe width:** `lw = max(0.75, restFraction × p / √2)` — coverage of the
tile area equals `restFraction`, so visual density directly encodes how much of
the total distance was rest.

**Pattern cache:** keyed on `${lightColor}|${stripeColor}|${bucket}` where
`bucket = round(restFraction × 20)` (5% granularity), preventing redundant
canvas allocation.

---

## Interval session display

### Concept2 interval data structure

The API attaches rest duration to the interval **that precedes the rest**:

```json
{ "type": "distance", "distance": 800, "rest_time": 480 }   ← 8:00 rest after
{ "type": "distance", "distance": 250, "rest_time": 0    }   ← no rest
{ "type": "distance", "distance": 200, "rest_time": 0    }   ← no rest
{ "type": "distance", "distance": 2000,"rest_time": 595  }   ← 9:55 rest after
```

An interval with `rest_time == 0` flows directly into the next with no
prescribed recovery.

### Block detection

`_build_interval_lines()` partitions the interval list into **blocks** by
scanning for `rest_time > 0`.  Each such interval closes a block; any trailing
intervals without rest form the final block.

This handles all real-world structures:
- Simple `N × Xm / Y:TT rest` (each block is a single interval)
- Pyramids / ladders with uniform rest
- Complex structured sets (`800+250+200+2000m / 8:00`)

### Tooltip line generation

**2a — all single-interval blocks:**
- Uniform time, uniform rest → `"N × M:SS  /  M:SS rest"`
- Uniform distance, uniform rest → `"N × Xm  /  M:SS rest"`
- Variable distances, uniform rest → `"d1–d2–...m  /  M:SS rest"`
- Variable rest → inline per-interval: `"800m/8:00  –  250m  –  200m  –  2000m/9:55"`
  (wrapped into rows of 6 if there are many intervals)

**2b — any multi-interval block:**
- One line per block: `"800m+250m+200m+2000m  /  8:00"` or just `"400m+600m+2000m"` if no rest.

A totals footer is always appended: `"Xm work  ·  Ym rest"` (from `_interval_totals()`).

---

## Chart area locking

**Problem:** Chart.js recalculates axis label widths on every update.  When the
tick density changes (e.g., switching from Monthly to Weekly view), the y-axis
label area resizes and the entire plot area shifts horizontally.  This causes
visible "jumps" even with `animation: false`.

**Solution:** `lockChartAreaPlugin` — a Chart.js plugin that runs in the
`afterLayout` hook and forcibly overwrites `chart.chartArea` and both scale
positions with pre-computed constants:

| Constant | Value | Justification |
|---|---|---|
| `CA_LEFT` | 68 px | Fits widest y-label ("1:45.0", ~5 chars at 12px) |
| `CA_TOP` | 18 px | Breathing room above topmost dot |
| `CA_RIGHT` | 14 px | Breathing room from right canvas edge |
| `CA_BOTTOM` | 30 px | One row of x-tick labels |

The plugin is registered only on the **main chart** (not the overview, which
has no y-axis labels and a minimal x-axis).

---

## Y-axis range calculation

**Goal:** show all sessions without wasting whitespace, while ensuring the
slowest (largest) session circle is not clipped at the top.

```javascript
function yRange() {
  // 1. Scan all points for fastest pace (lo), slowest pace (hi), and the
  //    radius of the slowest session (hiR).
  // 2. Compute provisional range = hi − yMin.
  // 3. Convert hiR from pixels to pace units using the actual plot height.
  // 4. yMax = hi + (hiR + 4) × pacePerPx   (one radius + 4px breathing room)
}
```

**Key:** `mainWrap.clientHeight` is the actual rendered height of the container
div (from the DOM), not a guess.  Subtracting `CA_TOP + CA_BOTTOM` yields the
true plot area height in pixels, enabling an accurate px → pace conversion.

The y-axis is **not reversed** in Chart.js terms.  Slower paces have larger
numeric values (more seconds per 500m) and are mapped to larger `yMax`, which
Chart.js then places at the top.

---

## X-axis adaptive ticks

`buildMainXScale.afterBuildTicks` generates ticks based on the visible span:

| Span | Granularity | Starting point |
|---|---|---|
| ≤ 21 days | Daily | Next midnight ≥ `axis.min` |
| ≤ 90 days | Weekly (Mondays) | Next Monday ≥ `axis.min` |
| > 90 days | Monthly (1st of month) | First month boundary ≥ `axis.min` |

All ticks are **guaranteed inside `[axis.min, axis.max]`**.  Earlier versions
rolled back to the 1st of the month unconditionally, which could produce a tick
before `axis.min`, causing Chart.js to widen the axis and compress all data
into a narrow right portion (the "stacking" bug on Week view).

The overview chart always uses **yearly** ticks (1 Jan of each year inside the
full data range).

---

## Focus+context / brush navigator

### Interaction model

| Gesture | Effect |
|---|---|
| Click **outside** brush | Jump: centres brush on click point, immediate rebuild |
| Drag **inside** brush | Pan: smooth brush movement, main chart debounced 100ms |
| Touch equivalents | Same logic via `touchstart/touchmove/touchend` |

**Smooth drag implementation:**
- `mousedown` / `touchstart` enters drag mode, recording the anchor position.
- `mousemove` / `touchmove` calls `setWindow(..., { rebuild: false })` — updates
  only the axis min/max and brush rect, skipping the expensive dataset rebuild.
- A 100ms `setTimeout` debounce triggers a full `rebuild: true` pass while the
  user is still dragging, so the main chart updates but doesn't thrash.
- `mouseup` / `touchend` fires a final `rebuild: true, report: true` call that
  pushes `brush_end` and `change_id` back to Python.

### State synchronisation

The brush position is **Python-owned** (`target_window_start` / `target_window_end`
props) for persistence across re-renders (e.g., filter changes), but JS maintains
its own `brushStartMs` / `brushEndMs` locals during interaction for smooth
real-time updates.  When a drag ends, JS reports the new position via
`ctx.updateProp("change_id", …)`.  Python detects `chart.change_id !=
state.last_change_id` and updates `state.window_end_ms` accordingly.

### Overview rendering

- **Out-of-window dots:** small (1.5px), uniform grey, no border.
- **In-window dots:** larger (2.5px), `c70` fill (70% opacity), thin `c` border.

---

## Session filters and window controls

All controls are stateful via `hd.state()`.  Defaults are intentionally off /
permissive so the chart shows everything on first load.

### Window size

Radio buttons: **Week | Month | Quarter | Season | Year**

Changing the window size snaps `window_end_ms = 0`, which causes `window_bounds_ms()`
to default to the latest session.

| Option | Days |
|---|---|
| Week | 7 |
| Month | 30 |
| Quarter | 91 |
| Season | 183 |
| Year | 365 |

The ◄/► step (if implemented) uses 75% of the window width.

### Interval filter

Radio buttons: **All | Intervals Only | No Intervals**

Uses `r.get("workout_type") in INTERVAL_WORKOUT_TYPES` (set from `rowing_utils.py`).

### 10k+ filter

Checkbox (off by default).  Keeps sessions where
`distance + rest_distance >= 10 000m` — uses both fields so that interval
sessions with significant rest distance are correctly included.

### Filter application order

1. `_apply_outlier_filter(workouts)` — runs on the full dataset before any
   user-facing filters.
2. 10k+ filter.
3. Interval type filter.
4. `compute_sb_ids(filtered)` — SBs are computed from the filtered set, so
   filtered-out sessions cannot claim SB status.
5. `prepare_points(filtered, sb_ids)`.

---

## Workouts-in-view table

After the chart, a data table shows every workout that falls within the current
brush window (both endpoints inclusive, using `_date_to_ms()` on the date
string).

- Sorted descending by date (most recent first).
- Capped at 250 rows (single-page; no pagination overflow).
- Uses the shared `result_table()` renderer from `components/workout_table.py`.
- The heading shows a count: `"Workouts in View  (N)"`.

The table reflects all active user-facing filters but **not** the outlier filter
(the outlier filter is pre-applied and invisible to the user).

---

## Tooltip design

Tooltips use Chart.js `mode: "nearest", intersect: true` so only the dot the
cursor is directly over activates.  The `_halo` dataset is excluded from tooltip
matching via a `filter` callback.

### Non-interval tooltip

Single compact line:
```
Apr 12, 2025
1:52.3 / 500m  ·  5,000m  ·  ★ SB
```

### Interval tooltip

Multi-line:
```
Nov 14, 2025
Avg pace  2:04.7 / 500m
800m+250m+200m+2000m  /  8:00
400m+600m+2000m  /  9:55
3,650m work  ·  1,200m rest
```

Each line in `ivl_desc` corresponds to one structural block as described in the
[Interval session display](#interval-session-display) section.  `rest_desc` is
the totals footer.

---

## Prop contract (Python ↔ JS)

### Python → JS (never written by JS after init)

| Prop | Type | Description |
|---|---|---|
| `points` | `list[dict]` | Point data; full schema below |
| `target_window_start` | `int` | Brush start (ms timestamp) |
| `target_window_end` | `int` | Brush end (ms timestamp) |
| `is_dark` | `bool` | Current theme mode |

### JS → Python (written only by JS interaction)

| Prop | Type | Description |
|---|---|---|
| `brush_start` | `int` | Brush start after last user drag |
| `brush_end` | `int` | Brush end after last user drag |
| `change_id` | `int` | Monotonically incremented on each interaction |

### Point dict schema

```python
{
    "x":        int,        # ms timestamp
    "y":        float,      # pace (sec/500m), rounded to 2dp
    "r":        float,      # outer radius (px) = 0.25 × √total_m
    "r2":       float,      # inner fill radius (px); equals r for non-intervals
    "c":        str,        # HSLA, 1.00 opacity (outlines)
    "c33":      str,        # HSLA, 0.33 opacity (regular dot fill)
    "c25":      str,        # HSLA, 0.25 opacity (hatch tile background)
    "c60":      str,        # HSLA, 1.00 opacity (interval circle border)
    "cHatch":   str,        # HSLA, 0.60 opacity (hatch stripe colour)
    "c70":      str,        # HSLA, 0.70 opacity (overview in-window dots)
    "ivl":      bool,       # is interval workout
    "sb":       bool,       # is season best
    "dist":     int,        # total meters (work + rest); used for draw-order sort
    "work_m":   int,        # work meters
    "rest_m":   int,        # rest meters (0 for non-intervals)
    "ivl_desc": list[str],  # one tooltip line per structural block
    "rest_desc": str,       # "Xm work  ·  Ym rest" totals footer
    "date_str": str,        # formatted date for tooltip ("Apr 12, 2025")
    "dist_str": str,        # formatted distance for tooltip ("5,000m")
}
```

---

## Key files

| File | Role |
|---|---|
| `components/sessions_chart_builder.py` | Data prep, outlier filter, SB detection, interval parsing, point serialisation, HyperDiv component |
| `components/sessions_chart_plugin.py` | HyperDiv `Plugin` subclass — prop definitions, JS asset registration |
| `components/rowing_chart_assets/sessions_chart.js` | Chart.js plugin, brush logic, hatch pattern generator, tooltip callbacks |
| `components/sessions_page.py` | Tab entry point; loads workouts, calls `sessions_chart()` |
| `services/rowing_utils.py` | `INTERVAL_WORKOUT_TYPES`, `RANKED_DIST_SET`, `RANKED_TIME_SET`, `compute_pace`, `get_season` |
| `services/critical_power_model.py` | `fit_critical_power()`, `critical_power_model()` |
