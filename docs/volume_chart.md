# Volume Chart — Design Document

## Overview

The Volume Chart on the Sessions tab shows how many meters were rowed across time, broken down by the physiological intensity zone each meter was performed at. The goal is to make it easy to understand training load distribution at a glance: how much was easy aerobic base, how much was threshold or race-pace work, and how much was rest between intervals.

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

## Scope Filter

| Scope         | Date range                          | Default for    |
|---------------|-------------------------------------|----------------|
| Past Year     | today − 365 days → today            | Weekly/Monthly |
| This Season   | May 1 of current season → Apr 30    |                |
| Past 2 Years  | today − 730 days → today            |                |
| Past 5 Years  | today − 1825 days → today           |                |
| All Time      | no lower bound                      | Seasonal       |

Each view (weekly / monthly / seasonal) remembers its own scope selection independently in `hd.state`.

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

## Architecture

### Service layer (`services/volume_bins.py`)

Pure Python — no HyperDiv, no I/O.

| Function                  | Purpose                                                      |
|---------------------------|--------------------------------------------------------------|
| `get_reference_sbs()`     | Best pace at key events within ±365 days                     |
| `compute_bin_thresholds()`| Build pace cutoffs from ref SBs + log-log fallback            |
| `classify_pace()`         | Map a pace value → bin index 1–6                             |
| `aggregate_workouts()`    | Accumulate meters by week/month/season × bin                 |

### Chart builder (`components/volume_chart_builder.py`)

| Function                   | Purpose                                                     |
|----------------------------|-------------------------------------------------------------|
| `build_volume_chart_config()`| Chart.js stacked bar config dict                          |
| `get_period_rows()`         | List of row dicts for the distribution table               |
| `_classify_distribution()` | 3-zone distribution classification (private)               |

### JS plugin (`components/rowing_chart_assets/volume_chart.js`)

Registered as `VolumeChart` in the HyperDiv plugin system. Injects:
- Y-axis tick formatter: meters → `"10.5k"` / `"500m"`
- Tooltip (`index` mode): shows each non-zero bin + footer total

### HyperDiv plugin wrapper (`components/volume_chart.py`)

`VolumeChart(hd.Plugin)` loads the same Chart.js CDN URL as `RowingChart` (deduplicated by HyperDiv) plus the `volume_chart.js` plugin.

### UI entry point (`components/sessions_tab.py`)

`sessions_tab()` orchestrates data loading (full `get_all_results()` with local cache fallback), the volume chart section, the distribution table, and the recent workouts table.

---

## Future Work

- **Trend line overlay** — rolling 4-week average Z1 % superimposed on the bar chart.
- **Target distribution toggle** — draw a horizontal reference line at a user-selected Z1 target percentage.
- **Per-sport breakdown** — show rower vs. skierg bars side by side rather than merged.
- **Acute:Chronic Workload Ratio (ACWR)** — 7-day rolling / 28-day rolling total meters ratio as an injury-risk indicator.
- **Export** — CSV export of the distribution table.
