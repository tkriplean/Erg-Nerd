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

LS_KEY = "reference_watts_v1"


def reference_watts_loader(all_workouts: list) -> bool:
    """Ensure the reference-watts index is built and persisted.

    Returns True when the service-module cache is seeded with an index that
    matches the current workouts.  Returns False while loading / building,
    rendering a progress UI inline.
    """
    loader_state = hd.state(
        ls_loaded=False,
        seeded_hash="",
        persisted_hash="",
        build_started=False,
        progress_i=0,
        progress_n=0,
        progress_label="",
    )

    target_hash = input_hash(all_workouts)

    # ── Step 1: one-time localStorage read ──────────────────────────────────
    if not loader_state.ls_loaded:
        ls = hd.local_storage.get_item(LS_KEY)
        if not ls.done:
            _progress_ui(loader_state, "Loading fitness baseline…")
            return False
        if ls.result:
            try:
                payload = json.loads(ls.result)
                index = deserialize_index(payload)
                if index.get("input_hash") == target_hash:
                    seed_reference_watts_index(index)
                    loader_state.seeded_hash = target_hash
                    loader_state.persisted_hash = target_hash
            except Exception:
                pass
        loader_state.ls_loaded = True

    # Fast path: the cached index matches the current workouts.
    if loader_state.seeded_hash == target_hash:
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
        loader_state.seeded_hash = index.get("input_hash", target_hash)
        if loader_state.persisted_hash != loader_state.seeded_hash:
            try:
                hd.local_storage.set_item(
                    LS_KEY, json.dumps(serialize_index(index))
                )
                loader_state.persisted_hash = loader_state.seeded_hash
            except Exception as exc:
                print(f"[reference_watts] persist failed: {exc}")
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
