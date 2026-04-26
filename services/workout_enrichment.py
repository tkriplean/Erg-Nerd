"""
Shared workout enrichment — attach Power Spread, HR Spread, and Quality fields.

Used by every page that needs spread/quality columns or filters: Intervals,
Sessions, Workout.  Each consumer adds its own page-specific fields on top
(intervals adds structure/grid placement; sessions adds chart point data).

Mutates each workout dict in-place to attach:

  _bin_meters         list[float]    Per-power-bin meter counts (idx 0 = Rest)
  _bar_uri            str            Data-URI SVG stacked power-zone bar
  _power_spread_score float | None   0–100 weighted power spread
  _hr_bin_meters      list | None    Per-HR-bin meter counts when max_hr known
  _hr_bar_uri         str | None     Data-URI SVG stacked HR-zone bar
  _hr_spread_score    float | None   0–100 weighted HR spread
  _quality            str | None     "Low"/"Medium"/"High"/"Ultra"
  _quality_score      float | None   Continuous quality score
  _quality_energy     dict | None    Per-category energy breakdown
"""

from __future__ import annotations

from typing import Callable, Optional

from services.heartrate_utils import hr_spread_score, workout_hr_meters
from services.threshold_cache import make_thresholds_resolver
from services.volume_bins import (
    bin_bar_svg,
    power_spread_score,
    workout_bin_meters,
)
from services.workout_quality import compute_workout_quality


def attach_spread_and_quality(
    workouts: list,
    all_workouts: list,
    max_hr: Optional[int],
    *,
    thresholds_for: Optional[Callable] = None,
    ref_watts_for: Optional[Callable] = None,
) -> None:
    """Attach spread and quality fields to each workout dict in ``workouts``.

    ``all_workouts`` is the full corpus used to resolve reference watts at
    each workout's date.  ``max_hr``, if None, leaves all HR fields as None.

    ``thresholds_for`` and ``ref_watts_for`` may be supplied by callers that
    already built a per-date resolver (e.g. the Intervals page builds one for
    its own enrichment loop and reuses it here).  When omitted, this builds
    a fresh resolver internally.
    """
    if thresholds_for is None or ref_watts_for is None:
        thresholds_for, ref_watts_for = make_thresholds_resolver(all_workouts)

    for r in workouts:
        bm = workout_bin_meters(r, thresholds_for(r))
        r["_bin_meters"] = bm
        r["_bar_uri"] = bin_bar_svg(bm)
        r["_power_spread_score"] = power_spread_score(bm)

        if max_hr:
            hrm = workout_hr_meters(r, max_hr)
            r["_hr_bin_meters"] = hrm
            # Render the HR bar using only classified meters (bins 0–5); drop
            # bin 6 so "no HR" doesn't dilute the colour signal.  bin_bar_svg
            # takes a 7-element list and skips index 0 internally, so pad
            # bins 1–5 with a 0 for the "No HR" slot.
            hr_for_bar = list(hrm)
            hr_for_bar[6] = 0
            r["_hr_bar_uri"] = bin_bar_svg(hr_for_bar)
            r["_hr_spread_score"] = hr_spread_score(hrm)
        else:
            r["_hr_bin_meters"] = None
            r["_hr_bar_uri"] = None
            r["_hr_spread_score"] = None

        quality = compute_workout_quality(r, ref_watts_for(r), thresholds_for(r))
        if quality is not None:
            r["_quality"] = quality["category"]
            r["_quality_score"] = quality["score"]
            r["_quality_energy"] = quality["per_category_energy"]
        else:
            r["_quality"] = None
            r["_quality_score"] = None
            r["_quality_energy"] = None
