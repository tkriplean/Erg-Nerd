# Performance Page — Design & UI Reference

The Performance Page is the central analytical view in Erg Nerd. It lets a rower visualise their entire
history of ranked-event performances, replay how their power-duration curve evolved over time,
and compare multiple prediction models against their personal bests.

For the mathematics of the four prediction models, see **[docs/prediction.md](prediction.md)**.

---

## 1. Sections

```

Chart box:
    Header: "Qualifying Performances through <date>"
    RowingLevel profile warning (only when RL predictor selected and profile incomplete)
    Transport bar: [▶ Play / ⏸ Pause]  [speed]  ─── DateSlider ───
    PowerCurveChart (75vh — Chart.js scatter/line)

    Row 1 settings:
        Intensity: [Pace | Watts]  Log Y   |   Length: [Distance | Duration]  Log X
    Row 2 settings:
        Power curves: [PBs | SBs | None]
    Row 3 settings (this may be out of date):
        Prediction: <custom dropdown>   Show components (toggle + description)     Include [All|PBs|SBs]  |  Events [dropdown]  |  Season [dropdown]


Prediction table (below chart):
    Columns: Event | Your PB | Critical Power | Log-Log | Avg. Paul's Law |
             Avg. RowingLevel | Average
    Accuracy footer: RMSE + R² for each predictor

Workout list (raw qualifying performances matched by current filters)
```

---

## 2. State Variables

All state is declared as `hd.state(...)` at the top of `power_curve_page()`.

### Chart / filter state

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `dist_enabled` | `tuple[bool]` | all True | One flag per RANKED_DISTANCES entry; controls event filter |
| `time_enabled` | `tuple[bool]` | all True | One flag per RANKED_TIMES entry; controls event filter |
| `best_filter` | `str` | `"SBs"` | Row filter: `"All"` \| `"PBs"` \| `"SBs"` |
| `chart_y_metric` | `str` | `"pace"` | Y-axis mode: `"pace"` \| `"watts"` |
| `chart_x_metric` | `str` | `"distance"` | X-axis mode: `"distance"` \| `"duration"` |
| `chart_predictor` | `str` | `"critical_power"` | Active prediction line: `"none"` \| `"pauls_law"` \| `"loglog"` \| `"rowinglevel"` \| `"critical_power"` \| `"average"` |
| `draw_power_curves` | `str` | `"PBs"` | Power-curve overlay: `"PBs"` \| `"SBs"` \| `"None"` |
| `chart_log_x` | `bool` | `True` | Log scale on x-axis |
| `chart_log_y` | `bool` | `False` | Log scale on y-axis |
| `chart_show_components` | `bool` | `False` | Show per-anchor/component sub-curves |
| `chart_compare_wc` | `bool` | `False` | Overlay WC records and WC prediction curve |
| `wc_fetch_key` | `str` | `""` | `"gender\|age\|weight_kg"` — invalidation key for WC data |
| `wc_fetch_done` | `bool` | `False` | True once the WC data fetch task has completed |
| `wc_data` | `dict\|None` | `None` | Cached world-class records and lifetime-best data |
| `cp_fit_key` | `str` | `""` | Hash of CP input data; used to cache the CP fit result |
| `cp_fit_result` | `dict\|None` | `None` | Cached CP fit params from `fit_critical_power()` |

### Simulation transport state

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `sim_playing` | `bool` | `False` | True while the JS animation interval is running |
| `sim_week` | `int` | `999999` | Day offset from `sim_start`; `999999` = "show all data" (end of timeline) |
| `sim_speed` | `str` | `"1x"` | Playback speed: `"0.5x"` \| `"1x"` \| `"4x"` \| `"16x"` |
| `sim_bundle` | `dict\|None` | `None` | Precomputed animation bundle sent to JS; `None` until built |
| `sim_bundle_key` | `str` | `""` | MD5 hash of all inputs that affect bundle content; stale ⟹ rebuild |
| `sim_pending_seek_day` | `int` | `-1` | Day to seek to on next render; `-1` = no pending seek |
| `last_ds_change_id` | `int` | `0` | Tracks DateSlider `change_id` to avoid re-applying stale slider events |
| `last_sim_day_out` | `int` | `-1` | Last `sim_day_out` prop received from JS; drives slider sync |
| `last_sim_done` | `int` | `0` | Last `sim_done` counter received from JS; edge-triggers animation-end handling |

### Render-to-render caches

These avoid re-running expensive filters/computations on every HyperDiv render.
Each cache is a `(key, data)` pair; when the key changes the data is recomputed.

| Prefix | What is cached |
|---|---|
| `_ranked_key` / `_ranked_data` | `_build_ranked_workouts()` — quality-filtered ranked list + seasons |
| `_display_key` / `_display_data` | `_apply_display_filter()` — chart/table display list |
| `_prefilt_key` / `_prefilt_data` | `_ranked_prefilt` — dist/time/excluded-season filtered list |
| `_prefilt_excl_key` / `_prefilt_excl_data` | `_prefilt_excl` — excluded-seasons-only filtered list |
| `_featured_key` / `_featured_data` | `compute_featured_workouts()` — historical PB/SB workouts |
| `_annot_key` / `_annot_data` | `build_sb_annotations()` — DateSlider tick marks |
| `_bounds_key` / `_bounds_data` | `_compute_axis_bounds()` — fixed `(x_bounds, y_bounds)` |

**Note on `excluded_seasons`:** this is a *parameter* passed into `power_curve_page()` from the global filter in `app.py`, not an internal state variable.

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
              ├─ build_chart_config(...)  →  chart_cfg  →  PowerCurveChart
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

### Timeline arithmetic

```
sim_start   = May 1 of the earliest included season's start year
sim_end     = min(today, April 30 of the year after the latest included season)
total_days  = (sim_end - sim_start).days + 1
sim_day_idx = clamp(state.sim_week, 0, total_days − 1)
sim_date    = sim_start + timedelta(days=sim_day_idx)
_at_today   = sim_day_idx >= total_days − 1
_SIM_TODAY  = 999999   (sentinel: "end of timeline / show all data")
```

---

### Architecture: client-side JS animation

The animation runs **entirely in the browser** — the Python/HyperDiv server does **zero work** during playback once the bundle has been delivered.

**One-time setup (on Play press):**
1. Python computes a `bundle_key` — an MD5 hash of all inputs that affect chart content (predictor, best_filter, event selection, excluded seasons, show_watts, x_mode, log_x, show_components, chart_compare_wc, data version).
2. If `state.sim_bundle` is absent or stale (key mismatch), Python spawns an `hd.task()` that runs `_build_sim_bundle_fn()` in a thread.  The bundle is sent to the JS plugin via the `sim_bundle` prop once the task completes.
3. Python sends `sim_command = "play"` via the `sim_command` prop.

**During playback:**
- JS runs `setInterval(tick, 350ms)`.
- Each tick rebuilds scatter datasets, prediction curves, and lookahead overlays from the pre-baked bundle — **no Python round-trips**.
- JS writes back `sim_day_out` (current day) and `sim_done` (completion counter) via `ctx.updateProp()`.

**Back-communication (JS → Python):**

| JS prop | Python action |
|---|---|
| `sim_day_out` | `state.sim_week = chart.sim_day_out` — keeps the DateSlider in sync |
| `sim_done` | When it changes, `state.sim_playing = False` — resets the Play button |

---

### Speed options

| Label | `sim_speed` | Days per JS tick |
|---|---|---|
| `0.5x` | `"0.5x"` | 1 |
| `1x` | `"1x"` | 7 |
| `4x` | `"4x"` | 30 |
| `16x` | `"16x"` | 91 |

Speed changes update only `currentStepDays` in JS via the `sim_speed` prop.  No bundle rebuild is needed.

---

### Bundle structure

`_build_sim_bundle_fn()` returns a dict with these top-level keys:

| Key | Description |
|---|---|
| `workout_manifest` | All workouts oldest-first, with pre-computed x/y/pace/watts/season_idx/cat_key_str fields |
| `keyframes` | Sparse list of frames emitted whenever `lifetime_best` changes (new PB). Each keyframe carries `pred_datasets`, `pred_canvas_labels`, `new_pb_labels`. |
| `static_datasets` | Time-invariant datasets (WC scatter + prediction) baked once |
| `season_meta` | Label, colour, and border colour per season |
| `total_days` | Timeline length |
| `start_day` | Day to begin animation from (30 days before first qualifying event) |
| `pb_badge_lifetime_steps` | How many ticks a "New PB!" badge stays visible (40) |
| `bundle_key` | Hash that JS uses to detect stale bundles |
| `draw_lifetime_line` | Whether to draw the lifetime-best connecting line |
| `draw_season_lines` | Whether to draw per-season best connecting lines |
| `pb_color` / `is_dark` / `show_watts` / `x_mode` / `x_bounds` / `y_bounds` / `log_x` | Display metadata consumed by JS dataset builders |

**Bundle invalidation:** any change to the inputs hashed in `_bundle_key` (predictor, filter, event selection, excluded seasons, theme, x/y mode, WC toggle, data version) causes `state.sim_bundle = None` and triggers a rebuild on the next Play press.

---

### sim_command protocol

Python communicates animation intent to JS via the `sim_command` prop (a string).  JS handles each command in `handleSimCommand()`.

| Value | When sent | JS effect |
|---|---|---|
| `"play"` | `sim_playing=True`, bundle present, no pending seek | Start `setInterval` if not already running |
| `"pause"` | `sim_playing=False` with bundle, OR bundle not ready yet | Clear `setInterval`; render one frame at current position |
| `"stop"` | `_at_today` (slider at end of timeline) | Clear `setInterval`; reset `currentDay = 0` |
| `"seek:N"` | Slider dragged (playing or paused, bundle present) | Pause → seek to day N → render → resume if was playing |

`sim_command` is diffed by HyperDiv; `onPropUpdate` fires only when the value changes, so repeated `"play"` renders cost nothing.

---

### Lookahead overlays

The JS `buildOverlayDatasets()` function scans `workout_manifest` for workouts in the range `(currentDay, currentDay + 4 × stepDays]` that beat the current best at their event category.  It renders:

- **Ghost dots** — a faint scatter point at the upcoming performance's position
- **Arrows** — a dashed line from the current best → the upcoming performance
- **"upcoming PB" canvas label** — event name, % improvement, "upcoming PB" text

### "New PB!" badge

When a keyframe's `new_pb_labels` is non-empty, JS:
1. Copies the labels into `pbBadgeLabels`.
2. Sets `pbBadgeCountdown = pb_badge_lifetime_steps` (40 ticks ≈ 14 seconds at 1×).
3. Merges `pbBadgeLabels` into `allCanvasLabels` on every tick until the countdown expires.

### CP crossover annotation

When predictor is Critical Power and Show Components is enabled, each keyframe carries `pred_canvas_labels` — a bottom-anchored canvas label array (format: `{x, _anchor:"bottom", lines:[...], color}`).  JS merges these into `allCanvasLabels` every tick so the "Fast-twitch and aerobic contributions are equal here" annotation tracks the current CP crossover point.

---

### Expected interaction behaviors

These are the canonical behaviors; any deviation is a bug.

#### Play button

| Starting state | Expected result |
|---|---|
| Slider at end of timeline (`_at_today`) | Rewinds to 30 days before the first qualifying event, then begins playing forward |
| Slider mid-timeline, no bundle cached | Starts bundle computation (loading); animation begins once bundle arrives |
| Slider mid-timeline, bundle cached | Animation resumes from current slider position immediately |
| Animation already playing | Button shows "⏸ Pause"; click pauses animation |

#### Pause button

- JS `setInterval` is cleared immediately on the same render cycle as the click.
- Chart freezes at the day it was on when pause was received (not necessarily the Python slider position — JS drives its own counter during playback).
- DateSlider snaps to the paused day via the `sim_day_out` back-prop.
- Subsequent Python renders send `"pause"` to JS, which is idempotent (no-op + re-render at current position).

#### Seek (slider drag or timeline annotation click)

**While playing:**
1. JS receives `seek:N`.
2. `setInterval` is cleared.
3. Chart renders a single frame at day N.
4. `setInterval` restarts — animation continues forward from day N.
5. DateSlider updates to N.

**While paused:**
1. JS receives `seek:N`.
2. `pauseAnimation()` is a no-op (already paused).
3. Chart renders a single frame at day N.
4. Animation stays paused.

#### Speed change

- Clicking the speed button cycles through `0.5x → 1x → 4x → 16x → 0.5x …`.
- Python updates `sim_speed` prop; JS updates `currentStepDays` immediately via the `sim_speed` prop handler.
- No bundle rebuild — speed is applied to the existing bundle on the very next tick.
- Position is not affected; animation continues forward from wherever it is.

#### Settings change while playing

Settings that affect bundle content (predictor, best_filter, event toggles, excluded seasons, show_watts, x_mode, log_x, show_components, WC toggle):

1. `state.sim_bundle = None` and `state.sim_bundle_key = <new_key>`.
2. Python sends `"pause"` to JS (bundle not ready) → animation halts immediately.
3. Bundle rebuild task launches in the background.
4. When the new bundle arrives, JS **resumes from the same day** (`currentDay` is preserved across bundle replacement).
5. Python sends `"play"` with the new bundle → animation continues.

Settings that do **not** affect bundle content (chart_log_y, draw_power_curves visual options that don't change bundle structure) are handled by the static `applyConfig` path and take effect on the next render without a bundle rebuild.

#### Animation end

When JS `currentDay` reaches `total_days`:
1. The final frame is rendered (all workouts visible).
2. `setInterval` is cleared.
3. `sim_done` counter is incremented and sent to Python via `ctx.updateProp`.
4. Python detects the counter change, sets `state.sim_playing = False`.
5. Play button reverts to "▶ Play".
6. DateSlider lands at the end of the timeline.

Pressing Play again at this point rewinds to `start_day` (30 days before first event).

---

## 6. Chart Settings

### Intensity axis (Row 1, left group)
| Control | State var | Effect |
|---|---|---|
| Pace / Watts toggle | `chart_y_metric` | Switches y-axis between sec/500m and watts |
| Log Y switch | `chart_log_y` | Logarithmic y-axis scale |

### Length axis (Row 1, right group)
| Control | State var | Effect |
|---|---|---|
| Distance / Duration toggle | `chart_x_metric` | Switches x-axis between meters and seconds |
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

## 10. World-Class Comparison

When the user's profile is complete (gender, date of birth, weight), the chart settings
row exposes a **"Compare vs. World-Class"** toggle (`chart_compare_wc`).  When enabled,
an additional CP model curve is drawn representing the age-group world record holder for
the user's category.

### How it works

1. `_load_wc_cp()` manages a lazy `hd.task()` that calls `_fetch_wc_data()`.
2. `_fetch_wc_data(gender, age, weight_kg)` calls `get_age_group_records()` from
   `services/concept2_records.py` to retrieve Concept2 official age-group world records.
3. The records are converted to CP model inputs via `records_to_cp_input()` and fitted
   with `fit_critical_power()`, producing the same four-parameter WC curve.
4. WC data is cached in `state.wc_data` and invalidated via `state.wc_fetch_key`
   (`"gender|age|weight_kg"`).  Re-fetching only occurs when the profile changes.

### State variables
| Variable | Description |
|---|---|
| `chart_compare_wc` | Toggle — show/hide the WC comparison curve |
| `wc_fetch_key` | Profile fingerprint used to detect stale WC data |
| `wc_fetch_done` | True once the WC fetch task has completed |
| `wc_data` | Cached dict with fitted CP params and category label |

### Requirement
Profile must be complete.  If gender, DOB, or weight is missing, the toggle is hidden.

---

## 11. Axis Bounds

To prevent the chart from shifting as the simulation scrubs backward in time, axis bounds are
computed once from the **full** end-state dataset (all qualified workouts across all selected
events/seasons) and held fixed. This includes excluded events (for x-bounds stability).

---

## 12. Code Organisation

| File | Responsibility |
|---|---|
| `components/power_curve_page.py` | State, orchestration, transport controls, bundle key/task management, all UI sub-components |
| `components/power_curve_chart_builder.py` | `build_chart_config()`, `build_pred_datasets()`, `build_wc_static_datasets()`, `compute_lifetime_bests()`, all dataset sub-builders |
| `components/power_curve_chart_plugin.py` | `PowerCurveChart` HyperDiv plugin — declares Python↔JS props (`config`, `show_watts`, `x_mode`, `sim_bundle`, `sim_command`, `sim_speed`, `sim_day_out`, `sim_done`) |
| `components/chart_assets/power_curve_chart_plugin.js` | Full JS animation engine: `tick()`, `tick_noadvance()`, `buildScatterDatasets()`, `buildOverlayDatasets()`, `buildSimOptions()`, `handleSimCommand()`, `applyBundle()`, `applyConfig()`, `canvasLabelsPlugin` |
| `components/date_slider_plugin.py` | `DateSlider` plugin — the timeline scrubber with annotation markers |
| `services/ranked_predictions.py` | `build_prediction_table_data()` — multi-model prediction computation |
| `services/ranked_filters.py` | Quality filters (`apply_quality_filters()`), `seasons_from()` |
| `services/critical_power_model.py` | CP model fitting, curve generation, crossover, sprint/stayer metrics |
| `services/rowing_utils.py` | Constants, pace/watts conversions, Paul's Law, log-log fit, `compute_featured_workouts()` |
| `services/rowinglevel.py` | rowinglevel.com scraper with caching |
| `services/concept2_records.py` | Concept2 official age-group world records fetch + CP fitting |
| `services/formatters.py` | All display formatters including `fmt_result_duration()` |
