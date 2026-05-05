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

``attach_ess_metrics`` is an additional Stage-3 attachment that fills the
Erg Stress Score family of fields (workout-level, session-aware).  The v2
model is a multi-band PDC-saturation intensity (see
:mod:`services.erg_stress`); the attribute names below are kept for
backwards compatibility with the column registry, but the underlying
values now derive from ``I(t)`` rather than the v1 single-anchor IF_eff:

  _ess                float | None   This workout's session-aware ESS slice
  _ess_session        float | None   Total session ESS this workout belongs to
  _if_eff             float | None   Workout-level intensity (mean of I(t)
                                     over work-seconds; column header reads
                                     "Intensity")
  _if_eff_session     float | None   Session-level intensity
  _severity           str  | None    "Low"/"Moderate"/"High"/"Maximal"
  _severity_score     float | None   Continuous severity score
  _anaerobic_strain   float | None   Skiba W'bal trough fraction, 0..1
  _w_bal_trough       float | None   Joules at the W'bal trough (for tooltip)
  _ess_segments       list | None    Per-segment dicts for the splits/intervals table
  _ess_timeline       list | None    Sub-sampled per-second I(t) / W'bal /
                                     per-zone-ratio trace (for the chart)
  _ess_session_summary dict | None   {ess, severity_bucket, duration_s,
                                      anaerobic_strain, member_ids} for rollup

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
    interval_structure_label,
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
from services.critical_power_model import fit_critical_power
from services.erg_stress import (
    compute_session_metrics,
    compute_w_prime_estimate,
)
from services.reference_watts import _interp_watts_at_duration


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
        w["intervals_label"] = interval_structure_label(w, compact=True)
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
    zero in the 'No HR' slot.  Pass ``HR_ZONE_COLORS`` so the bar uses the
    HR palette rather than the default power-zone palette — without this the
    cell bar's colours disagreed with the legend swatches and the tooltip."""
    from services.heartrate_utils import HR_ZONE_COLORS

    hr_for_bar = list(hrm)
    hr_for_bar[6] = 0
    return bin_bar_svg(hr_for_bar, colors=list(HR_ZONE_COLORS))


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


# ---------------------------------------------------------------------------
# Stage 3 — Erg Stress Score
# ---------------------------------------------------------------------------


def _ref_watts_at_duration_fn(ref_watts_for: Callable):
    """Bind a date-aware ref-watts resolver into a ``(date, dur_s) → watts`` callable.

    Caches the per-date refs dict so the inner ``_interp_watts_at_duration``
    is the only work for repeated duration lookups within one session
    integration.
    """
    cache: dict = {}

    def fn(when, duration_s):
        if when is None:
            return None
        refs = cache.get(when)
        if refs is None:
            refs = ref_watts_for({"date_dt": when, "day": when.isoformat()})
            cache[when] = refs
        if not refs:
            return None
        return _interp_watts_at_duration(refs, duration_s)

    return fn


def _cp_w_prime_for_refs(refs: dict, gender: Optional[str]) -> tuple:
    """Return ``(cp_watts, w_prime_joules, cp_params_or_None)`` from a refs dict.

    Builds a synthetic PB list from the date's ranked-event reference watts
    and runs :func:`fit_critical_power` over it; the resulting Pow1·tau1
    feeds :func:`compute_w_prime_estimate`.  Falls back to a population
    default for W' when the fit doesn't converge.

    CP itself is the rower's 60-min reference watts (``("time", 36000)``);
    falls back to ``None`` if the anchor is missing.
    """
    if not refs:
        return (None, compute_w_prime_estimate(None, gender), None)

    pb_list = []
    for ck, watts in refs.items():
        if watts is None or watts <= 0:
            continue
        if ck[0] == "time":
            d = ck[1] / 10.0
        else:
            pace = 500.0 * (2.80 / watts) ** (1.0 / 3.0)
            d = pace * ck[1] / 500.0
        pb_list.append({"duration_s": d, "watts": watts})

    cp_params = fit_critical_power(pb_list)
    cp = refs.get(("time", 36000))
    w_prime = compute_w_prime_estimate(cp_params, gender)
    return (cp, w_prime, cp_params)


def _session_cache_key(
    session_workouts: list,
    h: str,
    max_hr,
    gender: Optional[str],
) -> tuple:
    """Stable cache key for a session's metrics."""
    ids = tuple(
        sorted(w.get("id") for w in session_workouts if w.get("id") is not None)
    )
    return ("ess_session", ids, h, max_hr or 0, (gender or "").lower())


def _assign_ess(r: dict, sm: Optional[dict], wid) -> None:
    """Copy session-metric fields onto a workout dict."""
    if sm is None:
        for k in (
            "_ess",
            "_ess_session",
            "_if_eff",
            "_if_eff_session",
            "_severity",
            "_severity_score",
            "_anaerobic_strain",
            "_w_bal_trough",
            "_ess_segments",
            "_ess_timeline",
            "_ess_session_summary",
        ):
            r[k] = None
        return

    # Find this workout's per-workout slice
    pw = None
    for rec in sm.get("per_workout") or []:
        if rec.get("workout_id") == wid:
            pw = rec
            break

    # Per-workout column values (use this workout's slice, not the session's).
    # Field names use the legacy ``_if_eff`` key for backwards compatibility
    # with the column registry; the value is the v2 I(t) intensity mean.
    r["_ess"] = pw["ess"] if pw else None
    r["_if_eff"] = pw["intensity_avg"] if pw else None
    r["_severity"] = pw["severity_bucket"] if pw else None
    r["_severity_score"] = pw["severity_score"] if pw else None
    r["_anaerobic_strain"] = pw["anaerobic_strain"] if pw else None
    # Session-level values for the Workout Page summary / rollup widget.
    r["_ess_session"] = sm.get("ess")
    r["_if_eff_session"] = sm.get("intensity_session")
    r["_w_bal_trough"] = sm.get("w_bal_trough")
    r["_ess_segments"] = [
        seg for seg in (sm.get("per_segment") or []) if seg.get("workout_id") == wid
    ]
    r["_ess_timeline"] = sm.get("timeline")
    r["_ess_session_summary"] = {
        "ess": sm.get("ess"),
        "severity_bucket": sm.get("severity_bucket"),
        "severity_score": sm.get("severity_score"),
        "duration_s": sm.get("duration_s"),
        "anaerobic_strain": sm.get("anaerobic_strain"),
        "w_bal_trough": sm.get("w_bal_trough"),
        "if_eff_session": sm.get("intensity_session"),
        "member_ids": [pw["workout_id"] for pw in (sm.get("per_workout") or [])],
        "per_workout": sm.get("per_workout"),
    }


def attach_ess_metrics(
    workouts: list,
    all_workouts: list,
    sessions_dict: dict,
    profile: Optional[dict] = None,
    max_hr: Optional[int] = None,
    *,
    thresholds_for: Optional[Callable] = None,
    ref_watts_for: Optional[Callable] = None,
    reference_pbs_for: Optional[Callable] = None,
) -> None:
    """Attach ESS / Severity / Anaerobic-Strain fields to each workout in-place.

    For each workout in ``workouts``, we resolve its session via
    ``workout["session_id"]`` against ``sessions_dict`` (populated by the
    sync pipeline; see ``services.sessions``), compute
    :func:`compute_session_metrics` once per session, and attribute the
    per-workout slice onto ``r``.  See the module docstring above for the
    complete field list.

    ``sessions_dict`` is the ``AppContext.sessions_dict`` snapshot —
    ``{session_id: session_record}``.  Pass ``{}`` if unavailable.

    ``profile`` is optional — only ``gender`` is used (for the W' default).
    ``max_hr`` is accepted for signature compatibility with v1 but is
    ignored — the v2 model is power-only (no HR-track), so workouts
    lacking power will have ``None`` ESS.

    Uses the central metrics cache keyed by ``("ess_session",
    sorted_ids_tuple, input_hash, gender, "v2")`` so repeated renders of
    the same session reuse the result.  Cache identity is the **sorted
    member-id tuple**, not ``session_id``, so re-clustering that produces
    a fresh uid for an unchanged member set hits the same cached entry.
    The ``"v2"`` literal is part of the key to invalidate v1 cached
    entries on first render after the multi-band rewrite.
    """
    thresholds_for, ref_watts_for, reference_pbs_for = _resolvers(
        all_workouts, thresholds_for, ref_watts_for, reference_pbs_for
    )

    h = input_hash(all_workouts)
    gender = (profile or {}).get("gender")
    rwd_fn = _ref_watts_at_duration_fn(ref_watts_for)
    sessions_dict = sessions_dict or {}
    by_id = {str(w.get("id")): w for w in all_workouts if w.get("id") is not None}

    # Per-render memos.  Two separate caches:
    #
    # * ``session_memo`` — keyed by the session's sorted-id tuple; avoids
    #   redundant cache lookups when several session-mate workouts ask for
    #   the same SessionMetrics inside one render.
    #
    # * ``cp_memo`` — keyed by the rower's reference-watts contents.  The
    #   underlying ``fit_critical_power`` call (scipy curve_fit) is the
    #   second-largest hot spot in compute-uncached ESS rendering (~3.5 s
    #   per call in the May-2026 profile).  Date-aware reference watts
    #   change only when a new PB lands, so consecutive same-date workouts
    #   resolve to the *same refs dict content* — caching by content here
    #   collapses N curve-fits to one per distinct refs profile per render.
    session_memo: dict = {}
    cp_memo: dict = {}

    def _cached_cp_w_prime(refs: Optional[dict]) -> tuple:
        """Memoised wrapper around :func:`_cp_w_prime_for_refs`."""
        if not refs:
            return _cp_w_prime_for_refs(refs, gender)
        # Sorted-items tuple is hashable and identical when refs content matches.
        key = tuple(sorted([(d, round(w)) for d, w in refs.items()]))
        cached = cp_memo.get(key)
        if cached is None:
            cached = _cp_w_prime_for_refs(refs, gender)
            cp_memo[key] = cached
        return cached

    for r in workouts:
        wid = r.get("id")
        # Resolve this workout's session via session_id; fall back to the
        # legacy O(N) scan only when the persisted id is missing.
        sid = r.get("session_id")
        session_rec = sessions_dict.get(sid) if sid else None
        if session_rec is not None:
            session = [
                by_id[w_id_str]
                for w_id_str in session_rec.get("workout_ids") or ()
                if w_id_str in by_id
            ]
            if not session:
                session = [r]
        else:
            raise Exception("can't use sessions_dict")

        memo_key = tuple(
            sorted(w.get("id") for w in session if w.get("id") is not None)
        )
        sm = session_memo.get(memo_key)

        if sm is None:
            cache_key_workout_id = memo_key  # tuple of ids — hashable
            cache_extra = {"gender": (gender or "").lower(), "model": "v2"}

            def _compute(_session=session, _r=r):
                ref_watts = ref_watts_for(_r)
                cp, w_prime, _cp_params = _cached_cp_w_prime(ref_watts)
                return compute_session_metrics(
                    _session,
                    rwd_fn,
                    cp=cp,
                    w_prime=w_prime,
                )

            sm = get_or_compute(
                "ess_session",
                cache_key_workout_id,
                h,
                _compute,
                **cache_extra,
            )
            session_memo[memo_key] = sm

        _assign_ess(r, sm, wid)
