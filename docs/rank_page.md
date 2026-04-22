# Rank Page

The Rank Page contextualises a user's ranked rowing performances against the
rest of the rowing world. It answers two questions side-by-side:

1. **How close am I to the world record at each event?**
   (ranking_focus = `world_record`)
2. **Where do I sit in the leaderboard field at each event?**
   (ranking_focus = `c2_age_matched` or `c2_age_group`)

## Overview

The page displays one chart and one data table. The chart is a scatter with a
**categorical x-axis** — one tick per ranked event, ordered left → right from
shortest to longest — and a numeric y-axis that shows either "% of WR" or
"percentile of the ranking pool". Each point is one of the user's ranked
performances.

Underneath the chart, a data table lists the same performances with columns
tailored to the active focus mode.

## UI layout

```
H1 heading (inline dropdowns):
    Your [Season Bests ▾] Against [C2 Age-Matched Rankings ▾]

Main chart:
    RankChart — scatter over categorical x, dashed reference line

Legend:
    Series swatches in rendered order

Data table (columns vary by ranking_focus):
    focus = c2_age_matched:
        event | date | age | result | pace | watts | rank (N of M) | %ile | distribution
    focus = c2_age_group:
        event | date | age | age group | result | pace | watts | rank | %ile | distribution
    focus = world_record:
        event | date | age | result | pace | watts | % WR pace | % WR watts | WR pace
```

## State

| Name            | Type  | Default            | Purpose |
|---              |---    |---                 |---      |
| `include_filter`| str   | `"SBs"`            | `"SBs"` (Season Bests) or `"PBs"` (Personal Bests). |
| `ranking_focus` | str   | `"c2_age_matched"` | `"world_record"` / `"c2_age_matched"` / `"c2_age_group"`. |
| `k_age_match`   | int   | `0`                | Age-match tolerance for `c2_age_matched`. Not UI-exposed yet — reserved for experimentation. |

## Data flow

1. `sync_from_context(ctx)` loads workouts (owner = live sync; public = snapshot).
2. `get_profile_from_context(ctx)` pulls the profile (dob, gender, weight).
3. `_qualifying_performances(...)` runs the standard quality-filter pipeline
   and reduces to PBs or SBs per `include_filter`.
4. For each qualifying performance:
   * `age_on_date(dob, workout_date)` — the user's age **at the time of the
     performance**.
   * For `world_record`: `get_records_for_age(gender, age, weight_kg)` →
     per-age WR lookup, reusing the cached `_raw` payload so the first
     performance triggers at most one network round-trip.
   * For `c2_age_matched`: `filter_matched_rankings(...)` with
     `target_age = age_at_perf` and `k = k_age_match`.
   * For `c2_age_group`: `age_group_matched_rankings(...)` bucketed by the
     C2-rankings age_band derived from age at performance.
   * `rank_in_pool(pool, value, kind)` — (rank, total, percentile).
   * `histogram_watts(pool, kind, value)` — bar-chart bin counts + min/max
     watts for the inline distribution SVG.

## Age-matched vs. age-group

* **Age-matched** uses the user's *exact* age (± `k_age_match`) across every
  season of the rankings history. It is **not** the live logbook view, which
  only shows the latest season; our pool is cross-season.
* **Age-group** uses the canonical C2 age_band (19-29, 30-39, 40-49, …) that
  the performance would have fallen into based on age-at-performance. This
  matches what C2 shows on the rankings web page for that bucket.

## Comparison pool assumption

The pool is always restricted to the user's **current** gender and weight
class. The user's *historical* weight is unknown at render time — we assume
weight class has not changed when computing age-group or age-matched pools
for past performances. This is stated here explicitly so future edits don't
silently change the assumption.

## Cache layout

* `.c2_rankings/` — scraper cache, one JSON per paged HTML response.
  Written by `services/concept2_rankings.py` (scraper) and authoritative.
* `.c2_rankings_index/` — derived, one JSON per `(event_kind, event_value)`,
  built on first query by `services.concept2_rankings_index.load_event_index`.
  Safe to delete; rebuilds transparently.

## Esoteric filters (future)

The plan includes a collapsible "pool modifiers" dropdown with:

* **Also appeared in events** — restrict the pool to names who also appear in
  a chosen set of other ranked events.
* **Exclude unverified** — keep only `verified == "Y"` entries.
* **Minimum ranked performances per person** — 1 / 5 / 10 / 20.

These are represented by `RankingModifiers` in
`services/concept2_rankings.py`. The first UI iteration ships without the
dropdown; the dataclass and filter logic are already in place.

## The `k_age_match` experimentation knob

`k_age_match = 0` uses exact age-match. Setting it to (say) 1 would include
the ±1 year neighbourhood, trading noise for a larger sample. Exposed only
as an internal state field for now — flip it in code to experiment before
deciding on a UI affordance.
