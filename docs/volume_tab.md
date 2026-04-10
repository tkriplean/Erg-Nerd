# Volume Tab — Design Document

## Overview

The Volume tab provides a stacked bar chart of training meters broken down by physiological intensity zone, with a distribution data table beneath it. It supports two zone modes — **Pace** and **HR** — that share the same chart widget, aggregation layer, and table structure but differ in how metres are classified.

### Files Involved

| File | Role |
|---|---|
| `components/volume_page.py` | UI entry point: controls, HR callout, data flow, calls chart/table |
| `components/volume_chart_builder.py` | Pure chart config builder and table row generator |
| `components/volume_chart.py` | HyperDiv plugin wrapper for `VolumeChart` (Chart.js) |
| `components/rowing_chart_assets/volume_chart.js` | JS plugin: Y-axis formatter, Chart.js tooltips |
| `services/volume_bins.py` | Pace-zone binning, thresholds, aggregation (`aggregate_workouts`) |
| `services/heartrate_utils.py` | HR validation, max HR resolution, zone classification, HR binning |
| `services/rowinglevel.py` | `load_profile()` / `save_profile()` — max HR persisted to `.profile.json` |

For chart design and pace zone definitions see `docs/volume_chart.md`.
For HR data handling details see `docs/heartrate.md`.

---

## Controls

All controls live in a single `hd.hbox` row at the top of `_volume_section()`.

### View (Weekly / Monthly / Seasonal)
`hd.radio_buttons("Weekly", "Monthly", "Seasonal")` backed by `state.view` (lowercase). Determines which bucket from `aggregate_workouts()` output is used (`weeks` / `months` / `seasons`).

### Scope Dropdown
A `hd.select` with per-view state (`state.weekly_scope`, `state.monthly_scope`, `state.seasonal_scope`). Options: Past Year, This Season, Past 2 Years, Past 5 Years, All Time. Weekly and Monthly default to Past Year; Seasonal defaults to All Time.

The dropdown is keyed with `hd.scope(f"scope_{view}")` so that switching views doesn't carry over the previous view's selection widget state.

### Machine Filter
A `hd.select` populated dynamically from `{w.get("type") for w in all_workouts}`. **Only rendered when the user has more than one machine type.** When a single type is present, `state.machine` is forced to `"All"` and no dropdown is shown.

### Zone Mode (Pace / HR)
`hd.radio_buttons("Pace", "HR")` backed by `state.zone_mode` ("pace" or "hr"). Switches between pace-zone binning and HR-zone binning. See the _Aggregation Paths_ section below.

---

## State Variables

```python
state = hd.state(
    view="weekly",           # "weekly" | "monthly" | "seasonal"
    weekly_scope="past_year",
    monthly_scope="past_year",
    seasonal_scope="all_time",
    machine="All",           # "All" or a Concept2 type string (e.g. "rower")
    zone_mode="pace",        # "pace" | "hr"
)
```

All state is `hd.state` (session-scoped, not persisted across page refreshes). Scope is intentionally kept per-view so switching from Weekly to Monthly doesn't reset the monthly scope the user had selected.

---

## HR Callout

When `state.zone_mode == "hr"`, an info bar is rendered below the controls row by `_hr_callout(all_workouts)`.

### What it shows
- **Max HR** — resolved by `resolve_max_hr(profile, all_workouts)`. Displays the value and source note:
  - `"(estimated)"` — 98th percentile of all valid HR readings; no user input needed.
  - `"(from profile)"` — explicit value from `.profile.json`.
  - `"not set — enter below"` — if neither source yields a value.
- **Inline edit field** — always visible for overriding the max HR.
- **Save button** — only rendered when the field value differs from the stored max HR, preventing accidental saves.
- **HR coverage** — "HR data in N of M workouts."

### Saving max HR
The Save button calls `save_profile({**profile, "max_heart_rate": new_val})` which writes `.profile.json`. On the next render, `resolve_max_hr` will find the explicit value and use it.

### No max HR available
If `_hr_callout` returns `(None, False)` — both the estimate and profile are absent — `_volume_section` returns early, showing only the callout prompt. The chart and table are suppressed until a max HR is entered.

---

## Aggregation Paths

### Pace mode

```python
ref_sbs = get_reference_sbs(all_workouts)
thresholds = compute_bin_thresholds(ref_sbs, all_workouts)
aggregated = aggregate_workouts(all_workouts, thresholds, machine_filter)
```

`aggregate_workouts` uses `workout_bin_meters(w, thresholds)` by default to classify each workout.

### HR mode

```python
aggregated = aggregate_workouts(
    all_workouts,
    machine_filter=machine_filter,
    bin_fn=lambda w: workout_hr_meters(w, max_hr),
)
```

`bin_fn` replaces the default pace binning call. No `thresholds` are needed. The 7-bin shape is identical so the rest of the pipeline is unchanged.

---

## Chart

`build_volume_chart_config(aggregated, ...)` in `components/volume_chart_builder.py` returns a Chart.js config dict. In HR mode, `bin_names`, `bin_colors`, and `draw_order` are overridden with their HR equivalents from `heartrate_utils.py`; in pace mode the defaults apply.

The chart is rendered at `height="42vh"` in an `hd.box`. If `chart_config` is empty (no data for the scope), a "Not enough data" message is shown instead.

---

## Distribution Table

`get_period_rows(aggregated, view, scope, ...)` returns one row per time period. `_distribution_table(rows, view, zone_mode)` renders it with `hd.data_table`.

### Column layout — Pace mode

| Column | Contents |
|---|---|
| Period | Week / month / season label |
| Total | Total metres (work + rest) |
| Rest | Rest metres (intervals only) |
| Z1 Easy (Fast & Slow Aerobic) | Easy aerobic metres + % of work |
| Z2 Threshold | Threshold metres + % of work |
| Z3 Hard (5k + 2k + Fast) | High-intensity metres + % of work |
| Distribution | Classification label (Polarized, Pyramidal, etc.) |

### Column layout — HR mode

| Column | Contents |
|---|---|
| Period | Week / month / season label |
| Total | Total metres (work + rest) |
| Rest | Rest metres (intervals only) |
| Easy (<70%) | Z2 Aerobic + Z1 Recovery metres + % |
| Tempo (70–80%) | Z3 Tempo metres + % |
| Threshold (80–90%) | Z4 Threshold metres + % |
| Max (90%+) | Z5 Max metres + % |
| Distribution | Classification label (same thresholds as pace; "—" if < 500 HR-classified metres) |

HR mode adds a fourth intensity column (splitting "Hard" into Threshold and Max) and always includes Distribution. The distribution classification excludes "No HR" metres (bin 6) from the work denominator — see `docs/heartrate.md` for details.

### Distribution classification
Six possible values: **Easy / LSD**, **Polarized**, **Pyramidal**, **Threshold**, **High Intensity**, **Mixed**, or **—** (insufficient data). See `docs/volume_chart.md` for the threshold rules.

---

## Data Flow

```
volume_page()
  └── hd.task(_fetch)              # load workouts from API or cache
        └── _volume_section(all_workouts)
              ├── controls row     # radio_buttons, scope select, machine select
              ├── _hr_callout()    # [HR mode only] max HR resolve + edit UI
              │     └── resolve_max_hr() → load_profile() + estimate_max_hr()
              ├── aggregate_workouts(bin_fn=…)   # pace or HR path
              ├── build_volume_chart_config()    # Chart.js config dict
              ├── VolumeChart(config=…)          # renders chart
              ├── get_period_rows()              # table row dicts
              └── _distribution_table()          # hd.data_table
```

---

## How to Change Things

**Add a new scope option**: add an `hd.option(...)` inside each scope select and update `_scope_date_range()` in `volume_chart_builder.py`.

**Add a new machine type label**: add an entry to the `_LABELS` dict in `machine_label()`.

**Change the zone mode toggle options**: edit the `hd.radio_buttons("Pace", "HR", ...)` call and add a new aggregation branch in `_volume_section`.

**Change HR zone thresholds**: edit `hr_zone_idx()` in `heartrate_utils.py`. No other files need to change.

**Change the distribution table columns**: edit `_distribution_table()` in `volume_page.py` and potentially `get_period_rows()` in `volume_chart_builder.py` if new row fields are needed.

**Change the chart height**: the `height="42vh"` on the chart box in `_volume_section` is the only place to change it.
