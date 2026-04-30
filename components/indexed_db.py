"""
IndexedDB — generalized async key-value bridge between Hyperdiv (Python)
and the browser's IndexedDB.

Why this exists
    Browser ``localStorage`` caps out at ~5–10 MB per origin. Power users
    with many years of Concept2 history (workouts + per-stroke data) can
    exhaust that ceiling.  IndexedDB has 50 MB – 1 GB+ headroom and supports
    per-record reads/writes, eliminating the O(N) decompress/recompress
    cycle the strokes cache used to pay on every workout view.

Architecture
    A single :class:`IndexedDB` plugin instance is mounted at the top of
    the dashboard render via :func:`mount_indexed_db`.  Its Python ↔ JS
    contract is a request/response pair of props plus a ``ready`` flag:

        Python → JS:  ``request``  = {id, op, store, key?, keys?, value?}
        JS → Python:  ``response`` = {id, result, error}
        JS → Python:  ``ready``    bool — True once the DB is open

    A per-session :class:`_IDBState` ``hd.global_state`` carries the FIFO
    request queue, the in-flight id, a ready flag, and a read cache so
    every consumer in the app can dispatch ops without holding a reference
    to the plugin component itself.

Read cache
    Read results (``get``, ``get_many``, ``get_all``, ``keys``) are cached
    by ``(op, store, key|keys-set)`` in ``_IDBState.reads``.  This makes
    repeated polling cheap and gives us a single place to invalidate stale
    entries when a write lands.

    Any write to a store (``put``, ``put_many``, ``delete``, ``clear``)
    invalidates **every** cached read for that store.  Subsequent reads
    re-issue the request.  This mirrors the cache behaviour of
    ``hd.local_storage`` (writes invalidate reads at the same key) but
    expanded to whole-store granularity since IDB keys are per-record.

Polling shape
    Caller-facing helpers return a :class:`Cmd` polling object whose
    ``.done`` / ``.result`` / ``.error`` mirror ``hd.local_storage``'s
    ``async_command``.  Writes return an already-``done`` :class:`Cmd` —
    callers fire and move on; the queue still dispatches the request.

Usage
    # Once at app top (before any consumer):
    mount_indexed_db(IDB_STORES)

    # Anywhere in render code:
    cmd = indexed_db.get_all("workouts")
    if cmd.done:
        records = cmd.result or []   # list of {key, value}

    indexed_db.put("strokes", str(wid), strokes_list)
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

import hyperdiv as hd


_HERE = os.path.dirname(__file__)


# Object stores backing the app's client-side caches.
#   "workouts"  — key = str(workout_id), value = workout dict
#   "strokes"   — key = str(workout_id), value = raw Concept2 strokes list
#   "_meta"     — key = "schema_version" → INTEGRITY_VERSION (and similar)
IDB_STORES = ("workouts", "strokes", "_meta")

WORKOUTS_STORE = "workouts"
STROKES_STORE = "strokes"
META_STORE = "_meta"
SCHEMA_VERSION_KEY = "schema_version"


# ---------------------------------------------------------------------------
# Plugin (Python ↔ JS bridge)
# ---------------------------------------------------------------------------


class IndexedDB(hd.Plugin):
    """The thin Hyperdiv plugin shell.  All real interaction happens via
    :func:`mount_indexed_db` and the module-level helpers below."""

    _name = "IndexedDB"
    _assets_root = os.path.join(_HERE, "chart_assets")
    _assets = [
        ("js-link", os.path.join(_HERE, "chart_assets", "indexed_db_plugin.js")),
    ]

    db_name = hd.Prop(hd.String, "ergnerd")
    stores = hd.Prop(hd.Any, [])

    # Python → JS: the next request to dispatch.
    request = hd.Prop(hd.Any, None)

    # JS → Python: the most recent response.
    response = hd.Prop(hd.Any, None)

    # JS → Python: True once the DB is open.
    ready = hd.Prop(hd.Bool, False)


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@hd.global_state
class _IDBState(hd.BaseState):
    """Per-connection IDB queue, in-flight tracker, and read cache."""

    next_id = hd.Prop(hd.Int, 1)

    # FIFO queue of pending requests, each a tuple
    # ``(id, op, store, key, keys_tuple, value)``.
    queue = hd.Prop(hd.Any, ())

    # ID currently dispatched to JS.  0 = idle.
    in_flight_id = hd.Prop(hd.Int, 0)

    is_ready = hd.Prop(hd.Bool, False)

    # Read result cache: ``{cache_key: {"id", "done", "result", "error"}}``.
    # Cache keys are tuples; see :func:`_cache_key`.  Writes to a store
    # invalidate every entry for that store.
    reads = hd.Prop(hd.Any, {})


def _cache_key(
    op: str, store: str, key: Optional[str] = None, keys: Optional[Iterable[str]] = None
):
    if op == "get":
        return ("get", store, key)
    if op == "get_all":
        return ("get_all", store)
    if op == "keys":
        return ("keys", store)
    if op == "get_many":
        # Order-insensitive: same key set hits the same cache entry.
        return ("get_many", store, frozenset(keys or ()))
    return None


# ---------------------------------------------------------------------------
# Mount + dispatcher (call once per render at app top)
# ---------------------------------------------------------------------------


def mount_indexed_db(stores: Iterable[str]) -> None:
    """Render the IndexedDB plugin and pump its request/response queue.

    Call once per render at the top of the dashboard.  Idempotent across
    renders — Hyperdiv re-uses the same component instance and its props
    persist.
    """
    s = _IDBState()
    plugin = IndexedDB(stores=tuple(stores))

    if plugin.ready and not s.is_ready:
        s.is_ready = True

    # Dispatch the next queued item if the previous one has resolved.
    if s.is_ready and s.in_flight_id == 0 and s.queue:
        head = s.queue[0]
        req_id, op, store, key, keys, value = head
        plugin.request = {
            "id": req_id,
            "op": op,
            "store": store,
            "key": key,
            "keys": list(keys) if keys is not None else None,
            "value": value,
        }
        s.in_flight_id = req_id
        s.queue = s.queue[1:]

    # Pull any in-flight response into the read cache.
    resp = plugin.response
    if (
        isinstance(resp, dict)
        and resp.get("id")
        and resp["id"] == s.in_flight_id
    ):
        rid = int(resp["id"])
        err = resp.get("error")
        err_str = err if isinstance(err, str) else ("" if err is None else str(err))
        # Find the read-cache entry that fired this id (writes do not cache,
        # so a missing match means the response was for a fire-and-forget
        # write — nothing to record).
        updated = False
        new_reads = None
        for ck, entry in s.reads.items():
            if (
                isinstance(entry, dict)
                and entry.get("id") == rid
                and not entry.get("done")
            ):
                new_reads = dict(s.reads)
                new_reads[ck] = {
                    "id": rid,
                    "done": True,
                    "result": resp.get("result"),
                    "error": err_str,
                }
                updated = True
                break
        if updated and new_reads is not None:
            s.reads = new_reads
        s.in_flight_id = 0


# ---------------------------------------------------------------------------
# Polling handle returned by helpers
# ---------------------------------------------------------------------------


class Cmd:
    """Polling-friendly handle returned by IDB helpers."""

    __slots__ = ("done", "result", "error", "running")

    def __init__(
        self,
        *,
        done: bool = False,
        result: Any = None,
        error: str = "",
        running: bool = False,
    ) -> None:
        self.done = done
        self.result = result
        self.error = error
        self.running = running


_LOADING = Cmd(running=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read(
    op: str,
    store: str,
    key: Optional[str] = None,
    keys: Optional[Iterable[str]] = None,
) -> Cmd:
    """Cache-aware read dispatcher.

    Returns the cached entry if one exists for ``(op, store, key|keys)``.
    Otherwise enqueues a fresh request and seeds an in-flight cache entry
    so concurrent callers share the same response.
    """
    s = _IDBState()
    if not s.is_ready:
        return _LOADING

    ck = _cache_key(op, store, key=key, keys=keys)
    cached = s.reads.get(ck)
    if cached is not None:
        if cached.get("done"):
            return Cmd(
                done=True,
                result=cached.get("result"),
                error=cached.get("error") or "",
            )
        return _LOADING

    req_id = s.next_id
    s.next_id = req_id + 1
    keys_tuple = tuple(keys) if keys is not None else None
    s.queue = s.queue + ((req_id, op, store, key, keys_tuple, None),)
    new_reads = dict(s.reads)
    new_reads[ck] = {"id": req_id, "done": False, "result": None, "error": ""}
    s.reads = new_reads
    return _LOADING


def _write(
    op: str,
    store: str,
    key: Optional[str] = None,
    value: Any = None,
) -> Cmd:
    """Enqueue a write and invalidate the store's cached reads.

    Writes are fire-and-forget from the caller's perspective: this returns
    a ``Cmd`` already in ``done`` state.  The actual IDB transaction is
    dispatched by :func:`mount_indexed_db` from the queue on subsequent
    renders.
    """
    s = _IDBState()
    if not s.is_ready:
        return _LOADING

    req_id = s.next_id
    s.next_id = req_id + 1
    s.queue = s.queue + ((req_id, op, store, key, None, value),)

    # Invalidate every cached read for this store so the next read fires
    # against the new on-disk state.
    new_reads = {ck: e for ck, e in s.reads.items() if not (ck and ck[1] == store)}
    s.reads = new_reads

    return Cmd(done=True)


# ---------------------------------------------------------------------------
# Public read helpers
# ---------------------------------------------------------------------------


def get(store: str, key: str) -> Cmd:
    """Read one record.  ``cmd.result`` is the stored value or ``None``."""
    return _read("get", store, key=str(key))


def get_many(store: str, keys: Iterable[str]) -> Cmd:
    """Read multiple records in one round-trip.

    ``cmd.result`` is a ``{key: value}`` dict containing only keys that exist.
    """
    return _read("get_many", store, keys=[str(k) for k in keys])


def get_all(store: str) -> Cmd:
    """Read every record.  ``cmd.result`` is a list of ``{key, value}``."""
    return _read("get_all", store)


def keys_of(store: str) -> Cmd:
    """List every key in the store as strings."""
    return _read("keys", store)


# ---------------------------------------------------------------------------
# Public write helpers (fire-and-forget)
# ---------------------------------------------------------------------------


def put(store: str, key: str, value: Any) -> Cmd:
    """Write one record."""
    return _write("put", store, key=str(key), value=value)


def put_many(store: str, entries: dict) -> Cmd:
    """Write many records in a single transaction.

    ``entries`` is a ``{key: value}`` dict; keys are stringified.
    """
    return _write(
        "put_many",
        store,
        value={str(k): v for k, v in (entries or {}).items()},
    )


def delete(store: str, key: str) -> Cmd:
    """Delete one record."""
    return _write("delete", store, key=str(key))


def clear(store: str) -> Cmd:
    """Delete every record in the store."""
    return _write("clear", store)


def is_ready() -> bool:
    """True once the IndexedDB connection is open and ops can dispatch."""
    return bool(_IDBState().is_ready)
