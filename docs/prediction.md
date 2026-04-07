# Performance Prediction — Design & UI Reference

This document covers how performance prediction works in the Ranking Events tab:
the four prediction models, how they are fit and evaluated, the chart presentation,
and the prediction table.

---

## 1. Overview

When a rower has accumulated personal bests (PBs) across several ranked events,
those data points trace a characteristic power-duration curve.  The prediction
system fits four different models to that curve and uses them to:

- **Predict** what time or distance a rower should achieve at events they haven't
  attempted recently.
- **Compare** model assumptions — Paul's Law, log-log scaling, two-component
  physiology, and empirical population norms all make different implicit claims
  about how fitness degrades with distance/duration.
- **Quantify accuracy** (RMSE, R²) where predictions can be compared against known PBs.

All predictions are expressed as a pace in **sec/500m** internally, then
displayed as split (pace) and projected total time or distance depending on the
event type.

---

## 2. The Four Predictors

### 2.1 Log-Log Watts Fit

**Formula:**  `log(watts) = slope × log(dist) + intercept`

This fits a straight line in log–log space between watts (converted from pace via
`watts = 2.80 × (500/pace)³`) and distance.  The slope and intercept are
determined by ordinary least squares across all filtered lifetime PBs, excluding
the 100m and 1-minute events (which are dominated by anaerobic sprint mechanics
and distort an aerobic fit).

The resulting power law is equivalent to saying "power output scales as a fixed
exponent of distance", which is the implicit assumption behind tools like the
[Free Spirits Pace Predictor](https://freespiritsrowing.com).  Unlike those tools,
this fit uses *all* PBs, not just two anchor events, so it is more robust.

Predicting a timed event (e.g. 30 min) requires numerically solving for the
distance at which `predicted_pace(d) × d / 500 = T` (Brent's method,
`brentq`).

**Limitation:** Assumes a single power-law relationship across all distances.
Sprint events will be under-predicted and ultra-endurance events may be
over-predicted if the rower's physiology has a strong anaerobic component.

---

### 2.2 Paul's Law

**Formula:**  `pace(d₂) = pace(d₁) + 5 × log₂(d₂ / d₁)`

Paul's Law predicts that pace slows by exactly 5 sec/500m for each doubling of
distance.  It is simple and surprisingly accurate for distances in the 1k–10k
range for well-trained rowers.

Because the rule can be anchored to *any* existing PB, the app computes one
prediction per anchor (one per ranked event where the rower has a PB), then
**averages** them.  This makes the prediction robust to any single anchor being
an outlier.  The chart shows this averaged curve by default; enabling
"Show components" reveals the individual per-anchor curves.

Timed events are solved numerically (same brentq approach as log-log).

**Limitation:** The +5 s/500m-per-doubling constant is empirical and works best
for trained rowers at aerobic distances.  It does not model the sprint–endurance
crossover and cannot represent a rower who is unusually strong over short vs.
long distances.

---

### 2.3 RowingLevel

RowingLevel ([rowinglevel.com](https://rowinglevel.com)) provides a population-derived
predicted pace for every standard Concept2 distance, given a rower's gender, age,
bodyweight, and one reference performance.

The app scrapes RowingLevel once per profile configuration, generating one
prediction curve per ranked anchor event (the reference performance changes, so
each PB yields a distinct RL curve).  Those curves are then **averaged** in the
same way as Paul's Law.

RL provides predictions for standard distance events only (≥500m in practice).
Timed events (1 min, 4 min, 30 min, 60 min) are solved numerically: for each
anchor curve, `_rl_interp_pace` log-log interpolates the pace at an arbitrary
distance, then `brentq` finds the distance where `interp_pace(d) × d / 500 = T`.
The 1-minute event (~280m) almost always fails this solve since it falls below
RL's minimum distance.

RowingLevel requires a completed user profile (gender, age, weight).

---

### 2.4 Critical Power (Two-Component Model)

**Formula:**  `P(t) = Pow1 / (1 + t/tau1)  +  Pow2 / (1 + t/tau2)`

This is the veloclinic / rowsandall four-parameter model.  The two terms
represent:

| Term | Name | Interpretation |
|---|---|---|
| `Pow1 / (1 + t/tau1)` | Fast-twitch / anaerobic | Peak sprint power decaying with time constant `tau1` (~5–120 s) |
| `Pow2 / (1 + t/tau2)` | Slow-twitch / aerobic | Sustained aerobic power decaying with time constant `tau2` (~600–14400 s) |

Fitting is done in log-log space (so sprint and endurance events are weighted
equally) using `scipy.optimize.curve_fit`.  The fit is rejected if R² < 0.90 on
the original (t, P) scale, or if fewer than 5 PBs spanning a 10:1 duration ratio
are available.

The model has two unique chart decorations beyond the main curve:

- **Event markers** — predicted pace dots at each ranked distance/time, sized
  for easy hover.
- **Crossover point** — the duration `t*` at which fast-twitch and slow-twitch
  contributions are equal (i.e. `Pow1/(1+t*/tau1) = Pow2/(1+t*/tau2)`).  Shown
  as a teal dot.  Rowers whose crossover is under ~4 minutes are sprinters;
  rowers whose crossover exceeds ~20 minutes are stayers.

Enabling "Show components" draws the fast-twitch and slow-twitch curves
separately at reduced opacity, letting you see how the two terms combine.

**Requirement:** ≥5 PBs with a 10:1 ratio between shortest and longest duration
(e.g. 1-minute and 30-minute PBs together span a 30:1 ratio, easily sufficient).

---

## 3. The "Average" Column

The prediction table includes an **Average** column (rightmost) that is the
unweighted mean of whichever predictors have a value for that event.  If only two
of four predictors produce a value (e.g. CP is unavailable and RL doesn't cover
1-minute events), the average is taken across just those two.

This average is not necessarily more accurate than any individual predictor — it
is simply a central tendency across the model ensemble.  Its RMSE and R² are
reported in the accuracy row alongside the individual predictors.

---

## 4. Input Data and Scoping

### Filtered vs. unfiltered bests

Two sets of lifetime bests flow through the system:

| Name | Used for | Gated by |
|---|---|---|
| `lifetime_best` / `lifetime_best_anchor` | All prediction columns (drives model fit) | Sim date + season + event filter |
| `all_lifetime_best` / `all_lifetime_best_anchor` | "Your PB" column only | Sim date + season only (event filter ignored) |

This distinction means the "Your PB" column always shows a PB even for events the
user has hidden from the chart — you can hide the Marathon from the graph without
losing sight of your Marathon PB in the table.

### Sim date

The date slider controls a "simulation date" — all data after that date is hidden.
This lets you replay how predictions looked at any point in time.

### Event toggles

Each row in the prediction table has a toggle switch that mirrors the "Events"
filter in the chart toolbar.  Toggling an event off:
- Dims its PB cell (neutral color).
- Excludes it from the RMSE / R² accuracy calculation.
- Does *not* remove it from the prediction columns (predictions are always shown).

---

## 5. Pace–Watts–Distance Conversions

All models work internally in one of two domains (time or distance) and must be
converted to chart space (distance on x, pace or watts on y).

**Core formulas:**

```
watts  = 2.80 × (500 / pace_sec)³          # Concept2 standard
pace   = 500 × (2.80 / watts)^(1/3)
dist   = t × (500 / pace)                   # parametric: speed = 500/pace m/s
```

The chart x-axis is always **distance in meters**.  For models that work in time
(log-log, Paul's Law for timed events, Critical Power), this parametric conversion
maps `(t, watts)` → `(dist, y)`.

The y-axis is either **pace** (sec/500m, displayed as M:SS.t by Chart.js) or
**watts** (toggled by the Pace/Watts radio button).

---

## 6. The Chart

The prediction line appears as a dashed amber curve drawn behind all workout
scatter data.  Chart settings:

| Control | What it does |
|---|---|
| Pace / Watts | Switch y-axis between sec/500m and watts |
| Power curves: PBs / SBs / None | Overlay connected lifetime-best or season-best lines |
| Prediction line | Choose which model to show (or None) |
| Show components | (PL, RL, CP only) Reveal per-anchor or fast/slow sub-curves at reduced opacity |
| Log Y / Log X | Toggle logarithmic axes |

The **Show components** control only appears when the selected predictor supports
it (Paul's Law, RowingLevel, Critical Power).  Log-Log has a single curve with no
components to separate.

---

## 7. The Prediction Table

The table appears below the chart whenever any ranked PBs are in scope.

**Columns** (left → right):

| Column | Contents |
|---|---|
| Event | Ranked event name + enable/disable toggle |
| Your PB | Pace (sec/500m) and total time or distance of personal best |
| Critical Power | Model prediction |
| Log-Log Watts Fit | Model prediction |
| Avg. Paul's Law | Averaged Paul's Law prediction |
| Avg. RowingLevel | Averaged RowingLevel prediction |
| Average | Mean of all available model predictions |

For prediction columns, a **delta** is shown next to the pace when a PB exists for
that event — e.g. `+2.3s` (red) means the model predicts 2.3 sec/500m slower than
the rower's actual PB, suggesting the PB is atypically strong relative to the
model.  A negative delta (green) means the model predicts a faster time than the
rower has actually achieved — suggesting untapped potential at that event.

**Accuracy row** (bottom):  RMSE (sec/500m) and R² for each predictor, computed
only across events that are currently enabled via the row toggles.  Lower RMSE
and R² closer to 1.0 indicate a better fit.  `n=` shows how many events
contributed.

---

## 8. Code Organisation

| File | Responsibility |
|---|---|
| `services/rowing_utils.py` | `pauls_law_pace`, `loglog_fit`, `loglog_predict_pace`, pace/watts conversions, RANKED_DISTANCES / RANKED_TIMES constants |
| `services/critical_power_model.py` | Two-component CP model, fitting, curve generation, crossover and sprint/stayer metrics |
| `components/ranked_chart_builder.py` | `build_prediction_table_data` (all four predictors, both event types), `build_chart_config` (chart datasets including prediction curves and components), `compute_lifetime_bests`, `_rl_interp_pace` |
| `components/ranked_tab.py` | State management, chart settings UI, prediction table renderer, event toggles, accuracy row |
