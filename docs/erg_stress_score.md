# Erg Stress Score (ESS)

A multi-band power-duration-saturation training-load metric for the rower.
Four numbers travel together:

| | What it measures | Reads on |
|---|---|---|
| **ESS** | Total accumulated training stress over a session | Workout & session totals |
| **Intensity** | The session's average multi-band PDC-saturation `I(t)` | Per-segment, per-workout, per-session |
| **Severity** | Recovery demand (how cautious to be the next day) | Bucket: Low / Moderate / High / Maximal |
| **W' Used** | Anaerobic capacity depletion at the deepest trough | Percentage (0–100 %) |

This document has two halves.  The first is for rowers using the app.  The
second is for the curious — full formulae, the design rationale, and the
trade-offs we accepted.

---

## For the rower

### What ESS is, in one paragraph

ESS rates the total training stress of a workout (or a back-to-back session
of warmup + work + cooldown) on a scale where **a steady hour at your 60-min
power = 100**.  Underneath, the app maintains a running sense of "how
saturated is each duration zone of your power-duration curve right now?" —
across six zones from a 20-second sprint up to a two-hour ultra-aerobic
effort.  At every second, those six saturation values are combined into a
single intensity number, `I(t)`, and ESS is the time-integral of that
intensity squared.  Long easy work and short hard work both register; they
just light up different zones.

### What numbers to expect

Rough expectations for a typical adult rower (these will calibrate to your
own PB profile — yours may sit higher or lower):

| Workout | ESS | Severity |
|---|---|---|
| 60' @ your 60-min power (FTP-style continuous) | ≈ 100 | High |
| Solid 90' Z2 base row | 50–70 | Moderate |
| 40' steady at 0.85 × FTP (sweet spot) | 35–50 | High |
| Half-marathon at FTP (~85 min) | 150–200 | Maximal |
| 5k race effort | 20–35 | Maximal |
| 2k race effort | 5–12 | Maximal |
| 8 × 500 m / 1:30 rest at race pace | 30–50 | Maximal |
| 20' easy warmup | 5–10 | Low / Moderate |
| 10' cooldown after a max effort | 2–6 | Low |

**Why a 5k race scores lower ESS than 60' at FTP:** ESS integrates
intensity-squared *over time*.  A 5k is short.  Its **Severity** bucket
captures the recovery demand instead — short, savage efforts read Maximal
even though their ESS is moderate.

### Reading each column

**ESS** — accumulated stress.  Sum over a session equals the session's ESS.
Adds linearly across workouts.  If you do a warmup, a race, and a cooldown
on the same day (within 30 minutes of each other), the three rows' ESS
values will sum to the session ESS exactly.

**Intensity** — what `I(t)` averaged out to over the workout's working
seconds.  Calibrated so that **a sustained at-zone effort approaches ≈ 1.0**
once the relevant duration band has filled.  Easy aerobic work sits
0.4–0.7; race-pace and PB efforts climb above 1.0 because more than one
zone is saturated simultaneously.

The per-workout reading is **workout-isolated** — each workout's bands are
reset to zero at its start and run forward only over its own seconds.
That's deliberate: it means a cooldown immediately after a maximal effort
reads Low (the cooldown's own watts can't fill the bands, regardless of
what came before), not "Maximal-by-inheritance."  The session-level
intensity, by contrast, *does* carry state across workouts in the session
— that's what shows up on the chart.

**Severity** — how cautious to be the next day.  Peaks the rolling 5-min /
60-s / 15-s windows of `I(t)` over the workout, plus a bonus for anaerobic
depletion (W' draining).  Bucketed:

| Bucket | Severity range | Typical interpretation |
|---|---|---|
| Low | < 0.70 | Recovery, easy aerobic, warmup, cooldown |
| Moderate | 0.70–1.00 | Solid base / Z2 / aerobic threshold |
| High | 1.00–1.40 | Sharp threshold, VO₂ work, hard intervals |
| Maximal | ≥ 1.40 | Race-pace or PB territory; high recovery demand |

Workouts and individual workouts each get their own Severity rating.  The
session-level rating reflects the hardest moment anywhere in the session,
plus its overall W'-strain; the per-workout rating is computed
workout-isolated.

**W' Used** — the deepest fraction of your anaerobic reserve drained at any
moment during the workout.  Tightly correlated with how "wiped" you feel.
A 100% reading means your modeled W'bal hit zero.

### Workouts ("All workouts on this day")

Workouts logged within 30 minutes of each other count as one session.  The
"All workouts on this day" panel on the Workout Page shows each member's
own row (with workout-isolated Severity / Intensity), plus a combined
session rollup at the top with the session's combined ESS, peak Severity,
total time, and W' Used trough.

The "Effort & Stress" chart on the Workout Page paints the *session-level*
`I(t)` line — so you can see the warmup priming the race, the race climbing
into Maximal territory, and the cooldown's signal slowly bleeding off.  The
six color-coded zone traces on the right axis show which energy systems
were doing the work at each moment.

### Three things to know

1. **ESS is duration-honest.**  A 90-minute Z2 row will out-ESS a 5k race
   even though the race is harder — because ESS integrates over time.  Use
   Severity to compare recovery demand directly.

2. **Per-workout Severity is workout-isolated.**  A cooldown after a max
   effort reads Low.  This was a deliberate fix — it makes the column
   reflect what the workout *itself* demanded, not what the body inherited
   from the prior piece.

3. **Numbers are date-aware.**  Both the reference watts that anchor each
   zone and your modeled power duration and W' come from your PB history at
   each workout's date.  As you set new PBs, historical ESS values for the
   workouts that broke them will shift slightly downward (because the new
   PBs raise the bar for "at-zone" effort).  This is intentional.

---

## Technical reference

### Core formula

For each workout's *session* (the maximal run of same-day same-machine
workouts with consecutive gaps under 30 min — Concept2 logs the workout's
*end* time, so we recover `start = end − duration`), the metric runs at
1-Hz over the session timeline:

```
EMA_d(t)        = causal exponentially-weighted MA of P(t) with τ_d
zone_ratio_d(t) = EMA_d(t) / RW_d                 RW_d = ref watts at d
I(t)            = INTENSITY_SCALE · [ Σ_d zone_ratio_d(t)^k ]^(1/k)
ESS             = ∫ I(t)² dt × C_ESS
```

* `d ∈ ZONE_BANDS_S = {20s, 90s, 5min, 20min, 60min, 2h}` — the six
  duration bands, matching the rower's existing six-zone power-duration
  model.
* `RW_d` is the rower's date-aware reference watts at duration `d`,
  obtained from `services.reference_watts.reference_watts_at_duration`.
  Using one date per session (the latest workout's) keeps the metric
  stable across the session.
* `INTENSITY_SCALE = 1.0` and `SIGNAL_AMPLIFIER (k) = 3` — the L₃ (cube)
  norm.  Higher `k` emphasises the dominant zone more; cube is a
  middle-ground that lets two adjacent saturated bands reinforce while
  keeping a single saturated band dominant.
* `C_ESS` is calibrated empirically (see below) so that 60' @ FTP yields
  ESS = 100 exactly.

### Why six bands instead of one anchor

The v1 metric picked a single duration anchor per segment and graded `P /
P_ref(d_anchor)`.  That broke whenever the anchor decision was wrong — most
visibly when a 5k JustRow logged with 500 m splits had each split graded
against the rower's 500-m reference watts instead of their 5k reference.

Multi-band saturation removes the anchor decision entirely.  Every zone
votes; the dominant zone determines the answer.  A 5k effort lights up the
20-min band (and the shorter bands at lower ratios); an FTP hour lights up
the 60-min band (and slightly above-1.0 in the 2-h band, since the rower
is briefly above their 2-h-sustainable power); a sprint lights up only the
20-s and 90-s bands.  No anchor → no wrong-anchor bug.

### EMA τ tuning

A vanilla EMA with `τ = d` ("characteristic-time" memory) only reaches
`1 − 1/e ≈ 63%` saturation by `t = d`, and after a rest of length `d`
still retains `1/e ≈ 37%` of its prior value.  In testing on real
workouts that under-filled bands during max efforts and over-retained
signal across rest periods.

We use **`τ_d = d × EMA_TAU_FACTORS[d]`** with factors below 1.0,
defaulting to a physiologically motivated set:

| Band | Factor | τ | Physiological rationale |
|---|---|---|---|
| 20 s | 0.30 | 6 s | Phosphocreatine recovery (~20–30 s) |
| 90 s | 0.30 | 27 s | Fast-glycolysis lactate kinetics |
| 5 min | 0.33 | 99 s | VO₂ on/off-kinetics (~2 min) |
| 20 min | 0.33 | 6.6 min | MLSS / threshold dynamics |
| 60 min | 0.40 | 24 min | Substrate / hydration / glycogen drift |
| 2 h | 0.40 | 48 min | Long-aerobic durability |

With `τ_d = d/3` a band saturates ~95 % by `t = d` and decays to ~5 %
after a rest of length `d` — much closer to the "rolling-window over the
last d seconds" intuition.  The dict is exposed at module scope; tune
per-band if a particular zone reads off in your own data.

### ESS calibration (`C_ESS`)

`C_ESS` is computed at module import by `_calibrate_c_ess()` — it
simulates a synthetic 60-minute workout at FTP with the canonical
multi-duration profile (RW(20s) = 5·FTP, RW(90s) = 2.5·FTP, RW(5min) =
1.4·FTP, RW(20min) = 1.05·FTP, RW(60min) = FTP, RW(2h) = 0.95·FTP),
integrates `I(t)²` over the hour, and sets `C_ESS = 100 / integral`.

Under the current settings (`SIGNAL_AMPLIFIER = 3`,
`INTENSITY_SCALE = 1`, the τ-factors above), `C_ESS ≈ 0.0207`.  This
recomputes automatically if any of the upstream knobs change.

### Severity

```
severity_score = max( peak₅ₘᵢₙ I(t),
                      0.90 · peak₆₀ₛ I(t),
                      0.75 · peak₁₅ₛ I(t) )
                 + 0.50 · anaerobic_strain
```

The peak rolling means find the most intense sustained moment of the
workout at three time scales.  The strain bonus (0.50 × W' Used fraction)
rescues short max-efforts from under-rating: a 2k PB peaks `I` at maybe
1.1 (because the 5-min and 20-min bands haven't time to fully saturate),
but fully drains W'bal — the +0.50 pushes its Severity past the 1.40
Maximal threshold.

Bucket cut-offs (`SEVERITY_THRESHOLDS`): Low < 0.70, Moderate 0.70–1.00,
High 1.00–1.40, Maximal ≥ 1.40.  These were tuned against a synthetic
calibration suite and will need real-data refinement.

### Workout-isolated per-workout columns

A subtle issue with session-level state: a cooldown immediately after a
maximal effort inherits the saturated bands and reads "Maximal" by
carry-over, even though the cooldown's own watts wouldn't drive that
intensity.  Same problem in reverse for a race-after-warmup: it might
read *too high* per-workout because the warmup pre-saturated some bands.

The fix: per-workout Severity / Intensity run a **second EMA simulation
per workout**, with bands reset to zero at the workout's start.  Output:

| Workout in session | Per-workout reading |
|---|---|
| Warmup (alone) | Same as a standalone warmup |
| 5k race after warmup | Same as a standalone 5k race |
| Cooldown after race | Low (cooldown's own watts barely fill any band) |

The session-level `I(t)` (used for the chart and for ESS attribution)
*does* preserve carry-over.  ESS attribution is unchanged: each workout's
ESS is its time-slice of the session-state `∫ I(t)² dt × C_ESS`, so
**Σ ESS_workout = ESS_session** still holds (within per-second rounding).

### W'bal model

Unchanged from v1.  Skiba (2012) two-component model:

```
dW'bal/dt = −(P − CP)                        if P > CP   (deplete)
dW'bal/dt = (W' − W'bal) / τ_W'              if P ≤ CP   (recover)
τ_W'      = clamp( 546 · exp(−0.01 · DCP) + 316,  200, 1200 )    [seconds]
DCP       = mean(P) over seconds where P < CP, else CP/2 fallback
```

* `CP` is the rower's date-aware 60-min reference watts.
* `W' = Pow1 · tau1` from `fit_power_duration` when a CP fit converges,
  else a population default (28 kJ men, 22 kJ women).

`anaerobic_strain` is `1 − min(W'bal) / W'` over the session.  Per
workout, the strain is `(W'bal at workout start − min(W'bal during
workout)) / W'`, so a cooldown that begins on a depleted reserve and
only recovers reads 0 % strain.

### Comparison to other models

* **Coggan & Allen — Normalized Power**: single 30-s rectangular MA, raised
  to the 4th power, then root-mean-fourth.  Captures one band; misses
  multi-zone work entirely.
* **Skiba — xPower** (2008): same shape with a 25-s EMA.  Already uses an
  EMA; still single-band.
* **Allen-Coggan-McGregor / Pinot & Grappe — Power Profile / MMP curve**:
  the Mean-Maximal Power curve as a *per-workout descriptor*, compared to
  a reference PDC.  Conceptually our `zone_ratio_d` over time is this
  curve's continuous-time analog.  We don't appear to have seen the
  continuous-time formulation in the literature.
* **Skiba — W'bal / dynamic CP** (2012, 2014): multi-time-scale state
  (the W'bal recovery τ), but mechanistic rather than PDC-saturation.
  Complementary; we use it directly as our anaerobic-strain channel.
* **Banister TRIMP fitness/fatigue** (1975), **Busso ATL/CTL** (2003):
  multi-time-scale, but at the *across-day* chronic scope (7-day vs
  42-day EMAs of TRIMP).  Different problem.

The continuous-time multi-band-EMA formulation here appears to be a
synthesis of NP/xPower's multi-time-scale rolling-MA + power-mean idea
with the Power Profile's multi-band PDC framing.

### Logging-shape invariance

Because the metric is a strict time integral of a continuous signal that
is a pure function of the session timeline, **the same physical effort
logged differently produces the same session ESS** (within
per-second rounding):

* A 5k logged as one workout vs. four 1.25k splits vs. as part of a
  warmup-race-cooldown session — same ESS for the 5k portion, same
  session ESS for the whole.
* An interval workout logged as one entry vs. as several separate
  Concept2 entries (with timestamps within 30 min) — same session ESS.

State (the EMAs and W'bal) is integrated across the *session timeline*,
not per workout.

### Knobs you can tune

| Constant | Default | What it does |
|---|---|---|
| `ZONE_BANDS_S` | (20, 90, 300, 1200, 3600, 7200) | The six duration bands |
| `EMA_TAU_FACTORS` | per-band 0.30–0.40 | EMA fill / decay speed per band |
| `SIGNAL_AMPLIFIER` | 3 | The L_k norm exponent.  Higher = more "single dominant zone" emphasis. |
| `INTENSITY_SCALE` | 1 | Calibrates `I` to land at ≈ 1.0 for sustained at-zone effort |
| `SEVERITY_THRESHOLDS` | (Low, Moderate, High, Maximal) cut-offs | Bucket bounds |
| `SESSION_GAP_S` | 1800 (30 min) | Maximum gap that still counts as one session |

`C_ESS` recalibrates automatically when any of these change (it's
computed at module import).

### Known limitations

* **Severity thresholds are first-cut.**  They were tuned against a
  synthetic calibration suite, not real-data exposure.  Expect minor
  refinement.
* **No HR fallback.**  If a workout has no power data, ESS is `None`.
  v1 had an HR-track that mirrored the recency multiplier on HRR; under
  the multi-band model there's no clean equivalent (HR doesn't have a
  meaningful multi-band reference profile), so the HR track was dropped.
* **Single representative date for `RW_d`.**  We use the last workout's
  date in the session.  A session that crosses midnight (rare for an
  erg) would resolve to "yesterday's PBs" for the whole thing.
* **The synthetic calibration profile.**  `C_ESS` is calibrated against
  a typical adult-endurance PDC ratio (RW(5min) = 1.4·FTP, etc.).
  Athletes whose PDC departs sharply from this shape (very anaerobic
  sprinters, very durable ultra-aerobic specialists) will see ESS values
  shifted slightly relative to "100 = FTP hour" — that's a feature
  (the calibration is rower-shape independent), but worth knowing.
* **EMA seeding.**  Bands seed at zero at session start.  A user who
  logs only the cooldown of a much longer effort (with no warmup or
  work piece logged) would see an under-counted long-band signal for
  that orphan workout.

### References

* Allen, H., & Coggan, A. R. (2010). *Training and Racing with a Power
  Meter* (2nd ed.). VeloPress.
* Banister, E. W., et al. (1975). A systems model of training for
  athletic performance. *Australian Journal of Sports Medicine*, 7(3),
  57–61.
* Busso, T. (2003). Variable dose-response relationship between exercise
  training and performance. *Medicine & Science in Sports & Exercise*,
  35(7), 1188–1195.
* Pinot, J., & Grappe, F. (2011). The record power profile to assess
  performance in elite cyclists. *International Journal of Sports
  Medicine*, 32(11), 839–844.
* Skiba, P. F. (2008). Calculation of power output and quantification of
  training stress in distance runners: the development of the GOVSS
  algorithm.  Online publication.
* Skiba, P. F., et al. (2012). Modeling the expenditure and reconstitution
  of work capacity above power duration. *Medicine & Science in Sports
  & Exercise*, 44(8), 1526–1532.
* Skiba, P. F., et al. (2014). Intramuscular determinants of the ability
  to recover work capacity above power duration. *European Journal of
  Applied Physiology*, 114(11), 2289–2298.
