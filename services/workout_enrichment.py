"""
Shared workout enrichment — two stages, one home.

**Stage 2 (fetch-time)** — ``enrich_for_storage`` / ``enrich_all`` attach
cheap derived fields to every workout dict before it lands in
``AppContext.workouts_dict``.  These fields are first-class top-level keys
so call sites can read them directly instead of re-deriving on every render.

Universal fields (every workout):

  pace          float            sec/500m (never None — Stage-1 quarantine
                                  drops workouts with no derivable pace)
  watts         float            Concept2 wattage from pace
  date          str              the date and time of this workout 
  date_dt       datetime.date    parsed once
  date_ms       int              epoch ms (Chart.js x-axis)
  day           str              the day part of the date
  season        str              "YYYY-YY" (May→Apr)
  cat_key       tuple | None     ("dist", m) / ("time", tenths) when ranked
  machine       str              "rower" | "skierg" | "bikeerg"
  is_interval   bool             workout_type ∈ INTERVAL_WORKOUT_TYPES

Interval-only fields (when ``is_interval``):

  reps           int             rep count
  structure_key  str             rep-stripped structure label
  work_pace      float | None    avg work pace (tenths/500m)
  work_spm       float | None    work-weighted avg stroke rate

**Stage 3 (render-time)** — ``attach_spread_and_quality`` / ``attach_quality_only``
mutate each workout dict in-place to attach the heavier metrics:

  _bin_meters         list[float]    Per-power-bin meter counts (idx 0 = Rest)
  _bar_uri            str            Data-URI SVG stacked power-zone bar
  _power_spread_score float | None   0–100 weighted power spread
  _hr_bin_meters      list | None    Per-HR-bin meter counts when max_hr known
  _hr_bar_uri         str | None     Data-URI SVG stacked HR-zone bar
  _hr_spread_score    float | None   0–100 weighted HR spread
  _quality            str | None     "Low"/"Medium"/"High"/"Ultra"
  _quality_score      float | None   Continuous quality score
  _quality_energy     dict | None    Per-category energy breakdown

Every Stage-3 metric is routed through ``services.workout_metrics_cache``
so repeated renders (and other pages requesting the same workout) reuse
the result instead of recomputing.  HR-dependent metrics include
``max_hr`` in the cache key so a profile change naturally invalidates
only the affected entries.

``attach_quality_only`` is a lightweight variant that attaches only the
``_quality*`` fields — used by the Volume page's quality mode where the
spread/HR fields would be discarded.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Optional

from services.heartrate_utils import hr_spread_score, workout_hr_meters
from services.interval_utils import (
    avg_work_spm,
    avg_workpace_tenths,
    get_rep_count,
    interval_structure_key,
)
from services.reference_watts import input_hash
from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    compute_pace,
    compute_watts,
    parse_date,
    season_from_date,
    workout_cat_key,
)
from services.threshold_cache import make_thresholds_resolver
from services.volume_bins import (
    bin_bar_svg,
    power_spread_score,
    workout_bin_meters,
)
from services.workout_metrics_cache import get_or_compute
from services.workout_quality import compute_workout_quality


# ---------------------------------------------------------------------------
# Stage 2 — Fetch-time enrichment
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _date_to_ms(dt: date) -> int:
    """Epoch milliseconds at midnight UTC for ``dt``.

    Chart.js axes consume integer ms; centralising the conversion here
    means callers never need their own date-parsing helpers.
    """
    if dt is None or dt == date.min:
        return 0
    return int(
        (
            datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) - _EPOCH
        ).total_seconds()
        * 1000
    )


def enrich_for_storage(w: dict) -> dict:
    """Attach Stage-2 derived fields to a workout dict (in-place) and return it.

    Idempotent: calling twice has the same effect as calling once.

    Caller must ensure ``w['pace']`` is not None — this is enforced at
    Stage 1 by the data-integrity quarantine pass.  If pace is None here we
    still populate the field with None so the function never crashes, but
    this should not happen in normal operation.
    """

    if "date" not in w:
        raise ValueError("date not in w", w)

    pace = compute_pace(w)
    w["pace"] = pace
    w["watts"] = compute_watts(pace) if pace else None

    dt = parse_date(w.get("date") or "")
    w["date_dt"] = dt
    w["date_ms"] = _date_to_ms(dt)
    w["day"] = w["date"][:10]
    w["season"] = season_from_date(dt)

    w["machine"] = w.get("type") or "rower"
    w["cat_key"] = workout_cat_key(w)
    w["is_interval"] = w.get("workout_type") in INTERVAL_WORKOUT_TYPES

    if w["is_interval"]:
        w["reps"] = get_rep_count(w)
        w["structure_key"] = interval_structure_key(w, compact=True)
        w["work_pace"] = avg_workpace_tenths(w)
        w["work_spm"] = avg_work_spm(w)

    return w


def enrich_all(workouts_dict: dict) -> dict:
    """Apply ``enrich_for_storage`` to every value in ``workouts_dict``.

    Returns the same dict (mutated in-place) for convenient chaining.
    """
    for w in workouts_dict.values():
        enrich_for_storage(w)
    return workouts_dict


# ---------------------------------------------------------------------------
# Stage 3 — Render-time enrichment (spread + quality)
# ---------------------------------------------------------------------------


def _resolvers(
    all_workouts: list,
    thresholds_for: Optional[Callable],
    ref_watts_for: Optional[Callable],
    reference_pbs_for: Optional[Callable],
) -> tuple[Callable, Callable, Callable]:
    if thresholds_for is None or ref_watts_for is None or reference_pbs_for is None:
        return make_thresholds_resolver(all_workouts)
    return thresholds_for, ref_watts_for, reference_pbs_for


def _cached_quality(
    r: dict,
    h: str,
    ref_watts_for: Callable,
    thresholds_for: Callable,
    reference_pbs_for: Callable,
) -> Optional[dict]:
    """Quality dict for ``r`` from the central metrics cache."""
    return get_or_compute(
        "quality",
        r["id"],
        h,
        lambda: compute_workout_quality(
            r,
            ref_watts_for(r),
            thresholds_for(r),
            reference_pbs_for(r),
        ),
    )


def _assign_quality(r: dict, quality: Optional[dict]) -> None:
    if quality is not None:
        r["_quality"] = quality["category"]
        r["_quality_score"] = quality["score"]
        r["_quality_energy"] = quality["per_category_energy"]
    else:
        r["_quality"] = None
        r["_quality_score"] = None
        r["_quality_energy"] = None


def _hr_bar(hrm: list) -> str:
    """Render the HR bar using classified meters only (bins 0–5); drop
    bin 6 so 'no HR' doesn't dilute the colour signal.  ``bin_bar_svg``
    takes a 7-element list and skips index 0 internally, so we pad with a
    zero in the 'No HR' slot."""
    hr_for_bar = list(hrm)
    hr_for_bar[6] = 0
    return bin_bar_svg(hr_for_bar)


def attach_spread_and_quality(
    workouts: list,
    all_workouts: list,
    max_hr: Optional[int],
    *,
    thresholds_for: Optional[Callable] = None,
    ref_watts_for: Optional[Callable] = None,
    reference_pbs_for: Optional[Callable] = None,
) -> None:
    """Attach spread and quality fields to each workout dict in ``workouts``.

    ``all_workouts`` is the full corpus used to resolve reference watts at
    each workout's date.  ``max_hr``, if None, leaves all HR fields as None.

    ``thresholds_for``, ``ref_watts_for``, and ``reference_pbs_for`` may be
    supplied by callers that already built a per-date resolver (e.g. the
    Intervals page builds one for its own enrichment loop and reuses it
    here).  When omitted, this builds a fresh resolver internally.

    Every metric is memoised in ``services.workout_metrics_cache`` keyed by
    ``(metric, workout_id, input_hash[, max_hr])`` so subsequent renders
    are O(1) per workout.
    """
    thresholds_for, ref_watts_for, reference_pbs_for = _resolvers(
        all_workouts, thresholds_for, ref_watts_for, reference_pbs_for
    )

    h = input_hash(all_workouts)

    for r in workouts:
        wid = r["id"]

        bm = get_or_compute(
            "_bin_meters", wid, h, lambda r=r: workout_bin_meters(r, thresholds_for(r))
        )
        r["_bin_meters"] = bm
        r["_bar_uri"] = get_or_compute("bar_uri", wid, h, lambda bm=bm: bin_bar_svg(bm))
        r["_power_spread_score"] = get_or_compute(
            "power_spread_score", wid, h, lambda bm=bm: power_spread_score(bm)
        )

        if max_hr:
            hrm = get_or_compute(
                "_hr_bin_meters",
                wid,
                h,
                lambda r=r: workout_hr_meters(r, max_hr),
                max_hr=max_hr,
            )
            r["_hr_bin_meters"] = hrm
            r["_hr_bar_uri"] = get_or_compute(
                "hr_bar_uri",
                wid,
                h,
                lambda hrm=hrm: _hr_bar(hrm),
                max_hr=max_hr,
            )
            r["_hr_spread_score"] = get_or_compute(
                "hr_spread_score",
                wid,
                h,
                lambda hrm=hrm: hr_spread_score(hrm),
                max_hr=max_hr,
            )
        else:
            r["_hr_bin_meters"] = None
            r["_hr_bar_uri"] = None
            r["_hr_spread_score"] = None

        _assign_quality(
            r, _cached_quality(r, h, ref_watts_for, thresholds_for, reference_pbs_for)
        )


def attach_quality_only(
    workouts: list,
    all_workouts: list,
    *,
    thresholds_for: Optional[Callable] = None,
    ref_watts_for: Optional[Callable] = None,
    reference_pbs_for: Optional[Callable] = None,
) -> None:
    """Attach only ``_quality``/``_quality_score``/``_quality_energy``.

    Skips the bin-meters, power-spread, and HR-spread fields that
    ``attach_spread_and_quality`` computes.  Used by the Volume page's
    quality mode where the only field consumed downstream is ``_quality``.
    """
    thresholds_for, ref_watts_for, reference_pbs_for = _resolvers(
        all_workouts, thresholds_for, ref_watts_for, reference_pbs_for
    )

    h = input_hash(all_workouts)

    for r in workouts:
        _assign_quality(
            r, _cached_quality(r, h, ref_watts_for, thresholds_for, reference_pbs_for)
        )
