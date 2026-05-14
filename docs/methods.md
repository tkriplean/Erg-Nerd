# Erg Nerd — How the numbers work

This page is the reference for every number the app shows you. For each metric, we explain what it estimates, where it came from, and where its honest limits are.

If you came here because something in the app looked wrong or surprising, scroll to the page that surfaced it and read the entry. If you came here to push back on a calibration choice, the **Calibration log** at the bottom records what's been tuned and against what.

---

## How to read this doc

Every number in Erg Nerd carries a **provenance tag**. It tells you, at a glance, where the value came from and how much trust it earns.

| Tag | Meaning |
|---|---|
| **Measured** | Comes directly from your workout data — pace, time, heart rate, strokes per minute. The Concept2 monitor recorded it; we just display it. |
| **Computed** | Derived from your measured data via a calibrated calculation. Reference Watts (your fitness over time), CTL/ATL/TSB (training load) — these are math you can trust as long as the input data is clean. |
| **Estimated** | Inferred when you haven't given us the input. HRmax derived from your workout history is an estimate; HRmax you entered manually is not. Estimates are biased and noisy; replace them with a real measurement when you can. |
| **Population default** | A literature constant or population mean we use until you give us something better. W' anaerobic capacity uses 28 kJ for men / 22 kJ for women — your actual W' may differ by 30%. |
| **Heuristic** | A first-cut rule of thumb, not validated against data. Severity bucket boundaries (Low / Moderate / High / Maximal), stimulus dose thresholds for each duration band. We picked the numbers using physiological intuition and canonical interval prescriptions; we haven't checked them against recovery time or adaptive response. |

The tag is the first word of every entry below.

A note on naming: this app uses **Power-Duration** for what some training literature calls "Critical Power." The Critical Power name has a specific meaning (Monod-Scherrer / Skiba mechanistic model) that doesn't match what this app actually fits — a Veloclinic two-component empirical curve. We rename it to avoid the conflict.

---

## Power Curve page

The Power Curve page shows your power-duration profile — how much power you can sustain at every duration from a 100m sprint to a marathon — and predicts your pace at events you haven't done lately.

### Reference Watts

*Computed.* Your estimated watts at every ranked event distance, **for the date you're looking at**.

Most training apps grade every workout against a single "current fitness" snapshot. Erg Nerd builds a **quarterly index** of your fitness instead — Jan 1, Apr 1, Jul 1, Oct 1, every year you have data — so a 2009 row gets graded against 2009 fitness, not 2026 fitness. Between markers, watts are linearly interpolated.

**How the quarterly snapshot is built.** For each quarter marker, we pull your PBs from the prior 365 days and run a predictor cascade:

1. If you have ≥5 PBs spanning at least a 10:1 duration ratio and a power-duration fit reaches R² ≥ 0.90, use the fit's watts at each event — but only for events whose duration is within ±1 octave of an actual PB anchor. Outside that range the 4-parameter fit can produce unconstrained nonsense (above-WR watts at 100m if there's no sub-1min PB).
2. If you have ≥4 PBs, regress Paul's Law (your personal `k` seconds-per-doubling) and predict from there.
3. If you have only 1–3 PBs, use Paul's Law with the default `k = 5.0` anchored to your fastest PB.
4. If a PB exists at the event itself, blend: `watts = PB` if PB > prediction (a real performance above the curve is hard evidence), else `mean(prediction, PB)` (a sub-prediction PB may be a sub-maximal day).
5. Final pass: walk events in duration order and cap each longer event at the running minimum so the curve respects power-duration ordering.

**Source:** `services/reference_watts.py`. **Citations:** Allen-Coggan-McGregor / Pinot-Grappe (power profiling); Paul (Paul's Law). **Limits:** The quarterly cadence means watts at events without PBs in a quarter window are interpolated between adjacent markers — discontinuities at quarter boundaries are possible if a big PB lands on one side.

### Predictors

The chart can overlay one of several models that fit a curve through your PBs. Pick a predictor in the dropdown.

#### Power-Duration

*Computed.* A Veloclinic two-component empirical fit:

```
P(t) = Pow1 / (1 + t/τ1) + Pow2 / (1 + t/τ2)
```

`Pow1` and `τ1` describe a short-duration component (high power, fast falloff). `Pow2` and `τ2` describe a long-duration component (lower power, slow falloff). Their sum is the curve. The fit weights sprint and endurance events equally by running in log-log space.

**Not the same as Critical Power.** Some literature uses "Critical Power" specifically for the Monod-Scherrer or Skiba mechanistic models, where CP has a defined biological meaning (the highest power you can sustain without progressive W' depletion) and W' is a finite work integral. The Veloclinic fit is a different math — purely empirical, no mechanistic claim. We don't claim `Pow1` and `Pow2` map to fast-twitch vs slow-twitch fibers; they are mathematical components of the curve, no more.

**Source:** `services/critical_power_model.py`. **Citations:** Veloclinic / rowsandall (two-component empirical fit). **Limits:** Fit only accepted when ≥5 PBs, ≥10:1 duration ratio, and R² ≥ 0.90; otherwise marked unreliable.

#### Log-Log

*Computed.* A power-law fit through all your PBs in log-log space (linear regression on `log(pace)` vs `log(distance)`). Robust to outliers, conservative on the long end.

**Source:** `services/predictions.py`.

#### Paul's Law

*Computed.* "Paul's Law" — when distance doubles, 500m pace slows by a fixed number of seconds. Your personal `k` (the slowing constant) is fit from your PBs when you have ≥3 anchor distances. With fewer anchors we use the population default `k = 5.0` (typical for trained adults; well-trained rowers commonly land 4–6).

**Source:** `services/predictions.py`. **Citations:** Paul Smith, the eponymous coach. **Limits:** Single-anchor extrapolation has a sprint-bias when projecting from a long PB to a short event; multi-anchor averaging dilutes that bias.

#### RowingLevel

*Population default.* A prediction based on your demographics — age, weight, gender — from rowinglevel.com. Treats you as average for your demographics; useful as a sanity check.

**Source:** `services/rowinglevel.py` (scraper + cache). **Limits:** For atypical athletes (very strong or very new), RowingLevel pulls toward the population mean and over- or under-predicts you. It is *not* a model of your performance; it's a model of people who share your demographics.

#### Average

*Composite.* Arithmetic mean of whichever predictors above produced a value. A robust middle estimate when no single model dominates. Because RowingLevel is a demographics-based prior rather than a fit to your data, an atypical athlete will see Average pulled toward the population mean by it.

**Source:** `services/predictions.py`.

### Component view

*Visualization only.* When you check the components option, the chart shows `Pow1 / (1 + t/τ1)` and `Pow2 / (1 + t/τ2)` separately — the "short-duration component" and "long-duration component" of the Veloclinic fit. They are mathematical pieces of the curve; we don't claim they correspond to fast-twitch and slow-twitch muscle fibers.

### R² and RMSE

*Computed.* Below each prediction in the table footer:

- **R²** measures how much of the variance in your PBs the model explains (1.0 = perfect fit; below ~0.95 suggests the model is missing structure).
- **RMSE** is the root-mean-square error, in seconds per 500m, between the model's predictions and your actual PBs at the events you've enabled.

**How to read them.** Predictions are not targets. They are middle estimates from imperfect models. The R² and RMSE tell you how much to trust each model on your data specifically. If you're trying to set a goal pace, use the prediction as the middle of a range; the RMSE is the rough half-width of that range.

### Season Best (SB) markers

*Computed.* For each ranked event, your best pace within the current "season" gets marked with an SB pip. The season runs from your `season_start_month` (default May 1, configurable in Profile) through the same day next year.

### Timeline / simulation mode

The play/pause control replays your fitness over time, stepping through quarterly markers. This is a visualization of the same Reference Watts index you see elsewhere — at each timeline position, the chart shows the Power-Duration curve and predictions for that historical moment.

**Source:** `components/power_curve_animation.py`.

---

## Volume page

The Volume page summarizes *how much* of *what kind* of training you've done over a chosen window — by pace zone, by HR zone, or by workout severity. The right-side panel shows training load (CTL/ATL/TSB) and stimulus history.

### Pace zones (Power Spread tab)

*Computed.* Each second of every workout is classified into one of six power zones by where the second-by-second power lands relative to your Reference Watts at the relevant duration band:

| Bin | Name | Driven by |
|---|---|---|
| 1 | Sprint | 20-second power band |
| 2 | Anaerobic | 90-second band |
| 3 | VO2max | 5-minute band |
| 4 | Threshold | 20-minute band |
| 5 | Tempo | 60-minute band |
| 6 | Endurance | 2-hour band |

The classification is *PB-anchored*: a second's power is graded against the reference watts you can sustain for that duration, so the zones are about your individual ability rather than about absolute watts. The Volume bar stacks per-zone meters across whatever timescale (week / month / season) you've selected.

**Source:** `services/volume_bins.py`. **Limits:** Bin boundaries are tuned against your PB shape; the zones are stable within your data but may differ from a coach's externally-defined zones (e.g. Polar / Friel).

### HR zones (HR Spread tab)

*Estimated.* Time-in-HR-zone classified by percentage of your **maximum heart rate**:

| Zone | Band |
|---|---|
| Z5 Max | > 90% HRmax |
| Z4 Threshold | 80 – 90% |
| Z3 Tempo | 70 – 80% |
| Z2 Aerobic | 60 – 70% |
| Z1 Recovery | < 60% |

**The honest part.** These boundaries are population averages, not individualized. For a well-trained rower, the true lactate-threshold HR (LT2) can fall anywhere from low Z2 to high Z4 — meaning the labels can lie. "Z2 Aerobic" might be recovery work for one rower and tempo work for another.

The zone *labels* (Aerobic, Tempo, Threshold) imply zones anchored to lactate thresholds. **They are not.** Until you give us a measured threshold HR, treat zone classifications as a rough sort, not as a calibrated training prescription.

**Source:** `services/heartrate_utils.py`. **Citations:** Generic 5-zone HRmax model. **Limits:** Anchoring zones to your own LT2 is on the roadmap (see Future Work F1.1). Outlier detection rejects HR ≤ 40, HR > 220, and HR > 1.05 × HRmax as monitor artifacts.

### Workout Severity tab

*Heuristic.* Stacks workouts by their severity bucket (Low / Moderate / High / Maximal). See the **Workout page → Severity** entry below for the underlying formula and its known limits.

### CTL / ATL / TSB chart

The Banister model of training load. Three curves over time.

- **CTL (Fitness)** — *Computed.* A 42-day exponentially-weighted moving average of your daily ESS. Rises with consistent training, falls with rest.
- **ATL (Fatigue)** — *Computed.* A 7-day exponentially-weighted moving average. Reflects recent loading and short-term fatigue.
- **TSB (Form)** — *Computed.* `CTL − ATL`. Positive = fresh; negative = carrying fatigue. Rough ranges: `TSB > +10` (tapered), `−10 to +5` (productive), `< −25` (overreached).

**Formula** (per day, with τ days as time constant):

```
EW[t] = EW[t-1] + (load[t] − EW[t-1]) · (1/τ)
```

τ_CTL = 42, τ_ATL = 7. These are the TrainingPeaks-canonical values, originally validated for cycling.

**Source:** `services/training_load.py`. **Citations:** Banister & Calvert (1980); TrainingPeaks PMC. **Limits:** Decay constants are cycling-derived and may not perfectly fit ergometer rowing recovery dynamics, but no rowing-specific values exist in the published literature. The series is currently seeded at zero — the first ~42 days of your history will read artificially low (as if you arrived "very rested") until the averages converge. Seeding from the first-week mean is on the roadmap.

### Training Stimulus history (right panel)

*Heuristic.* Two views of your stimulus dose history (see **Workout page → Stimulus dose** for the underlying calculation):

- **Panel A** counts the workouts that produced a "Solid+" dose (≥0.80) in each of the six duration bands, over rolling 7 / 28 / 90 / 180 / 365-day and all-time windows.
- **Panel B** counts days since the last Full (≥0.95), Solid (≥0.80), or Partial (≥0.50) dose per system, with adaptation-decay coloring (≤7d green, 8–14d yellow, >14d red).

The decay-color thresholds are heuristic — they reflect the rough adaptive-decay timescales for each system, but we haven't validated them against measured detraining curves.

---

## Workout page

The Workout page shows one session in detail: chart, splits, per-system stimulus dose, and three composite numbers — Severity, W' Remaining, Glycogen Used.

### Severity

*Heuristic.* A composite estimate of how hard the workout was on your body. Computed as:

```
severity = max(peak_5min_intensity, peak_60s_intensity, peak_20s_intensity)
         + 0.50 · W'_strain
         + 0.40 · glycogen_used
```

Bucketed as:

| Bucket | Range | Color |
|---|---|---|
| Low | < 0.70 | Green |
| Moderate | 0.70 – 1.00 | Yellow |
| High | 1.00 – 1.40 | Orange |
| Maximal | ≥ 1.40 | Red |

**The honest part.** The 0.50 / 0.40 weights are hand-picked. The bucket boundaries are first-cut guesses. We have not validated either against next-day RPE or recovery time. Treat Severity as a relative ordering ("this workout was harder on me than that one") rather than a calibrated recovery prediction.

In particular: in vivo, draining both the W' (anaerobic) and glycogen (fuel) reservoirs likely produces super-additive recovery demand, not the simple addition we use here. The v1 formula is "good enough to rank workouts on your own log"; it is not a coaching prescription.

**Source:** `services/erg_stress.py`. **Limits:** TODO.md flags two specific calibration mismatches: an easy 1500m recovery row reads higher severity than expected; a 2k at 1:45 pace reads lower than expected. These are open issues — see Calibration log.

### ESS (Erg Stress Score)

*Computed.* The session's total training stress, integrated over time:

```
ESS = C_ESS · ∫ I(t)² dt
```

where `I(t)` is the multi-band intensity signal (see below), and `C_ESS` is a calibration constant chosen so that 60 minutes at your 60-min Reference Watts (your sustainable hour-power) yields `ESS ≈ 100`. ESS is strictly additive — a session's ESS is the sum of its workouts', and a workout's is the sum of its segments'.

ESS flows into the daily training-load rollup that drives CTL/ATL/TSB on the Volume page.

**Source:** `services/erg_stress.py`. **Citations:** Coggan & Allen (Normalized Power / TSS); Skiba (xPower / BikeScore). **Limits:** The `C_ESS` calibration uses cycling-canonical power-duration ratios as its synthetic reference profile. Reasonable for rowing as a first approximation; rowing-specific ratios are on the roadmap.

### Intensity signal `I(t)`

*Computed.* The multi-band intensity that drives ESS, severity, and stimulus dose.

For each second of the workout, for each of six duration bands (20s, 90s, 5min, 20min, 60min, 2h):

```
EMA_d(t)        = causal exponential moving average of power with τ = d · factor[d]
zone_ratio_d(t) = EMA_d(t) / RW_d                         where RW_d = your Reference Watts at duration d
I(t)            = 0.5 · cube_root( Σ_d zone_ratio_d(t)³ )
```

The L³ norm gives every band a vote in the intensity signal — there's no single anchor duration the way Normalized Power and xPower use a single rolling window. The cube emphasizes the dominant band.

**Per-band τ factors** are physiologically motivated: 0.30 for the short bands (phosphocreatine and fast-glycolysis kinetics), 0.33 for the mid bands (VO₂ and MLSS), 0.40 for the long bands (substrate and durability). With factor `f`, each band fills to ~95% by time `d` and decays to ~5% after a rest of length `d` — closer to "rolling window over the last `d` seconds" than "exponential memory."

**Source:** `services/erg_stress.py`. **Limits:** The cube-and-cube-root combination (`SIGNAL_AMPLIFIER = 3`) is a calibration knob without explicit physiological grounding. Different exponents would shift downstream metrics.

### W' Remaining (W'bal)

*Model proxy.* An estimate of your anaerobic work reservoir through the workout, using Skiba's W'bal model:

```
dW'bal/dt = − (P − CP) when P > CP        (depletion above CP)
dW'bal/dt = (W'₀ − W'bal) / τ_W' when P < CP  (recovery below CP)
τ_W' = 546 · exp(−0.01 · DCP) + 316
```

where:
- `CP` is the rower's 60-minute Reference Watts (used as a proxy for true Critical Power)
- `W'₀` is the starting anaerobic capacity — either `Pow1 · τ1` from your Power-Duration fit, or a population default (28 kJ for men, 22 kJ for women) when no fit is available
- `DCP` is the session-mean watts below `CP`, used in Skiba's empirical recovery formula

**Why this is "model proxy" and not "computed."** Several inputs are approximate:

1. The 60-minute Reference Watts is **not** the true Critical Power. Critical Power in the Monod-Scherrer / Skiba sense is closer to 95% of 20-minute power, or about 105% of cycling FTP. Using 60-min power as `CP` makes every watt above your 60-min power count as "above CP" — so W' depletes too easily, and the recovery time constant is calibrated against the wrong baseline.
2. `Pow1 · τ1` from the Veloclinic fit is dimensionally watts·seconds, but it is not a finite anaerobic work integral the way Skiba's `W'` is. We use it because it's the best per-rower estimate we have, but the units lie.
3. The population defaults are gendered without scaling for mass. A 60kg lightweight woman and a 95kg heavyweight man should not get the same `W'`. Mass-scaling is on the roadmap.

**How to read it.** As a *relative-to-yourself* tracker — useful for comparing how much anaerobic dig each of your own workouts demanded. **Not** as a calibrated joule count to compare across rowers or against published research.

**Source:** `services/erg_stress.py`, `services/critical_power_model.py`. **Citations:** Skiba et al. (2012, 2014) for W'bal mechanism.

### Glycogen Used

*Model proxy.* An estimate of fuel-substrate depletion. The CHO oxidation rate as a fraction of total energy expenditure rises with intensity (Brooks's "crossover concept"):

```
cho_fraction(I)         = clip(0.30 + 0.70 · I, 0.30, 1.00)
metabolic_watts         = mechanical_watts / 0.25         (gross efficiency)
cho_burn_rate (kJ/s)    = metabolic_watts · cho_fraction(I) / 1000
session_cho_kJ          = ∫ cho_burn_rate(t) dt     over work-only seconds
glycogen_used (fraction) = session_cho_kJ / (80 kJ/kg · mass_kg)
```

Calibrated against bonk-landmark workouts:
- Marathon-finishing-bonk: ~95%
- The wall / bonk-onset feeling: ~75%
- Half-marathon finished hard but not bonked: ~55%
- 4-hour ultra-aerobic effort: > 100% (bonk territory)

**The honest parts.**
- The linear CHO-fraction ramp likely over-estimates carbohydrate use at sustained low intensity (Z2 base). Brooks's crossover concept puts CHO at ~40–50% of fuel at moderate aerobic intensity; our model says ~65% at the same intensity. A power-law variant (`0.30 + 0.70 · I^1.5`) is contemplated to fix this; on the roadmap as Tier 1.1.
- Gross efficiency is fixed at 0.25 (the literature range for trained rowers is 0.22–0.27).
- The reservoir is fixed at 80 kJ/kg of body mass — a conservative estimate of *depletable* glycogen. Trained athletes never deplete to literal zero.
- Without bodyweight, the metric returns blank.

**Source:** `services/glycogen.py`. **Citations:** Brooks (1991) crossover concept; Achten & Jeukendrup (2003) CHO-fat oxidation rates.

### Stimulus dose bars (per duration band)

*Heuristic.* For each duration band, a 0.0 – 3.0 score indicating whether the workout produced a meaningful training stimulus for that physiological system.

```
peak_d = max( zone_ratio_d(t) )  over the workout
S_thresh_d = the saturation threshold for band d
```

The threshold for each band is set where a sustained effort at the Reference Watts for that duration would saturate the EMA's response curve. Below threshold, dose ramps quadratically from 0 to ~0.95. At threshold, dose = 1.0. Above threshold, dose scales linearly up to 3.0 for super-PB efforts.

**Threshold values:**

| Band | Threshold | Label |
|---|---|---|
| 20s | 0.60 | Sprint |
| 90s | 0.65 | Anaerobic |
| 5min | 0.75 | VO2max |
| 20min | 0.80 | Threshold |
| 60min | 0.85 | Tempo |
| 2h | 0.75 | Endurance |

**Stimulus confirmation gate.** Before reporting dose, we check whether you actually spent any time at watts classified to this band or higher. Without this gate, a steady Z2 row would register partial Sprint / Anaerobic / VO2max stimulus because the EMA bands cross threshold at any sustained power. The gate keeps the dose physiologically honest.

**The honest part.** The thresholds are set against the EMA-curve shape, not validated against adaptive response in rowers. TODO.md asks "is it too hard to get a 100% tempo dose?" — that question is open.

**Source:** `services/erg_stress.py`. **Limits:** Per-band Tempo thresholds in particular need data; see Calibration log.

### Split table

*Measured.* Pace, watts, distance, SPM, heart-rate per Concept2-recorded split. Nothing computed here — these are the raw values from the export.

### SPM (strokes per minute)

*Measured.* Currently displayed in the workout table as a column, with no analysis. On the roadmap (Future Work F2.1): SPM-vs-pace scatter, SPM drift within a workout (fatigue indicator), and combining SPM drift with HR drift into an "Aerobic Durability" score per workout.

### Drag factor

*Measured.* Currently not surfaced in the workout view; the same workout at DF 100 vs DF 140 is a different physiological challenge. Severity and stimulus don't currently normalize for DF; on the roadmap (Future Work F2.4).

---

## Intervals page

The Intervals page groups your interval sessions on a 2-dimensional grid — work-interval duration on the X-axis, work:rest ratio on the Y-axis — and shows what kinds of structures you've done.

### The grid

*Heuristic.* Each cell represents a *structure*, with a stimulus-category label and a canonical example workout. Cell labels come from physiology literature (Allen-Coggan, Buchheit-Laursen, Seiler-Tønnessen prescriptions).

**Critical context.** The grid is **purely geometric** — it groups your sessions by *structure*, not by *power*. A 4×4min/4'r structure at 2k+10s pace is VO2max work; the same structure at 10k pace is sustained tempo. The grid treats them the same.

This is intentional. The grid answers "what kinds of structure have I done?". The per-session severity / stimulus / Power Spread numbers (in the same page's session table) answer "did the session stress the right system?". The two views are complementary, not competing. If you want to know whether a 4×4min was actually VO2max work, look at the session row's stimulus chips, not at where it lands on the grid.

### Axes

| Axis | Bins |
|---|---|
| Work duration (X) | ≤30s · 30s–1min · 1–3min · 3–8min · 8–20min · 20min+ |
| Work:rest ratio (Y) | Continuous (<9% rest) · Short (9–33%) · Balanced (33–60%) · Long (60–80%) · Very Long (>80%) |

### Cell labels

The cells are populated from a static `_STIMULUS_INFO` matrix. Each cell has a name, description, canonical example workout, and an expected-intensity score (0–100). The score is an **editorial estimate** of how taxing a textbook execution of that structure ought to be — it's not derived from data, it's our judgment based on canonical prescriptions. Use it as a guide, not as a calibrated target.

**Source:** `components/intervals_page.py`.

---

## Profile page

Profile holds the personal data the app uses to personalize models — and the small set of computed values derived from those entries.

### Editable fields

| Field | Type | Persistence | Notes |
|---|---|---|---|
| Gender | Male / Female | Saves immediately | Used for W' default (Tier 0–1: will be replaced by mass-scaled W'). |
| Date of Birth | YYYY-MM-DD | On click "Update" | Used to derive age for HRmax default (220 − age). |
| Bodyweight | kg or lbs | On click "Update" | Drives glycogen-reserve calc and (Tier 1) the mass-scaled W' default. |
| Weight Class | Heavyweight / Lightweight | Saves immediately | Display label; doesn't drive metrics directly. |
| Max Heart Rate | bpm | On click "Update" | If blank, app estimates from your workouts. |
| Public Profile | Toggle | Saves immediately with confirmation | Publishes your profile + workouts at a shareable URL. |

### Computed / derived values

#### Implied glycogen reserve

*Computed.* `bodyweight (kg) × 80 kJ/kg`. Displayed below the bodyweight input. Drives the **Glycogen Used** metric on the Workout page; see that entry for the model. Without a bodyweight, Glycogen Used returns blank.

#### Estimated HRmax (default value when none entered)

*Estimated.* When you haven't entered an HRmax manually, the app estimates it from your workout history (currently the 98th percentile of split-average HR pooled across all your workouts).

**This estimator is biased low.** Split averages don't reach instantaneous peak HR, and for rowers who rarely go all-out the highest split-average is essentially their steady-state HR at hard-but-not-max intensity. The result: estimated HRmax is too low, %HRmax of any workout looks too high, and zone classification on the Volume page inflates toward harder zones.

**Mitigations.**
1. Enter your HRmax manually after an all-out test (max 2k, or 6×500m, or a max 30-min effort).
2. A more sophisticated stroke-level estimator is on the roadmap (Tier 1.4): filter to long maximal sessions → pull stroke-by-stroke HR → take the highest reading from the longest max effort.

**Source:** `services/heartrate_utils.py`. **Citations:** None — population-average HRmax inference.

### Fields not yet in the profile

Three fields would materially improve the app's physiological honesty if added. They're on the roadmap (see Future Work F1):

- **Lactate Threshold HR (LT2)** — would let HR zones anchor to your own physiology instead of % HRmax.
- **60-min Power** — would replace the inferred 60-min Reference Watts with your measured value as the anchor for ESS, W'bal, and stimulus calibration.
- **Season start month** — would make the "Season Best" framing on the Power Curve match your competitive calendar (default May 1; southern-hemisphere and academic-calendar rowers want different cutoffs).

---

## What the app *does not* model

These are gaps we acknowledge — features that other rowing tools may have, that Erg Nerd does not.

- **Within-workout pacing analysis.** A 2k where you go out at 1:45 and fade to 1:55 versus an even-paced 1:50/1:50/1:50/1:50 average to the same pace but tell different stories. The PB record stores neither.
- **HR drift and Pw:HR decoupling.** A 30-minute tempo where HR drifts 150 → 165 versus a flat 158 across the same piece are physiologically different. We don't compute drift.
- **Stroke-level analysis.** Drive force curves, stroke-to-stroke variability, drive-to-recovery ratio — these are rowing-specific signals we don't model yet.
- **HRV / autonomic recovery state.** Modern training tools use HRV to gate intensity. CTL/ATL/TSB are coarse proxies for the same idea.
- **Subjective RPE.** No post-workout perceived-effort log. RPE data would let us back-calibrate severity and stimulus thresholds against how you actually felt.
- **Drag-factor sensitivity.** Same wattage at DF 100 vs DF 140 is different physiologically; we don't normalize.

Most of these are on the roadmap (see `FUTURE_WORK.md`).

---

## Calibration log

Append-only record of when constants were last reviewed and against what data. Future tweaks add an entry rather than silently moving the goalpost.

| Date | Constant | Old → New | Calibrated against | Notes |
|---|---|---|---|---|
| _initial_ | `EMA_TAU_FACTORS` | uniform → per-band {0.30, 0.30, 0.33, 0.33, 0.40, 0.40} | Physiological intuition for each band | Phosphocreatine and glycolysis: 0.30. VO₂ and MLSS: 0.33. Substrate and durability: 0.40. Not validated against measured response. |
| _initial_ | `STIMULUS_S_THRESH` | (first-cut) {0.60, 0.65, 0.75, 0.80, 0.85, 0.75} | Canonical interval prescriptions | The 5min band sits at 0.75 (not 0.70) to keep 5 min @ 60-min power out of the "full VO2max" bucket. Tempo (60min, 0.85) is high enough that "is it too hard to get a 100% tempo dose?" is still open. |
| _initial_ | `SEVERITY_THRESHOLDS` | (first-cut) {Low 0.70, Moderate 1.00, High 1.40, Maximal ∞} | Hand-picked | Not validated against next-day RPE or recovery time. Bucket colors imply a recovery framework we haven't measured against. |
| _initial_ | `SEVERITY` weights | (first-cut) `peak + 0.50·W'_strain + 0.40·glycogen_used` | Hand-picked | Linear additivity is a v1 choice; real recovery is likely super-additive when both reservoirs drain. |
| _initial_ | `RESERVE_KJ_PER_KG` | 80 kJ/kg | Bonk-landmark workouts (marathon ≈95%, wall ≈75%, hard HM ≈55%, 4h ultra >100%) | Conservative end of the 80–100 kJ/kg lit range. Linear-CHO-fraction ramp may over-estimate at Z2 base — power-law variant on roadmap. |
| _initial_ | `GROSS_EFFICIENCY` | 0.25 | Trained-rower lit range 0.22–0.27 | Fixed in v1. |
| _initial_ | `W_PRIME_DEFAULT_M` / `W_PRIME_DEFAULT_F` | 28 kJ / 22 kJ | Cycling-derived population defaults | Gendered without mass scaling. Mass-scaled version on roadmap. |
| _initial_ | `CTL_DECAY_DAYS` / `ATL_DECAY_DAYS` | 42 / 7 | TrainingPeaks PMC canonical (cycling-validated) | No rowing-specific values exist in the literature. |
| _initial_ | `PAULS_DEFAULT_K` | 5.0 sec/500m per doubling | Population average for trained adults | Well-trained rowers commonly land 4–6. Personal k is fit when ≥3 anchor distances exist. |
| _initial_ | C_ESS calibration profile | Cycling-canonical RW ratios (5.0 / 2.5 / 1.4 / 1.05 / 1.00 / 0.95) | Allen-Coggan / Pinot-Grappe | Rowing-shape RW ratios on roadmap. |

---

## Known open issues

Tracked in [TODO.md](../TODO.md). Notable calibration items:

- The easy 1500m recovery workout reads higher severity than expected; the 2k at 1:45 pace reads lower than expected. (See TODO.md, "ESS calibration issue".)
- Warmup / cooldown detection is incomplete — workouts that should be tagged warmup-only sometimes register stimulus.
- Stroke-level HRmax estimator (Tier 1.4) would replace the biased-low split-average percentile.

---

## Sources

- Banister, E. W., & Calvert, T. W. (1980). Planning for future performance. *Canadian Journal of Applied Sport Sciences*.
- Brooks, G. A., & Mercier, J. (1994). Balance of carbohydrate and lipid utilization during exercise: the "crossover" concept. *Journal of Applied Physiology*.
- Buchheit, M., & Laursen, P. B. (2013). High-intensity interval training, solutions to the programming puzzle. *Sports Medicine*.
- Coggan, A. R., & Allen, H. (2010). *Training and Racing with a Power Meter*.
- Monod, H., & Scherrer, J. (1965). The work capacity of a synergic muscular group. *Ergonomics*.
- Paul, P. (n.d.). Paul's Law (folkloric origin in rowing-coaching tradition).
- Pinot, J., & Grappe, F. (2011). The record power profile. *International Journal of Sports Medicine*.
- Seiler, S., & Tønnessen, E. (2009). Intervals, thresholds, and long slow distance. *Sportscience*.
- Skiba, P. F., et al. (2012, 2014). W'bal dynamics; CP modeling.
- Veloclinic / rowsandall — two-component power-duration model (rowsandall.com).
