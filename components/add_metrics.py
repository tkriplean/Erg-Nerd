"""
Stage-3 render-time metric attachment.

Single entry point — :func:`add_metrics`.  Reads ``sessions_dict``, the
active profile, and the machine-filtered full corpus from
:class:`AppContext` so callers just hand over the workout list they
care about.

Fields attached in-place to each workout dict:

  ESS family (session-aware)
    _ess, _ess_session, _if_eff, _if_eff_session, _severity,
    _severity_score, _anaerobic_strain, _w_bal_trough,
    _glycogen_used, _glycogen_kj, _glycogen_used_session,
    _glycogen_kj_session, _stimulus_doses, _stimulus_systems,
    _ess_segments, _ess_timeline, _ess_session_summary

  Zone Spread (workout-isolated)
    _zone_time_fractions, _zone_bin_fractions

  HR Spread (when ``max_hr`` resolves)
    _hr_bin_meters, _hr_spread_score

Zone-spread fractions come from the per-workout slice of the session
ESS pass (see ``_attach_per_workout_records`` in
:mod:`services.erg_stress`) — no second power-spread pass.

``with_timeline=True`` only when the caller will render the per-second
timeline (the Workout page Effort & Stress chart).  Default ``False``
saves the ~1.8 M dict allocations in :func:`_build_session_timeline` per
session compute.

Every metric routes through :mod:`services.workout_metrics_cache` so
repeated renders (and other pages requesting the same session) reuse
the result.
"""

from __future__ import annotations

from typing import Callable, Optional

from components.app_context import AppContext, get_profile
from components.concept2_sync import get_all_workouts
from services.critical_power_model import fit_critical_power
from services.erg_stress import (
    compute_session_metrics,
    compute_w_prime_estimate,
)
from services.glycogen import mass_kg_from_profile
from services.heartrate_utils import (
    hr_spread_score,
    resolve_max_hr,
    workout_hr_meters,
)
from services.reference_watts import _interp_watts_at_duration, input_hash
from services.threshold_cache import make_thresholds_resolver
from services.volume_bins import zone_fractions_to_bin_list
from services.workout_metrics_cache import get_or_compute


# Process-wide critical-power fit cache.  ``fit_critical_power`` (scipy
# ``curve_fit`` over a 4-parameter 2-component CP model) costs ~3.5 ms
# per call but fires once per distinct refs-content per render.  The
# refs dict is purely a function of the rower's PB index, so two renders
# with the same PBs produce the same fit — caching by content here
# survives across renders (the per-render ``cp_memo`` inside
# :func:`add_metrics` only collapses fits within one render).
_CP_FIT_CACHE: dict[tuple, tuple] = {}
_CP_FIT_CACHE_MAX = 4096


def _ref_watts_at_duration_fn(ref_watts_for: Callable):
    """Bind a date-aware ref-watts resolver into a ``(date, dur_s) → watts`` callable.

    Caches the per-date refs dict so :func:`_interp_watts_at_duration` is
    the only work for repeated duration lookups within one session
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


def _cp_w_prime_for_refs(
    refs: dict, gender: Optional[str], mass_kg: Optional[float] = None
) -> tuple:
    """Return ``(cp_watts, w_prime_joules, cp_params_or_None)`` from a refs dict.

    Builds a synthetic PB list from the date's ranked-event reference watts
    and runs :func:`fit_critical_power` over it; the resulting Pow1·tau1
    feeds :func:`compute_w_prime_estimate`.  Falls back to a mass-scaled
    population default for W' when the fit doesn't converge.

    CP itself is the rower's 60-min reference watts (``("time", 36000)``);
    falls back to ``None`` if the anchor is missing.
    """
    if not refs:
        return (None, compute_w_prime_estimate(None, gender, mass_kg), None)

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
    w_prime = compute_w_prime_estimate(cp_params, gender, mass_kg)
    return (cp, w_prime, cp_params)


def _assign_session_metrics(r: dict, sm: Optional[dict], wid) -> None:
    """Copy session-metric fields onto a workout dict.

    ``_zone_time_fractions`` / ``_zone_bin_fractions`` come from the
    per-workout slice's ``zone_time_fractions`` (built inside
    :func:`_attach_per_workout_records` in :mod:`services.erg_stress`)
    so we don't rebuild them with a separate power-spread pass.
    """
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
            "_zone_time_fractions",
            "_zone_bin_fractions",
            "_glycogen_used",
            "_glycogen_kj",
            "_glycogen_used_session",
            "_glycogen_kj_session",
            "_stimulus_doses",
            "_stimulus_systems",
        ):
            r[k] = None
        return

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
    r["_glycogen_used"] = pw.get("glycogen_used") if pw else None
    r["_glycogen_kj"] = pw.get("glycogen_kj") if pw else None
    r["_stimulus_doses"] = pw.get("stimulus_doses") if pw else None
    r["_stimulus_systems"] = pw.get("stimulus_systems") if pw else None
    if pw and pw.get("zone_time_fractions"):
        r["_zone_time_fractions"] = pw["zone_time_fractions"]
        r["_zone_bin_fractions"] = zone_fractions_to_bin_list(
            pw["zone_time_fractions"]
        )
    else:
        r["_zone_time_fractions"] = None
        r["_zone_bin_fractions"] = None

    # Session-level values for the Workout Page summary / rollup widget.
    r["_ess_session"] = sm.get("ess")
    r["_if_eff_session"] = sm.get("intensity_session")
    r["_w_bal_trough"] = sm.get("w_bal_trough")
    r["_glycogen_used_session"] = sm.get("glycogen_used")
    r["_glycogen_kj_session"] = sm.get("glycogen_kj")
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
        "glycogen_used": sm.get("glycogen_used"),
        "glycogen_kj": sm.get("glycogen_kj"),
        "w_bal_trough": sm.get("w_bal_trough"),
        "if_eff_session": sm.get("intensity_session"),
        "member_ids": [pw["workout_id"] for pw in (sm.get("per_workout") or [])],
        "per_workout": sm.get("per_workout"),
    }


def add_metrics(
    workouts: list,
    *,
    with_timeline: bool = False,
) -> None:
    """Attach Stage-3 render-time metrics in-place to each workout dict.

    Resolves :class:`AppContext` state internally:

      * ``ctx.sessions_dict`` — session membership (``{sid: session_record}``)
      * :func:`get_profile` — gender / weight for W' default + glycogen mass
      * :func:`get_all_workouts(apply_season_filters=False)` — full
        machine-filtered corpus, used to build the date-aware reference-
        watts resolver (the corpus *must* be unfiltered, otherwise the
        synthetic PB list fed to :func:`fit_critical_power` collapses
        and severity scores tank — this used to be a footgun on every
        caller).

    Returns early (no fields written) when ``ctx.sessions_dict`` hasn't
    been populated yet (e.g. mid-sync).

    Cache identity for the per-session compute is the **sorted member-id
    tuple** of the workout's session, so re-clustering that produces a
    fresh session uid for an unchanged member set still hits the same
    cached entry.  ``with_timeline`` is part of the cache key so both
    timeline and no-timeline variants can coexist for one session.
    """
    ctx = AppContext()
    if ctx.sessions_dict is None:
        return

    profile = get_profile()
    sync_result = get_all_workouts(apply_season_filters=False)
    if sync_result is None:
        return
    _, full_corpus = sync_result
    max_hr, _ = resolve_max_hr(profile, full_corpus)

    ref_watts_for = make_thresholds_resolver(full_corpus)
    rwd_fn = _ref_watts_at_duration_fn(ref_watts_for)

    h = input_hash(full_corpus)
    gender = (profile or {}).get("gender")
    mass_kg = mass_kg_from_profile(profile)
    sessions_dict = ctx.sessions_dict
    by_id = {str(w.get("id")): w for w in workouts if w.get("id") is not None}

    # Per-render memos.  Two separate caches:
    #
    # * ``session_memo`` — keyed by the session's sorted-id tuple; avoids
    #   redundant cache lookups when several session-mate workouts ask
    #   for the same SessionMetrics inside one render.
    #
    # * ``cp_memo`` — keyed by the rower's reference-watts contents.  The
    #   underlying ``fit_critical_power`` call (scipy curve_fit) is the
    #   second-largest hot spot in compute-uncached ESS rendering.  Date-
    #   aware reference watts change only when a new PB lands, so
    #   consecutive same-date workouts resolve to the *same refs dict
    #   content* — caching by content here collapses N curve-fits to one
    #   per distinct refs profile per render.
    session_memo: dict = {}
    cp_memo: dict = {}
    gender_key = (gender or "").lower()
    # Round mass to 0.5 kg for the cache key — the W' default is linear in
    # mass, so half-kg quantisation keeps within-noise.  None stays None.
    mass_key = round(mass_kg * 2.0) / 2.0 if mass_kg else None

    def _cached_cp_w_prime(refs: Optional[dict]) -> tuple:
        if not refs:
            return _cp_w_prime_for_refs(refs, gender, mass_kg)
        # Round watts to nearest 5W for the cache key (the fit itself still
        # runs on unrounded watts).  Empirically this collapses ~46% of
        # distinct keys with <1% mean shift in CP/W' — well below physiological
        # noise.
        content_key = tuple(
            sorted((d, int(round(w / 5.0) * 5)) for d, w in refs.items())
        )
        cached = cp_memo.get(content_key)
        if cached is not None:
            return cached
        global_key = (content_key, gender_key, mass_key)
        cached = _CP_FIT_CACHE.get(global_key)
        if cached is None:
            cached = _cp_w_prime_for_refs(refs, gender, mass_kg)
            if len(_CP_FIT_CACHE) >= _CP_FIT_CACHE_MAX:
                _CP_FIT_CACHE.clear()
            _CP_FIT_CACHE[global_key] = cached
        cp_memo[content_key] = cached
        return cached

    for r in workouts:
        wid = r.get("id")

        # ── Session-aware ESS pass + zone fractions ────────────────────
        sid = r.get("session_id")
        session_rec = sessions_dict.get(sid) if sid else None
        if session_rec is None:
            raise Exception("can't use sessions_dict")

        session = [
            by_id[w_id_str]
            for w_id_str in session_rec.get("workout_ids") or ()
            if w_id_str in by_id
        ]
        if not session:
            session = [r]

        memo_key = tuple(
            sorted(w.get("id") for w in session if w.get("id") is not None)
        )
        sm = session_memo.get(memo_key)

        if sm is None:
            cache_extra = {
                "gender": gender_key,
                # ``mass_kg`` is rounded for cache stability — small profile
                # weight tweaks shouldn't bust the cache, but a real change
                # (kg vs lb, big weight loss, profile fix) should.
                "mass": str(int(round(mass_kg))) if mass_kg else "0",
                "model": "v7",  # v7: closest-RW confirmation gate on stimulus
                "tl": "1" if with_timeline else "0",
            }

            def _compute(_session=session, _r=r):
                ref_watts = ref_watts_for(_r)
                cp, w_prime, _cp_params = _cached_cp_w_prime(ref_watts)
                return compute_session_metrics(
                    _session,
                    rwd_fn,
                    cp=cp,
                    w_prime=w_prime,
                    mass_kg=mass_kg,
                    with_timeline=with_timeline,
                )

            sm = get_or_compute(
                "ess_session",
                memo_key,
                h,
                _compute,
                **cache_extra,
            )
            session_memo[memo_key] = sm

        _assign_session_metrics(r, sm, wid)

        # ── HR spread (independent of the session pass) ────────────────
        if max_hr:
            hrm = get_or_compute(
                "_hr_bin_meters",
                wid,
                h,
                lambda r=r: workout_hr_meters(r, max_hr),
                max_hr=max_hr,
            )
            r["_hr_bin_meters"] = hrm
            r["_hr_spread_score"] = get_or_compute(
                "hr_spread_score",
                wid,
                h,
                lambda hrm=hrm: hr_spread_score(hrm),
                max_hr=max_hr,
            )
        else:
            r["_hr_bin_meters"] = None
            r["_hr_spread_score"] = None
