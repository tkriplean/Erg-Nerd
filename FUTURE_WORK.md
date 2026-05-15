# Future Work

Tracked but unscheduled work, expanded from the deferred items in `/Users/travis/.claude/plans/you-re-an-expert-endurance-humming-feather.md`. Each entry describes motivation, scope, rough effort, expected impact, open risks, and links to related items. Pick from this file when you have time to spend; the plan does not attempt to sequence them.

The Tier 0 and Tier 1 work from that plan is already in the codebase. What follows is everything that was deliberately deferred so the immediate physiology and language work could land first.

## Tier-F1: Threshold anchoring

The deepest physiological upgrade still on the table. Turns population-zone heuristics into individualized zones. Each item below is small in isolation, but together they unlock honest threshold-anchored reporting across the app.

### F1.1 — Lactate Threshold HR field in Profile

**Motivation.** HR zones today are anchored to a percentage of max HR, which is a population average for the LT1 / LT2 transitions. Two rowers with the same HRmax can have very different lactate thresholds, and the app's Z2 / Z4 labels imply LT-anchored zones they are not. A user-entered LT2 HR collapses that mismatch.

**Scope.** Add `lactate_threshold_hr_bpm` to the profile, exposed in [components/profile_page.py](components/profile_page.py) as a `number_input` with the field copy from the plan ("Heart rate at your second lactate threshold ..."). When the field is set, [services/heartrate_utils.py](services/heartrate_utils.py:211-233) switches to a Seiler 3-zone model anchored to LT2 (`< 0.85·LT2` easy, `0.85–1.00·LT2` tempo, `> 1.00·LT2` hard). The Volume page's HR-mode chart, the HR-filter dropdown in [components/spread_quality_legends.py](components/spread_quality_legends.py), and the per-workout HR row all read through the same resolver, so the switch is one resolver call.

**Effort.** Two to three days. The math is short, but the surface area (profile UI, zone resolver, three downstream renderers, plus a toggle for users who prefer the 5-zone Friel / Coggan model) takes time to land cleanly.

**Expected impact.** This is the single biggest physiology win available. The numbers stop being calibrated against the wrong reference once a real LT2 sits in the profile.

**Risks / open questions.** Users who do not know their LT2 will not enter one, so the default stays `%HRmax`. Need a tooltip path for "how to find your LT2" that does not promise more than the app can verify. Whether to support Friel / Coggan 5-zone as a secondary anchor is open; the plan recommends Seiler 3-zone first and a toggle later.

**Related.** F1.2 (60-min Power) is the wattage analogue; F3.3 (lactate-test upload) is the gold-standard data source if it ever ships.

### F1.2 — 60-min Power field in Profile

**Motivation.** Several metrics (ESS calibration, W'bal recovery rate, the implicit "Critical Power" the W'bal model uses) are anchored to the rower's 60-min reference watts derived from their PB curve. When the rower knows their true 60-min sustainable power from a real test, that should override the derived value. The 60-min Power field is also the lever that resolves the longstanding "W'bal uses 60-min RW as CP" honesty issue.

**Scope.** New profile field `sixty_min_power_watts`. When set, it overrides the 60-min reference watts at the user's chosen anchor date, propagating into [services/erg_stress.py:1259-1271](services/erg_stress.py) (W'bal `cp` resolution) and [services/reference_watts.py](services/reference_watts.py) (the time-aware ref-watts cascade). The profile copy is the field language from the plan ("Your sustainable wattage for ~60 minutes ... we avoid the names 'FTP' and 'Threshold Power' because rowing has multiple physiological thresholds").

**Effort.** Half a day for the field plus the propagation. The downstream code paths already accept a `cp_watts` argument from a single resolver, so the threading is light.

**Expected impact.** Removes one of the largest source-of-truth gaps in the app. W'bal becomes a calibrated joule count rather than a "relative-to-self" reading.

**Risks / open questions.** A user-entered 60-min Power that conflicts with what the PB curve implies needs a discoverable mismatch prompt, similar to the HRmax / Concept2 mismatch already implemented in Tier 0.5.

**Related.** F1.3 (True CP test) supplies a higher-priority W'bal anchor when available. The Tier-0 Power-Duration rename and this field together close the "Critical Power is not CP" issue end to end.

### F1.3 — True CP test workflow

**Motivation.** Skiba's W'bal model wants real Critical Power and real W' as inputs, not their loose proxies. A standard 3-min all-out test gives both directly: `CP = mean(last 30s power)`, `W' = ∫(P − CP) dt` over the 3 minutes. Once captured, these should win over both the PB-derived approximations and the F1.2 60-min Power field for W'bal purposes.

**Scope.** New module `services/cp_test.py` for the test-derivation math (pure functions). New profile fields `cp_test_watts` and `cp_test_w_prime_joules`, plus a `cp_test_date` so the user can refresh them after retests. An onboarding flow in [components/profile_page.py](components/profile_page.py) that walks the rower through scheduling the test and entering the result. Plumb into [services/erg_stress.py](services/erg_stress.py) so the W'bal resolver prefers measured CP / W' over the PD-derived `Pow1·tau1` fallback.

**Effort.** Three to five days, mostly in the onboarding UX and the resolver priority logic.

**Expected impact.** Best-case W'bal accuracy on the app, with provenance to back it up.

**Risks / open questions.** A 3-min all-out test is hard to pace, and a poorly executed test can mislead the model. The onboarding needs guardrails (a pacing chart, a "did you actually go all out" sanity check on the entered numbers).

**Related.** Supersedes F1.2 for W'bal purposes when both are present.

## Tier-F2: Rowing-specific physiological richness

### F2.1 — Stroke-metric integration

**Motivation.** The Workout page surfaces SPM as a column with no analysis. The Concept2 stroke export carries enough information to compute drift, pace-stroke decoupling, and drive-to-recovery ratio. These are the highest-leverage rowing-specific signals the app currently ignores.

**Scope.** [components/workout_page.py](components/workout_page.py) gains: an SPM-versus-pace scatter (technique consistency), an SPM drift trace within the workout (fatigue indicator), and a drive-to-recovery ratio if the export carries it. The three combine into a per-workout "Aerobic Durability" score using SPM drift, HR drift, and Pw:HR decoupling. Stroke data is already cached locally via the strokes pipeline, so the data path is already in place.

**Effort.** One to two weeks, depending on how far the durability score goes and whether it lands in the workout summary or as its own panel.

**Expected impact.** Closes the gap between "this is a Concept2 dashboard" and "this is a rowing-specific training tool." The durability score is the strongest single addition for rowers training for distance events.

**Risks / open questions.** Drive-to-recovery ratio may not be on every Concept2 firmware version; need a graceful absence path. The durability score is a synthesis that needs calibration data before it gets a chip in the summary panel.

**Related.** F2.2 (HR drift / decoupling) shares the within-workout fatigue framing; F2.4 (drag factor) provides a covariate that explains some of the scatter.

### F2.2 — Within-workout HR drift and Pw:HR decoupling

**Motivation.** A 30-minute tempo where HR drifts from 150 to 165 has a very different physiological signature than one held flat at 158. Drift slope (bpm per minute) over sustained pieces, and the Pw:HR ratio comparing first half against second half, capture this without any new data sources. The data already exists in the per-split records.

**Scope.** New module `services/hr_drift.py` for the pure-function math. A new panel on [components/workout_page.py](components/workout_page.py) showing the drift slope and decoupling percentage for the sustained portion of the workout. Skip the analysis when the workout is short enough that drift is not meaningful (under about 15 minutes of sustained work).

**Effort.** Three to four days.

**Expected impact.** A second per-workout signal for aerobic durability that does not require any new instrumentation.

**Risks / open questions.** Drift calculation needs a clean "sustained work" boundary; interval workouts with rest periods complicate the math. Use the existing work-mask logic from [services/erg_stress.py](services/erg_stress.py).

**Related.** Feeds into F2.1's durability score.

### F2.3 — Pacing analysis

**Motivation.** Per-workout split-fade analysis (each split's pace as a delta from the first split) reveals a rower's pacing style: positive split, negative split, even, J-shaped. Knowing the rower's fade profile turns the Race-page predictor from "here is the target 2k pace" into "here is the split sequence that matches how you actually race."

**Scope.** A pacing classifier in `services/pacing.py` that categorizes a workout into one of the four pacing shapes. A new tab or panel on the Workout page that visualizes the split-fade. On the Race page, a pacing-strategy generator that takes a target time and the rower's typical fade profile and proposes a split sequence.

**Effort.** One to two weeks for the analysis and visualization, plus a few more days if the race-strategy generator gets its own UI.

**Expected impact.** High-leverage feature for users training toward a specific race. Less interesting for casual training tracking.

**Risks / open questions.** Pacing style varies by distance and effort level; classifying across the rower's full history may average out useful detail. May need per-event classification (2k pacing versus 10k pacing).

### F2.4 — Drag-factor surfacing on the workout page

**Motivation.** Drag factor materially affects physiological cost and meaningful comparisons across workouts. A 2k at drag 130 is physiologically different from a 2k at drag 105, even at the same pace. Today the workout page treats DF as a single integer with no context.

**Scope.** Compute the rower's modal drag factor per machine in [services/concept2.py](services/concept2.py). Flag workouts whose DF deviates significantly (more than 10 units) from the mode, both inline on the Workout page and in the workout-table column. Optionally add a per-DF cohort filter on the Power Curve page so PBs at unusual DFs do not anchor predictions for typical-DF efforts.

**Effort.** Two to three days.

**Expected impact.** Modest. The flag prevents misleading comparisons but does not unlock new analyses.

**Risks / open questions.** Cohorting by DF reduces the PB sample size; the predictor cascade needs a fall-through when the cohort is too small.

### F2.5 — C_ESS rowing-shape recalibration

**Motivation.** The synthetic profile that calibrates `C_ESS` in [services/erg_stress.py:793-841](services/erg_stress.py) bakes a cycling-shape assumption into ESS: the 5-second / 30-second / 5-minute / 20-minute / 60-minute / 120-minute power ratios (5.0 / 2.5 / 1.4 / 1.05 / 1.00 / 0.95) are canonical cycling values, not rowing values. Rowing's typical ratios run higher at the long end (around 5.5 / 2.5 / 1.4 / 1.05 / 1.00 / 0.85–0.90) because erg rowing is more aerobic-leaning than road cycling. The bias is small at the 60-min anchor and grows as the workout gets longer.

**Scope.** Re-derive `C_ESS` against a rowing-canonical synthetic profile. Update the calibration table generator. Document the constant change in the methodology page's calibration log.

**Effort.** Half a day for the math; another half day for re-running the calibration table and confirming nothing else shifts.

**Expected impact.** Small per workout but compounds across long efforts. Brings ESS into honest rowing-specific calibration.

**Risks / open questions.** The "canonical rowing profile" is itself an estimate; want to validate against a real rower's PB curve before committing the constants.

## Tier-F3: Aspirational

### F3.1 — HRV / wellness ingestion

**Motivation.** Training-load metrics (CTL / ATL / TSB, ESS) describe externally-measured stress but ignore autonomic recovery state. HRV from Garmin / Apple / Polar carries the recovery side of the equation. Gating ESS-driven recommendations against HRV is the gap between "your training stress looks fine" and "your training stress looks fine but your autonomic system says no."

**Scope.** OAuth integrations to Garmin Connect, Apple Health, or Polar Flow (one at a time, starting with whichever the user actually uses). New module `services/wellness.py` for the data fetch and normalization. New "Recovery" lane on the Volume / Training Load chart.

**Effort.** Months. Each integration is its own auth flow and rate-limit story.

**Expected impact.** Transformative for users who actually wear an HRV device. Zero for users who do not.

**Risks / open questions.** OAuth quotas, rate-limits, and the platforms' tendency to change their APIs. Worth scoping a one-platform proof-of-concept (likely Garmin) before committing to a multi-platform integration.

### F3.2 — RPE logging post-workout

**Motivation.** Closes the calibration loop the methodology page admits is open. Severity buckets, stimulus dose thresholds, and the glycogen reserve constant are all heuristics that could be retuned against actual perceived effort if the app collected it. A tiny "1-10 RPE" prompt after each workout sync builds the dataset over time.

**Scope.** A modal or inline prompt in [components/workouts_page.py](components/workouts_page.py) that asks for RPE on newly-synced workouts. Persist to localStorage. Surface RPE as a column in the workout table, and as a calibration signal on the methodology page's calibration log.

**Effort.** Three to four days for the prompt and persistence. Months before the dataset is big enough to retune anything.

**Expected impact.** Long-run. The constants in this app will only get better with feedback data.

**Risks / open questions.** Users typically do not log RPE consistently. Capturing it in the moment (right after a sync) is the only path that has any hope of consistency.

**Related.** All the heuristic constants in [services/erg_stress.py](services/erg_stress.py) and [services/glycogen.py](services/glycogen.py) are candidates for RPE-anchored retuning.

### F3.3 — Lactate-test upload

**Motivation.** Direct measurement of LT1 / LT2 from a blood-lactate step test beats every proxy in the app. Users with access to a lab or a home lactate meter can anchor their zones to ground truth.

**Scope.** A profile-level file upload for a CSV / JSON of `(power_watts, lactate_mmol_l)` pairs. New module `services/lactate.py` for the inflection-point detection (LT1 at the rise above baseline, LT2 at the deflection toward exponential growth). The detected thresholds populate the F1.1 LT2 field and a new LT1 field, plus their power equivalents for F1.2.

**Effort.** One to two weeks for a clean inflection-point detection and the upload UI.

**Expected impact.** Niche but definitive for the users who have the data.

**Risks / open questions.** Inflection-point detection is sensitive to test protocol (step length, increment size). Need to either constrain the input or detect protocol parameters from the data shape.

### F3.4 — Coaching-prescription view

**Motivation.** The app describes what has happened; a coaching-prescription view would describe what should happen next. "You have not done VO2max in twelve days, here are three options sized to your current TSB."

**Scope.** Synthesis of every existing signal: time since last stimulus per system (already on Volume page), current TSB (Training Load tab), the rower's typical workout shapes (Workouts table). New page or tab that proposes 2 to 3 candidate workouts each refresh, sized to the rower's current recovery state and stimulus debt.

**Effort.** Months. The synthesis is straightforward; the editorial layer (which signals to weight, how to phrase the suggestions, how to avoid generic over-recommendations) is the work.

**Expected impact.** Transformative for self-coached athletes. Risky too: prescription tools have higher trust requirements than the rest of the app.

**Risks / open questions.** Crosses the line from "personal tool" to "coaching tool" and inherits the trust bar that goes with that. Probably needs an alpha-only opt-in and a clear "this is a sketch, not a coach" framing.

## Cross-cutting deferred items

The plan flagged these explicitly as out-of-scope for now. They live here so the next refactor pass remembers them.

### `EMA_TAU_FACTORS` recalibration

The per-band factors in [services/erg_stress.py:165](services/erg_stress.py) (`{20: 0.30, 90: 0.30, 300: 0.33, 1200: 0.33, 3600: 0.40, 7200: 0.40}`) are physiologically motivated guesses. The slow bands could go slower again to honor substrate-dynamics kinetics. Defer until there is RPE data (F3.2) or lactate data (F3.3) to validate against.

### PMC decay constants (42 / 7)

[services/training_load.py](services/training_load.py) uses the TrainingPeaks-canonical 42-day CTL and 7-day ATL constants. Both were originally validated for cycling. Defer until rowing-specific data exists in the literature.

### Severity additivity weights

Severity composes peak intensity, W' debt, and glycogen drain as `peak_I + 0.5·W'_used + 0.4·glycogen_used`. These weights are first-cut hand-tuning. Defer retuning until RPE data (F3.2) can validate the bucket boundaries.

### Interval grid (`components/intervals_page.py`)

Geometric work-rest grid that answers "what kinds of structure have I done" rather than "did the workout stress the right system." The plan decided to keep it geometric (the workout-level severity and stimulus already answer the latter question). One tooltip clarifies its purpose; no further changes planned.

### Average predictor math

The Average predictor takes the arithmetic mean of available predictors. Reviewer 1 suggested weighting by epistemic status (treat RowingLevel as a prior rather than a peer). The plan keeps the math unchanged and addresses the concern in tooltip copy ("RowingLevel is a demographics-based prior, so for atypical athletes it can pull the average toward the population mean"). Implicit Bayesian shrinkage (weight RL by `1/(1+n_pbs/k)`) is a possible Tier-2 polish.

### `SIGNAL_AMPLIFIER` refit

`SIGNAL_AMPLIFIER = 3` in [services/erg_stress.py:124](services/erg_stress.py) controls how aggressively the L³ norm picks the dominant band. Could be `L²` or `L⁴` with different physiological consequences. Defer until there is enough comparative data to choose between exponents.
