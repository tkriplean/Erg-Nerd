# Volume Tab — Design Document

## Overview

The Volume tab provides a stacked bar chart of training meters broken down by physiological intensity zone, with a distribution data table beneath it. It supports two zone modes — **Pace Intensity** and **HR** — that share the same chart widget, aggregation layer, and table structure but differ in how meters are classified.

### Files Involved

| File | Role |
|---|---|
| `components/volume_page.py` | UI entry point: controls, HR callout, data flow, calls chart/table |
| `components/volume_chart_builder.py` | Pure chart config builder and table row generator |
| `components/volume_chart_plugin.py` | HyperDiv plugin wrapper for `VolumeChart` (Chart.js) |
| `components/rowing_chart_assets/volume_chart.js` | JS plugin: Y-axis formatter, Chart.js tooltips |
| `services/volume_bins.py` | Pace-zone binning, thresholds, aggregation (`aggregate_workouts`) |
| `services/heartrate_utils.py` | HR validation, max HR resolution, zone classification, HR binning |

For chart design and pace zone definitions see `docs/volume_chart.md`.
For HR data handling details see `docs/heartrate.md`.

---

## Controls

All controls live in a single `hd.hbox` row at the bottom of `_volume_section()`, rendered
**after** the chart.

### View (Weekly / Monthly / Seasonal)
`hd.radio_buttons("Weekly", "Monthly", "Seasonal")` backed by `state.view` (lowercase). Determines which bucket from `aggregate_workouts()` output is used (`weeks` / `months` / `seasons`).

### Zone Mode (Pace Intensity / HR)
`hd.radio_buttons("Pace Intensity", "HR Intensity")` backed by `state.zone_mode` (`"pace_intensity"` or `"hr"`). Switches between pace-zone binning and HR-zone binning. See the _Aggregation Paths_ section below.

**Note:** Season and machine filtering are applied globally (passed in from `app.py`). The volume page itself does not render a scope or machine dropdown — those controls live in the nav bar.

---

## State Variables

```python
state = hd.state(
    view="monthly",            # "weekly" | "monthly" | "seasonal"
    zone_mode="pace_intensity", # "pace_intensity" | "hr"
)
```

All state is `hd.state` (session-scoped, not persisted across page refreshes).

---

## Rendering Order

Within `_volume_section()`, UI is rendered top-to-bottom as:

1. H1 heading ("How Does Your Work Stack Up?")
2. Chart (stacked bar) — or "Not enough data" notice if `chart_config` is empty
3. Controls row (view toggle + zone mode toggle)
4. HR callout (HR mode only, rendered after controls)
5. Distribution table

---

## HR Callout

When `state.zone_mode == "hr"`, an info bar is rendered below the controls row by `_hr_callout(all_workouts, profile)`.

### What it shows
- **Max HR** — resolved by `resolve_max_hr(profile, all_workouts)`. Displays the value and source note:
  - `"(estimated)"` — 98th percentile of all valid HR readings; no user input needed.
  - `"(from profile)"` — explicit value from browser localStorage (`"profile"` key).
  - No label shown if neither source yields a value.
- **Inline edit field** — always visible for overriding the max HR.
- **Save button** — only rendered when the field value differs from the stored max HR, preventing accidental saves.
- **HR coverage** — "HR data in N of M workouts."

### Saving max HR
The Save button writes directly to `hd.local_storage.set_item("profile", ...)`, merging the new `max_heart_rate` into the existing profile JSON. On the next render, `resolve_max_hr` will find the explicit value and use it.

### No max HR available
`max_hr` is resolved by `resolve_max_hr()` **before** the chart is rendered. If `state.zone_mode == "hr"` and `max_hr` is falsy, `_volume_section` returns early, showing only the controls row and HR callout prompt. The chart and table are suppressed until a max HR is entered.

---

## Aggregation Paths

### Pace Intensity mode

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

The chart is rendered at `height="42vh"` in an `hd.box`. If `chart_config` is empty (no data), a "Not enough data" message is shown instead.

---

## Distribution Table

`get_period_rows(aggregated, view, scope, ...)` returns one row per time period. `_distribution_table(rows, view, zone_mode)` renders it with a custom CSS Grid table.

### Column layout — Pace Intensity mode

| Column | Contents |
|---|---|
| Period | Week / month / season label |
| Total | Total meters (work + rest) |
| Rest | Rest meters (intervals only) |
| Z1 Easy (Fast & Slow Aerobic) | Easy aerobic meters + % of work |
| Z2 Threshold | Threshold meters + % of work |
| Z3 Hard (5k + 2k + Fast) | High-intensity meters + % of work |
| Distribution | Classification label (Polarized, Pyramidal, etc.) |

### Column layout — HR mode

| Column | Contents |
|---|---|
| Period | Week / month / season label |
| Total | Total meters (work + rest) |
| Rest | Rest meters (intervals only) |
| Easy (<70%) | Z2 Aerobic + Z1 Recovery meters + % |
| Tempo (70–80%) | Z3 Tempo meters + % |
| Threshold (80–90%) | Z4 Threshold meters + % |
| Max (90%+) | Z5 Max meters + % |
| Distribution | Classification label (same thresholds as pace; "—" if < 500 HR-classified meters) |

HR mode adds a fourth intensity column (splitting "Hard" into Threshold and Max) and always includes Distribution. The distribution classification excludes "No HR" meters (bin 6) from the work denominator — see `docs/heartrate.md` for details.

### Distribution classification
Six possible values: **Easy / LSD**, **Polarized**, **Pyramidal**, **Threshold**, **High Intensity**, **Mixed**, or **—** (insufficient data). See `docs/volume_chart.md` for the threshold rules.

---

## Data Flow

```
volume_page()
  └── concept2_sync(client, user_id)   # load/sync workouts
        └── _volume_section(all_workouts, profile, machine)
              ├── max_hr = resolve_max_hr()   # computed before chart
              ├── aggregate_workouts(bin_fn=…) # pace or HR path
              ├── build_volume_chart_config()  # Chart.js config dict
              ├── VolumeChart(config=…)        # renders chart
              ├── controls row                 # view + zone mode toggles
              ├── _hr_callout()                # [HR mode only] after controls
              └── _distribution_table()        # CSS Grid table
```

---

## How to Change Things

**Add a new machine type label**: add an entry to the `_LABELS` dict in `machine_label()`.

**Change the zone mode toggle options**: edit the `hd.radio_buttons(...)` call and add a new aggregation branch in `_volume_section`.

**Change HR zone thresholds**: edit `hr_zone_idx()` in `heartrate_utils.py`. No other files need to change.

**Change the distribution table columns**: edit `_distribution_table()` in `volume_page.py` and potentially `get_period_rows()` in `volume_chart_builder.py` if new row fields are needed.

**Change the chart height**: the `height="42vh"` on the chart box in `_volume_section` is the only place to change it.
