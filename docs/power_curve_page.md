`Power Curve Page` — Design & UI Reference
==========================================

The Power Curve Page is the central analytical view in Erg Nerd. It lets a rower
visualise their entire history of ranked-event performances, replay how their
power-duration curve evolved over time, and compare multiple prediction models
against their personal bests.

For the mathematics of the four prediction models, see [docs/prediction.md][1].

[1]: <prediction.md>

1. Sections
-----------

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Chart box:
    Header: "Qualifying Performances through <date>"
    RowingLevel profile warning (only when RL predictor selected and profile incomplete)
    Transport bar: [▶ Play / ⏸ Pause]  [speed] 
    PowerCurveChart (75vh — Chart.js scatter/line)

    Row 1 settings:
        Intensity: [Pace | Watts]  Log Y   |   Length: [Distance | Duration]  Log X
    Row 2 settings:
        Overlay bests: [PBs | SBs | None]
    Row 3 settings:
        Prediction: <custom dropdown>   Show components (toggle + description)
        Include [All|PBs|SBs]  |  Events [dropdown]  |  Season [dropdown]
        Compare vs. World-Class (when profile complete)

Prediction table (below chart):
    Columns: Event | Your PB | Critical Power | Log-Log | Avg. Paul's Law |
             Avg. RowingLevel | Average
    Accuracy footer: RMSE + R² per predictor (computed in services layer)

Workout list (raw qualifying performances matched by current filters)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

2. Module Guide
---------------

The Power Curve feature spans the UI layer, a pure-Python pipeline layer, and a
JS animation engine. These modules are arranged so that every layer has a single
responsibility and can be read top-to-bottom without chasing definitions across
files.

### UI layer (HyperDiv — `components/`)

+--------------------------------------------+------------------------------------------+
| Module                                     | Responsibility                           |
+--------------------------------------------+------------------------------------------+
| `power_curve_page.py`                      | Orchestrator + page-local UI. Declares   |
|                                            | `hd.state(...)`, wires the               |
|                                            | filter/pipeline/animation layers         |
|                                            | together, and hosts the layout and       |
|                                            | sub-view functions (`_page_header`,      |
|                                            | `_chart_section`, `_chart_settings`,     |
|                                            | `_prediction_table`,                     |
|                                            | `_wr_compare_section`,                   |
|                                            | `_rl_profile_notice`). Contains no model |
|                                            | math and no cache-invalidation hashing — |
|                                            | those live in the pure layers.           |
+--------------------------------------------+------------------------------------------+
| `power_curve_workouts.py`                  | **Pure, no HyperDiv.** The filtered      |
|                                            | collection of the user's workouts as the |
|                                            | page sees it. `FilterSpec` (frozen       |
|                                            | dataclass — the cache key; `hash(        |
|                                            | filters)` replaces the hand-rolled       |
|                                            | string MD5s that used to gate each       |
|                                            | pipeline stage) +  `WorkoutView` +       |
|                                            | `build_workout_view(raw_workouts,        |
|                                            | filters)` — one traversal collapsing the |
|                                            | four filter stages (`quality_efforts`,   |
|                                            | `efforts_filtered_by_event`,             |
|                                            | `efforts_filtered_by_event_and_display`, |
|                                            | `featured_efforts`) into a single        |
|                                            | value-object.                            |
+--------------------------------------------+------------------------------------------+
| `power_curve_animation.py`                 | The animation layer top-to-bottom.       |
|                                            | Snapshot helpers (`compute_timeline_     |
|                                            | snapshot`, `ol_event_line`, `pcts`),     |
|                                            | keyframe build (`build_keyframes` heavy  |
|                                            | loop run in a background `hd.task`,      |
|                                            | `wrap_payload` cheap style-only          |
|                                            | wrapper, `build_sb_annotations`,         |
|                                            | `build_wr_static_datasets`), and the     |
|                                            | HyperDiv bundle lifecycle                |
|                                            | (`manage_animation_bundle` computes the  |
|                                            | split `data_key` / `style_key`, caches   |
|                                            | keyframes in `state.sim_bundle_data`,    |
|                                            | re-wraps via `wrap_payload` on           |
|                                            | style-only change, and returns the       |
|                                            | `sim_command` the JS plugin consumes;    |
|                                            | `lookup_bundle_entry` — pred-table       |
|                                            | lookup by day).                          |
+--------------------------------------------+------------------------------------------+
| `power_curve_chart_config.py`              | **Pure, no HyperDiv.** Builds the        |
|                                            | Chart.js config dict for the static      |
|                                            | (non-animating) chart: predictor curves, |
|                                            | scatter datasets, season/lifetime        |
|                                            | overlay lines, canvas labels, WC         |
|                                            | overlay. Contains `build_chart_config`,  |
|                                            | `build_pred_datasets`, `compute_axis_    |
|                                            | bounds`, and the per-model dataset       |
|                                            | builders used by                         |
|                                            | `compute_timeline_snapshot` in the       |
|                                            | animation module (tightly-coupled        |
|                                            | siblings).                               |
+--------------------------------------------+------------------------------------------+
| `concept2_sync.py`                         | Render-top helpers that ensure the data  |
|                                            | the page needs is loaded: `concept2_     |
|                                            | sync(client)` for user workouts,         |
|                                            | `load_world_record_data(state, profile)` |
|                                            | for the lazy WC fetch.                   |
+--------------------------------------------+------------------------------------------+
| `power_curve_chart_plugin.py`              | `PowerCurveChart` HyperDiv plugin shell  |
|                                            | — declares the Python↔JS props           |
|                                            | (`config`, `show_watts`, `x_mode`,       |
|                                            | `sim_bundle`, `sim_command`,             |
|                                            | `sim_speed`, `sim_day_out`, `sim_done`,  |
|                                            | plus scrubber/date-slider props).        |
+--------------------------------------------+------------------------------------------+
| `chart_assets/power_curve_chart_plugin.js` | The JS animation engine. Paths:          |
|                                            | `applyConfig` for static config and      |
|                                            | `applyBundle` for the precomputed        |
|                                            | animation bundle. Ticking, dataset       |
|                                            | rebuilding, overlay lookahead, PB badge  |
|                                            | countdown, and scrubber integration all  |
|                                            | live here. `tick_noadvance()` is the     |
|                                            | render-once primitive used on `"pause"`, |
|                                            | `"stop"` (when a bundle is cached), and  |
|                                            | scrubber seeks.                          |
+--------------------------------------------+------------------------------------------+
| `date_slider_plugin.py`                    | Legacy separate slider — the current     |
|                                            | chart integrates its own scrubber.       |
|                                            | Retained for other pages.                |
+--------------------------------------------+------------------------------------------+
| `hyperdiv_extensions.py`                   | Shared HyperDiv subclasses               |
|                                            | (`radio_group`, `shadowed_box`,          |
|                                            | `aligned_button`).                       |
+--------------------------------------------+------------------------------------------+

### Services layer (pure Python — `services/`)

+---------------------------+------------------------------------------+
| Module                    | Responsibility                           |
+---------------------------+------------------------------------------+
| `predictions.py`          | Predictor registry (`Predictor`,         |
|                           | `PREDICTORS`, `PREDICTORS_BY_KEY` —      |
|                           | single source of truth for each          |
|                           | predictor's `name`, `extended_           |
|                           | description`, `computed_from_            |
|                           | components`, optional `component_label`  |
|                           | / `component_desc`), per-model pace      |
|                           | samplers (`cp_pace_at`, `loglog_pace_    |
|                           | at`, `pauls_law_pace_at`, `rowinglevel_  |
|                           | pace_at`), and `build_prediction_table_  |
|                           | data` (multi-model prediction            |
|                           | computation returning `{"rows": [...],   |
|                           | "accuracy": {...}}` — per-model RMSE /   |
|                           | R² / n over enabled events; makes        |
|                           | `_prediction_table` a pure renderer).    |
+---------------------------+------------------------------------------+
| `critical_power_model.py` | 2-component CP model fitting, curve      |
|                           | generation, sprint/stayer crossover,     |
|                           | performance metrics.                     |
+---------------------------+------------------------------------------+
| `concept2_records.py`     | Concept2 official age-group world        |
|                           | records fetch + CP fitting for the       |
|                           | Compare-vs-World-Class overlay.          |
+---------------------------+------------------------------------------+
| `rowinglevel.py`          | rowinglevel.com scraper with caching     |
|                           | (rate-limited).                          |
+---------------------------+------------------------------------------+
| `rowing_utils.py`         | Constants, pace/watts conversions,       |
|                           | Paul's Law, log-log fit, season helpers, |
|                           | rankability/quality filters              |
|                           | (`is_rankable_noninterval`,              |
|                           | `apply_quality_filters`, `seasons_from`, |
|                           | `workouts_before_date`),                 |
|                           | `compute_featured_workouts`,             |
|                           | `compute_lifetime_bests`.                |
+---------------------------+------------------------------------------+
| `formatters.py`           | Display formatters including             |
|                           | `fmt_result_duration`.                   |
+---------------------------+------------------------------------------+

### Data flow summary

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
concept2_sync()                                   [components/concept2_sync.py]
  └─ raw workouts
       └─ build_workout_view(raw, FilterSpec)     [power_curve_workouts.py]
            └─ WorkoutView
                 ├─ compute_axis_bounds()         [power_curve_chart_config.py]
                 ├─ compute_featured_workouts()   [services/rowing_utils.py]
                 └─ manage_animation_bundle()     [power_curve_animation.py]
                       ├─ build_keyframes() (bg)  [power_curve_animation.py]
                       │     └─ compute_timeline_snapshot() per PB keyframe
                       │           └─ build_prediction_table_data()
                       │                 └─ samplers (cp/loglog/pl/rl)
                       ├─ wrap_payload()          [power_curve_animation.py]
                       │     injects log_x/log_y/overlay_bests/x_bounds/y_bounds
                       └─ sim_command
                 ⬇
           sim_bundle + sim_command props → PowerCurveChart plugin
                 ⬇
           applyBundle + handleSimCommand         [power_curve_chart_plugin.js]
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

3. State Variables
------------------

All state is declared as `hd.state(...)` at the top of `power_curve_page()`.
`FilterSpec` is defined in `power_curve_workouts.py` and constructed once per
render from the flat attributes; it is the cache key the workouts pipeline
receives (`hash(filters)` invalidates the whole pipeline atomically).

### Chart / filter state

+-------------------------+---------------+--------------------+---------+------------------------------------------+
| Variable                | Type          | Default            | Group   | Purpose                                  |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `dist_enabled`          | `tuple[bool]` | all True           | Filter  | One flag per RANKED\_DISTANCES entry     |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `time_enabled`          | `tuple[bool]` | all True           | Filter  | One flag per RANKED\_TIMES entry         |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `best_filter`           | `str`         | `"SBs"`            | Filter  | Row filter: `"All"` \| `"PBs"` \|        |
|                         |               |                    |         | `"SBs"`                                  |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_y_metric`        | `str`         | `"pace"`           | Style   | Y-axis mode: `"pace"` \| `"watts"`       |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_x_metric`        | `str`         | `"distance"`       | Style   | X-axis mode: `"distance"` \|             |
|                         |               |                    |         | `"duration"`                             |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_log_x`           | `bool`        | `True`             | Style   | Log scale on x-axis                      |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_log_y`           | `bool`        | `False`            | Style   | Log scale on y-axis                      |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_predictor`       | `str`         | `"critical_power"` | Style   | `PREDICTORS_BY_KEY` key                  |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_show_components` | `bool`        | `False`            | Style   | Show per-anchor / component sub-curves   |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `overlay_bests`         | `str`         | `"PBs"`            | Style   | Overlay line: `"PBs"` \| `"SBs"` \|      |
|                         |               |                    |         | `"None"` (renamed from                   |
|                         |               |                    |         | `draw_power_curves`)                     |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `chart_compare_wc`      | `bool`        | `False`            | Style   | Overlay WC records + WC prediction curve |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `wr_fetch_key`          | `str`         | `""`               | WC task | `"gender\|age\|weight_kg"` — WC fetch    |
|                         |               |                    |         | invalidation key                         |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `wr_fetch_done`         | `bool`        | `False`            | WC task | True once the WC fetch task has          |
|                         |               |                    |         | completed                                |
+-------------------------+---------------+--------------------+---------+------------------------------------------+
| `wr_data`               | `dict\|None`  | `None`             | WC task | Cached WC records + fitted CP curve      |
+-------------------------+---------------+--------------------+---------+------------------------------------------+

### Animation state

+--------------------+-------------------+---------+------------------------------------------+
| Variable           | Type              | Default | Purpose                                  |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_playing`      | `bool`            | `False` | True while the JS animation interval is  |
|                    |                   |         | running                                  |
+--------------------+-------------------+---------+------------------------------------------+
| `timeline_day`     | `int\|None`       | `None`  | Day offset from `sim_start`; `None`      |
|                    |                   |         | means "end of timeline / show all".      |
|                    |                   |         | Replaces the old `_SIM_TODAY = 999999`   |
|                    |                   |         | sentinel.                                |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_speed`        | `str`             | `"1x"`  | Playback speed: `"0.5x"` \| `"1x"` \|    |
|                    |                   |         | `"4x"` \| `"16x"`                        |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_bundle`       | `dict\|None`      | `None`  | Final JS payload = `bundle_data` + style |
|                    |                   |         | wrapper; sent to the chart plugin        |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_bundle_key`   | `str`             | `""`    | Combined `data_key-style_key` baked into |
|                    |                   |         | `sim_bundle`; JS compares this to detect |
|                    |                   |         | stale bundles                            |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_bundle_data`  | `dict\|None`      | `None`  | Heavy keyframes dict — cached by         |
|                    |                   |         | `sim_data_key`, re-wrapped by            |
|                    |                   |         | `wrap_payload` on any style-only change  |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_data_key`     | `str`             | `""`    | Hash of data-side inputs. When it        |
|                    |                   |         | changes, the background                  |
|                    |                   |         | `build_keyframes` task re-fires.         |
+--------------------+-------------------+---------+------------------------------------------+
| `sim_pred_lookup`  | `dict[int, dict]` | `{}`    | `{keyframe_day: {"pred_rows",            |
|                    |                   |         | "pauls_k_fit", "accuracy"}}` — populated |
|                    |                   |         | alongside `sim_bundle_data`              |
+--------------------+-------------------+---------+------------------------------------------+
| `last_sim_day_out` | `int`             | `-1`    | Last `sim_day_out` received from JS      |
|                    |                   |         | (back-prop)                              |
+--------------------+-------------------+---------+------------------------------------------+
| `last_sim_done`    | `int`             | `0`     | Last `sim_done` counter received;        |
|                    |                   |         | edge-triggers `sim_playing = False` at   |
|                    |                   |         | animation end                            |
+--------------------+-------------------+---------+------------------------------------------+

### Render-to-render caches

The per-stage caches that used to live here (`_key_for_quality_efforts`,
`_key_for_efforts_filtered_by_event`, etc.) have been replaced by a single
`WorkoutView`:

+--------------------------------+------------------------------------------+
| Variable                       | Purpose                                  |
+--------------------------------+------------------------------------------+
| `workout_view`                 | Cached `WorkoutView` — collapses all 4   |
|                                | filter stages + `all_seasons` into one   |
|                                | value-object. Rebuilt whenever           |
|                                | `_view_key` changes.                     |
+--------------------------------+------------------------------------------+
| `_view_key`                    | `(hash(FilterSpec), workout_count)` —    |
|                                | invalidates the whole pipeline           |
|                                | atomically when filters or the           |
|                                | underlying workout count changes.        |
+--------------------------------+------------------------------------------+
| `_annot_key` / `_annot_data`   | Cached slider annotation list `[{day,    |
|                                | label, color}]`                          |
+--------------------------------+------------------------------------------+
| `_bounds_key` / `_bounds_data` | Cached `(x_bounds, y_bounds)` from       |
|                                | `compute_axis_bounds`                    |
+--------------------------------+------------------------------------------+

**Note on** `excluded_seasons`**:** this is a *parameter* passed into
`power_curve_page()` from the global filter in `app.py`, not an internal state
variable — it still feeds into `FilterSpec.excluded_seasons` via the
value-object construction.

4. Seasons
----------

-   Format: `"YYYY-YY"` e.g. `"2024-25"`, spanning **May 1 → April 30**.

-   `WorkoutView.all_seasons` is sorted newest-first via `seasons_from()`.

-   `excluded_seasons` is a tuple of season strings; use
    `set(state.excluded_seasons)` for O(1) lookup.

-   `_included_seasons` is derived as `[s for s in all_seasons if s not in
    excluded_seasons]`.

-   Seasons drive both the simulation timeline bounds and the colour palette for
    scatter dots.

5. Simulation / Timeline
------------------------

### Timeline arithmetic

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
sim_start      = May 1 of the earliest included season's start year
sim_end        = min(today, April 30 of the year after the latest included season)
total_days     = (sim_end - sim_start).days + 1
sim_day_idx    = state.timeline_day (or total_days − 1 when None)
timeline_date  = sim_start + timedelta(days=sim_day_idx)
at_today       = state.timeline_day is None         # None == "end of timeline"
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Storing `None` directly in `hd.state(...)` is fine — HyperDiv state holds
arbitrary Python values. The old `_SIM_TODAY = 999999` sentinel is gone; every
`at_today` check is now `state.timeline_day is None`.

### Architecture: client-side JS animation with split cache keys

The animation runs **entirely in the browser** — the Python/HyperDiv server does
**zero work** during playback once the bundle has been delivered. What changed
recently is how Python decides whether to rebuild the bundle:

**Two cache keys, not one.** `manage_animation_bundle` computes a `data_key` and
a `style_key` independently, then concatenates them into the bundle\_key the JS
compares.

+-------------+------------------------------------------+------------------------------------------+
| Key         | Inputs                                   | Changing triggers                        |
+-------------+------------------------------------------+------------------------------------------+
| `data_key`  | `chart_predictor`, `best_filter`,        | Full re-run of `build_keyframes` in a    |
|             | selected dists/times, excluded seasons,  | background `hd.task`. Heavy.             |
|             | `show_watts`, `chart_x_metric`,          |                                          |
|             | `chart_show_components`,                 |                                          |
|             | `chart_compare_wc`, `wr_fetch_done`,     |                                          |
|             | `wr_fetch_key`, view identity            |                                          |
|             | `(hash(FilterSpec), workout_count)`      |                                          |
+-------------+------------------------------------------+------------------------------------------+
| `style_key` | `chart_log_x`, `chart_log_y`,            | Synchronous re-wrap via `wrap_payload` — |
|             | `overlay_bests`, `x_bounds`, `y_bounds`  | O(1), no task.                           |
+-------------+------------------------------------------+------------------------------------------+

`x_bounds`/`y_bounds` live on the **style** side because they depend on `log_x`
(log axes use multiplicative padding; linear uses additive). Baking them into
`bundle_data` would leave the JS chart stuck on the bounds captured when the
bundle was first built — e.g. toggling `log_x` would leave gridlines filtered by
the wrong range.

**Bundle lifecycle:** 1. Data change (e.g. toggling events, predictor):
`sim_bundle_data = None`, `sim_bundle = None`. Background `hd.task` runs
`build_keyframes`. Python sends `"pause"` to JS until the task completes. 2.
Style change only (e.g. toggling `log_x`): `sim_bundle_data` is untouched.
`wrap_payload` re-wraps it synchronously; `sim_bundle` is updated in the same
render cycle. No spinner, no task. 3. Either change: Python increments
`sim_bundle_key`; JS sees the key change in `onPropUpdate` and calls
`applyBundle`.

**Back-communication (JS → Python):**

+---------------+------------------------------------------+
| JS prop       | Python action                            |
+---------------+------------------------------------------+
| `sim_day_out` | `state.timeline_day = chart.sim_day_out` |
|               | — keeps the scrubber in sync             |
+---------------+------------------------------------------+
| `sim_done`    | When it changes, `state.sim_playing =    |
|               | False` — resets the Play button          |
+---------------+------------------------------------------+

### Speed options

+--------+-------------+------------------+
| Label  | `sim_speed` | Days per JS tick |
+--------+-------------+------------------+
| `0.5x` | `"0.5x"`    | 1                |
+--------+-------------+------------------+
| `1x`   | `"1x"`      | 7                |
+--------+-------------+------------------+
| `4x`   | `"4x"`      | 30               |
+--------+-------------+------------------+
| `16x`  | `"16x"`     | 91               |
+--------+-------------+------------------+

Speed changes update only `currentStepDays` in JS via the `sim_speed` prop — no
bundle rebuild.

### Bundle structure

The animation layer has two halves:

`build_keyframes` **(heavy) returns** `bundle_data`**:**

+------------------------------------------+------------------------------------------+
| Key                                      | Description                              |
+------------------------------------------+------------------------------------------+
| `workout_manifest`                       | All workouts oldest-first, with          |
|                                          | pre-computed                             |
|                                          | x/y/pace/watts/season\_idx/cat\_key\_str |
|                                          | fields                                   |
+------------------------------------------+------------------------------------------+
| `keyframes`                              | Sparse list of frames, one per new PB    |
|                                          | date. Each keyframe carries              |
|                                          | `pred_datasets`, `pred_canvas_labels`,   |
|                                          | `new_pb_labels`.                         |
+------------------------------------------+------------------------------------------+
| `static_datasets`                        | Time-invariant datasets (WC scatter +    |
|                                          | prediction) baked once                   |
+------------------------------------------+------------------------------------------+
| `season_meta`                            | Label, colour, border colour per season  |
+------------------------------------------+------------------------------------------+
| `total_days`                             | Timeline length                          |
+------------------------------------------+------------------------------------------+
| `pb_badge_lifetime_steps`                | How many ticks a "New PB!" badge stays   |
|                                          | visible (40)                             |
+------------------------------------------+------------------------------------------+
| `pb_color` / `is_dark` / `show_watts` /  | Display metadata consumed by JS dataset  |
| `x_mode`                                 | builders                                 |
+------------------------------------------+------------------------------------------+
| `grid_color`                             | Gridline colour                          |
|                                          | (`rgba(180,180,180,0.35)`). Kept         |
|                                          | identical to the static chart's axis     |
|                                          | grid so toggling between `applyConfig`   |
|                                          | and `applyBundle` paths doesn't visibly  |
|                                          | snap gridlines on/off. Python is the     |
|                                          | single source of truth per CLAUDE.md.    |
+------------------------------------------+------------------------------------------+

`wrap_payload` **(cheap) adds the style wrapper:**

+-------------------------+------------------------------------------+
| Key                     | Source                                   |
+-------------------------+------------------------------------------+
| `bundle_key`            | `"{data_key}-{style_key}"` — JS uses     |
|                         | this to decide whether to re-apply       |
+-------------------------+------------------------------------------+
| `log_x` / `log_y`       | From the page's style state              |
+-------------------------+------------------------------------------+
| `draw_lifetime_line`    | `overlay_bests == "PBs"`                 |
+-------------------------+------------------------------------------+
| `draw_season_lines`     | `overlay_bests == "SBs"`                 |
+-------------------------+------------------------------------------+
| `x_bounds` / `y_bounds` | Injected here — depend on `log_x`, not   |
|                         | baked into keyframes                     |
+-------------------------+------------------------------------------+

**Side output —** `pred_table_lookup`**:** `build_keyframes` also returns
`{keyframe_day: {"pred_rows": [...], "pauls_k_fit": float|None, "accuracy":
{...}}}` which lives server-side in `state.sim_pred_lookup`. This replaces the
old `state._pauls_k_fit` scratch bridge between the slow and fast paths: Paul's
personalised K now travels inside the lookup, atomically with `pred_rows` and
`accuracy`, so the fast path reads all three from the same keyframe without a
slow-path render having gone first.

### sim\_command protocol

Python communicates animation intent to JS via the `sim_command` prop. JS
handles each command in `handleSimCommand()`. Seeking is handled entirely in JS
via the integrated scrubber — Python only signals play / pause / stop.

+-----------+------------------------------------------+------------------------------------------+
| Value     | When sent (from                          | JS effect                                |
|           | `manage_animation_bundle`)               |                                          |
+-----------+------------------------------------------+------------------------------------------+
| `"play"`  | `sim_playing=True`, not at\_today,       | Start `setInterval` if not already       |
|           | bundle ready                             | running                                  |
+-----------+------------------------------------------+------------------------------------------+
| `"pause"` | `sim_playing=True` but bundle not yet    | Clear `setInterval`; `tick_noadvance()`  |
|           | built (hold JS), or paused with bundle   | to render one frame at current position  |
+-----------+------------------------------------------+------------------------------------------+
| `"stop"`  | `at_today` (`timeline_day is None`), or  | Clear `setInterval`; **if a cached       |
|           | no bundle at all                         | bundle is present,** `tick_noadvance()`  |
|           |                                          | **renders the end-of-timeline frame** so |
|           |                                          | the chart doesn't go blank after         |
|           |                                          | navigation or on initial mount           |
+-----------+------------------------------------------+------------------------------------------+

The `tick_noadvance()` on `"stop"` is the fix for the "navigate away and back
shows an empty chart" regression: `hd.task` scopes persist across component
unmount, so on re-entry the task is already `done` and `sim_bundle` is
populated; JS takes the `applyBundle` path with `sim_command="stop"`, which
previously left the chart empty.

`sim_command` is diffed by HyperDiv; `onPropUpdate` fires only on change, so
repeated `"play"` renders cost nothing.

### Lookahead overlays

The JS `buildOverlayDatasets()` function scans `workout_manifest` for workouts
in `(currentDay, currentDay + 4 × stepDays]` that beat the current best at their
event category. It renders:

-   **Ghost dots** — a faint scatter point at the upcoming performance's
    position

-   **Arrows** — a dashed line from the current best → the upcoming performance

-   **"upcoming PB" canvas label** — event name, % improvement, "upcoming PB"
    text

### "New PB!" badge

When a keyframe's `new_pb_labels` is non-empty, JS: 1. Copies the labels into
`pbBadgeLabels`. 2. Sets `pbBadgeCountdown = pb_badge_lifetime_steps` (40 ticks
≈ 14 s at 1×). 3. Merges `pbBadgeLabels` into `allCanvasLabels` every tick until
the countdown expires.

### CP crossover annotation

When predictor is Critical Power and Show Components is enabled, each keyframe
carries `pred_canvas_labels` — a bottom-anchored canvas label array. JS merges
these into `allCanvasLabels` every tick so the "Fast-twitch and aerobic
contributions are equal here" annotation tracks the current CP crossover point.

### Expected interaction behaviors

These are the canonical behaviors; any deviation is a bug.

#### Play button

+------------------------------------------+------------------------------------------+
| Starting state                           | Expected result                          |
+------------------------------------------+------------------------------------------+
| At end of timeline (`timeline_day is     | Rewinds to 30 days before the first      |
| None`)                                   | qualifying event, then begins playing    |
|                                          | forward                                  |
+------------------------------------------+------------------------------------------+
| Mid-timeline, no bundle cached           | Starts bundle computation (loading);     |
|                                          | animation begins once bundle arrives     |
+------------------------------------------+------------------------------------------+
| Mid-timeline, bundle cached              | Animation resumes from current scrubber  |
|                                          | position immediately                     |
+------------------------------------------+------------------------------------------+
| Animation already playing                | Button shows "⏸ Pause"; click pauses     |
|                                          | animation                                |
+------------------------------------------+------------------------------------------+

#### Pause button

-   JS `setInterval` is cleared immediately on the same render cycle as the
    click.

-   Chart freezes at the day it was on when pause was received (not necessarily
    the Python scrubber position — JS drives its own counter during playback).

-   Scrubber snaps to the paused day via the `sim_day_out` back-prop.

-   Subsequent Python renders send `"pause"` to JS, which calls
    `tick_noadvance()` — idempotent + re-renders at current position.

#### Seek (scrubber drag or timeline annotation click)

Seeking is JS-internal. The scrubber directly drives JS's `currentDay` and
triggers `tick_noadvance()`. Python sees the new day only via the `sim_day_out`
back-prop and does not issue a seek command.

#### Speed change

-   Clicking the speed button cycles `0.5x → 1x → 4x → 16x → 0.5x …`.

-   Python updates `sim_speed` prop; JS updates `currentStepDays` via the prop
    handler.

-   No bundle rebuild.

#### Settings change while playing

Data-side settings (predictor, best\_filter, event toggles, excluded seasons,
show\_watts, x\_mode, show\_components, WC toggle):

1.  `sim_data_key` flips → `sim_bundle_data = None`, `sim_bundle = None`.

2.  Python sends `"pause"` to JS (bundle not ready) → animation halts.

3.  `build_keyframes` task launches in the background.

4.  When the new bundle arrives, JS **resumes from the same day** (`currentDay`
    is preserved across bundle replacement).

5.  Python sends `"play"` with the new bundle → animation continues.

Style-side settings (`log_x`, `log_y`, `overlay_bests`, bounds):

1.  `sim_style_key` flips; `sim_bundle_data` is kept.

2.  `wrap_payload` re-wraps synchronously. New `sim_bundle` ships in the same
    render.

3.  JS sees a new `bundle_key`, calls `applyBundle` with the already-cached
    keyframes, re-renders at current day. No pause, no task, no spinner.

#### Animation end

When JS `currentDay` reaches `total_days`: 1. The final frame is rendered. 2.
`setInterval` is cleared. 3. `sim_done` counter is incremented, sent to Python.
4. Python sets `state.sim_playing = False`; scrubber lands at end. 5. Play
button reverts to "▶ Play". Pressing Play again rewinds to 30 days before first
event.

6. Chart Settings
-----------------

### Intensity axis (Row 1, left)

+--------------+------------------+------------------------------------------+
| Control      | State var        | Effect                                   |
+--------------+------------------+------------------------------------------+
| Pace / Watts | `chart_y_metric` | Switches y-axis between sec/500m and     |
|              |                  | watts                                    |
+--------------+------------------+------------------------------------------+
| Log Y        | `chart_log_y`    | Logarithmic y-axis                       |
+--------------+------------------+------------------------------------------+

### Length axis (Row 1, right)

+---------------------+------------------+------------------------------------------+
| Control             | State var        | Effect                                   |
+---------------------+------------------+------------------------------------------+
| Distance / Duration | `chart_x_metric` | Switches x-axis between meters and       |
|                     |                  | seconds                                  |
+---------------------+------------------+------------------------------------------+
| Log X               | `chart_log_x`    | Logarithmic x-axis (also affects         |
|                     |                  | `compute_axis_bounds` padding)           |
+---------------------+------------------+------------------------------------------+

When **Duration** is selected, scatter points use `workout["time"] / 10` as x
(seconds), and prediction curves are transformed so x = `dist × pace / 500`
(parametric time).

### Overlay bests (Row 2)

+----------+------------------------------------------+
| Value    | What is drawn                            |
+----------+------------------------------------------+
| `"PBs"`  | Dashed line connecting lifetime-best     |
|          | dots across all events                   |
+----------+------------------------------------------+
| `"SBs"`  | One line per season connecting that      |
|          | season's best dots                       |
+----------+------------------------------------------+
| `"None"` | No connecting lines                      |
+----------+------------------------------------------+

### Prediction line (Row 3)

A custom dropdown shows each entry from `PREDICTORS` (name in bold +
description). See [docs/prediction.md][2] for full model mathematics.

+--------------------+------------------------------------------+
| Key                | Model                                    |
+--------------------+------------------------------------------+
| `"none"`           | No prediction line                       |
+--------------------+------------------------------------------+
| `"loglog"`         | Log-Log Watts Fit                        |
+--------------------+------------------------------------------+
| `"pauls_law"`      | Paul's Law (personalised K)              |
+--------------------+------------------------------------------+
| `"critical_power"` | Two-component Critical Power             |
+--------------------+------------------------------------------+
| `"rowinglevel"`    | RowingLevel population norms             |
+--------------------+------------------------------------------+
| `"average"`        | Ensemble average of all available models |
+--------------------+------------------------------------------+

[2]: <prediction.md>

### Show components (Row 3)

Available for every predictor whose `supports_components=True` in the registry.
Label and description are pulled from the registry — no duplicated metadata.

### CP Crossover point

The duration `t*` at which fast-twitch and slow-twitch CP contributions are
equal. Visible when Critical Power is selected with Show components enabled.
Rendered as a dashed vertical teal line at `t*` with an explanation label at the
chart bottom.

7. Paul's Law — Personalised Constant
-------------------------------------

The population default is K = 5.0 sec/500m per doubling of distance. The app
fits a personalised K from the rower's own PBs (regression through origin; ≥2
PBs required, clamped to [0.5, 15.0]).

**Interpretation:** - **K \< 5** (e.g. 3–4): aerobic-dominant. - **K ≈ 5**:
typical balanced rower. - **K \> 5** (e.g. 6–8): sprint-dominant.

`pauls_k_fit` travels inside `pred_table_lookup` alongside `pred_rows` — the
fast path reads it from the cached keyframe atomically. No slow-path dependency,
no cross-path scratch state (the old `state._pauls_k_fit` bridge is gone).

8. Prediction Table
-------------------

`_prediction_table` in `power_curve_page.py` is a **pure renderer**. All
computation — per-model predictions *and* the RMSE / R² accuracy — lives in
`build_prediction_table_data` in the services layer, which returns:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
{
    "rows": [...],
    "accuracy": {"cp": {"rmse", "r2", "n"}, "loglog": {...}, "pl": {...},
                 "rl": {...}, "avg": {...}},
}
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Accuracy is folded into the services layer so it's computed once per snapshot
(rather than re-looping on every render) and travels with `pred_rows` through
`pred_table_lookup` — an atomic unit per keyframe.

### Columns

+-------------------+------------------------------------------+
| Column            | Contents                                 |
+-------------------+------------------------------------------+
| Event             | Event name + enable/disable toggle       |
+-------------------+------------------------------------------+
| Your PB           | Unfiltered personal best pace + total    |
|                   | time/distance                            |
+-------------------+------------------------------------------+
| Critical Power    | CP model prediction + delta vs PB        |
+-------------------+------------------------------------------+
| Log-Log Watts Fit | Log-log prediction + delta               |
+-------------------+------------------------------------------+
| Avg. Paul's Law   | Averaged Paul's Law prediction + delta   |
+-------------------+------------------------------------------+
| Avg. RowingLevel  | Averaged RL prediction + delta (hidden   |
|                   | when profile incomplete)                 |
+-------------------+------------------------------------------+
| Average           | Mean of all available predictions        |
+-------------------+------------------------------------------+

### Row ordering

Rows are sorted by expected duration (not by distance/time category separately),
so timed events interleave with distance events at their natural positions on
the curve.

### Deltas

`+2.3s` (red) means the model predicts 2.3 sec/500m *slower* than the PB — the
PB is atypically strong. Negative delta (green) means the model predicts
*faster* than the rower has achieved, suggesting untapped potential.

### Event toggle

The switch next to each event name controls whether that event is included in
the model fits (prediction columns) **and** in the RMSE / R² calculation.
Toggling an event off dims its "Your PB" cell.

### Accuracy footer

RMSE (sec/500m) and R² per predictor across **enabled events** that have both a
prediction and a PB. `n=` shows how many events contributed. Lower RMSE and R²
closer to 1.0 indicate better fit.

9. Slow-path / Fast-path Snapshots
----------------------------------

`_compute_chart_data` in `power_curve_page.py` dispatches between two snapshot
helpers:

-   `_slow_path_snapshot` — paused or initial-load path. Computes
    `compute_timeline_snapshot` fresh from the current workouts at
    `timeline_date`. Cost: one CP fit, one Paul's K fit, one pred-table pass.
    Used when no cached bundle covers the current day (e.g. the first render
    after filters change).

-   `_fast_path_snapshot` — during animation. Reads `pred_rows`, `pauls_k_fit`,
    and `accuracy` directly from `state.sim_pred_lookup[day]` via
    `lookup_bundle_entry`. O(1) — no model work.

Both return the same 5-tuple `(chart_cfg, pred_rows, pauls_k_fit, pauls_k,
accuracy)`.

10. RowingLevel Profile Requirement
-----------------------------------

RowingLevel predictions require a complete profile (gender, date of birth,
bodyweight). When incomplete: - The RL chart line and table column are hidden. -
A dismissible warning banner appears (dismissal stored in `localStorage` under
`"rl_notif_dismissed"`).

11. World-Class Comparison
--------------------------

`load_world_record_data()` in `power_curve_animation.py` manages the lazy
`hd.task` that fetches WC records.

1.  `fetch_wr_data(gender, age, weight_kg)` (in `services/concept2_records.py`)
    retrieves age-group WRs.

2.  Records are converted to CP inputs via `records_to_cp_input()` and fitted
    with `fit_critical_power()` — same four-parameter curve as the user's own CP
    fit.

3.  Result cached in `state.wr_data`, invalidated by `state.wr_fetch_key`
    (`"gender|age|weight_kg"`). Re-fetch only when the profile changes.

4.  `wr_fetch_done` / `wr_fetch_key` feed into `data_key` so the bundle rebuilds
    once the fetch completes — otherwise y-bounds baked at the pre-fetch render
    would persist.

Requirement: profile must be complete (gender, DOB, weight). Otherwise the
toggle is hidden.

12. Axis Bounds
---------------

`compute_axis_bounds` lives in `power_curve_chart_config.py` because axis
geometry is a chart-config concern. Its sole data input is `quality_efforts`
(the first pipeline stage). It's sensitive to `log_x`: log axes pad
multiplicatively, linear axes pad additively.

Bounds are held fixed across the simulation so the chart doesn't shift as the
scrubber moves. They **are** recomputed when `log_x` toggles — which is why they
live on the **style** side of the bundle-key split (baked into `wrap_payload`,
not into `bundle_data`).

13. Python ↔ JS Constants
-------------------------

Per CLAUDE.md, Python is the single source of truth for constants that both
sides need (colours, bounds, labels). Recent additions:

-   `grid_color` — passed through `bundle_data` so `buildSimOptions` in JS uses
    the exact same gridline colour as the static `applyConfig` path. Without
    this, the sim-mode axes fell back to Chart.js defaults and gridlines visibly
    snapped on/off when the chart transitioned between the two paths.

-   `x_bounds` / `y_bounds` — passed via `wrap_payload` (style side) so log-axis
    toggles re-pad them correctly.

-   `pb_color`, `season_meta.color/dim_color/border_color` — baked into
    `bundle_data`.

-   Ranked distances / durations for gridline positions — shipped in the static
    config dict (`_ranked_dists`, `_ranked_durations`).
