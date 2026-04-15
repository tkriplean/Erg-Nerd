# Critical Power Model & Stayer/Sprinter Metric

## Overview

This document describes the plan for adding a Critical Power prediction curve and a
stayer/sprinter profile to the ranked-workouts chart. It is a living design document
— sections will be updated as the feature is built and iterated on.

---

## Background Reading

- **Model derivation:** [veloclinic — Derivation of the Mean Maximal Power Duration
  Models](https://veloclinic.com/wp-content/uploads/2014/04/PowerModelDerivation-1.pdf)
  (Ward-Smith / Morton / veloclinic, 2014)
- **Practical application:** [rowsandall — Ergometer Scores: How Great Are
  You?](https://analytics.rowsandall.com/2018/01/12/ergometer-scores-how-great-are-you/)
  (Jan 2018)

---

## The Model

Both sources converge on the same **four-parameter two-component power-duration model**:

```
P(t) = Pow1 / (1 + t/tau1)  +  Pow2 / (1 + t/tau2)
```

| Parameter | Physiological meaning | Typical range |
|---|---|---|
| **Pow1** | Peak fast-twitch (anaerobic/phosphagen) power | 200–3000 W |
| **tau1** | Fast-twitch depletion time constant | 5–120 s |
| **Pow2** | Peak slow-twitch (aerobic) power | 50–1500 W |
| **tau2** | Slow-twitch depletion time constant | 600–14400 s |

**Key properties:**
- At t → 0: P approaches Pow1 + Pow2 (theoretical sprint maximum)
- At t → ∞: P approaches 0 (unlike the classic two-parameter CP model, which
  predicts a non-zero sustainable power forever — a physiological impossibility at
  very long durations)
- The model avoids the "infinite power at zero duration" failure of the simpler
  hyperbolic model P = W′/t + CP

---

## Data Preparation

### Input: personal bests within selected seasons

The fitter uses the **best performance per ranked event category** from the
**currently selected seasons**. The user controls time-windowing indirectly through
the season filter — no separate windowing control is needed.

Both distance events and time events are included.

### Converting each PB to (duration, watts)

**Distance events** (e.g., 2000 m):
```python
pace       = compute_pace(workout)          # seconds per 500 m
duration_s = pace * distance_m / 500        # total seconds
watts      = compute_watts(pace)            # 2.80 × (500/pace)³
```

**Time events** (e.g., 30-min piece):
```python
duration_s = time_tenths / 10              # directly from the event definition
pace       = duration_s * 500 / distance_m  # derived from distance covered
watts      = compute_watts(pace)
```

A helper `compute_duration_s(workout)` will be added to `services/rowing_utils.py`
to encapsulate this logic.

---

## Model Fitting

### Method

`scipy.optimize.curve_fit` with bounded nonlinear least squares. Fitting is
performed in log-log space (`log t`, `log P`) so that short and long events are
weighted equally — otherwise a 42 km marathon would dominate a 100 m sprint
numerically.

### Minimum data requirement

- **≥ 5 personal-best data points**
- Spanning **at least a 10× duration range** (e.g., at least one point under
  ~3 minutes and at least one point over ~30 minutes)

If the data threshold is not met, the Critical Power option silently produces no
curve. A brief note in the UI ("insufficient data range for Critical Power model")
may be added later.

### Initial parameter guesses (derived from data)

```python
Pow1_0 = max(watts) - median(watts)   # fast-twitch share of peak
Pow2_0 = median(watts)                 # aerobic base
tau1_0 = 30.0                          # seconds
tau2_0 = 3600.0                        # seconds
```

### Bounds

```python
bounds = (
    [100,  5,   50,   600  ],   # lower: Pow1, tau1, Pow2, tau2
    [5000, 240, 2000, 14400],   # upper
)
```

### Quality gate

If R² on the (t, P) fit is below **0.90**, the curve is suppressed. A low-R²
fit usually means the data is too narrow in duration range to constrain all four
parameters independently.

---

## Rendering the Prediction Curve

### Coordinate transform

The chart's x-axis is **distance in meters**. The model outputs **power at a
given duration**. These connect parametrically:

Given duration `t` (seconds):
```python
P    = critical_power_model(t, Pow1, tau1, Pow2, tau2)   # watts
pace = 500 * (2.80 / P) ** (1/3)                         # sec/500m
d    = t * (P / 2.80) ** (1/3)                           # meters
y    = P if show_watts else pace
```

This places the curve in the same (distance, pace-or-watts) coordinate space as all
other datasets — no secondary axis required.

### Point generation

~200 log-spaced `t` values from 10 s to 10 800 s (3 hours), converted
parametrically to `(d, y)`, then filtered to within the chart's current x/y bounds.

### Style

Initially rendered identically to the other prediction-line options (Paul's Law,
log-log, RowingLevel): dashed amber line via the existing `_pred_dataset()` factory.
Style can be differentiated later if desired.

### Crossover point annotation

The crossover point is the duration `t*` at which the fast-twitch and slow-twitch
components contribute equally:

```
Pow1 / (1 + t*/tau1)  =  Pow2 / (1 + t*/tau2)
```

Solved numerically (e.g., `scipy.optimize.brentq`). The corresponding
`(d*, y*)` chart coordinate is added to the curve's dataset as a **distinctly
colored point** — a different color from the prediction line itself. Its tooltip
label reads something like:

> **Sprint/Endurance crossover — 4 min 12 s**
> Fast-twitch and aerobic contributions equal here.
> Efforts shorter than this are sprint-dominant; longer are endurance-dominant.

---

## Stayer/Sprinter Profile

### Reference power values (from the fitted model)

| Variable | Duration | Physiological zone |
|---|---|---|
| P10 | 10 s | Phosphagen / peak sprint |
| P240 | 4 min | Glycolytic threshold / VO2max range |
| P3600 | 60 min | Aerobic base / lactate threshold |

### Ratios (from rowsandall ML feature importance)

| Ratio | Label | Higher value → |
|---|---|---|
| P10 / P240 | Sprint index | More fast-twitch dominant |
| P3600 / P240 | Stayer index | Better aerobic retention |

These are the two metrics rowsandall identified as most predictive for
stayer/sprinter classification in their Concept2 population analysis. We cannot
reproduce their exact percentile scores (that requires population data), but the
ratios themselves are physiologically meaningful on their own terms.

The crossover point (see above) serves as the primary stayer/sprinter indicator in
the chart UI. The sprint and stayer index values can be surfaced in the crossover
tooltip and/or in a small summary panel below the chart.

---

## Architecture

### `services/critical_power_model.py`

```python
def critical_power_model(t, Pow1, tau1, Pow2, tau2) -> float:
    """Four-parameter two-component power-duration model."""

def fit_critical_power(pb_list: list[dict]) -> dict | None:
    """
    Fit the model to a list of {'duration_s': float, 'watts': float} dicts.
    Returns {'Pow1', 'tau1', 'Pow2', 'tau2', 'r_squared'} or None if fit fails
    or data quality threshold is not met.
    """

def critical_power_curve_points(
    params: dict,
    x_min: float, x_max: float,
    y_min: float, y_max: float,
    show_watts: bool,
) -> list[dict]:
    """
    Generate Chart.js {x, y} point dicts for the prediction curve.
    x = distance in meters, y = watts or pace (sec/500m).
    """

def crossover_point(params: dict, show_watts: bool) -> dict | None:
    """
    Return the {x, y, t_seconds} crossover point, or None if no crossing exists
    in the physiologically meaningful range (10 s – 3 hours).
    """

def stayer_sprinter_metrics(params: dict) -> dict:
    """
    Return {P10, P240, P3600, sprint_index, stayer_index}.
    """
```

### `services/rowing_utils.py`

- `compute_duration_s(workout) -> float | None` — converts any ranked workout
  to its duration in seconds; handles both distance and time event types.

### `components/power_curve_chart_builder.py`

- Imports from `critical_power_model`
- `build_chart_config()` accepts a `critical_power_params` keyword argument
- `"critical_power"` predictor branch:
  - Calls `critical_power_curve_points()` → passes to `_pred_dataset()`
  - Calls `crossover_point()` → adds as a separate single-point dataset with a
    distinctive color and `pointRadius` ≈ 8

### `components/power_curve_page.py`

- "Critical Power" is available in the predictor dropdown (default selection)
- After collecting filtered workouts, gathers the best-per-category `(duration_s,
  watts)` pairs and calls `fit_critical_power()`. Result cached in `state.cp_fit_result`
  keyed on `state.cp_fit_key` (hash of input data).
- Passes `critical_power_params=state.cp_fit_result` to `build_chart_config()`

### Dependency: `scipy`

Used by `fit_critical_power()` for bounded nonlinear least squares.

---

## Future Work

The following enhancements are out of scope for the initial implementation but are
noted here for later consideration:

- **Age-group world records:** Extract Concept2's official age-group records for all
  ranked distances. This enables benchmarking each personal best as a percentage of
  the world record for the user's age/weight/gender category — a richer standard
  than Paul's Law or log-log extrapolation.
- **Concept2 rankings population data:** Extract aggregated Concept2 rankings data
  to build a distribution of rower performance. Combined with the stayer/sprinter
  ratios, this would allow reproducing rowsandall-style population percentile scores
  without relying on their service.
- **Sub-curve visualization:** Toggle to show the fast-twitch (Pow1 component) and
  slow-twitch (Pow2 component) curves separately alongside the total, to give a
  direct visual intuition of how the two energy systems contribute across race
  distances.
- **Time-series of CP parameters:** Animate how Pow1, Pow2, tau1, tau2 evolve
  season over season — the CP model parameters are intrinsically interesting
  training metrics in their own right.
