"""
Experimental Page — reference watts over time.

A throwaway-friendly sandbox for visualising the time-indexed reference-watts
fitness baseline (see ``services.reference_watts``). One line series per ranked
event; each marker contributes a (date, watts) point.
"""

from __future__ import annotations

from datetime import datetime, timezone

import hyperdiv as hd

from components.app_context import your
from components.concept2_sync import get_all_workouts
from components.reference_watts_loader import reference_watts_loader
from components.shared_ui import global_filter_ui
from services.global_state import GlobalFilters
from services.reference_watts import resolve_index
from services.rowing_utils import ranked_distances, ranked_times


def _cat_key_label(cat_key: tuple, label_map: dict[tuple, str]) -> str:
    return label_map.get(cat_key, f"{cat_key[0]}:{cat_key[1]}")


def _label_map(machine: str) -> dict[tuple, str]:
    out: dict[tuple, str] = {}
    for d, lbl in ranked_distances(machine):
        out[("dist", d)] = lbl
    for t, lbl in ranked_times(machine):
        out[("time", t)] = lbl
    return out


def _date_to_ms(d) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _build_datasets(index: dict, machine: str) -> list[dict]:
    """One line dataset per ranked event present in any marker."""
    markers = index.get("markers") or []
    if not markers:
        return []

    label_map = _label_map(machine)
    # Preserve the canonical event order (distances first, then times).
    ordered_keys: list[tuple] = list(label_map.keys())

    datasets: list[dict] = []
    for ck in ordered_keys:
        points: list[tuple[int, float]] = []
        for m in markers:
            w = m["watts"].get(ck)
            if w is None:
                continue
            points.append((_date_to_ms(m["date"]), round(float(w), 2)))
        if not points:
            continue
        datasets.append(
            dict(
                data=points,
                label=_cat_key_label(ck, label_map),
            )
        )
    return datasets


def experimental_page() -> None:
    sync = get_all_workouts()
    if sync is None:
        hd.box(padding=2, min_height="80vh")
        return
    _workouts_dict, all_workouts = sync
    machine = GlobalFilters().machine

    with hd.box(align="center", gap=2, padding=2, min_height="80vh"):
        with hd.box(gap=0.2, align="center"):
            with hd.h1(font_weight="normal"):
                hd.text(f"{your()} Reference Watts Over Time")
            global_filter_ui()

        if not all_workouts:
            hd.text(
                "No workouts available for the selected filter.",
                font_color="neutral-500",
            )
            return

        if not reference_watts_loader(all_workouts):
            return

        index = resolve_index(all_workouts)
        datasets = _build_datasets(index, machine)

        if not datasets:
            hd.text(
                "Not enough quality efforts to build a fitness baseline yet.",
                font_color="neutral-500",
            )
            return

        with hd.box(width="100%", max_width="1100px", height="65vh"):
            hd.line_chart(
                *datasets,
                x_axis="timeseries",
                y_axis="linear",
                grid_color="neutral-200" if not hd.theme().is_dark else "neutral-700",
            )
