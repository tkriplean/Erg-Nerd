"""
Session clustering — pure Python.

A session is the maximal run of workouts on the same date and machine where
consecutive entries have a gap below ``SESSION_GAP_S`` between ``prev.end``
and ``next.start``.  Concept2's ``date`` field is the workout end;
``start = end − duration``.  The result is 
persisted alongside the workouts dict in IndexedDB (and in the public
profile mirror) so we don't recompute on every render.

Public API
    SESSIONS_SCHEMA_VERSION                                       -> int
    SESSIONS_SCHEMA_VERSION_KEY                                   -> str
    build_sessions_from_scratch(workouts_dict)                    -> (sessions_dict, mutations)
    assign_sessions_incremental(workouts_dict, existing, affected) -> (sessions_dict, mutations, dropped_uids)

Session record shape
    {
      "session_id":     str,         # uuid4 hex (key in IDB store)
      "machine":        str,         # "rower" | "skierg" | "bikeerg"
      "day":            str,         # "YYYY-MM-DD" (start_dt's day)
      "start_dt":       str,         # ISO "YYYY-MM-DD HH:MM:SS"
      "end_dt":         str,         # ISO
      "workout_ids":    list[str],   # chronological, str(workout_id)
      "schema_version": int,         # SESSIONS_SCHEMA_VERSION at write time
    }

Notes
    - Cluster bucketing is by (machine, day) where ``day`` is the end-date
      prefix (``date[:10]``).  A
      workout that ends after midnight is grouped with workouts on its
      *end* day, not its start day.
    - Workouts whose ``date`` won't parse become a singleton session — they
      still get a uid so the rest of the pipeline can rely on
      ``workout["session_id"]`` being populated.
    - Bucketing key uses ``date[:10]``; the singleton fallback uses
      ``("__nodate__", str(wid))`` so unparseable workouts never collide
      with each other.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable

from services.erg_stress import (
    SESSION_GAP_S,
    _parse_workout_datetime,
    _workout_total_duration_s,
)


SESSIONS_SCHEMA_VERSION = 1
SESSIONS_SCHEMA_VERSION_KEY = "sessions_schema_version"


def _machine_of(w: dict) -> str:
    return w.get("type") or "rower"


def _bucket_key(w: dict) -> tuple[str, str]:
    """Return (machine, day) for clustering, or a singleton sentinel for
    workouts with no parseable date."""
    day = (w.get("date") or "")[:10]
    if not day:
        return ("__nodate__", str(w.get("id")))
    return (_machine_of(w), day)


def _start_end_dt(w: dict) -> tuple[datetime, datetime] | None:
    """Return (start_dt, end_dt) for a workout using raw fields, or None
    when the date won't parse."""
    end_dt = _parse_workout_datetime(w.get("date"))
    if end_dt is None:
        return None
    duration_s = _workout_total_duration_s(w)
    return end_dt - _td_seconds(duration_s), end_dt


def _td_seconds(secs: float):
    from datetime import timedelta

    return timedelta(seconds=secs)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _session_record(
    session_id: str,
    machine: str,
    cluster: list[tuple[datetime, datetime, dict]],
) -> dict:
    """Build a session record dict from a chronologically-sorted cluster of
    (start_dt, end_dt, workout) tuples."""
    start_dt = cluster[0][0]
    end_dt = cluster[-1][1]
    return {
        "session_id": session_id,
        "machine": machine,
        "day": start_dt.strftime("%Y-%m-%d"),
        "start_dt": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_dt": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "workout_ids": [str(w.get("id")) for _, _, w in cluster],
        "schema_version": SESSIONS_SCHEMA_VERSION,
    }


def _singleton_record(w: dict) -> dict:
    """Build a singleton session record for a workout whose date won't
    parse.  The ``start_dt`` / ``end_dt`` strings fall back to whatever
    ``date`` carries (or empty)."""
    raw = (w.get("date") or "")[:19]
    return {
        "session_id": _new_session_id(),
        "machine": _machine_of(w),
        "day": raw[:10],
        "start_dt": raw,
        "end_dt": raw,
        "workout_ids": [str(w.get("id"))],
        "schema_version": SESSIONS_SCHEMA_VERSION,
    }


def _cluster_bucket(
    workouts: list[dict],
) -> list[list[tuple[datetime, datetime, dict]]]:
    """Sort a single (machine, day) bucket by start_dt, then greedy-split
    by SESSION_GAP_S.  Workouts with unparseable dates are excluded — the
    caller handles them as singletons."""
    timed: list[tuple[datetime, datetime, dict]] = []
    for w in workouts:
        se = _start_end_dt(w)
        if se is None:
            continue
        timed.append((se[0], se[1], w))
    if not timed:
        return []
    timed.sort(key=lambda x: x[0])

    clusters: list[list[tuple[datetime, datetime, dict]]] = [[timed[0]]]
    for prev, curr in zip(timed, timed[1:]):
        gap = (curr[0] - prev[1]).total_seconds()
        if gap < SESSION_GAP_S:
            clusters[-1].append(curr)
        else:
            clusters.append([curr])
    return clusters


def _bucketize(workouts: Iterable[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for w in workouts:
        out.setdefault(_bucket_key(w), []).append(w)
    return out


def _build_for_bucket(
    bucket_key: tuple[str, str], workouts: list[dict]
) -> tuple[dict, dict]:
    """Build session records for a single bucket.

    Returns (records_by_uid, assignments_by_wid_str).  ``__nodate__``
    buckets always produce singleton records; everything else runs through
    the gap-split clustering.
    """
    records: dict = {}
    assignments: dict = {}

    if bucket_key[0] == "__nodate__":
        # One workout, but the bucket is a list (it has a single entry).
        for w in workouts:
            rec = _singleton_record(w)
            records[rec["session_id"]] = rec
            assignments[str(w.get("id"))] = rec["session_id"]
        return records, assignments

    machine = bucket_key[0]
    clusters = _cluster_bucket(workouts)

    # Workouts in the bucket whose date didn't parse — singletons.
    timed_ids = {str(w.get("id")) for cluster in clusters for _, _, w in cluster}
    for w in workouts:
        wid_str = str(w.get("id"))
        if wid_str not in timed_ids:
            rec = _singleton_record(w)
            records[rec["session_id"]] = rec
            assignments[wid_str] = rec["session_id"]

    for cluster in clusters:
        sid = _new_session_id()
        rec = _session_record(sid, machine, cluster)
        records[sid] = rec
        for _, _, w in cluster:
            assignments[str(w.get("id"))] = sid

    return records, assignments


def build_sessions_from_scratch(workouts_dict: dict) -> tuple[dict, dict]:
    """One-pass clustering of every workout into sessions.

    Returns ``(sessions_dict, mutations)`` where:

      - ``sessions_dict``: ``{session_id: session_record}``.
      - ``mutations``:    ``{workout_id_str: session_id}`` for every workout.

    Caller writes mutations back onto each ``workouts_dict[wid]`` as
    ``session_id`` before persisting.
    """
    sessions_dict: dict = {}
    mutations: dict = {}
    for bucket_key, group in _bucketize(workouts_dict.values()).items():
        records, assignments = _build_for_bucket(bucket_key, group)
        sessions_dict.update(records)
        mutations.update(assignments)
    return sessions_dict, mutations


def _existing_sessions_by_bucket(
    existing_sessions: dict,
) -> dict[tuple[str, str], list[str]]:
    """Index existing sessions by (machine, day) → list of session_ids."""
    out: dict[tuple[str, str], list[str]] = {}
    for sid, rec in existing_sessions.items():
        machine = rec.get("machine") or "rower"
        day = rec.get("day") or ""
        out.setdefault((machine, day), []).append(sid)
    return out


def assign_sessions_incremental(
    workouts_dict: dict,
    existing_sessions: dict,
    affected_wids: Iterable[str],
) -> tuple[dict, dict, set[str]]:
    """Re-cluster only the (machine, day) buckets touched by ``affected_wids``.

    Returns ``(sessions_dict, mutations, dropped_session_ids)``:

      - ``sessions_dict``:   ``{session_id: session_record}`` — full dict
        (existing minus dropped, plus rebuilt buckets).
      - ``mutations``:       ``{wid: session_id}`` only for workouts whose
        assignment changed.
      - ``dropped_session_ids``: stale sessions to delete from IDB.

    Every workout in ``workouts_dict`` is expected to be the post-normalize
    shape (raw ``date``/``time``/``rest_time``/``type`` present).  Workouts
    in ``affected_wids`` that no longer exist in ``workouts_dict`` (deletion
    case) only contribute their old bucket to the rebuild set.
    """
    affected_set = set(affected_wids)
    if not affected_set:
        return existing_sessions, {}, set()

    by_bucket_existing = _existing_sessions_by_bucket(existing_sessions)
    workouts_by_bucket = _bucketize(workouts_dict.values())

    # Buckets that need rebuild: any bucket containing an affected workout
    # in workouts_dict, plus any bucket of an existing session that
    # contained an affected workout (deletion case).
    touched_buckets: set[tuple[str, str]] = set()
    for wid in affected_set:
        w = workouts_dict.get(wid)
        if w is not None:
            touched_buckets.add(_bucket_key(w))
    for sid, rec in existing_sessions.items():
        if any(wid in affected_set for wid in rec.get("workout_ids") or []):
            touched_buckets.add(((rec.get("machine") or "rower"), rec.get("day") or ""))

    dropped_session_ids: set[str] = set()
    for bucket in touched_buckets:
        for sid in by_bucket_existing.get(bucket, ()):
            dropped_session_ids.add(sid)

    # Old assignment lookup so we only emit mutations for workouts whose
    # session_id actually changed.
    old_assignment: dict[str, str] = {}
    for sid, rec in existing_sessions.items():
        for wid in rec.get("workout_ids") or []:
            old_assignment[wid] = sid

    # Carry over untouched sessions; rebuild touched buckets.
    sessions_dict: dict = {
        sid: rec
        for sid, rec in existing_sessions.items()
        if sid not in dropped_session_ids
    }
    mutations: dict = {}
    for bucket in touched_buckets:
        group = workouts_by_bucket.get(bucket, [])
        if not group:
            continue
        records, assignments = _build_for_bucket(bucket, group)
        sessions_dict.update(records)
        for wid, new_sid in assignments.items():
            if old_assignment.get(wid) != new_sid:
                mutations[wid] = new_sid

    return sessions_dict, mutations, dropped_session_ids
