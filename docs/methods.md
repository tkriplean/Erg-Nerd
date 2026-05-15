# Erg Nerd — How the numbers work

This page is the reference for every number Erg Nerd shows you. For each metric, what it estimates, where it came from, where it's calibrated, where it's a guess. Each entry also notes where the metric appears in the app.

<a id="provenance-tags"></a>
## Provenance tags

Every number you see has a provenance tag, telling you where it came from and how much trust it earns.

| Tag | Meaning |
|---|---|
| **Measured** | Direct from your workout data: pace, time, heart rate, strokes per minute. The Concept2 monitor recorded it and we just display it. |
| **Estimated** | Derived from measured data. Reference Watts, CTL/ATL/TSB, ESS. HRmax derived from HR data in your workout history is an estimate, while HRmax you entered manually is not. |
| **Population default** | A literature constant or population mean used until you give us something better (or you give us enough data to make a better estimate). The W' anaerobic-capacity defaults are population averages and may differ from yours by 30%. |
| **Heuristic** | A rule of thumb, not validated against data, like severity bucket boundaries and stimulus-dose thresholds. Picked using physiological intuition and calibrated against max efforts in your workout history, but not validated against recovery time or adaptive response. |

<a id="reference-watts"></a>
## Reference Watts

*Estimated.* Your estimated watts at every ranked event distance, **for the date you're looking at.**

Most training apps grade every workout against a single snapshot of "current fitness." Erg Nerd builds a quarterly index instead, with markers at Jan 1, Apr 1, Jul 1, and Oct 1 of every year you have data, so a 2009 row gets graded against 2009 fitness, not 2026 fitness. Between markers, watts are linearly interpolated.

For each marker, the app pulls your PBs from the prior 365 days and runs a predictor cascade:

1. If you have at least 5 PBs spanning a 10:1 duration ratio and the Power-Duration fit clears R² ≥ 0.90, use the fit's watts at each event, but only for events within ±1 octave of an actual PB duration. Outside that range the four-parameter fit can produce unconstrained nonsense (above-WR watts at 100m if there's no sub-1min PB anchor).
2. If you have 4 or more PBs, regress Paul's Law (your personal `k` seconds-per-doubling) and predict from there.
3. If you have only 1-3 PBs, use Paul's Law with the default `k = 5.0` anchored to your fastest PB.
4. If a PB exists at the event itself, blend: `watts = PB` when PB beats the prediction (a real performance above the curve is hard evidence), else `mean(prediction, PB)` (a sub-prediction PB may be a sub-maximal day).
5. Final pass: walk events in duration order and cap each longer event at the running minimum so the curve respects power-duration ordering.

**Used on:** The Power Curve page (the chart and prediction table are built from this index). The Volume page (every workout's time-in-zone is classified against the reference watts for that workout's own date). The Workout page (stimulus dose, ESS, and severity all use reference-watts-anchored intensity). The Intervals page (filter chips use the same zones). Reference Watts is the most-used computation in the app.

**Limits.** The quarterly cadence means watts at events without PBs in a quarter window are interpolated between adjacent markers. Small discontinuities at quarter boundaries are possible when a big PB lands on one side.

<a id="predictors"></a>
## Predictors

The Power Curve page can overlay one of several models fit through your PBs. Pick a predictor in the dropdown. Each model has different strengths and biases.

<a id="power-duration"></a>
### Power-Duration (Veloclinic two-component fit)

*Estimated.* A two-component empirical curve:

```
P(t) = Pow1 / (1 + t/τ1) + Pow2 / (1 + t/τ2)
```

`Pow1` and `τ1` describe a short-duration component (high power, fast falloff). `Pow2` and `τ2` describe a long-duration component (lower power, slow falloff). Their sum is your power-duration curve. The fit runs in log-log space so sprint and endurance events get equal weight.

This is not the Critical Power model in the sense some endurance-physiology literature uses the term. Monod-Scherrer / Skiba Critical Power has a defined biological meaning (the highest power you can sustain without progressive W' depletion) and W' as a finite work integral. The Veloclinic fit is purely empirical: it captures curve shape, nothing more. We don't claim `Pow1` and `Pow2` map to fast-twitch versus slow-twitch fibers; they are mathematical components.

**Used on:** Power Curve page chart and prediction table. The fit also feeds Reference Watts (predictor cascade step 1) and W' Remaining (as a proxy for the anaerobic capacity baseline).

**Sources for the model:** Damoiseaux at Veloclinic, [Power Model Derivation (PDF)](https://veloclinic.com/wp-content/uploads/2014/04/PowerModelDerivation-1.pdf). Sander Roosendaal at rowsandall, [How do we calculate critical power](https://analytics.rowsandall.com/2017/06/17/how-do-we-calculate-critical-power/).

**Limits.** The fit is only accepted when you have ≥5 PBs, ≥10:1 duration ratio, and R² ≥ 0.90. Otherwise the app falls back to other predictors.

<a id="log-log"></a>
### Log-Log

*Estimated.* A power-law fit through your PBs in log-log space (linear regression on `log(pace)` versus `log(distance)`). Less sensitive to outliers than the four-parameter fit. Tends to be conservative on the long-duration end.

**Used on:** Power Curve page.

<a id="pauls-law"></a>
### Paul's Law

*Estimated.* Paul Smith's coaching observation: when distance doubles, your 500m pace slows by a fixed number of seconds. Your personal `k` is fit from your PBs when you have at least 3 anchor distances. With fewer anchors the app uses the population default `k = 5.0` (typical for trained adults; well-trained rowers commonly land 4-6).

Single-anchor extrapolation has a sprint-bias when projecting from a long PB to a short event. The app handles this by averaging across all anchors rather than projecting from only one.

**Used on:** Power Curve page; also Reference Watts (predictor cascade steps 2 and 3).


<a id="rowinglevel"></a>
### RowingLevel

*Population default.* A prediction based on your demographics (age, weight, gender) from [rowinglevel.com](https://rowinglevel.com). It treats you as average for your demographics.

For atypical athletes (very strong or very new), RowingLevel pulls toward the population mean. It is not a model of your performance; it is a model of people who share your demographics. Useful as a sanity check, not as a target.

**Used on:** Power Curve page chart and prediction table.


<a id="average"></a>
### Average

*Composite.* The arithmetic mean of whichever predictors above produced a value. A robust middle estimate when no single model dominates. Because RowingLevel is demographic rather than a fit to your data, an atypical athlete will see the Average pulled toward the population mean.

**Used on:** Power Curve page.


<a id="r2-rmse"></a>
## R² and RMSE

*Estimated.* Goodness-of-fit numbers in the prediction-table footer:

- **R²** measures how much of the variance in your PBs each model explains. 1.0 is perfect; below 0.95 suggests the model is missing structure your data contains.
- **RMSE** is root-mean-square error in seconds per 500m, between each model's predictions and your actual PBs at the events you've enabled.

Predictions are middle estimates from imperfect models, not targets. The R² and RMSE tell you how much to trust each model on your data specifically. If you're using a prediction to set a goal pace, use it as the middle of a range and use the RMSE as the rough half-width of that range.

**Used on:** Power Curve page prediction table.


<a id="power-zones"></a>
## Power zones

*Estimated.* Each second of every workout is classified into one of six PB-anchored power zones, based on where the second-by-second power lands relative to your Reference Watts at the relevant duration band.

| Zone | Driven by |
|---|---|
| Sprint | 20-second power band |
| Anaerobic | 90-second band |
| VO2max | 5-minute band |
| Threshold | 20-minute band |
| Tempo | 60-minute band |
| Endurance | 2-hour band |

The classification is PB-anchored: a second's power is graded against the watts you can sustain for that duration, so the zones reflect your individual ability rather than absolute watts.

**Used on:** Volume page Power Spread tab (the default view; stacks per-zone meters across week/month/season). Workout page per-workout breakdown. Filter chips on the Workouts and Intervals pages (Power Zones Engaged / Power Zones Trained).


**Limits.** The bin boundaries are tuned against your own PB shape. They are stable within your data but may differ from a coach's externally-defined zones.

<a id="hr-zones"></a>
## HR zones

*Estimated.* Time in heart-rate zone classified by percentage of your maximum HR.

| Zone | Band |
|---|---|
| Z5 Max | > 90% HRmax |
| Z4 Threshold | 80-90% |
| Z3 Tempo | 70-80% |
| Z2 Aerobic | 60-70% |
| Z1 Recovery | < 60% |

The zone *labels* (Aerobic, Tempo, Threshold) imply zones anchored to lactate thresholds. They are not. The boundaries are population averages, not individualized to you. For a well-trained rower the true lactate threshold HR (LT2) can land anywhere from low Z2 to high Z4, which means the labels can lie. "Z2 Aerobic" might be recovery work for one rower and tempo work for another.

Until you give us a measured threshold HR, treat zone classifications as a rough sort rather than a calibrated training prescription.

**Used on:** Volume page HR Spread tab. HR Zone filter chips on the Workouts and Intervals pages.


**Limits.** Outlier detection rejects HR ≤ 40, HR > 220, and HR > 1.05 × HRmax as monitor artifacts.

<a id="severity"></a>
## Severity

*Heuristic.* A single number capturing how hard the workout was on your body. Combines peak rolling intensity (5/20/60 min windows) with anaerobic strain (W' depletion) and substrate cost (glycogen used):

```
severity = max(peak_5min_intensity, peak_60s_intensity, peak_20s_intensity)
         + 0.50 · W'_strain
         + 0.40 · glycogen_used
```

Bucketed as:

| Bucket | Range | Color |
|---|---|---|
| Low | < 0.70 | Green |
| Moderate | 0.70 - 1.00 | Yellow |
| High | 1.00 - 1.40 | Orange |
| Maximal | ≥ 1.40 | Red |

**Used on:** Workout page (the chip on the workout summary). Volume page (Workout Severity stacking tab; filter chips). Workouts list (color-coded scatter dots when Severity-coloring is selected).


**Limits.** The 0.50 and 0.40 weights are hand-picked. The bucket boundaries are first-cut guesses. Neither has been validated against next-day RPE or recovery time. Treat severity as a relative ordering ("this workout was harder on me than that one") rather than a calibrated recovery prediction. In real physiology, draining both the W' (anaerobic) and glycogen (fuel) reservoirs likely produces super-additive recovery demand, not the simple addition used here.

<a id="ess"></a>
## ESS (Erg Stress Score)

*Estimated.* The session's total training stress, integrated over time:

```
ESS = C_ESS · ∫ I(t)² dt
```

Where `I(t)` is the multi-band intensity signal (see below) and `C_ESS` is a calibration constant chosen so that 60 minutes at your 60-min Reference Watts (your sustainable hour-power) yields `ESS ≈ 100`. ESS is strictly additive: a session's ESS is the sum of its workouts', and a workout's is the sum of its segments'.

**Used on:** Workout page (Erg Stress Score statistic on the summary). Volume page CTL/ATL/TSB rollup (the daily ESS sum is the input to the Banister model).


**Sources for the model:** Coggan & Allen's Normalized Power / TSS framework, plus Skiba's xPower / BikeScore approach. The multi-band intensity signal is described at Sander Roosendaal's [Ergometer scores, how great are you?](https://analytics.rowsandall.com/2018/01/12/ergometer-scores-how-great-are-you/) on rowsandall.

**Limits.** The `C_ESS` calibration uses a cycling-canonical power-duration shape as its synthetic reference profile. Reasonable for rowing as a first approximation, though rowing-specific RW ratios would shift the constant slightly.

<a id="intensity"></a>
## Intensity signal I(t)

*Estimated.* A continuous second-by-second intensity signal that combines six EMA bands (20s, 90s, 5min, 20min, 60min, 2h) anchored to your Reference Watts at each band.

For each second of the workout, for each band:

```
EMA_d(t)         = causal exponential moving average of power with τ = d · factor[d]
zone_ratio_d(t)  = EMA_d(t) / RW_d                  where RW_d = your Reference Watts at duration d
I(t)             = 0.5 · cube_root( Σ_d zone_ratio_d(t)³ )
```

The L³ norm gives every band a vote in the intensity signal. There is no single anchor duration the way Normalized Power and xPower use a single rolling window. The cube emphasizes the dominant band.

Per-band τ factors are physiologically motivated. Short bands (phosphocreatine, fast glycolysis) get 0.30. Mid bands (VO₂, MLSS) get 0.33. Long bands (substrate dynamics, durability) get 0.40. With factor `f`, each band fills to about 95% by time `d` and decays to about 5% after a rest of length `d`. Closer to "rolling window over the last `d` seconds" than to "exponential memory."

**Used on:** Feeds ESS, severity, and stimulus dose. Plotted directly on the Workout page chart in some modes.


**Limits.** The cube-and-cube-root combination is a calibration knob without explicit physiological grounding. A different exponent would shift downstream metrics.

<a id="stimulus-dose"></a>
## Stimulus dose

*Heuristic.* For each duration band, a 0.0-3.0 score indicating whether the workout produced a meaningful training stimulus for that system.

```
peak_d  = max( zone_ratio_d(t) )  over the workout
dose    = quadratic ramp 0 -> 0.95 below threshold
          = 1.0 at threshold
          = linear scaling up to 3.0 above threshold (super-PB)
```

The threshold for each band is set where a sustained effort at the Reference Watts for that duration would saturate the EMA's response curve.

| Band | Threshold | Label |
|---|---|---|
| 20s | 0.60 | Sprint |
| 90s | 0.65 | Anaerobic |
| 5min | 0.75 | VO2max |
| 20min | 0.80 | Threshold |
| 60min | 0.85 | Tempo |
| 2h | 0.75 | Endurance |

Before reporting dose for a band, the app gates on whether you actually spent time at watts classified to that band or higher. Without this gate, a steady Z2 row would register partial Sprint / Anaerobic / VO2max stimulus because the EMA bands cross threshold at any sustained power. The gate keeps the dose physiologically honest.

**Used on:** Workout page (per-system dose bars and chips). Volume page (Training Stimulus panels: solid+ counts over rolling windows, days-since-last-stimulus per system). Filter chips on the Workouts page (Training Stimulus filter).


**Limits.** The thresholds are set against the EMA-curve shape, not validated against adaptive response.

<a id="w-prime"></a>
## W' / W'bal

*Model proxy.* An estimate of your anaerobic work reservoir through the workout, using Skiba's W'bal model:

```
dW'bal/dt = -(P - CP)                           when P > CP   (depletion above CP)
dW'bal/dt = (W'₀ - W'bal) / τ_W'                when P < CP   (recovery below CP)
τ_W'      = 546 · exp(-0.01 · DCP) + 316
```

Where `CP` is your 60-minute Reference Watts (used as a proxy for true Critical Power), `W'₀` is the starting anaerobic capacity (taken as `Pow1 · τ1` from your Power-Duration fit when available, falling back to a population default of 28 kJ for men or 22 kJ for women), and `DCP` is the session-mean watts below `CP`.

This is *model proxy* rather than *computed* because several inputs are approximate. The 60-minute Reference Watts is not the true Critical Power. CP in the Monod-Scherrer / Skiba sense is closer to 95% of 20-minute power, or about 105% of FTP. Using 60-min power as `CP` makes every watt above your 60-min power count as "above CP," so W' depletes too easily and the recovery time constant gets calibrated against the wrong baseline. `Pow1 · τ1` from the Veloclinic fit is dimensionally watts·seconds, but it is not a finite anaerobic work integral the way Skiba's W' is. The population defaults are gendered without scaling for mass; a 60kg lightweight woman and a 95kg heavyweight man should not get the same W'.

Read W' Remaining as a relative-to-yourself tracker. It is useful for comparing how much anaerobic dig each of your own workouts demanded, but not as a calibrated joule count to compare across rowers or against published research.

**Used on:** Workout page (W' Remaining statistic). Severity formula (W'_strain term, see Severity).


**Sources for the model:** Skiba et al. (2012, 2014) for the W'bal mechanism.

<a id="glycogen-used"></a>
## Glycogen Used

*Model proxy.* An estimate of fuel-substrate depletion. The CHO oxidation rate as a fraction of total energy expenditure rises with intensity (Brooks's crossover concept):

```
cho_fraction(I)          = clip(0.30 + 0.70 · I, 0.30, 1.00)
metabolic_watts          = mechanical_watts / 0.25       (gross efficiency)
cho_burn_rate (kJ/s)     = metabolic_watts · cho_fraction(I) / 1000
session_cho_kJ           = ∫ cho_burn_rate(t) dt     over work-only seconds
glycogen_used (fraction) = session_cho_kJ / (80 kJ/kg · mass_kg)
```

Calibrated against bonk-landmark workouts:

- Marathon-finishing-bonk: about 95%
- Hitting the wall / bonk-onset feeling: about 75%
- Half-marathon finished hard but not bonked: about 55%
- 4-hour ultra-aerobic effort: well over 100% (bonk territory)

The linear CHO-fraction ramp probably over-estimates carbohydrate use at sustained low intensity (Z2 base). Brooks's crossover concept puts CHO at about 40-50% of fuel at moderate aerobic intensity; the current model says about 65% at the same intensity. A power-law variant (`0.30 + 0.70 · I^1.5`) is contemplated.

Gross efficiency is fixed at 0.25 (the literature range for trained rowers is 0.22-0.27). The reservoir is 80 kJ/kg of body mass, a conservative estimate of *depletable* glycogen (trained athletes never deplete to literal zero). Without bodyweight entered in Profile, the metric returns blank.

**Used on:** Workout page (Glycogen Used statistic). Severity formula (glycogen_used term).


**Sources for the model:** Brooks (1991) crossover concept; Achten & Jeukendrup (2003) CHO-fat oxidation rates.

<a id="ctl-atl-tsb"></a>
## CTL / ATL / TSB

The Banister model of training load. Three curves over time, all driven by daily ESS rollup.

CTL (Fitness) is *computed* as a 42-day exponentially-weighted moving average of daily ESS. It rises with consistent training, falls with rest.

ATL (Fatigue) is the 7-day exponentially-weighted moving average. It reflects recent loading and short-term fatigue.

TSB (Form) is `CTL - ATL`. Positive means fresh; negative means carrying fatigue. Rough heuristic ranges: above +10 is tapered, -10 to +5 is productive, below -25 is overreached.

Per-day recurrence with time constant τ days:

```
EW[t] = EW[t-1] + (load[t] - EW[t-1]) · (1/τ)
```

τ_CTL = 42, τ_ATL = 7. These are the TrainingPeaks-canonical values, originally validated for cycling.

**Used on:** Volume page Training Load tab. The CTL chart drives the visualization of long-term fitness trajectory; ATL and TSB sit on the same axes.


**Sources for the model:** Banister & Calvert (1980); TrainingPeaks PMC framework.

**Limits.** Decay constants are cycling-derived and may not perfectly fit ergometer rowing. No rowing-specific values exist in the published literature. The series is currently seeded at zero, so the first 42 days read artificially low (as if you arrived "very rested") until the averages converge.

<a id="interval-grid"></a>
## Interval grid

*Heuristic.* A 2D map of your interval sessions: work-duration on the X-axis, work:rest ratio on the Y-axis. Each cell represents a kind of structure (a 2k at 1:45 pace prescription, an 8×500m on 90s block, etc.) with a stimulus-category label and a canonical example.

The grid is purely geometric. It groups your sessions by structure, not by power. A 4×4min/4'r structure at 2k+10s pace is VO2max work; the same structure at 10k pace is sustained tempo. The grid treats them the same.

This is intentional. The grid answers "what kinds of structure have I done?" The per-session severity, stimulus dose, and Power Spread (in the same page's session table) answer "did the session stress the right system?" The two views are complementary. If you want to know whether a 4×4min was actually VO2max work, look at the session row's stimulus chips, not at where it lands on the grid.

The cell labels (e.g. "Lactate production," "VO₂max (medium)") come from canonical interval prescriptions in the physiology literature. Each cell also has an expected-intensity score (0-100), an editorial estimate of how taxing a textbook execution of that structure should be. The score is judgment-based, not derived from data; use it as a guide, not a calibrated target.

| Axis | Bins |
|---|---|
| Work duration (X) | ≤30s · 30s-1min · 1-3min · 3-8min · 8-20min · 20min+ |
| Work:rest ratio (Y) | Continuous (<9% rest) · Short (9-33%) · Balanced (33-60%) · Long (60-80%) · Very Long (>80%) |

**Used on:** Intervals page (the entire grid view).


<a id="glycogen-reserve"></a>
## Implied glycogen reserve

*Estimated.* `bodyweight (kg) × 80 kJ/kg`. The denominator in the Glycogen Used formula and the only mass-dependent input to severity that doesn't go through W'.

**Used on:** Profile page (displayed below the bodyweight input). Indirectly drives the Workout page Glycogen Used statistic.


<a id="hrmax-estimation"></a>
## HRmax estimation

*Estimated.* When you haven't entered an HRmax manually, the app estimates it from your workout history: the 98th percentile of split-average HR pooled across all your workouts.

The estimator is biased low. Split averages don't reach instantaneous peak HR. For rowers who rarely go all-out, the highest split-average is essentially their steady-state HR at hard-but-not-max intensity. The result: estimated HRmax is too low, %HRmax of any workout looks too high, and zone classification on the Volume page inflates toward harder zones.

What helps:

1. Enter your HRmax manually after an all-out test (max 2k, 6×500m, or max 30-min effort).
2. A stroke-level estimator is planned: filter to long maximal sessions, pull stroke-by-stroke HR, and take the highest reading from the longest max effort.

**Used on:** Profile page (the placeholder / default value for the HRmax input). Volume page HR Spread (the denominator for the % HRmax classification). HR Zone filter chips.

<a id="sources"></a>
## Sources

- Achten, J., & Jeukendrup, A. E. (2003). The effect of pre-exercise carbohydrate feedings on the intensity that elicits maximal fat oxidation. *Journal of Sports Sciences.*
- Banister, E. W., & Calvert, T. W. (1980). Planning for future performance. *Canadian Journal of Applied Sport Sciences.*
- Brooks, G. A., & Mercier, J. (1994). Balance of carbohydrate and lipid utilization during exercise: the "crossover" concept. *Journal of Applied Physiology.*
- Buchheit, M., & Laursen, P. B. (2013). High-intensity interval training. *Sports Medicine.*
- Coggan, A. R., & Allen, H. (2010). *Training and Racing with a Power Meter.*
- Damoiseaux. [Power Model Derivation (PDF)](https://veloclinic.com/wp-content/uploads/2014/04/PowerModelDerivation-1.pdf). Veloclinic.
- Monod, H., & Scherrer, J. (1965). The work capacity of a synergic muscular group. *Ergonomics.*
- Pinot, J., & Grappe, F. (2011). The record power profile. *International Journal of Sports Medicine.*
- Roosendaal, S. [Ergometer scores, how great are you?](https://analytics.rowsandall.com/2018/01/12/ergometer-scores-how-great-are-you/) rowsandall (2018).
- Roosendaal, S. [How do we calculate critical power?](https://analytics.rowsandall.com/2017/06/17/how-do-we-calculate-critical-power/) rowsandall (2017).
- Seiler, S., & Tønnessen, E. (2009). Intervals, thresholds, and long slow distance. *Sportscience.*
- Skiba, P. F., et al. (2012, 2014). W'bal dynamics; CP modeling.
