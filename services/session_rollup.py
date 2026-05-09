"""
Session rollup — pure Python.

Builds parent rows for the :func:`components.workout_table.WorkoutTable`
tree-mode view by grouping workouts into sessions and computing per-session
aggregates.

A "session" is a maximal run of workouts on the same day/machine within
``SESSION_GAP_S`` of each other (see :mod:`services.sessions`).  Each
member workout already has its ``session_id`` set during sync, and
session-level metrics (ESS, severity, anaerobic_strain, intensity_session,
total duration) are already computed and attached to every member as
``r["_ess_session_summary"]`` (see
:func:`components.add_metrics.add_metrics`).  This module composes the
per-workout fields into a per-session parent row that the JS plugin
renders as the top of an expandable group.

Public API
----------
``build_session_rows(workouts, sessions_dict)`` → ``list[dict]``

Aggregates use only the workouts present in ``workouts`` (typically the
Workouts-page in-window subset).  Session-level severity / ESS / strain
are pulled from ``_ess_session_summary`` which reflects the *full*
persisted session (these aren't strictly additive across a subset).

Workout role classification
---------------------------
Each member of a multi-workout session is tagged ``"warmup"`` /
``"main"`` / ``"recovery"`` / ``"cooldown"`` using the per-workout
``_severity_score`` field:

* threshold = ``THRESHOLD_K`` × max(scores in session)
* ``is_main[i]`` = ``scores[i] >= threshold``
* ``warmup`` = before all mains
* ``cooldown`` = after all mains
* ``recovery`` = below threshold AND strictly between two mains
* singletons → ``"single"``
* all-zero scores (no power data) → every workout marked main, so the
  parent row aggregates over everything rather than nothing

Gap rows
--------
``_children`` interleaves synthetic ``{"_row_kind": "gap"}`` rows between
consecutive workouts so the expanded view shows how much wall-clock time
elapsed between pieces.  Gaps under ``_GAP_MIN_S`` (30 s) collapse — the
threshold filters out trivial pauses (settings menu, water sip) that
would only add visual noise.
"""

from __future__ import annotations

from typing import Optional

from services.erg_stress import _parse_workout_datetime, _workout_total_duration_s
from services.formatters import (
    fmt_distance,
    fmt_split,
    format_time,
    pace_tenths,
)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


#: A workout is "main" when its per-workout severity score reaches at least
#: this fraction of the session-max severity score.  0.6 keeps the bracket
#: tight enough that a clear warmup or cooldown lands below it while leaving
#: any genuine "interspersed easy piece between two efforts" labeled
#: ``recovery``.
THRESHOLD_K = 0.6


# ---------------------------------------------------------------------------
# Time-of-day
# ---------------------------------------------------------------------------


def _time_of_day(start_dt_str: str) -> str:
    """Colloquial label for a session start time.

    ``"Morning"`` 5–11, ``"Afternoon"`` 12–16, ``"Evening"`` 17–20,
    ``"Night"`` otherwise.  Falls back to ``""`` for unparseable input.
    """
    if not start_dt_str or len(start_dt_str) < 13:
        return ""
    try:
        h = int(start_dt_str[11:13])
    except ValueError:
        return ""
    if 5 <= h < 12:
        return "Morning"
    if 12 <= h < 17:
        return "Afternoon"
    if 17 <= h < 21:
        return "Evening"
    return "Night"


# ---------------------------------------------------------------------------
# Workout classification
# ---------------------------------------------------------------------------


def _start_dt(w: dict):
    return _parse_workout_datetime(w.get("date"))


def _classify_roles(session_workouts: list[dict]) -> list[tuple[str, dict]]:
    """Return ``[(role, workout), ...]`` ordered earliest-first.

    Roles: ``"single"`` (singleton sessions only), ``"warmup"``, ``"main"``,
    ``"recovery"``, ``"cooldown"``.
    """
    if len(session_workouts) == 1:
        return [("single", session_workouts[0])]

    # Sort earliest-first by start datetime.  Workouts that don't parse
    # land at the end in input order (rare).
    def _sort_key(w):
        dt = _start_dt(w)
        return (dt is None, dt)

    ws = sorted(session_workouts, key=_sort_key)

    scores = [(w.get("_severity_score") or 0.0) for w in ws]
    s_max = max(scores) if scores else 0.0

    if s_max <= 0:
        # No power data anywhere — refuse to invent a warmup/cooldown
        # split.  Treat everything as main so the parent row aggregates
        # over the full session.
        is_main = [True] * len(ws)
    else:
        threshold = THRESHOLD_K * s_max
        is_main = [s >= threshold for s in scores]

    main_idx = [i for i, m in enumerate(is_main) if m]
    first_main = main_idx[0]
    last_main = main_idx[-1]

    out: list[tuple[str, dict]] = []
    for i, w in enumerate(ws):
        if is_main[i]:
            role = "main"
        elif i < first_main:
            role = "warmup"
        elif i > last_main:
            role = "cooldown"
        else:
            role = "recovery"
        out.append((role, w))
    return out


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _work_seconds(w: dict) -> float:
    """Work-only seconds.  ``r["time"]`` is already work-only for intervals
    (``rest_time`` is tracked separately), so this works uniformly."""
    return (w.get("time") or 0) / 10.0


def _is_time_based(w: dict) -> bool:
    """Heuristic for non-interval main-work line formatting."""
    return "Time" in (w.get("workout_type") or "")


def _other_meters(w: dict, role: str) -> int:
    """Per-workout contribution to the session's ``Other Distance`` total.

    * Main interval: rest distance (work meters already counted in Work
      Distance).
    * Non-main steady: full distance (warmup / cooldown / recovery).
    * Non-main interval: full distance plus rest distance.
    * Main steady: 0 (its work distance lives in Work Distance).
    """
    is_interval = bool(w.get("is_interval"))
    dist = int(w.get("distance") or 0)
    rest = int(w.get("rest_distance") or 0)
    if role == "main":
        return rest if is_interval else 0
    return dist + rest if is_interval else dist


def _weighted_avg(values_and_weights: list[tuple[float, float]]) -> Optional[float]:
    """Weighted average; returns None if total weight is 0 or no values."""
    num = 0.0
    den = 0.0
    for v, w in values_and_weights:
        if v is None or w is None or w <= 0:
            continue
        num += v * w
        den += w
    if den <= 0:
        return None
    return num / den


def _effective_spm(w: dict) -> Optional[float]:
    """Prefer ``work_spm`` for intervals (already work-weighted, ignores
    rest); fall back to ``stroke_rate``.  None when neither is set."""
    if w.get("is_interval") and w.get("work_spm"):
        return float(w["work_spm"])
    sr = w.get("stroke_rate")
    return float(sr) if sr else None


# ---------------------------------------------------------------------------
# Main Work line generation
# ---------------------------------------------------------------------------


def _main_work_line(w: dict) -> str:
    """One line describing a main workout's work content.

    Interval → existing ``intervals_label``.  Time-based steady →
    ``"<duration> @ <pace>"``.  Distance-based steady → ``"<distance> @
    <pace>"``.
    """
    if w.get("is_interval"):
        return w.get("intervals_label") or ""
    pace_str = fmt_split(pace_tenths(w))
    if _is_time_based(w):
        head = format_time(w.get("time") or 0)
    else:
        head = fmt_distance(w.get("distance") or 0)
    return f"{head} @ {pace_str}"


def _build_main_work_lines(mains: list[dict]) -> list[str]:
    """Per spec: blank when exactly one main and that main is not interval."""
    if len(mains) == 1 and not mains[0].get("is_interval"):
        return []
    return [_main_work_line(m) for m in mains]


# ---------------------------------------------------------------------------
# Parent row builder
# ---------------------------------------------------------------------------


def _session_total_duration_s(start_dt_str: str, end_dt_str: str) -> float:
    """Wall-clock seconds spanned by a session record."""
    start = _parse_workout_datetime(start_dt_str)
    end = _parse_workout_datetime(end_dt_str)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _workout_start_end(w: dict):
    """Return (start_dt, end_dt) for a workout, or (None, None) when the
    end timestamp doesn't parse.  ``date`` is the workout end (Concept2
    convention); start = end - total_duration."""
    end_dt = _parse_workout_datetime(w.get("date"))
    if end_dt is None:
        return None, None
    duration_s = _workout_total_duration_s(w)
    return end_dt - _td_seconds(duration_s), end_dt


def _td_seconds(secs: float):
    from datetime import timedelta
    return timedelta(seconds=secs)


def _build_children_with_gaps(roles, sid: str) -> list[dict]:
    """Interleave gap rows between consecutive workouts.

    A gap row is a synthetic dict with ``_row_kind="gap"`` carrying the
    elapsed wall-clock seconds between the previous workout's end and
    the next workout's start.  Gaps under ``_GAP_MIN_S`` collapse — there's
    no point telling the user about a 12-second pause that's just the
    rower's settings menu.
    """
    out: list[dict] = []
    prev_end = None
    for i, (role, w) in enumerate(roles):
        start, end = _workout_start_end(w)
        if prev_end is not None and start is not None:
            gap_s = (start - prev_end).total_seconds()
            if gap_s >= _GAP_MIN_S:
                out.append({
                    "_row_kind": "gap",
                    "_session_id": sid,
                    "_gap_seconds": gap_s,
                    "id": f"__gap__{sid}__{i}",
                })
        out.append({
            **w, "_row_kind": "workout", "_session_id": sid, "_role": role,
        })
        prev_end = end
    return out


#: Gap rows under this threshold collapse — too short to be interesting
#: and just adds visual noise between back-to-back workouts.
_GAP_MIN_S = 30.0


def _parent_from_session(
    sid: str,
    session_rec: Optional[dict],
    workouts: list[dict],
) -> dict:
    """Build a synthetic parent row from a session's visible member workouts.

    ``session_rec`` may be None when the persisted record is missing — in
    that case we derive start/end from the visible members.
    """
    roles = _classify_roles(workouts)
    children = _build_children_with_gaps(roles, sid)
    mains = [w for role, w in roles if role in ("main", "single")]

    # Most-severe main — used for spread placeholders and View link target.
    most_severe = max(
        mains, key=lambda m: (m.get("_severity_score") or -1.0)
    ) if mains else (workouts[0] if workouts else {})

    # Stimulus aggregate — max dose per band across all mains.  A session
    # row should flag a system as stimulated if *any* of its workouts hit
    # full dose (e.g. a session of "warmup + VO2max set + cooldown" should
    # show VO2max stimulated, not be diluted by the easy bookend pieces).
    session_stim_doses: dict[int, float] = {}
    for m in mains:
        per_workout_doses = m.get("_stimulus_doses") or {}
        for band, dose in per_workout_doses.items():
            session_stim_doses[band] = max(
                session_stim_doses.get(band, 0.0), float(dose)
            )
    session_stim_systems = sorted(
        d for d, dose in session_stim_doses.items() if dose >= 1.0
    ) if session_stim_doses else []

    # Aggregates over mains only.
    work_durations = [(m, _work_seconds(m)) for m in mains]
    total_work_s = sum(s for _, s in work_durations)

    pace_avg = _weighted_avg(
        [(pace_tenths(m), s) for m, s in work_durations]
    )
    watts_avg = _weighted_avg(
        [(m.get("watts"), s) for m, s in work_durations]
    )
    drag_avg = _weighted_avg(
        [(m.get("drag_factor"), s) for m, s in work_durations]
    )
    spm_avg = _weighted_avg(
        [(_effective_spm(m), s) for m, s in work_durations]
    )

    work_distance_m = sum(int(m.get("distance") or 0) for m in mains)
    main_ids = {id(m) for m in mains}
    other_distance_m = 0
    for role, w in roles:
        actual_role = "main" if id(w) in main_ids else role
        other_distance_m += _other_meters(w, actual_role)

    # Session-level severity / ESS / strain — already computed; pull from
    # any visible member's _ess_session_summary.
    summary = next(
        (w.get("_ess_session_summary") for w in workouts
         if w.get("_ess_session_summary")),
        {},
    ) or {}

    # Session start / end for the date column.  Prefer the persisted
    # session record (which spans the full session even when only a
    # subset is visible); fall back to the visible members.
    if session_rec:
        start_dt_str = session_rec.get("start_dt") or ""
        end_dt_str = session_rec.get("end_dt") or ""
    else:
        starts = [_start_dt(w) for w in workouts]
        starts = [s for s in starts if s is not None]
        start_dt_str = (
            min(starts).strftime("%Y-%m-%d %H:%M:%S") if starts else
            (workouts[0].get("date") if workouts else "")
        )
        end_dt_str = max(
            (w.get("date") or "" for w in workouts),
            default="",
        )

    member_count = len(workouts)

    return {
        # ── Identity ─────────────────────────────────────────────────
        "id": sid,
        "_row_kind": "session",
        "session_id": sid,
        "_member_count": member_count,
        "_children": children,

        # ── Date column ──────────────────────────────────────────────
        # Reuse the existing `date` SORT_KEY (sorts strings) by setting
        # date to the session start ISO string.
        "date": start_dt_str,
        "_session_start_dt": start_dt_str,
        "_session_end_dt": end_dt_str,
        "_session_tod": _time_of_day(start_dt_str),
        "_session_total_duration_s": _session_total_duration_s(
            start_dt_str, end_dt_str
        ),

        # ── Main Work ────────────────────────────────────────────────
        "_main_work_lines": _build_main_work_lines(mains),

        # ── Work / Other distances + duration ────────────────────────
        "_work_duration_s": total_work_s,
        "_work_distance_m": work_distance_m,
        "_other_distance_m": other_distance_m,

        # ── Weighted averages ────────────────────────────────────────
        "_pace_tenths": pace_avg,
        # Reuse the `watts` key so the existing FORMATS.watts / SORT_KEYS.watts
        # work without a tree-aware override.
        "watts": watts_avg,
        "_drag": drag_avg,
        "_spm": spm_avg,

        # ── Zone Spread placeholders (most-severe main) ──────────────
        # Carry the most-severe-main's zone time fractions and bin-aligned
        # list onto the parent row so the JS Watts cell can render the
        # session-row's stacked Zone Spread bar.
        "_zone_time_fractions": most_severe.get("_zone_time_fractions"),
        "_zone_bin_fractions": most_severe.get("_zone_bin_fractions"),
        "_hr_spread_score": most_severe.get("_hr_spread_score"),
        "_hr_bin_meters": most_severe.get("_hr_bin_meters"),
        "heart_rate": most_severe.get("heart_rate"),

        # ── Session severity / ESS / strain — from _ess_session_summary
        "_severity": summary.get("severity_bucket"),
        "_severity_score": summary.get("severity_score"),
        "_ess": summary.get("ess"),
        "_anaerobic_strain": summary.get("anaerobic_strain"),
        # Glycogen Used integrated over the entire session timeline
        # (additive across workouts — glycogen does not refill in a 30-min
        # session gap the way W' partially does).  Pulled from the
        # session-level value computed in :func:`compute_session_metrics`,
        # not from a per-workout aggregation here.
        "_glycogen_used": summary.get("glycogen_used"),
        "_glycogen_kj": summary.get("glycogen_kj"),
        # Training Stimulus aggregate (max dose per band across mains).
        # Differs from glycogen's session-cumulative additive model: a
        # rower doing two separate VO2max sets in one session shouldn't
        # be flagged as "double VO2max stimulated" — the adaptation
        # response saturates per session.
        "_stimulus_doses": session_stim_doses or None,
        "_stimulus_systems": session_stim_systems or None,
        # Session-level intensity factor (parallel to per-workout _if_eff).
        "_if_eff": summary.get("if_eff_session"),

        # ── View link target ─────────────────────────────────────────
        "_view_target_id": str(most_severe.get("id"))
        if most_severe.get("id") is not None else "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_session_rows(
    workouts: list[dict],
    sessions_dict: dict,
) -> list[dict]:
    """Group ``workouts`` by ``session_id`` and emit one parent row per
    session.

    Aggregates over the visible subset for work-side fields; session-level
    severity / ESS / strain are taken from ``_ess_session_summary`` which
    reflects the full persisted session.  Workouts whose ``session_id`` is
    missing are emitted as singleton parent rows (each on its own).
    """
    by_session: dict = {}
    for w in workouts:
        sid = w.get("session_id")
        if not sid:
            # Synthetic singleton bucket using the workout id as the key
            # so each missing-session_id workout becomes its own group.
            sid = f"__nosess__{w.get('id')}"
        by_session.setdefault(sid, []).append(w)

    parents: list[dict] = []
    for sid, group in by_session.items():
        rec = sessions_dict.get(sid) if not sid.startswith("__nosess__") else None
        parents.append(_parent_from_session(sid, rec, group))
    return parents
