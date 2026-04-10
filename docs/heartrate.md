# Heart Rate — Design Document

## Overview

Heart rate data flows from the Concept2 Logbook API into the volume chart's HR mode. The primary module is `services/heartrate_utils.py`, which provides validation, max HR resolution, zone classification, and per-workout binning. Its output is a 7-element bin vector with the same shape as `services/volume_bins.workout_bin_meters()`, so HR data drops in as a `bin_fn` without any changes to the aggregation layer.

---

## Data Source — Concept2 API HR Fields

The Concept2 API returns HR data at three levels of granularity, all populated when an HR monitor was worn during the workout:

### Workout level
```json
{
  "heart_rate": {
    "min": 120,
    "average": 158,
    "max": 178,
    "ending": 165
  }
}
```
Always present in the response; values are `null` / `0` when no monitor was worn.

### Split level (`workout.splits[]`)
```json
{
  "distance": 500,
  "time": 1234,
  "heart_rate": {
    "min": 148,
    "average": 162,
    "max": 172,
    "ending": 165
  }
}
```
One entry per 500 m split (or time-based split for time pieces). HR reflects that split's intensity rather than the workout average.

### Interval level (`workout.intervals[]`)
```json
{
  "type": "work",
  "distance": 500,
  "time": 1220,
  "rest_time": 120,
  "heart_rate": {
    "min": 160,
    "average": 174,
    "max": 181,
    "ending": 170
  }
}
```
One entry per work or rest interval. `type` is `"work"` or `"rest"`. HR reflects that interval's intensity.

Only `average` is currently used for zone classification; `min`, `max`, and `ending` are available for future features.

---

## Module: `services/heartrate_utils.py`

### Bin layout

The 7-bin layout mirrors `services/volume_bins.BIN_NAMES` so a single aggregation loop serves both modes:

| Bin | Name          | HRmax %  | Colour         |
|-----|---------------|----------|----------------|
| 0   | Rest          | (rest metres) | Grey      |
| 1   | Z5 Max        | > 90 %   | Red            |
| 2   | Z4 Threshold  | 80–90 %  | Orange         |
| 3   | Z3 Tempo      | 70–80 %  | Yellow-green   |
| 4   | Z2 Aerobic    | 60–70 %  | Blue           |
| 5   | Z1 Recovery   | < 60 %   | Light blue     |
| 6   | No HR         | (invalid or absent) | Neutral grey |

Stacked-bar draw order (bottom → top): `[6, 5, 4, 3, 2, 1, 0]` — same as pace mode so visual conventions are consistent.

### Exported constants

| Name | Value | Purpose |
|---|---|---|
| `HR_ZONE_NAMES` | 7-element list | Chart legend labels |
| `HR_ZONE_COLORS` | 7-element list of `(dark, light)` RGBA pairs | Chart colours |
| `HR_ZONE_DRAW_ORDER` | `[6, 5, 4, 3, 2, 1, 0]` | Pass to `build_volume_chart_config(draw_order=)` |
| `HR_Z1_BINS` | `frozenset({4, 5})` | Easy zone (< 70 %) for 3-zone table |
| `HR_Z2_BINS` | `frozenset({3})` | Tempo zone (70–80 %) |
| `HR_Z3_BINS` | `frozenset({1, 2})` | Hard zone (> 80 %) |

`HR_Z3_BINS` is further split in the volume tab into `_HR_Z3A_BINS = frozenset({2})` (Threshold, 80–90 %) and `_HR_Z3B_BINS = frozenset({1})` (Max, > 90 %) for the 4-column table layout. These are defined in `components/volume_page.py` rather than exported from heartrate_utils since they are a UI-level concern.

---

## Validation — `is_valid_hr(val, max_hr=None)`

| Condition | Verdict |
|---|---|
| `val` is `None` or ≤ 0 | Invalid — monitor not worn |
| `val < 40` or `val > 220` | Invalid — physiologically impossible |
| `max_hr` provided and `val > max_hr × 1.05` | Invalid — artifact spike |

The 1.05 × headroom avoids rejecting legitimate readings near true max while filtering sensor glitches.

---

## Max HR Resolution

Max HR is required to compute zone percentages. Resolution is handled by two functions:

### `estimate_max_hr(workouts) → int | None`

Scans all valid HR readings (top-level + per-split + per-interval) across all workouts and returns the **98th percentile**. Returns `None` if fewer than 10 valid readings are found (insufficient data).

The 98th percentile is used rather than the raw maximum to avoid letting a single sensor glitch set an inflated ceiling.

### `resolve_max_hr(profile, workouts) → (int | None, bool)`

Returns `(max_hr, is_estimated)`. Resolution order:

1. **Explicit profile value** (`profile["max_heart_rate"]` — set by the user in the HR callout UI). Wins over any estimate.
2. **Data estimate** via `estimate_max_hr()`. `is_estimated = True`.
3. Both fail → returns `(None, True)`.

The `is_estimated` flag drives the source note in the HR callout: "(estimated)" vs "(from profile)".

---

## Zone Classification — `hr_zone_idx(avg_hr, max_hr) → int`

Maps a validated average HR to a bin index 1–5:

| % of HRmax | Bin | Name          |
|------------|-----|---------------|
| > 90 %     | 1   | Z5 Max        |
| 80–90 %    | 2   | Z4 Threshold  |
| 70–80 %    | 3   | Z3 Tempo      |
| 60–70 %    | 4   | Z2 Aerobic    |
| ≤ 60 %     | 5   | Z1 Recovery   |

Assumes `avg_hr` has already been validated. Invalid HR reads → bin 6 (No HR), handled by the caller.

---

## Per-Workout Binning — `workout_hr_meters(workout, max_hr) → list[float]`

Returns a 7-element float list. Resolution is attempted in this priority order:

### 1. Per-split HR (highest resolution)
If `workout.workout.splits` exists and at least one split has a valid HR average:
- Each split's metres go into its own HR zone bin.
- Splits without valid HR → bin 6 (No HR).
- Returns early; does not fall through to interval or top-level.

### 2. Per-interval HR
If `workout.workout.intervals` exists and at least one interval has a valid HR average:
- Intervals with `type == "rest"` → bin 0 (Rest), regardless of HR.
- Work intervals classified by their own average HR.
- Work intervals without valid HR → bin 6.
- Returns early.

### 3. Top-level HR
If `workout.heart_rate.average` is valid:
- For interval workouts: rest metres (from explicit rest intervals or `rest_distance` fields) → bin 0; remaining work metres → HR zone bin.
- For steady-state workouts: all `workout.distance` metres → HR zone bin.

### 4. No HR data
- Interval rest metres (if any) → bin 0.
- All other metres → bin 6 (No HR).

**Key design decision**: interval rest metres always go to bin 0 regardless of HR. Even if a rest-period HR reading were available, rest metres are physiologically and analytically different from work metres and should be separated.

---

## Coverage Counting — `hr_coverage(workouts) → (int, int)`

Returns `(workouts_with_hr, total_workouts)`. A workout "has HR" if its **top-level** `heart_rate.average` is valid. Per-split and per-interval HR are not checked here — top-level presence is a reliable and cheap proxy.

This count is displayed in the HR callout: "HR data in N of M workouts."

---

## 3-Zone Model for the Distribution Table

When HR mode is active, `get_period_rows()` in `volume_chart_builder.py` receives:

| Parameter | Value | Meaning |
|---|---|---|
| `z1_bins` | `frozenset({4, 5})` | Easy: Z2 Aerobic + Z1 Recovery (< 70 %) |
| `z2_bins` | `frozenset({3})` | Tempo: Z3 Tempo (70–80 %) |
| `z3_bins` | `frozenset({1, 2})` | Hard: Z4 Threshold + Z5 Max (> 80 %) |
| `z3a_bins` | `frozenset({2})` | Threshold sub-zone (80–90 %) |
| `z3b_bins` | `frozenset({1})` | Max sub-zone (> 90 %) |
| `no_data_bins` | `frozenset({6})` | Exclude "No HR" from classification denominator |

The `no_data_bins` exclusion matters: if a weekly period has 80 % of its metres in the "No HR" bin, the remaining 20 % classified by HR should still receive a meaningful distribution classification — rather than being diluted to near-zero percentages that produce "—".

Distribution classification thresholds (Polarized, Pyramidal, etc.) are the same as pace mode. They are applied to the fractions computed over HR-classified metres only. A period with < 500 classified metres → "—".

---

## Design Decisions

**Why % of HRmax rather than HRR (heart rate reserve)?**
HRmax is simpler to estimate and does not require a resting HR measurement. The Concept2 data does not include resting HR. HRmax zones are the more common framing in endurance sport literature.

**Why 5 zones rather than 3?**
The 5-zone model (Z1–Z5) maps naturally to established physiology: recovery, aerobic, tempo, threshold, VO₂max. The 3-zone summary (Easy / Tempo / Hard) is derived from the 5-zone data and used in the distribution table for consistency with pace mode.

**Why estimate at the 98th percentile rather than the max?**
The raw maximum reading can be inflated by sensor dropout spikes. The 98th percentile is robust to occasional artifacts while still reflecting true near-max efforts.

**Why top-level HR for coverage, not per-split?**
Top-level HR is always populated (with zeros) even when no monitor was worn, making it easy to check. Per-split HR requires iterating nested arrays, which is slower and unnecessary for a simple coverage count.

---

## Future Extensions

- **HR drift analysis**: show how HR trends across splits within a workout.
- **Per-workout HR zone pie** in the session detail view.
- **Resting HR field** in profile to enable HRR-based zones.
- **HR-pace efficiency metric**: e.g. pace / (HR / max_hr) per workout.
- **Tempo/threshold boundary refinement**: lactate threshold correlates with a deflection point in the HR–pace curve, potentially auto-detectable from workout history.
