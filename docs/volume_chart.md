# Volume Chart — Design Document

## Overview

The Volume Chart on the Volume page shows how many meters were rowed across time, broken down by the physiological intensity zone each meter was performed at. The goal is to make it easy to understand training load distribution at a glance: how much was easy aerobic base, how much was threshold or race-pace work, and how much was rest between intervals.

The chart supports two **zone modes**:

- **Pace mode** (default) — zones derived from personal-best pace thresholds, relative to the user's recent performances.
- **HR mode** — zones derived from percentage of HRmax, using per-split or per-interval HR data where available.

A **Pace / HR toggle** in the controls row switches between the two modes. Both modes share the same visual language (7-bin stacked bar, same draw order, same distribution table structure).

---

## Visual Layout

**Stacked bar chart** — one bar per time period, stacked bottom-to-top:

| Position | Zone             | Rationale                                                   |
|----------|------------------|-------------------------------------------------------------|
| Bottom   | Slow Aerobic     | The largest volume zone for healthy training; forms the base |
| ↑        | Fast Aerobic     | Still easy aerobic; sits naturally above the recovery base   |
| ↑        | Threshold        | Moderate intensity; the "middle" of the stack visually       |
| ↑        | 5k               | High intensity                                               |
| ↑        | 2k               | Very high intensity                                          |
| ↑        | Fast             | Sprint / VO₂max+                                             |
| Top      | Rest             | Interval rest distance; floated to the top to distinguish it from work |

---

## Pace Zone Definitions

Zones are defined relative to **reference SBs** — the best performance at each key event within ±365 days of today. This window spans past and future because the goal is to establish a stable physiological baseline for binning, not to enforce a cutoff.

| Bin           | Pace Range                                        | Approx. Physiology              |
|---------------|---------------------------------------------------|---------------------------------|
| Fast          | < midpoint(1k SB, 2k SB)                         | Phosphagen / max sprint         |
| 2k            | midpoint(1k, 2k) → midpoint(2k, 5k)              | VO₂max race pace                |
| 5k            | midpoint(2k, 5k) → midpoint(5k, 60min)           | VO₂max / high aerobic           |
| Threshold     | midpoint(5k, 60min) → midpoint(60min, marathon)   | Lactate threshold / tempo       |
| Fast Aerobic  | midpoint(60min, marathon) → marathon + 3 s        | Aerobic base, upper end         |
| Slow Aerobic  | > marathon + 3 s                                  | Recovery / easy distance        |
| Rest          | N/A (interval rest distance, explicitly flagged)  | Active recovery                 |

The marathon + 3 s boundary for the Fast/Slow Aerobic split was chosen because marathon pace represents an athlete's long-run aerobic ceiling; anything marginally slower is aerobic base, and anything substantially slower is recovery-pace rowing.

### Reference SB Key Events

| Event    | Type      | Proxy distance for log-log fallback |
|----------|-----------|-------------------------------------|
| 1k       | Distance  | 1,000 m                             |
| 2k       | Distance  | 2,000 m                             |
| 5k       | Distance  | 5,000 m                             |
| 60 min   | Time      | 10,000 m (≈ 60 min at moderate pace)|
| Marathon | Distance  | 42,195 m                            |

### Fallback when SBs are missing

1. **Log-log power-law fit** across all lifetime ranked non-interval workouts. If at least two ranked categories have data, the fit predicts pace at any distance.
2. **Simple proportional extrapolation** for any event still missing after the log-log step (e.g. 1k ≈ 2k × 0.96, marathon ≈ 60min × 1.15).
3. If neither 2k nor 5k can be determined, binning is skipped entirely and all work meters are placed in Slow Aerobic (totals remain accurate).

---

## Interval Workout Handling

For **interval workouts** (`workout_type` in `INTERVAL_WORKOUT_TYPES`):

- Each individual interval is classified by its own average pace: `(interval_time / 10) / (interval_dist / 500)`.
- Interval rest distance is taken from the top-level `rest_distance` field if present; otherwise it equals `total_distance − sum(interval_distances)`.
- All rest distance goes into the **Rest** bin.

For **steady-state workouts**: the session's overall average pace determines the bin (one bin for the entire workout).

---

## Time Bucketing

| View     | Bucket         | Key format   | Label example  |
|----------|----------------|--------------|----------------|
| Weekly   | ISO week       | `YYYY-Www`   | `Jan 6`        |
| Monthly  | Calendar month | `YYYY-MM`    | `Jan '25`      |
| Seasonal | Rowing season  | `YYYY-YY`    | `2025-26`      |

Seasons run **May 1 → Apr 30**, consistent with the rest of the app.

---

## Time Windowing

Time-windowing is handled by the global `excluded_seasons` filter in `app.py` — the volume
page itself does not have a scope selector. All periods are shown by default; the user can
hide specific seasons via the global filter in the nav bar.

---

## Machine Filter

A dropdown populated dynamically from the `type` fields in the local workout cache (e.g. `rower`, `skierg`, `bike`). Defaults to **All Machines**.

---

## Distribution Classification (Data Table)

The data table below the chart shows one row per period with zone breakdowns and a **training distribution** classification. Distribution uses a **3-zone model**:

| Zone | Bins                            | Physiological meaning       |
|------|---------------------------------|-----------------------------|
| Z1   | Fast Aerobic + Slow Aerobic     | Below LT1 — easy aerobic    |
| Z2   | Threshold                       | LT1–LT2 — moderate/tempo   |
| Z3   | 5k + 2k + Fast                  | Above LT2 — high intensity  |

Zone percentages are computed from **work meters only** (rest is excluded from the denominator).

### Classification Rules (applied in order)

| Label          | Criteria                                          | Literature reference        |
|----------------|---------------------------------------------------|-----------------------------|
| Easy / LSD     | Z1 ≥ 90 %, Z2 < 5 %, Z3 < 5 %                   | Pure base / long slow distance |
| Polarized      | Z1 ≥ 65 %, Z3 ≥ 15 %, Z3 > Z2                   | Seiler polarized model      |
| Pyramidal      | Z1 ≥ 65 %, Z2 > Z3, Z2 ≥ 10 %                   | Classic pyramidal model     |
| Threshold      | Z2 ≥ 20 %                                        | High threshold / tempo bias |
| High Intensity | Z3 ≥ 35 %                                        | Race-prep / peaking block   |
| Mixed          | Does not satisfy any pattern above               | Unstructured / transition   |
| —              | Work meters < 500 m                              | Insufficient data           |

The thresholds are deliberately generous (65 % Z1 rather than 80 %) to accommodate the natural variation in weekly training data versus idealised textbook models.

### Table Columns

| Column       | Contents                                         |
|--------------|--------------------------------------------------|
| Period       | Week / month / season label                      |
| Total        | Total meters (work + rest)                       |
| Rest         | Rest distance (intervals only)                   |
| Z1 Easy      | Easy aerobic meters + % of work                  |
| Z2 Threshold | Threshold meters + % of work                     |
| Z3 Hard      | High-intensity meters + % of work                |
| Distribution | Classification label with colour badge           |

Distribution badge colours:
- 🔵 **Polarized** (blue)
- 🟢 **Pyramidal** (green)
- 🟠 **Threshold** (orange)
- 🔴 **High Intensity** (red)
- 🩵 **Easy / LSD** (light blue)
- ⚫ **Mixed** (grey)

---

## Architecture (Pace Mode)

### Service layer (`services/volume_bins.py`)

Pure Python — no HyperDiv, no I/O.

| Function                  | Purpose                                                              |
|---------------------------|----------------------------------------------------------------------|
| `get_reference_sbs()`     | Best pace at key events within ±365 days                             |
| `compute_bin_thresholds()`| Build pace cutoffs from ref SBs + log-log fallback                   |
| `classify_pace()`         | Map a pace value → bin index 1–6                                     |
| `aggregate_workouts(bin_fn=)` | Accumulate meters by week/month/season × bin; `bin_fn` overrides default binning |

### Chart builder (`components/volume_chart_builder.py`)

| Function                   | Purpose                                                     |
|----------------------------|-------------------------------------------------------------|
| `build_volume_chart_config()`| Chart.js stacked bar config dict (accepts `bin_names`, `bin_colors`, `draw_order`) |
| `get_period_rows()`         | List of row dicts for the distribution table (accepts `z1/z2/z3_bins`) |
| `_classify_distribution()` | 3-zone distribution classification (private)               |

### JS plugin (`components/rowing_chart_assets/volume_chart.js`)

Registered as `VolumeChart` in the HyperDiv plugin system. Injects:
- Y-axis tick formatter: meters → `"10.5k"` / `"500m"`
- Tooltip (`index` mode): shows each non-zero bin + footer total

### HyperDiv plugin wrapper (`components/volume_chart_plugin.py`)

`VolumeChart(hd.Plugin)` loads the same Chart.js CDN URL as `RowingChart` (deduplicated by HyperDiv) plus the `volume_chart.js` plugin.

### UI entry point (`components/volume_page.py`)

`volume_page()` orchestrates data loading, the volume chart section (with zone-mode toggle, optional HR callout), and the distribution table.

---

## HR Mode

> For full HR data handling details (validation, zone model, binning algorithm) see `docs/heartrate.md`.
> For UI controls and data flow see `docs/volume_page.md`.

### Enabling HR Mode

Toggle the **Pace / HR** radio buttons in the controls row. The mode is stored in `state.zone_mode` ("pace" | "hr").

### Zone Definitions (% of HRmax)

| Bin | Name          | HRmax %      | Colour         |
|-----|---------------|--------------|----------------|
| 0   | Rest          | (rest metres)| Grey           |
| 1   | Z5 Max        | > 90 %       | Red            |
| 2   | Z4 Threshold  | 80–90 %      | Orange         |
| 3   | Z3 Tempo      | 70–80 %      | Yellow/green   |
| 4   | Z2 Aerobic    | 60–70 %      | Blue           |
| 5   | Z1 Recovery   | < 60 %       | Light blue     |
| 6   | No HR         | (no valid HR) | Neutral grey  |

Draw order (bottom → top): `[6, 5, 4, 3, 2, 1, 0]` — No HR at the visual bottom, Z5 near the top, Rest as a thin cap.

### Resolution Priority

For each workout, bin assignment uses the highest-resolution HR data available:

1. **Per-split HR** (`workout.splits[].heart_rate.average`) — each split's metres are classified by its own average HR. Splits without valid HR → bin 6 (No HR).
2. **Per-interval HR** (`workout.intervals[].heart_rate.average`) — each work interval classified by its HR; explicit rest intervals (`type == "rest"`) → bin 0 (Rest). Intervals without valid HR → bin 6.
3. **Top-level HR** (`workout.heart_rate.average`) — all work metres go into one HR zone bin.
4. **No HR anywhere** → all metres → bin 6 (No HR).

Interval rest metres always go to bin 0 regardless of HR data.

### Max HR

Max HR is required to compute zone percentages. Resolution order:

1. **Explicit profile value** (`max_heart_rate` in `.profile.json`) — wins over any estimate.
2. **Estimated from data** — 98th percentile of all valid HR readings across all workouts (top-level + per-split + per-interval). Requires at least 10 valid readings; returns `None` otherwise.

The HR mode callout (shown below the controls row when HR mode is active) displays the current max HR with its source note and an inline edit field that saves to `.profile.json` via `save_profile()`.

If no max HR can be determined, the chart and table are suppressed and only the edit prompt is shown.

### Outlier / Validation Rules (`is_valid_hr`)

| Rule | Condition | Result |
|---|---|---|
| Missing / zero | `hr is None` or `hr ≤ 0` | Invalid |
| Physiologically impossible | `hr < 40` or `hr > 220` | Invalid |
| Artifact above max | `hr > max_hr × 1.05` | Invalid |

Invalid readings are treated as No HR (bin 6) rather than causing errors.

### HR Coverage

The callout line shows "HR data in N of M workouts." A workout is counted as having HR if its top-level `heart_rate.average` is valid. Per-split / per-interval HR is not checked here — top-level presence is the cheapest reliable signal.

### Distribution Table in HR Mode

HR mode uses a 5-zone model exposed as 4 data columns (Z3 is split into two):

| Column header | HR bins | Description |
|---|---|---|
| Easy (<70%) | bins 4, 5 | Z2 Aerobic + Z1 Recovery |
| Tempo (70–80%) | bin 3 | Z3 Tempo |
| Threshold (80–90%) | bin 2 | Z4 Threshold |
| Max (90%+) | bin 1 | Z5 Max |

A **Distribution** column is included. Classification uses the same thresholds as pace mode (Polarized, Pyramidal, etc.) but the percentages are computed over HR-classified metres only — the "No HR" bin (bin 6) is excluded from the denominator so that unmonitored sessions don't dilute zone fractions. Periods with fewer than 500 HR-classified metres receive "—".

---

## Architecture

### Service layer

| File | Key functions |
|---|---|
| `services/volume_bins.py` | `get_reference_sbs()`, `compute_bin_thresholds()`, `aggregate_workouts(bin_fn=)` |
| `services/heartrate_utils.py` | `is_valid_hr()`, `resolve_max_hr()`, `hr_zone_idx()`, `workout_hr_meters()`, `hr_coverage()` |

`aggregate_workouts()` accepts a `bin_fn` keyword argument. When provided, it replaces the default `workout_bin_meters(w, thresholds)` call, allowing HR-mode binning without any other code changes. The call from the volume tab in HR mode is:

```python
aggregate_workouts(
    all_workouts,
    machine_filter=machine_filter,
    bin_fn=lambda w: workout_hr_meters(w, max_hr),
)
```

### Chart builder (`components/volume_chart_builder.py`)

Both exported functions accept optional override arguments so the same code serves pace and HR modes:

```python
build_volume_chart_config(aggregated, ..., bin_names=None, bin_colors=None, draw_order=None)

get_period_rows(
    aggregated, ...,
    z1_bins=None, z2_bins=None, z3_bins=None,
    z3a_bins=None, z3b_bins=None,   # split Z3 into two sub-columns
    no_data_bins=None,               # exclude from classification denominator
)
```

All defaults preserve pace-mode behavior — no existing callers need to change.

---

## Future Work

- **Trend line overlay** — rolling 4-week average Z1 % superimposed on the bar chart.
- **Target distribution toggle** — draw a horizontal reference line at a user-selected Z1 target percentage.
- **Per-sport breakdown** — show rower vs. skierg bars side by side rather than merged.
- **Acute:Chronic Workload Ratio (ACWR)** — 7-day rolling / 28-day rolling total meters ratio as an injury-risk indicator.
- **Export** — CSV export of the distribution table.
- **HR mode refinements** — intra-workout HR dropout detection; pace–HR mismatch flagging; per-split HR visualisation.
