"""
Workouts-page filter pipeline.

``apply_workout_filters`` takes a list of metric-enriched workouts plus the
current filter-state values and returns the filtered subset.

When ``filter_at_session_level=False`` (default), each filter is evaluated
per-workout and the passing workouts are returned independently.

When ``filter_at_session_level=True``, workouts are grouped by ``session_id``
and each filter is evaluated against the session as a whole.  A session
either fully passes (all member workouts are kept) or fully fails (all are
dropped).  This is the right mode for pages that aggregate workouts into a
session-level tree table — without it, a filter like "Endurance power bin"
clears the hard main pieces of a hard session and leaves only the warm-up
and cool-down rows aggregated under that session.

Per-filter session-level semantics:
    10k+         total of (distance + rest_distance) across the session ≥ 10000 m
    Intervals    session contains any interval workout
    Continuous   session contains no interval workout
    power bin    any member workout has ≥10% time in the selected bin
    hr bin       any member workout has meaningful meters in the selected bin
    severity     session-level ``_ess_session_summary.severity_bucket`` matches
    stimulus     any member workout has dose ≥1.0 in the selected band
                 (matches the max-per-band aggregation used by session_rollup)
"""

from __future__ import annotations

from services.heartrate_utils import hr_bin_passes
from services.volume_bins import power_bin_passes


def apply_workout_filters(
    workouts: list[dict],
    *,
    filter_10k: bool,
    filter_ivl: str,
    active_power_bins: tuple,
    active_hr_bins: tuple,
    active_severity: tuple,
    active_stimulus_bands: tuple,
    filter_at_session_level: bool = False,
) -> list[dict]:
    """Apply Workouts-page filters and return the surviving workouts."""
    if filter_at_session_level:
        return _filter_per_session(
            workouts,
            filter_10k=filter_10k,
            filter_ivl=filter_ivl,
            active_power_bins=active_power_bins,
            active_hr_bins=active_hr_bins,
            active_severity=active_severity,
            active_stimulus_bands=active_stimulus_bands,
        )
    return _filter_per_workout(
        workouts,
        filter_10k=filter_10k,
        filter_ivl=filter_ivl,
        active_power_bins=active_power_bins,
        active_hr_bins=active_hr_bins,
        active_severity=active_severity,
        active_stimulus_bands=active_stimulus_bands,
    )


# ---------------------------------------------------------------------------
# Per-workout pipeline
# ---------------------------------------------------------------------------


def _filter_per_workout(
    workouts: list[dict],
    *,
    filter_10k: bool,
    filter_ivl: str,
    active_power_bins: tuple,
    active_hr_bins: tuple,
    active_severity: tuple,
    active_stimulus_bands: tuple,
) -> list[dict]:
    out = workouts
    if filter_10k:
        out = [w for w in out if _workout_distance_m(w) >= 10_000]
    if filter_ivl == "Intervals":
        out = [w for w in out if w.get("is_interval")]
    elif filter_ivl == "Continuous":
        out = [w for w in out if not w.get("is_interval")]
    if active_power_bins:
        sel = set(active_power_bins)
        out = [w for w in out if _workout_passes_power(w, sel)]
    if active_hr_bins:
        sel = set(active_hr_bins)
        out = [w for w in out if _workout_passes_hr(w, sel)]
    if active_severity:
        sel = set(active_severity)
        out = [w for w in out if w.get("_severity") in sel]
    if active_stimulus_bands:
        sel = set(active_stimulus_bands)
        out = [w for w in out if _workout_passes_stimulus(w, sel)]
    return out


# ---------------------------------------------------------------------------
# Per-session pipeline
# ---------------------------------------------------------------------------


def _filter_per_session(
    workouts: list[dict],
    *,
    filter_10k: bool,
    filter_ivl: str,
    active_power_bins: tuple,
    active_hr_bins: tuple,
    active_severity: tuple,
    active_stimulus_bands: tuple,
) -> list[dict]:
    by_session = _group_by_session(workouts)

    out: list[dict] = []
    for group in by_session.values():
        if filter_10k and sum(_workout_distance_m(w) for w in group) < 10_000:
            continue
        if filter_ivl == "Intervals" and not any(w.get("is_interval") for w in group):
            continue
        if filter_ivl == "Continuous" and any(w.get("is_interval") for w in group):
            continue
        if active_power_bins:
            sel = set(active_power_bins)
            if not any(_workout_passes_power(w, sel) for w in group):
                continue
        if active_hr_bins:
            sel = set(active_hr_bins)
            if not any(_workout_passes_hr(w, sel) for w in group):
                continue
        if active_severity:
            sel = set(active_severity)
            if _session_severity_bucket(group) not in sel:
                continue
        if active_stimulus_bands:
            sel = set(active_stimulus_bands)
            if not any(_workout_passes_stimulus(w, sel) for w in group):
                continue
        out.extend(group)
    return out


def _group_by_session(workouts: list[dict]) -> dict:
    """Group workouts by ``session_id``; missing ids become singleton groups."""
    by_session: dict = {}
    for w in workouts:
        sid = w.get("session_id") or f"__nosess__{w.get('id')}"
        by_session.setdefault(sid, []).append(w)
    return by_session


def _session_severity_bucket(group: list[dict]) -> str | None:
    """Session-level severity bucket — taken from the first member with a
    ``_ess_session_summary``.  All members of the same session share this
    value, so it doesn't matter which one we pick."""
    for w in group:
        summary = w.get("_ess_session_summary")
        if summary:
            return summary.get("severity_bucket")
    return None


# ---------------------------------------------------------------------------
# Per-workout predicates (shared by both modes)
# ---------------------------------------------------------------------------


def _workout_distance_m(w: dict) -> int:
    return int(w.get("distance") or 0) + int(w.get("rest_distance") or 0)


def _workout_passes_power(w: dict, sel: set) -> bool:
    bins = w.get("_zone_bin_fractions") or []
    return any(power_bin_passes(bins, i) for i in sel)


def _workout_passes_hr(w: dict, sel: set) -> bool:
    bins = w.get("_hr_bin_meters")
    return any(hr_bin_passes(bins, i) for i in sel)


def _workout_passes_stimulus(w: dict, sel: set) -> bool:
    doses = w.get("_stimulus_doses") or {}
    return any(float(doses.get(b, 0.0)) >= 1.0 for b in sel)
