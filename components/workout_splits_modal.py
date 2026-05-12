"""
components/workout_splits_modal.py — Custom splits editor (modal dialog).

Replaces the inline chip-row editor that used to sit beside the splits table.
The dialog gives the editor space to breathe and adds:

  * **Presets-first UX** — distance (500m / 1k / 2k / 5k) and time
    (1 / 2 / 5 / 10 / 15 min) preset buttons that fill the chip list with a
    repeating chunk plus a tail chip for the remainder.
  * **Divide-into-N** — preset count dropdown that produces an even-as-possible
    division of the workout's total.
  * **Mixed units** — each chip carries its own ``u`` ("m" or "s"); the
    Advanced editor exposes a per-chip unit toggle.  The common case (all
    one unit) is the headline UI.
  * **Tab scaffolding for ranked events** — the modal hosts a [Splits | Ranked
    events] tab pair.  Ranked events is a Phase-2 placeholder for now.

Public surface
--------------
    render_splits_modal(dialog, workout, *, current_values, on_apply)

The dialog is created by the caller (``workout_page``) so its component
state survives across re-renders of the right column.  This module renders
into the dialog and reports edits via the ``on_apply`` callback.

Storage
-------
LocalStorage key ``"custom_splits"`` (managed by ``workout_page``) holds::

    {
        "<workout_id>": {
            "splits": {"values": [{"u":"m","v":1000}, ...]},
            "interval_sub": {...}   # Phase 3
        },
        ...
    }

This module is concerned only with the chip list inside ``splits.values``.
"""

from __future__ import annotations

from typing import Optional

import hyperdiv as hd

from services.formatters import fmt_split, format_time
from services.rowing_utils import ranked_distances, ranked_times, workout_machine
from components.hyperdiv_extensions import radio_group
from services.splits import (
    DEFAULT_SPLIT_COUNT,
    SPLIT_COUNT_OPTIONS,
    TIME_BASED_WORKOUT_TYPES,
    best_window_distance,
    best_window_time,
    even_splits,
    format_mmss,
    from_start_distance,
    from_start_time,
    parse_time_input,
    stitch_interval_times,
)


# ---------------------------------------------------------------------------
# Preset tables
# ---------------------------------------------------------------------------

# (chunk, label)
_DISTANCE_PRESETS = [
    (500, "500m"),
    (1000, "1k"),
    (2000, "2k"),
    (5000, "5k"),
]
_TIME_PRESETS = [
    (60, "1 min"),
    (120, "2 min"),
    (300, "5 min"),
    (600, "10 min"),
    (900, "15 min"),
]

# Reverse lookup: chunk size → label, used by the dropdown trigger so it
# echoes the currently-active preset (e.g. "1k distance splits").
_DISTANCE_CHUNK_LABEL = {chunk: lbl for chunk, lbl in _DISTANCE_PRESETS}
_TIME_CHUNK_LABEL = {chunk: lbl for chunk, lbl in _TIME_PRESETS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_entry(saved) -> Optional[dict]:
    """Coerce one workout's localStorage entry to the new shape.

    Accepts the new shape ``{"splits": {"values": [{u,v},...]},
    "interval_sub": {...}}`` and the pre-modal legacy ``{"unit": "m"|"s",
    "values": [int,...]}``.  Either ``splits`` or ``interval_sub`` is
    sufficient to keep the entry; both are optional individually.  Returns
    ``None`` when nothing usable is left.
    """
    if not isinstance(saved, dict):
        return None

    out: dict = {}

    # New shape — splits sub-key.
    if "splits" in saved and isinstance(saved.get("splits"), dict):
        raw = saved["splits"].get("values") or []
        cleaned = _clean_chip_list(raw)
        if cleaned:
            out["splits"] = {"values": cleaned}

    # New shape — interval_sub sub-key.
    sub = _clean_interval_sub(saved.get("interval_sub"))
    if sub is not None:
        out["interval_sub"] = sub

    if out:
        return out

    # Legacy shape — top-level unit + raw int list.
    if "values" in saved and "unit" in saved:
        unit = saved.get("unit", "m")
        cleaned = []
        for v in saved.get("values") or []:
            try:
                cleaned.append({"u": str(unit), "v": int(v)})
            except (TypeError, ValueError):
                continue
        if cleaned:
            return {"splits": {"values": cleaned}}

    return None


def _clean_interval_sub(raw) -> Optional[dict]:
    """Validate an ``interval_sub`` spec.  Returns a tidy dict or None."""
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode == "n_pieces":
        try:
            n = int(raw.get("n", 0))
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        return {"mode": "n_pieces", "n": n}
    if mode == "absolute":
        unit = raw.get("unit")
        if unit not in ("m", "s"):
            return None
        try:
            value = int(raw.get("value", 0))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return {"mode": "absolute", "unit": unit, "value": value}
    if mode == "custom":
        cleaned = _clean_chip_list(raw.get("values") or [])
        if not cleaned:
            return None
        return {"mode": "custom", "values": cleaned}
    return None


def _expand_sub_spec_to_chips(
    spec: Optional[dict], intervals: list
) -> list:
    """Convert any legacy ``interval_sub`` shape to a chip list applied per
    work interval.

    The modal edits a single chip pattern that is broadcast to every work
    interval.  Existing saved data may still carry the older shapes
    ``{mode: "n_pieces", n: 4}`` or ``{mode: "absolute", unit, value}`` —
    when the modal opens those, we expand them using the first work
    interval's dimensions so the chip editor shows what the spec amounts
    to.  The user can adjust from there.
    """
    if not spec:
        return []
    work_ivs = [iv for iv in intervals if (iv.get("time") or 0) > 0]
    if not work_ivs:
        return []
    first = work_ivs[0]
    iv_dur_s = int(round((first.get("time") or 0) / 10.0))
    iv_dist_m = int(round(first.get("distance") or 0))
    iv_type = (first.get("type") or "").lower()
    primary_unit = "s" if iv_type == "time" else "m"
    primary_target = iv_dur_s if primary_unit == "s" else iv_dist_m

    mode = spec.get("mode")
    if mode == "n_pieces":
        try:
            n = int(spec.get("n", 0))
        except (TypeError, ValueError):
            n = 0
        if n <= 0 or primary_target <= 0:
            return []
        sizes = even_splits(primary_target, n)
        return [{"u": primary_unit, "v": s} for s in sizes]
    if mode == "absolute":
        unit = spec.get("unit", primary_unit)
        try:
            value = int(spec.get("value", 0))
        except (TypeError, ValueError):
            value = 0
        target = iv_dist_m if unit == "m" else iv_dur_s
        if value <= 0 or target <= 0:
            return []
        full = target // value
        rem = target - full * value
        out = [{"u": unit, "v": value} for _ in range(full)]
        if rem > 0:
            out.append({"u": unit, "v": rem})
        return out
    if mode == "custom":
        return _clean_chip_list(spec.get("values") or [])
    return []


def _clean_chip_list(raw) -> list:
    """Filter a raw chip list to well-formed ``{"u","v"}`` dicts only."""
    out = []
    if not isinstance(raw, list):
        return out
    for v in raw:
        if isinstance(v, dict) and "u" in v and "v" in v:
            try:
                u = str(v["u"])
                if u not in ("m", "s"):
                    continue
                out.append({"u": u, "v": int(v["v"])})
            except (TypeError, ValueError):
                continue
    return out


def _preset_chips(target: int, chunk: int, unit: str) -> list:
    """Build a chip list of ``chunk``-sized splits covering ``target`` with
    a tail-split for the remainder when ``target % chunk != 0``.
    """
    if chunk <= 0 or target <= 0:
        return []
    full = target // chunk
    rem = target - full * chunk
    out = [{"u": unit, "v": chunk} for _ in range(full)]
    if rem > 0:
        out.append({"u": unit, "v": rem})
    return out


def _dominant_unit(values: list, fallback: str) -> str:
    """Return the more common unit in ``values``; ``fallback`` on a tie."""
    if not values:
        return fallback
    m_count = sum(1 for v in values if v.get("u") == "m")
    s_count = sum(1 for v in values if v.get("u") == "s")
    if m_count == s_count:
        return fallback
    return "m" if m_count > s_count else "s"


def _sum_by_unit(values: list) -> tuple:
    """Return ``(sum_m, sum_s)`` — values grouped by their declared unit."""
    sm = 0
    ss = 0
    for v in values:
        try:
            x = int(v.get("v", 0))
        except (TypeError, ValueError):
            continue
        if v.get("u") == "s":
            ss += x
        else:
            sm += x
    return sm, ss


def _validate_sum(
    values: list, total_dist_m: int, total_time_s: int
) -> tuple:
    """Decide whether the chip list reconciles to the workout's totals.

    Returns ``(ok, message)``.  Mixed-unit chip lists are accepted without
    a strict sum check — chip boundaries land on stroke-interpolated points
    rather than on a single axis, so a single-axis sum would mis-measure
    them.  Pure-distance or pure-time lists tolerate ±2 of slop.
    """
    sm, ss = _sum_by_unit(values)

    if sm == 0 and ss == 0:
        return False, "Add at least one split."

    # Pure-distance
    if ss == 0 and sm > 0:
        diff = sm - total_dist_m
        if abs(diff) <= 2:
            return True, f"✓ Sum: {sm:,}m"
        return False, (
            f"Sum ({sm:,}m) must equal workout distance "
            f"({total_dist_m:,}m) — off by {diff:+,}m."
        )

    # Pure-time
    if sm == 0 and ss > 0:
        diff = ss - total_time_s
        if abs(diff) <= 2:
            return True, f"✓ Sum: {format_mmss(ss)}"
        return False, (
            f"Sum ({format_mmss(ss)}) must equal workout time "
            f"({format_mmss(total_time_s)}) — off by {diff:+}s."
        )

    # Mixed-unit case.  Each chip starts where the previous chip's axis put
    # the cursor; reconciling to the workout total requires the stroke
    # interpolator.  Accept without a strict sum check.
    return True, f"✓ Mixed: {sm:,}m + {format_mmss(ss)}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render_splits_modal(
    dialog,
    workout: dict,
    strokes: Optional[list],
    *,
    current_values: Optional[list],
    current_sub_spec: Optional[dict] = None,
    on_apply,
    on_close=None,
) -> None:
    """Render the splits editor contents into the (open) ``dialog``.

    ``on_apply(payload)`` is called when the user clicks Apply.  ``payload``
    is a dict ``{"values": list|None, "sub_spec": dict|None}`` — only one
    field carries content based on whether the workout is an interval one.
    Passing ``None`` for the appropriate field means "clear".

    ``strokes`` is the stroke list for the ranked-events tab.  The Splits
    tab does not need it (preset computation is metadata-only); ranked
    events lazily compute when their tab is opened.
    """
    workout_id = workout["id"]
    wtype = workout.get("workout_type", "")
    is_time_based = wtype in TIME_BASED_WORKOUT_TYPES
    is_interval = bool(workout.get("is_interval"))
    total_dist_m = workout.get("distance") or 0
    total_time_s = (workout.get("time") or 0) // 10
    primary_unit_default = "s" if is_time_based else "m"

    modal_state = hd.state(
        initialized_for=None,
        open_seq=0,  # bumps on every fresh open — used as a scope key so
        # Shoelace components (radio_group) pick up externally-set values.
        values=[],
        error="",
    )

    # When the dialog is closed, drop the init sentinel so the next open
    # re-syncs the working state from the currently-applied values.
    if not dialog.opened:
        if modal_state.initialized_for is not None:
            modal_state.initialized_for = None
        return

    # First render while open (or workout switch): seed the working chips
    # from current state.  For interval workouts the saved spec may be in
    # the legacy ``{mode: n_pieces|absolute, …}`` shape — expand it to a
    # chip list using the first work interval, since the modal now edits
    # a single chip pattern that's applied per interval.
    if modal_state.initialized_for != workout_id:
        modal_state.initialized_for = workout_id
        # Bump the open counter so positional Shoelace components scoped by
        # it get recreated with the freshly-seeded prop values.
        modal_state.open_seq = int(modal_state.open_seq or 0) + 1
        modal_state.error = ""
        if is_interval:
            intervals = (workout.get("workout") or {}).get("intervals") or []
            modal_state.values = _expand_sub_spec_to_chips(
                current_sub_spec, intervals
            )
        else:
            modal_state.values = list(current_values or [])

    with dialog:
        with hd.box(gap=1):
            if is_interval:
                _interval_sub_splits_tab(
                    workout=workout,
                    modal_state=modal_state,
                )
            else:
                _splits_tab(
                    workout=workout,
                    modal_state=modal_state,
                    total_dist_m=total_dist_m,
                    total_time_s=total_time_s,
                    primary_unit_default=primary_unit_default,
                )

            _splits_footer(
                dialog=dialog,
                modal_state=modal_state,
                workout=workout,
                total_dist_m=total_dist_m,
                total_time_s=total_time_s,
                on_apply=on_apply,
                on_close=on_close,
            )


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def _splits_tab(
    workout: dict,
    modal_state,
    total_dist_m: int,
    total_time_s: int,
    primary_unit_default: str,
) -> None:
    """The Splits editor body: workout summary, "Divide into…" presets,
    sum indicator, always-visible chip editor."""
    is_time_based = primary_unit_default == "s"

    # Workout total readout.
    total_label = (
        format_mmss(total_time_s) if is_time_based else f"{total_dist_m:,}m"
    )
    secondary_label = ""
    if is_time_based and total_dist_m:
        secondary_label = f" · {total_dist_m:,}m"
    elif not is_time_based and total_time_s:
        secondary_label = f" · {format_mmss(total_time_s)}"
    hd.text(
        f"Workout total: {total_label}{secondary_label}",
        font_size="small",
        font_color="neutral-600",
    )

    # Three preset dropdowns.  Each shortcut overwrites the chip editor
    # below, which always remains the source of truth.
    _divide_into_row(
        modal_state=modal_state,
        total_dist_m=total_dist_m,
        total_time_s=total_time_s,
        primary_unit_default=primary_unit_default,
    )

    # Sum indicator.
    with hd.box(padding_top=0.25, gap=0.25):
        ok, msg = _validate_sum(
            modal_state.values, total_dist_m, total_time_s
        )
        hd.text(
            msg,
            font_color="success" if ok else "warning-600",
            font_size="x-small",
        )

    # Always-visible chip editor.
    _advanced_chip_editor(modal_state, primary_unit_default)


def _divide_into_row(
    modal_state,
    total_dist_m: int,
    total_time_s: int,
    primary_unit_default: str,
    label: str = "Divide into…",
) -> None:
    """Render the unified "Divide into…" row with three dropdowns.

    The "equal splits" target follows whatever unit dominates the current
    chip list; if the user has just switched from a 5min preset to "10
    equal splits" on a distance workout, the target stays in seconds so we
    don't accidentally sum to a totally different total.
    """
    primary = _dominant_unit(modal_state.values, primary_unit_default)
    equal_target = total_time_s if primary == "s" else total_dist_m
    uniform_unit = (
        primary
        if modal_state.values
        and all(v.get("u") == primary for v in modal_state.values)
        else None
    )

    with hd.hbox(gap=0.5, align="center", padding_top=0.5, wrap="wrap"):
        hd.text(
            label,
            font_size="small",
            font_color="neutral-700",
        )

        # ── Equal splits dropdown ───────────────────────────────────────
        with hd.dropdown() as eq_dd:
            eq_label = (
                f"{len(modal_state.values)} equal splits"
                if modal_state.values and uniform_unit is not None
                else "equal splits"
            )
            eq_btn = hd.button(
                eq_label, caret=True, size="small", slot=eq_dd.trigger
            )
            if eq_btn.clicked:
                eq_dd.opened = not eq_dd.opened
            with hd.box(
                padding=0.5,
                background_color="neutral-50",
                min_width=10,
                align="start",
            ):
                for n in SPLIT_COUNT_OPTIONS:
                    with hd.scope(f"eq_{n}"):
                        is_active = (
                            uniform_unit is not None
                            and n == len(modal_state.values)
                        )
                        opt = hd.button(
                            f"{n} splits",
                            variant="text",
                            size="small",
                            font_weight="bold" if is_active else "normal",
                        )
                        if opt.clicked:
                            sizes = even_splits(equal_target, n)
                            unit_for_equal = primary
                            modal_state.values = [
                                {"u": unit_for_equal, "v": s} for s in sizes
                            ]
                            modal_state.error = ""
                            eq_dd.opened = False

        hd.text("or", font_size="x-small", font_color="neutral-500")

        # ── Distance splits dropdown ────────────────────────────────────
        active_dist_chunk = _active_chunk(modal_state.values, "m")
        with hd.dropdown() as dist_dd:
            dist_label = (
                _DISTANCE_CHUNK_LABEL.get(active_dist_chunk, "")
                + " distance splits"
                if active_dist_chunk
                else "distance splits"
            )
            dist_btn = hd.button(
                dist_label.strip(), caret=True, size="small", slot=dist_dd.trigger
            )
            if dist_btn.clicked:
                dist_dd.opened = not dist_dd.opened
            with hd.box(
                padding=0.5,
                background_color="neutral-50",
                min_width=10,
                align="start",
            ):
                for chunk, lbl in _DISTANCE_PRESETS:
                    with hd.scope(f"dpre_{chunk}"):
                        disabled = chunk > total_dist_m
                        is_active = chunk == active_dist_chunk
                        opt = hd.button(
                            f"{lbl} splits",
                            variant="text",
                            size="small",
                            disabled=disabled,
                            font_weight="bold" if is_active else "normal",
                        )
                        if opt.clicked and not disabled:
                            modal_state.values = _preset_chips(
                                total_dist_m, chunk, "m"
                            )
                            modal_state.error = ""
                            dist_dd.opened = False

        hd.text("or", font_size="x-small", font_color="neutral-500")

        # ── Time splits dropdown ────────────────────────────────────────
        active_time_chunk = _active_chunk(modal_state.values, "s")
        with hd.dropdown() as time_dd:
            time_label = (
                _TIME_CHUNK_LABEL.get(active_time_chunk, "")
                + " time splits"
                if active_time_chunk
                else "time splits"
            )
            time_btn = hd.button(
                time_label.strip(), caret=True, size="small", slot=time_dd.trigger
            )
            if time_btn.clicked:
                time_dd.opened = not time_dd.opened
            with hd.box(
                padding=0.5,
                background_color="neutral-50",
                min_width=10,
                align="start",
            ):
                for chunk, lbl in _TIME_PRESETS:
                    with hd.scope(f"tpre_{chunk}"):
                        disabled = chunk > total_time_s
                        is_active = chunk == active_time_chunk
                        opt = hd.button(
                            f"{lbl} splits",
                            variant="text",
                            size="small",
                            disabled=disabled,
                            font_weight="bold" if is_active else "normal",
                        )
                        if opt.clicked and not disabled:
                            modal_state.values = _preset_chips(
                                total_time_s, chunk, "s"
                            )
                            modal_state.error = ""
                            time_dd.opened = False


def _active_chunk(values: list, unit: str) -> Optional[int]:
    """Return the chunk size if ``values`` looks like a chunked preset for
    ``unit`` (all chips of that unit are equal except possibly the last as
    a tail).  Returns ``None`` when the list isn't a preset match.
    """
    if not values:
        return None
    same_unit = [v for v in values if v.get("u") == unit]
    if len(same_unit) != len(values):
        return None
    nums = [int(v.get("v", 0)) for v in values]
    if len(nums) == 1:
        return nums[0]
    head_chunk = nums[0]
    # All "leading" chips equal head_chunk; the tail is allowed to differ
    # (it represents the remainder).
    if not all(n == head_chunk for n in nums[:-1]):
        return None
    if nums[-1] > head_chunk:
        return None
    return head_chunk


def _interval_sub_splits_tab(workout: dict, modal_state) -> None:
    """Sub-splits editor for interval workouts.

    Mirrors the steady-state splits editor: a "Divide each interval into…"
    row with three preset dropdowns (equal pieces / distance chunks / time
    chunks), a sum indicator against the first work interval, and the
    always-visible chip editor.  The chip list is applied identically to
    each work interval.

    For uniform-length interval workouts (the common case — 5×8min,
    3×2k, …) the presets produce an exact chip pattern.  For variable
    workouts the pattern is computed against the first work interval and
    over- or under-shoots the others; users can tweak chips directly to
    compensate.
    """
    intervals = (workout.get("workout") or {}).get("intervals") or []
    work_ivs = [iv for iv in intervals if (iv.get("time") or 0) > 0]
    if not work_ivs:
        hd.text(
            "No work intervals in this workout.",
            font_color="neutral-500",
            font_size="small",
        )
        return

    first_iv = work_ivs[0]
    iv_dur_s = int(round((first_iv.get("time") or 0) / 10.0))
    iv_dist_m = int(round(first_iv.get("distance") or 0))
    iv_type = (first_iv.get("type") or "").lower()
    primary_unit = "s" if iv_type == "time" else "m"

    uniform = all(
        iv.get("time") == first_iv.get("time")
        and iv.get("distance") == first_iv.get("distance")
        for iv in work_ivs
    )
    iv_label = (
        format_mmss(iv_dur_s) if primary_unit == "s" else f"{iv_dist_m:,}m"
    )
    summary = f"{len(work_ivs)} work intervals × {iv_label}"
    if not uniform:
        summary += "  (varied — pattern is based on the first interval)"
    hd.text(summary, font_size="small", font_color="neutral-600")

    # The three "Divide…" dropdowns + chip editor read/write
    # ``modal_state.values`` — same as the steady-state splits flow.  For
    # intervals, those chips are applied to each work interval.
    _divide_into_row(
        modal_state=modal_state,
        total_dist_m=iv_dist_m,
        total_time_s=iv_dur_s,
        primary_unit_default=primary_unit,
        label="Divide each interval into…",
    )

    with hd.box(padding_top=0.25):
        ok, msg = _validate_sum(
            modal_state.values, iv_dist_m, iv_dur_s
        )
        hd.text(
            msg,
            font_color="success" if ok else "warning-600",
            font_size="x-small",
        )

    _advanced_chip_editor(modal_state, primary_unit)


def _advanced_chip_editor(modal_state, primary_unit_default: str) -> None:
    """Per-chip text + unit toggle + add/remove buttons."""
    with hd.box(
        gap=0.5,
        padding=0.75,
        border="1px solid neutral-200",
        border_radius="medium",
    ):
        with hd.hbox(gap=0.5, wrap="wrap", align="center"):
            for i, chip in enumerate(list(modal_state.values)):
                with hd.scope(f"adv_{i}"):
                    unit = chip.get("u", "m")
                    display = (
                        format_mmss(int(chip.get("v", 0)))
                        if unit == "s"
                        else str(int(chip.get("v", 0)))
                    )
                    ti = hd.text_input(value=display, width=5, size="small")
                    if ti.changed:
                        if unit == "s":
                            val, err = parse_time_input(ti.value)
                        else:
                            try:
                                val = max(1, int(ti.value))
                                err = None
                            except ValueError:
                                val = None
                                err = "Whole metres only."
                        if val is None:
                            modal_state.error = err or "Invalid input."
                        else:
                            lst = list(modal_state.values)
                            lst[i] = {"u": unit, "v": val}
                            modal_state.values = lst
                            modal_state.error = ""

                    unit_btn = hd.button(
                        unit,
                        variant="default",
                        size="small",
                        pill=True,
                        font_weight="semibold",
                    )
                    if unit_btn.clicked:
                        new_u = "s" if unit == "m" else "m"
                        lst = list(modal_state.values)
                        lst[i] = {"u": new_u, "v": int(chip.get("v", 0))}
                        modal_state.values = lst
                        modal_state.error = ""

            add_btn = hd.icon_button(
                "plus-circle", font_size="small", font_color="primary"
            )
            if add_btn.clicked:
                primary = _dominant_unit(
                    modal_state.values, primary_unit_default
                )
                default_val = 60 if primary == "s" else 500
                modal_state.values = list(modal_state.values) + [
                    {"u": primary, "v": default_val}
                ]
                modal_state.error = ""

            if len(modal_state.values) > 0:
                rem_btn = hd.icon_button(
                    "dash-circle", font_size="small", font_color="neutral-400"
                )
                if rem_btn.clicked:
                    modal_state.values = list(modal_state.values)[:-1]
                    modal_state.error = ""

        if modal_state.error:
            hd.text(modal_state.error, font_color="danger", font_size="x-small")


def _splits_footer(
    dialog,
    modal_state,
    workout: dict,
    total_dist_m: int,
    total_time_s: int,
    on_apply,
    on_close=None,
) -> None:
    """Cancel + Apply + Clear.

    The Apply payload depends on the workout type:
        * non-interval → ``{"values": [...]}``
        * interval     → ``{"sub_spec": {...}}``
    Clear sends ``{"values": None}`` or ``{"sub_spec": None}`` so the caller
    can drop the relevant entry from localStorage.
    """
    is_interval = bool(workout.get("is_interval"))

    if is_interval:
        # Validate the chip list against the first work interval's totals.
        intervals = (workout.get("workout") or {}).get("intervals") or []
        work_ivs = [iv for iv in intervals if (iv.get("time") or 0) > 0]
        if work_ivs:
            first = work_ivs[0]
            iv_dur_s = int(round((first.get("time") or 0) / 10.0))
            iv_dist_m = int(round(first.get("distance") or 0))
            ok, _msg = _validate_sum(
                modal_state.values, iv_dist_m, iv_dur_s
            )
        else:
            ok = False
    else:
        ok, _msg = _validate_sum(
            modal_state.values, total_dist_m, total_time_s
        )

    with hd.hbox(gap=0.5, justify="end", align="center", padding_top=1):
        clear_btn = hd.button("Clear", variant="text", size="small")
        hd.box(grow=True)
        cancel_btn = hd.button("Cancel", variant="text", size="small")
        apply_btn = hd.button(
            "Apply", variant="primary", size="small", disabled=not ok
        )

        if clear_btn.clicked:
            on_apply(
                {"sub_spec": None} if is_interval else {"values": None}
            )
            dialog.opened = False
            if on_close:
                on_close()
        if cancel_btn.clicked:
            modal_state.initialized_for = None  # force reset on next open
            dialog.opened = False
            # Programmatic close doesn't fire dialog.was_closed; tell the
            # caller explicitly so it can drop its trigger flag (otherwise
            # workout_page would re-open the dialog on the next render).
            if on_close:
                on_close()
        if apply_btn.clicked and ok:
            if is_interval:
                payload = {
                    "sub_spec": {
                        "mode": "custom",
                        "values": [dict(c) for c in modal_state.values],
                    }
                }
            else:
                payload = {"values": list(modal_state.values)}
            on_apply(payload)
            modal_state.initialized_for = None
            dialog.opened = False
            if on_close:
                on_close()


# ---------------------------------------------------------------------------
# Ranked events tab
# ---------------------------------------------------------------------------


def _compute_ranked_rows(
    workout: dict, strokes: list, mode: str = "best"
) -> list:
    """Build the list of ranked-event rows for ``workout``.

    Walks ``ranked_distances(machine)`` and ``ranked_times(machine)``,
    keeping each event that fits comfortably inside the workout and computing
    its metrics via the helpers in ``services.splits``.  The ``mode``
    selector picks which helper:

        * ``"best"`` (default) — the fastest contiguous segment anywhere in
          the workout, via :func:`best_window_distance` /
          :func:`best_window_time`.
        * ``"from_start"`` — the segment anchored at the beginning of the
          workout, via :func:`from_start_distance` / :func:`from_start_time`.

    Returns a list of dicts, one per surviving event::

        {
          "kind": "dist" | "time",
          "value": int,            # metres or tenths-of-seconds
          "label": "5k", "30 min", ...
          "metrics": {time_tenths, pace_tenths, watts_avg, watts_max,
                      spm, hr_avg, hr_max,
                      start_offset_tenths, end_offset_tenths},
        }
    """
    if not strokes:
        return []

    # For interval workouts the Concept2 API resets stroke t at every
    # boundary — without stitching, ``interp_time`` produces garbage as
    # soon as a window crosses an interval edge (a 100m straddling iv0→iv1
    # would compute its duration as t_iv1_first - t_iv0_last < 0, giving
    # wildly tiny times and absurd watts).  Stitch the t axis up-front so
    # the sliding window operates on a monotonic timeline.
    intervals = (workout.get("workout") or {}).get("intervals") or []
    if workout.get("is_interval") and intervals:
        strokes = stitch_interval_times(strokes, intervals)
        if not strokes:
            return []

    if mode == "from_start":
        dist_fn = from_start_distance
        time_fn = from_start_time
    else:
        dist_fn = best_window_distance
        time_fn = best_window_time

    machine = workout_machine(workout)
    rows: list = []

    for d, label in ranked_distances(machine):
        m = dist_fn(strokes, workout, d)
        if m is None:
            continue
        rows.append(
            {"kind": "dist", "value": d, "label": label, "metrics": m}
        )

    for t, label in ranked_times(machine):
        m = time_fn(strokes, workout, t)
        if m is None:
            continue
        rows.append(
            {"kind": "time", "value": t, "label": label, "metrics": m}
        )

    return rows


def render_ranked_events_modal(
    dialog,
    workout: dict,
    strokes: Optional[list],
    *,
    on_close=None,
) -> None:
    """Render the ranked-events viewer into the (open) ``dialog``.

    This is a pure read-only view of the best contiguous segments for each
    ranked distance and time that fits inside the workout.  Lazy-computed
    on first open per workout and cached on the modal's local state so the
    O(N·events) sweep doesn't repeat on every re-render.

    The view is its own modal (rather than a tab inside the splits editor)
    because it is informational, not editable.
    """
    state = hd.state(
        ranked_for=None, ranked_rows=None, mode="best", mode_workout_id=None
    )

    if not dialog.opened:
        return

    # Reset to the default mode when the user switches workouts so each
    # workout starts in "Best segment" rather than inheriting a stale toggle.
    if state.mode_workout_id != workout["id"]:
        state.mode = "best"
        state.mode_workout_id = workout["id"]

    cache_key = (workout["id"], state.mode)
    if state.ranked_for != cache_key:
        state.ranked_rows = _compute_ranked_rows(
            workout, strokes or [], mode=state.mode
        )
        state.ranked_for = cache_key

    with dialog:
        with hd.box(gap=1):
            with hd.hbox(gap=0.5, align="center"):
                rg = radio_group(value=state.mode, size="small")
                with rg:
                    hd.radio_button("Best segment", value="best")
                    hd.radio_button("From start", value="from_start")
                if rg.changed:
                    state.mode = rg.value

            if state.mode == "from_start":
                hd.text(
                    "Time and metrics starting from the beginning of the "
                    "workout, for each ranked distance and time it reaches.",
                    font_size="small",
                    font_color="neutral-600",
                )
            else:
                hd.text(
                    "Best contiguous segment for each ranked distance and "
                    "time that fits inside this workout.",
                    font_size="small",
                    font_color="neutral-600",
                )
            if not strokes:
                hd.text(
                    "Stroke-level data isn't available for this workout.",
                    font_color="neutral-500",
                    font_size="small",
                )
            elif not (state.ranked_rows or []):
                hd.text(
                    "No ranked events fit inside this workout.",
                    font_color="neutral-500",
                    font_size="small",
                )
            else:
                _ranked_events_table(state.ranked_rows)

            with hd.hbox(gap=0.5, justify="end", align="center", padding_top=1):
                close_btn = hd.button(
                    "Close", variant="primary", size="small"
                )
                if close_btn.clicked:
                    dialog.opened = False
                    if on_close:
                        on_close()


# Column widths chosen to match the rest of the splits/intervals chrome.
_RANKED_COLS = {
    "event": 5.5,
    "time": 5.5,
    "pace": 5.0,
    "watts": 6.5,
    "spm": 3.0,
    "hr": 6.0,
    "start": 5.0,
}


def _ranked_events_table(rows: list) -> None:
    """Render a flat table with one row per ranked event."""
    border = "1px solid neutral-200"
    headers = [
        ("Event", _RANKED_COLS["event"]),
        ("Time", _RANKED_COLS["time"]),
        ("Pace", _RANKED_COLS["pace"]),
        ("Watts", _RANKED_COLS["watts"]),
        ("SPM", _RANKED_COLS["spm"]),
        ("HR", _RANKED_COLS["hr"]),
        ("Start", _RANKED_COLS["start"]),
    ]

    with hd.box(border=border, border_radius="medium"):
        with hd.hbox(
            padding=(0.35, 0.75, 0.35, 0.75),
            background_color="neutral-50",
            border_bottom=border,
            gap=0.5,
        ):
            for label, width in headers:
                with hd.scope(f"h_{label}"):
                    hd.text(
                        label,
                        font_color="neutral-500",
                        font_size="x-small",
                        font_weight="semibold",
                        width=width,
                    )

        for i, row in enumerate(rows):
            with hd.scope(i):
                _ranked_event_row(row)


def _ranked_event_row(row: dict) -> None:
    m = row["metrics"]
    label = row["label"]
    time_tenths = m.get("time_tenths")
    pace_tenths_v = m.get("pace_tenths")
    watts_avg = m.get("watts_avg")
    watts_max = m.get("watts_max")
    spm = m.get("spm")
    hr_avg = m.get("hr_avg")
    hr_max = m.get("hr_max")
    start_tenths = m.get("start_offset_tenths") or 0

    if watts_avg is None:
        watts_str = "—"
    elif watts_max is not None:
        watts_str = f"{watts_avg} / {int(round(watts_max))}"
    else:
        watts_str = str(watts_avg)

    if hr_avg is None:
        hr_str = "—"
    elif hr_max:
        hr_str = f"{hr_avg:.0f} / {hr_max:.0f}"
    else:
        hr_str = f"{hr_avg:.0f}"

    cells = [
        (label, _RANKED_COLS["event"]),
        (format_time(int(round(time_tenths))) if time_tenths else "—",
         _RANKED_COLS["time"]),
        (fmt_split(pace_tenths_v), _RANKED_COLS["pace"]),
        (watts_str, _RANKED_COLS["watts"]),
        (f"{spm:.0f}" if spm else "—", _RANKED_COLS["spm"]),
        (hr_str, _RANKED_COLS["hr"]),
        (format_time(int(round(start_tenths))) if start_tenths else "0:00.0",
         _RANKED_COLS["start"]),
    ]

    with hd.hbox(
        padding=(0.35, 0.75, 0.35, 0.75),
        gap=0.5,
        align="center",
    ):
        for idx, (val, width) in enumerate(cells):
            with hd.scope(f"c_{idx}"):
                hd.text(val, font_size="small", width=width)
