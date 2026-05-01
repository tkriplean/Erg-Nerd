"""
Loader for the time-indexed reference-watts index.

Persistence shell around :mod:`services.reference_watts`.  Services are pure
Python — HyperDiv I/O (localStorage, tasks, progress UI) lives here.

The index is expensive for a multi-year rower (one CP fit per quarterly
marker, dozens of markers) so we persist it to localStorage under the key
:data:`LS_KEY` and rebuild only when the workout-identity hash changes.

Usage::

    ready = reference_watts_loader(all_workouts)
    if not ready:
        return  # progress bar is already rendered inline by the loader

Then any component in the same render tree can call
``services.reference_watts.get_reference_watts(when, all_workouts)`` without
re-triggering the build.
"""

from __future__ import annotations

import json

import hyperdiv as hd

from services.reference_watts import (
    build_reference_watts_index,
    deserialize_index,
    input_hash,
    seed_reference_watts_index,
    serialize_index,
)
from services.global_state import GlobalFilters

_LS_KEY_PREFIX = "reference_watts_v1"


def _ls_key(machine: str) -> str:
    """Per-machine localStorage slot so switching machines preserves both indices."""
    return f"{_LS_KEY_PREFIX}_{machine}"


@hd.global_state
class ReferenceWattsState(hd.BaseState):
    # Per-machine bookkeeping so switching machines doesn't trample the other
    # machine's loader state.  Each field maps machine → value.
    ls_loaded_by_machine = hd.Prop(hd.Any, {})
    seeded_hash_by_machine = hd.Prop(hd.Any, {})
    persisted_hash_by_machine = hd.Prop(hd.Any, {})
    build_started = hd.Prop(hd.Bool, False)
    progress_i = hd.Prop(hd.Int, 0)
    progress_n = hd.Prop(hd.Int, 0)
    progress_label = hd.Prop(hd.String, "")


def reference_watts_loader(all_workouts: list) -> bool:
    """Ensure the reference-watts index is built and persisted.

    Returns True when the service-module cache is seeded with an index that
    matches the current workouts.  Returns False while loading / building,
    rendering a progress UI inline.

    ``all_workouts`` must be from a single machine — callers route this
    through ``get_all_workouts()`` which honors ``gstate.machine``.
    """
    loader_state = ReferenceWattsState()
    machine = GlobalFilters().machine

    target_hash = input_hash(all_workouts)
    ls_loaded = dict(loader_state.ls_loaded_by_machine or {})
    seeded_by_machine = dict(loader_state.seeded_hash_by_machine or {})
    persisted_by_machine = dict(loader_state.persisted_hash_by_machine or {})

    # ── Step 1: one-time localStorage read (per machine) ────────────────────
    if not ls_loaded.get(machine):
        ls = hd.local_storage.get_item(_ls_key(machine))
        if not ls.done:
            _progress_ui(loader_state, "Loading fitness baseline…")
            return False
        if ls.result:
            try:
                payload = json.loads(ls.result)
                index = deserialize_index(payload)
                if index.get("input_hash") == target_hash:
                    seed_reference_watts_index(index)
                    seeded_by_machine[machine] = target_hash
                    persisted_by_machine[machine] = target_hash
            except Exception:
                pass
        ls_loaded[machine] = True
        loader_state.ls_loaded_by_machine = ls_loaded
        loader_state.seeded_hash_by_machine = seeded_by_machine
        loader_state.persisted_hash_by_machine = persisted_by_machine

    # Fast path: the cached index matches the current workouts.
    if seeded_by_machine.get(machine) == target_hash:
        return True

    # ── Step 2: background build ────────────────────────────────────────────
    task = hd.task()

    def _run(wkts):
        def on_progress(i, n, label):
            loader_state.progress_i = i
            loader_state.progress_n = n
            loader_state.progress_label = label

        return build_reference_watts_index(wkts, on_progress=on_progress)

    if not task.running and not task.done:
        task.run(_run, all_workouts)
        loader_state.build_started = True

    if task.running:
        _progress_ui(loader_state, "Computing fitness baseline…")
        return False

    if task.error:
        hd.alert(
            f"Could not compute fitness baseline: {task.error}",
            variant="warning",
            opened=True,
        )
        return False

    if task.done:
        index = task.result
        # ``build_reference_watts_index`` already seeds the service cache;
        # just mark our loader state and persist.
        seeded_by_machine[machine] = index.get("input_hash", target_hash)
        if persisted_by_machine.get(machine) != seeded_by_machine[machine]:
            try:
                hd.local_storage.set_item(
                    _ls_key(machine), json.dumps(serialize_index(index))
                )
                persisted_by_machine[machine] = seeded_by_machine[machine]
            except Exception as exc:
                print(f"[reference_watts] persist failed: {exc}")
        loader_state.seeded_hash_by_machine = seeded_by_machine
        loader_state.persisted_hash_by_machine = persisted_by_machine
        return True

    return False


def _progress_ui(state, prefix: str) -> None:
    n = state.progress_n
    i = state.progress_i
    with hd.box(align="center", padding=4, gap=1):
        if n > 0:
            pct = min(100, round(i / n * 100))
            with hd.box(width=32):
                hd.progress_bar(value=pct)
            line = f"{prefix} {state.progress_label} ({i}/{n})"
        else:
            hd.spinner()
            line = prefix
        hd.text(line, font_color="neutral-500")
