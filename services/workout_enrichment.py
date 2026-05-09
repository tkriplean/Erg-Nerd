"""
Stage-2 workout enrichment — fetch-time derived fields.

``enrich_for_storage`` / ``enrich_all`` attach cheap derived fields to
every workout dict before it lands in ``AppContext.workouts_dict``.
These fields are first-class top-level keys so call sites can read them
directly instead of re-deriving on every render.

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

Stage-3 (render-time) attachment lives in
:mod:`components.add_metrics` — see :func:`add_metrics` for the ESS /
Zone Spread / HR Spread fields.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from services.interval_utils import (
    avg_work_spm,
    avg_workpace_tenths,
    build_interval_lines,
    get_rep_count,
    interval_structure_key,
    interval_structure_label,
)
from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    compute_pace,
    compute_watts,
    parse_date,
    season_from_date,
    workout_cat_key,
)


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
        label_lines = build_interval_lines(w, compact=True)
        w["intervals_full_label"] = tuple(label_lines)
        w["intervals_label"] = interval_structure_label(
            w, compact=True, lines=label_lines
        )
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
