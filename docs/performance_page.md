# Performance Page — Design & UI Reference

The Performance Page is the central analytical view in Erg Nerd. It lets a rower visualise their entire
history of ranked-event performances, replay how their power-duration curve evolved over time,
and compare multiple prediction models against their personal bests.

For the mathematics of the four prediction models, see **[docs/prediction.md](prediction.md)**.

---

## 1. Sections

```
Filter bar:
    Include [All|PBs|SBs]  |  Events [dropdown]  |  Season [dropdown]

Chart box:
    Header: "Qualifying Performances through <date>"
    RowingLevel profile warning (only when RL predictor selected and profile incomplete)
    Transport bar: [▶ Play / ⏸ Pause]  [speed]  ─── DateSlider ───
    PerformanceChart (75vh — Chart.js scatter/line)

    Row 1 settings:
        Intensity: [Pace | Watts]  Log Y   |   Length: [Distance | Duration]  Log X
    Row 2 settings:
        Power curves: [PBs | SBs | None]
    Row 3 settings:
        Prediction: <custom dropdown>   Show components (toggle + description)

Prediction table (below chart):
    Columns: Event | Your PB | Critical Power | Log-Log | Avg. Paul's Law |
             Avg. RowingLevel | Average
    Accuracy footer: RMSE + R² for each predictor

Workout list (raw qualifying performances matched by current filters)
```

---

## 2. State Variables

All state is declared as `hd.state(...)` at the top of `performance_page()`.

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `dist_enabled` | `tuple[bool]` | all True | One flag per RANKED_DISTANCES entry; controls event filter |
| `time_enabled` | `tuple[bool]` | all True | One flag per RANKED_TIMES entry; controls event filter |
| `excluded_seasons` | `tuple[str]` | `()` | Seasons hidden from view; entries are "YYYY-YY" format |
| `best_filter` | `str` | `"SBs"` | Row filter: `"All"` \| `"PBs"` \| `"SBs"` |
| `chart_metric` | `str` | `"Pace"` | Y-axis mode: `"Pace"` \| `"Watts"` |
| `chart_x_mode` | `str` | `"distance"` | X-axis mode: `"distance"` \| `"duration"` |
| `chart_predictor` | `str` | `"loglog"` | Active prediction line: `"none"` \| `"pauls_law"` \| `"loglog"` \| `"rowinglevel"` \| `"critical_power"` \| `"average"` |
| `chart_lines` | `str` | `"PBs"` | Power-curve overlay: `"PBs"` \| `"SBs"` \| `"None"` |
| `chart_log_x` | `bool` | `True` | Log scale on x-axis |
| `chart_log_y` | `bool` | `False` | Log scale on y-axis |
| `chart_show_components` | `bool` | `False` | Show per-anchor/component sub-curves |
| `sim_playing` | `bool` | `False` | Whether the animation ticker is running |
| `sim_week` | `int` | `999999` | Day offset from sim_start; `999999` = "show all data" |
| `sim_speed` | `str` | `"1x"` | Playback speed: `"0.5x"` \| `"1x"` \| `"4x"` \| `"16x"` |
| `sim_tick_id` | `int` | `0` | Monotonically incrementing; increment to trigger next tick |
| `sim_last_pb_label` | `str` | `""` | Display text for the "New PB!" badge |
| `sim_pb_set_at_day` | `int` | `-9999` | Day index when most recent PB was set (for badge lifetime) |
| `sim_pb_stored_labels_json` | `str` | `"[]"` | JSON-serialised list of PB overlay label dicts captured at detection time |
| `last_ds_change_id` | `int` | `0` | Tracks DateSlider changes to avoid re-applying stale scrubs |
| `cp_fit_key` | `str` | `""` | Hash of CP input data; used to cache the CP fit result |
| `cp_fit_result` | `dict\|None` | `None` | Cached CP fit params from `fit_critical_power()` |

---

## 3. Data Flow

```
concept2_sync(client)
    └─ all_ranked (quality-filtered)
         └─ all_ranked_raw  ←─ basis for ALL subsequent filtering
              │
              ├─ seasons_from()  →  all_seasons
              │
              ├─ sim_workouts_at(sim_date, selected_dists, selected_times,
              │                  excluded_seasons, best_filter)
              │       └─ sim_wkts  (workouts visible at sim_date)
              │
              ├─ compute_lifetime_bests(sim_wkts)   →  _lb, _lb_anchor
              ├─ compute_lifetime_bests(all_pre_sim) →  _lb_all, _lb_all_anchor
              │
              ├─ excluded_workouts  (events toggled off — plotted faintly)
              │
              ├─ fit_critical_power(_cp_pb_list)  →  _cp_params  (cached)
              ├─ fetch_all_pb_predictions(...)    →  rl_predictions  (async task)
              ├─ compute_pauls_constant()         →  _pauls_k
              │
              ├─ build_chart_config(...)  →  chart_cfg  →  PerformanceChart
              │
              └─ build_prediction_table_data(...)  →  _pred_rows  →  _prediction_table
```

### Filtered vs. all lifetime bests

| Set | Used for | Filtered by |
|---|---|---|
| `_lb` / `_lb_anchor` | Prediction model fits (drives chart curve + table prediction columns) | sim_date + excluded seasons + event toggle |
| `_lb_all` / `_lb_all_anchor` | "Your PB" column only | sim_date + excluded seasons only (event toggle ignored) |

This means toggling an event off hides it from predictions but always preserves its PB in the table.

### Excluded events in scatter

Events deselected via the Events filter are not included in prediction calculations or power-curve lines.
They are still plotted on the chart at very low opacity (alpha ≈ 0.18) so the rower can see them
without them influencing the model fits.

---

## 4. Seasons

- Format: `"YYYY-YY"` e.g. `"2024-25"`, spanning **May 1 → April 30**.
- `all_seasons` is sorted newest-first via `seasons_from()`.
- `excluded_seasons` is a tuple of season strings; use `set(state.excluded_seasons)` for O(1) lookup.
- `_included_seasons` is derived as `[s for s in all_seasons if s not in excluded_seasons]`.
- Seasons drive both the simulation timeline bounds and the colour palette for scatter dots.

---

## 5. Simulation / Timeline

```
sim_start  = May 1 of the earliest included season's start year
sim_end    = min(today, April 30 of the year after the latest included season)
total_days = (sim_end - sim_start).days + 1
sim_day_idx = clamp(state.sim_week, 0, total_days - 1)
sim_date   = sim_start + timedelta(days=sim_day_idx)
_at_today  = sim_day_idx >= total_days - 1
_SIM_TODAY = 999999   (sentinel for "end of timeline")
```

### Playback mechanics

When **Play** is pressed:
- If `_at_today`, the simulation rewinds to 30 days before the first qualifying event,
  so the very first workout appears almost immediately after pressing play rather than showing a blank graph.
- A background task (`hd.task()`) sleeps for `_BASE_TICK_SECS = 0.35s`, then increments
  `sim_week` by `_SPEED_DAYS[sim_speed]` and increments `sim_tick_id` to spawn the next tick.
- The scope key `f"sim_tick_{sim_tick_id}"` ensures each tick is an independent task
  (prevents stale ticks from resuming after a scrub).

### Speed options

| Label | Days per tick |
|---|---|
| `0.5x` | 1 day |
| `1x` | 7 days |
| `4x` | 30 days |
| `16x` | 91 days |

### Lookahead overlays

When not at today, `_compute_lookahead_overlays()` scans the next `4 × step` days for
upcoming PBs and renders:
- **Ghost dots** (faint version of an upcoming improved performance)
- **Arrows** (from current best → upcoming PB location)
- **"New PB!" badge** (canvas overlay, persists for ~40 ticks after a PB is set)

---

## 6. Chart Settings

### Intensity axis (Row 1, left group)
| Control | State var | Effect |
|---|---|---|
| Pace / Watts toggle | `chart_metric` | Switches y-axis between sec/500m and watts |
| Log Y switch | `chart_log_y` | Logarithmic y-axis scale |

### Length axis (Row 1, right group)
| Control | State var | Effect |
|---|---|---|
| Distance / Duration toggle | `chart_x_mode` | Switches x-axis between meters and seconds |
| Log X switch | `chart_log_x` | Logarithmic x-axis scale |

When **Duration** is selected, scatter points use `workout["time"] / 10` as x (seconds),
and prediction curves are transformed so x = `dist × pace / 500` (parametric time).

### Power curves (Row 2)
| Value | What is drawn |
|---|---|
| `"PBs"` | Dashed line connecting lifetime-best dots across all events |
| `"SBs"` | One line per season connecting that season's best dots |
| `"None"` | No connecting lines |

### Prediction line (Row 3)
A custom dropdown shows each method's name (bold) and a short description.
See [docs/prediction.md](prediction.md) for full model mathematics.

| Value | Model |
|---|---|
| `"none"` | No prediction line |
| `"loglog"` | Log-Log Watts Fit |
| `"pauls_law"` | Paul's Law (personalised K) |
| `"critical_power"` | Two-component Critical Power |
| `"rowinglevel"` | RowingLevel population norms |
| `"average"` | Ensemble average of all available models |

### Show components (Row 3, adjacent to predictor dropdown)
Available for all predictors except Log-Log (which has no components to separate).

| Predictor | What "Show components" draws |
|---|---|
| Paul's Law | One curve per PB anchor (before averaging) |
| RowingLevel | One RL curve per PB anchor (before distance-weighted averaging) |
| Critical Power | Fast-twitch and slow-twitch component curves separately |
| Average | All individual model curves used in the average |

### CP Crossover point
The duration `t*` at which fast-twitch and slow-twitch CP contributions are equal.
Visible **only** when Critical Power is selected and **Show components** is enabled.
Rendered as a dashed vertical teal line at `t*` with explanation text at the chart bottom.
Rowers with `t* < ~4 min` are sprint-dominant; `t* > ~20 min` are endurance-dominant.

---

## 7. Paul's Law — Personalised Constant

The population default is K = 5.0 sec/500m per doubling of distance.
The app fits a personalised K from the rower's own PBs (regression through origin;
requires ≥2 PBs, clamped to [0.5, 15.0]).

**Interpretation of K:**
- **K < 5** (e.g. 3–4): aerobic-dominant. You slow down less than average as distance grows.
- **K ≈ 5**: typical balanced rower.
- **K > 5** (e.g. 6–8): sprint-dominant. You're stronger at short distances relative to long ones.

The fitted value and its interpretation are shown below the "Show components" toggle
when Paul's Law is selected.

---

## 8. Prediction Table

### Columns

| Column | Contents |
|---|---|
| Event | Event name + enable/disable toggle |
| Your PB | Unfiltered personal best pace + total time or distance |
| Critical Power | CP model prediction + delta vs PB |
| Log-Log Watts Fit | Log-log model prediction + delta vs PB |
| Avg. Paul's Law | Averaged Paul's Law prediction + delta |
| Avg. RowingLevel | Averaged RL prediction + delta (hidden when profile incomplete) |
| Average | Mean of all available predictions |

### Row ordering
Rows are sorted by expected duration (not by distance/time category separately), so timed events
(1 min, 4 min, 30 min, 60 min) appear interleaved with distance events at their natural positions on
the power-duration curve. For example, 1 min typically appears between 100m and 500m.

### Deltas
A delta such as `+2.3s` (red) means the model predicts 2.3 sec/500m *slower* than the rower's PB
— the PB is atypically strong. A negative delta (green) means the model predicts *faster* than the
rower has achieved, suggesting untapped potential at that event.

### Event toggle
The switch next to each event name controls whether that event is included in the model fits
(prediction columns) and the accuracy RMSE/R² calculation. Toggling an event off also dims its
"Your PB" cell. Tooltip explains: "Include this event's PB in prediction calculations?"

### Result formatting
Sub-hour totals are displayed as `M:SS.t` (e.g. `7:40.8`).
Multi-hour totals are displayed as `1hr 23m 03.7s` for readability.

### Accuracy row
RMSE (sec/500m) and R² computed for each predictor across **enabled events** that have both
a prediction and a PB. Lower RMSE and R² closer to 1.0 indicate better fit.
`n=` shows how many events contributed.

---

## 9. RowingLevel Profile Requirement

RowingLevel predictions require a **completed profile** (gender, date of birth, bodyweight).
When the profile is incomplete:
- The RL chart line and table column are hidden.
- A dismissible warning banner appears. Dismissed state is stored in localStorage
  under key `"rl_notif_dismissed"`.

---

## 10. Axis Bounds

To prevent the chart from shifting as the simulation scrubs backward in time, axis bounds are
computed once from the **full** end-state dataset (all qualified workouts across all selected
events/seasons) and held fixed. This includes excluded events (for x-bounds stability).

---

## 11. Code Organisation

| File | Responsibility |
|---|---|
| `components/performance_page.py` | State, orchestration, all UI sub-components |
| `components/performance_chart_builder.py` | `build_chart_config()`, `build_prediction_table_data()` wrapper, `compute_lifetime_bests()`, dataset sub-builders |
| `components/performance_chart_plugin.py` | HyperDiv `PerformanceChart` Plugin wrapping Chart.js |
| `components/chart_assets/performance_chart_plugin.js` | Custom JS: tick formatters, tooltip callbacks, `canvasLabelsPlugin` |
| `components/date_slider_plugin.py` | `DateSlider` plugin — the timeline scrubber |
| `services/ranked_predictions.py` | `build_prediction_table_data()` — multi-model prediction computation |
| `services/ranked_filters.py` | Quality filters, `sim_workouts_at()`, `seasons_from()` |
| `services/critical_power_model.py` | CP model fitting, curve generation, crossover, sprint/stayer metrics |
| `services/rowing_utils.py` | Constants, pace/watts conversions, Paul's Law, log-log fit |
| `services/rowinglevel.py` | rowinglevel.com scraper with caching |
| `services/formatters.py` | All display formatters including `fmt_result_duration()` |
