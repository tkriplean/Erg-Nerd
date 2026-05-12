"""
services/splits.py — Shared helpers for the Workout-Page custom-splits feature.

Centralises the pure-Python primitives consumed by:

  * components/workout_splits_modal.py  (the splits editor / ranked-events tab)
  * components/workout_page.py          (splits table; chart wiring)
  * components/workout_chart_builder.py (chart annotation bands)

By living in ``services/`` this module is free of HyperDiv imports and breaks
the circular dependency that used to require ``workout_chart_builder`` to do
``from components.workout_page import _build_interp, _format_mmss`` lazily
inside a function body.

Public surface
--------------
Constants:
    DEFAULT_SPLIT_COUNT, SPLIT_COUNT_OPTIONS, TIME_BASED_WORKOUT_TYPES

Pure functions:
    format_mmss(seconds) -> str
    parse_time_input(text) -> (seconds, err)
    even_splits(total, n) -> list[int]
    legacy_splits_to_values(custom_splits) -> list[dict] | None
    build_interp(strokes, workout) -> (interp_time, interp_distance)
    recalculate_splits(strokes, workout, values) -> list[SplitRow]

The "values" list is the mixed-unit chip shape used by the new modal::

    [{"u": "m", "v": 1000}, {"u": "s", "v": 120}, ...]

Each chip carries its own unit so a single splits list may interleave
distance- and time-based segments.  All-distance and all-time lists are
the common case; mixed lists are an advanced affordance.

Stroke convention (Concept2 API)
--------------------------------
A stroke dict has::

    t   — elapsed time in tenths of a second
    d   — elapsed distance in decimeters
    p   — pace tenths-of-a-second per 500m (compute_watts wants sec/500m)
    spm — strokes per minute
    hr  — heart rate (bpm) or 0/None when no HR strap
"""

from __future__ import annotations

from typing import Optional

from services.rowing_utils import compute_watts
from services.stroke_utils import ensure_raw_stroke_origin


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SPLIT_COUNT = 5
SPLIT_COUNT_OPTIONS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 16, 20, 21, 42)

# Concept2 workout_type values whose total is measured in time, not distance.
TIME_BASED_WORKOUT_TYPES = {"FixedTimeSplits"}


# ---------------------------------------------------------------------------
# Small formatting / parsing helpers
# ---------------------------------------------------------------------------


def format_mmss(seconds: int) -> str:
    """Integer seconds → ``"M:SS"`` (e.g. 90 → ``"1:30"``)."""
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def parse_time_input(text: str):
    """Parse user-entered chip text into integer seconds.

    Accepts bare integer seconds (``"90"``) or ``M:SS`` (``"1:30"``).  The
    seconds portion of an ``M:SS`` value must be two digits — ``"1:5"`` is
    rejected because it could mean 65s or 105s.

    Returns ``(seconds, None)`` on success or ``(None, error_message)`` on
    failure.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Enter a duration."
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None, 'Use "M:SS" format.'
        m_str, s_str = parts
        if len(s_str) != 2:
            return (
                None,
                'Use seconds ("90") or M:SS ("1:30"). '
                f'"{raw}" is ambiguous — write "{m_str}:{s_str.zfill(2)}".',
            )
        try:
            m_val = int(m_str)
            s_val = int(s_str)
        except ValueError:
            return None, f'Could not parse "{raw}" as M:SS.'
        if m_val < 0 or s_val < 0 or s_val >= 60:
            return None, f'"{raw}" is out of range.'
        total = m_val * 60 + s_val
        if total <= 0:
            return None, "Duration must be positive."
        return total, None
    try:
        v = int(raw)
    except ValueError:
        return None, 'Use seconds ("90") or M:SS ("1:30").'
    if v <= 0:
        return None, "Duration must be positive."
    return v, None


def even_splits(total: int, n: int) -> list:
    """Divide ``total`` into ``n`` integer splits as evenly as possible.

    The remainder is distributed onto the trailing splits (each gets +1) so
    the list sums exactly to ``total``.  Example::

        even_splits(5001, 5) → [1000, 1000, 1000, 1000, 1001]
    """
    if n <= 0 or total <= 0:
        return []
    base = total // n
    rem = total - base * n
    return [base + (1 if i >= n - rem else 0) for i in range(n)]


def legacy_splits_to_values(custom_splits: Optional[dict]) -> Optional[list]:
    """Convert the legacy ``{unit, values:[int,...]}`` shape to the new
    mixed-unit chip list ``[{u, v}, ...]``.

    Used at the seam while ``state.custom_splits`` still carries the legacy
    shape — Phase 1 of the modal redesign will write the new shape directly
    and this adapter will become a no-op fallback for pre-existing
    localStorage values.

    Returns ``None`` when the input is falsy or carries no values.
    """
    if not isinstance(custom_splits, dict):
        return None
    raw = custom_splits.get("values") or []
    if not raw:
        return None
    unit = custom_splits.get("unit", "m")
    out = []
    for v in raw:
        try:
            out.append({"u": unit, "v": int(v)})
        except (TypeError, ValueError):
            continue
    return out or None


# ---------------------------------------------------------------------------
# Stroke time stitching (for interval workouts)
# ---------------------------------------------------------------------------


def stitch_interval_times(
    strokes: list, intervals: Optional[list] = None
) -> list:
    """Return a copy of ``strokes`` with ``t`` made monotonically increasing.

    Thin wrapper over :func:`stitch_with_starts` for callers that only need
    the stitched list.  See that function for the full algorithm.
    """
    stitched, _ = stitch_with_starts(strokes, intervals)
    return stitched


def stitch_with_starts(
    strokes: list, intervals: Optional[list] = None
) -> tuple:
    """Stitch interval-reset stroke t values + return per-interval start anchors.

    The Concept2 API resets stroke ``t`` to 0 at the start of each work
    interval (rest strokes continue counting up from where the prior work
    strokes left off — exactly one backward jump per interval boundary).
    At each jump we advance the offset by the full canonical duration of
    the interval that just ended (work + rest), so the last stroke before
    a boundary isn't compressed against the boundary itself.  Falls back
    to accumulating ``prev_t`` if interval metadata is absent or
    exhausted.

    Returns ``(stitched_strokes, interval_starts)`` where
    ``interval_starts[i]`` is the ``(stitched_t_tenths, d_dm)`` pair of
    the first stroke of the i-th *work* interval (i=0 for the workout's
    opening interval).  Sub-split callers anchor each interval's window
    directly off these so a missing/zero ``rest_time`` in the workout
    metadata doesn't desynchronise the windows from where the strokes
    actually reset.

    Anomalous backward-t noise is filtered upstream in
    ``concept2.get_strokes`` — every backward jump here is a genuine
    section reset.
    """
    if not strokes:
        return [], []

    strokes = ensure_raw_stroke_origin(strokes)

    result: list = []
    starts: list = []  # (stitched_t, d_dm) per work interval
    offset = 0
    prev_t = 0
    interval_idx = 0  # canonical interval whose duration was just advanced
    for i, s in enumerate(strokes):
        t = s.get("t", 0)
        if i == 0:
            # The very first stroke starts work interval 0.
            starts.append((0, s.get("d", 0)))
        elif t < prev_t:
            # Interval boundary.  Advance the offset by whichever is larger:
            # the canonical metadata duration, or the actual last-stroke t
            # we just saw.  Using the larger of the two protects us against
            # two opposing failure modes — metadata that omits a rest
            # period (where the stream's prev_t is the truth) and a stream
            # that ends a few tenths shy of the canonical boundary (where
            # the metadata avoids compressing successive intervals).
            advance_meta = 0
            if intervals and interval_idx < len(intervals):
                iv = intervals[interval_idx]
                advance_meta = (iv.get("time") or 0) + (iv.get("rest_time") or 0)
            offset += max(advance_meta, prev_t)
            interval_idx += 1
            starts.append((t + offset, s.get("d", 0)))
        prev_t = t
        stitched = dict(s)
        stitched["t"] = t + offset
        result.append(stitched)
    return result, starts


# ---------------------------------------------------------------------------
# Stroke interpolation
# ---------------------------------------------------------------------------


def build_interp(strokes: list, workout: dict):
    """Return ``(interp_time, interp_distance)`` callables over the stroke
    stream, extended with a synthetic sentinel at the workout's totals.

    A synthetic final entry at ``(total_distance, total_time)`` is appended
    when the last real stroke tails short of either total.  This guarantees
    that ``interp_time(total_distance) == total_time`` and
    ``interp_distance(total_time) == total_distance``, so split sums
    reconcile to the workout totals.

    The synthetic sentinel is used for interpolation only; callers that need
    per-stroke aggregations (SPM, HR, watts averages) should iterate the
    original ``strokes`` list, not the helpers' internal arrays.
    """
    if not strokes:
        return None, None

    total_d_dm = (workout.get("distance") or 0) * 10  # decimetres
    total_t_tenths = workout.get("time") or 0  # tenths of seconds

    last = strokes[-1]
    last_d_dm = last.get("d", 0)
    last_t = last.get("t", 0)
    need_synth = (total_d_dm > 0 and last_d_dm < total_d_dm - 5) or (
        total_t_tenths > 0 and last_t < total_t_tenths - 1
    )

    extended = list(strokes)
    if need_synth:
        synth_d = max(total_d_dm, last_d_dm)
        synth_t = max(total_t_tenths, last_t)
        extended.append({"d": synth_d, "t": synth_t})

    d_m = [s.get("d", 0) / 10.0 for s in extended]
    t_t = [s.get("t", 0) for s in extended]

    def interp_time(target_m: float):
        if target_m <= 0:
            return 0.0
        if target_m >= d_m[-1]:
            return float(t_t[-1])
        lo, hi = 0, len(d_m) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if d_m[mid] < target_m:
                lo = mid
            else:
                hi = mid
        span = d_m[hi] - d_m[lo]
        if span <= 0:
            return float(t_t[lo])
        frac = (target_m - d_m[lo]) / span
        return t_t[lo] + frac * (t_t[hi] - t_t[lo])

    def interp_distance(target_t: float):
        if target_t <= 0:
            return 0.0
        if target_t >= t_t[-1]:
            return float(d_m[-1])
        lo, hi = 0, len(t_t) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if t_t[mid] < target_t:
                lo = mid
            else:
                hi = mid
        span = t_t[hi] - t_t[lo]
        if span <= 0:
            return float(d_m[lo])
        frac = (target_t - t_t[lo]) / span
        return d_m[lo] + frac * (d_m[hi] - d_m[lo])

    return interp_time, interp_distance


# ---------------------------------------------------------------------------
# Split recalculation
# ---------------------------------------------------------------------------


def recalculate_splits(
    strokes: list,
    workout: dict,
    values: Optional[list],
    *,
    start_t: float = 0.0,
    start_m: float = 0.0,
) -> list:
    """Interpolate stroke data to compute split metrics at chip boundaries.

    ``values`` is the mixed-unit chip list::

        [{"u": "m"|"s", "v": int}, ...]

    Chips are consumed in order.  Each chip's start position is the end
    position of the previous chip (along whichever axis the previous chip
    used); chip i's end position is start + v along chip i's own axis.
    Cross-axis boundaries are reconciled through ``build_interp`` so an
    ``"m"`` chip following an ``"s"`` chip lines up at the same point in
    the workout regardless of which axis named it.

    ``start_t`` (tenths) and ``start_m`` (metres) seed the cursor — used
    for sub-splits scoped to an interval's window inside the larger
    workout.  Both default to 0 (whole-workout splits).

    Returns a list of dicts:
        ``{distance, time_tenths, pace_tenths, spm, hr_avg, hr_max, max_watts}``
    """
    if not strokes or not values:
        return []

    interp_time, interp_distance = build_interp(strokes, workout)
    if interp_time is None:
        return []

    d_m_real = [s.get("d", 0) / 10.0 for s in strokes]
    t_t_real = [s.get("t", 0) for s in strokes]

    def strokes_in_d_range(lo_m: float, hi_m: float) -> list:
        return [s for s, dm in zip(strokes, d_m_real) if lo_m <= dm <= hi_m]

    def strokes_in_t_range(lo_t: float, hi_t: float) -> list:
        return [s for s, tt in zip(strokes, t_t_real) if lo_t <= tt <= hi_t]

    result: list = []
    cursor_t = float(start_t)  # cumulative time in tenths
    cursor_m = float(start_m)  # cumulative distance in metres

    for chip in values:
        unit = chip.get("u", "m")
        try:
            v = int(chip.get("v", 0))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue

        if unit == "s":
            start_t = cursor_t
            end_t = cursor_t + v * 10
            end_m = interp_distance(end_t)
            dist_m = max(0.0, end_m - cursor_m)
            dur_tenths = end_t - start_t
            window = strokes_in_t_range(start_t, end_t)
        else:
            start_m = cursor_m
            end_m = cursor_m + v
            end_t = interp_time(end_m) or 0.0
            dist_m = float(v)
            dur_tenths = end_t - cursor_t
            window = strokes_in_d_range(start_m, end_m)

        spm_vals = [s.get("spm") for s in window if s.get("spm")]
        hr_vals = [s.get("hr") for s in window if s.get("hr")]
        pace_tenths_v = (dur_tenths * 500.0 / dist_m) if dist_m > 0 else None
        max_w = None
        if window:
            wl = [
                compute_watts(s["p"] / 10.0)
                for s in window
                if s.get("p") and s["p"] > 0
            ]
            if wl:
                max_w = max(wl)

        result.append(
            {
                "distance": dist_m,
                "time_tenths": dur_tenths,
                "pace_tenths": pace_tenths_v,
                "spm": (sum(spm_vals) / len(spm_vals)) if spm_vals else None,
                "hr_avg": (sum(hr_vals) / len(hr_vals)) if hr_vals else None,
                "hr_max": max(hr_vals) if hr_vals else None,
                "max_watts": max_w,
            }
        )
        cursor_t = end_t
        cursor_m = end_m

    return result


# ---------------------------------------------------------------------------
# Interval sub-splits
# ---------------------------------------------------------------------------


def recalculate_interval_sub_splits(
    strokes: list,
    workout: dict,
    intervals: list,
    interval_sub: Optional[dict],
) -> list:
    """Compute sub-split rows for each work interval per the ``interval_sub`` spec.

    Walks ``intervals`` in the same order as
    ``components.workout_chart_builder.build_interval_rows_and_bands``
    (matching its ``_iter_valid_intervals`` filter), producing one entry
    per band — work intervals carry a list of sub-split row dicts, rest
    intervals carry ``[]``.  The shape of each sub-split row matches
    ``recalculate_splits``'s output.

    ``interval_sub`` is one of::

        {"mode": "n_pieces", "n": 4}
        {"mode": "absolute", "unit": "m"|"s", "value": 500}
        {"mode": "custom", "values": [{u:"m"|"s", v:int}, ...]}

    In "n_pieces" mode the interval is divided into N equal pieces along
    its primary dimension (distance for ``iv.type == "distance"``, time for
    ``iv.type == "time"``).  In "absolute" mode the unit is honoured
    regardless of the interval's type, with a tail-split for the remainder.
    In "custom" mode the chip list is applied verbatim within each work
    interval.

    Stitches the stroke time axis internally — Concept2 resets stroke
    ``t`` to 0 at each interval boundary, which would break ``build_interp``.
    """
    if not strokes or not intervals or not interval_sub:
        return []

    stitched, starts = stitch_with_starts(strokes, intervals)
    if not stitched:
        return []

    _, interp_distance = build_interp(stitched, workout)
    if interp_distance is None:
        return []

    mode = interval_sub.get("mode", "n_pieces")
    out: list = []
    work_idx = 0  # index into `starts` — one entry per work interval

    for iv in intervals:
        t_tenths = iv.get("time") or 0
        if t_tenths <= 0:
            continue  # matches _iter_valid_intervals

        # Anchor the interval's window at the actual stroke-reset point
        # captured during stitching.  Robust against the workout dict
        # carrying a missing/zero ``rest_time`` (the stream's reset is
        # what actually shifts the t axis between intervals).
        if work_idx >= len(starts):
            # Strokes don't cover this interval — emit empty band entries
            # so the row/band alignment in the caller is preserved.
            out.append([])
            if (iv.get("rest_time") or 0) > 0:
                out.append([])
            work_idx += 1
            continue

        start_t_tenths, start_d_dm = starts[work_idx]
        work_idx += 1
        start_t = float(start_t_tenths)
        start_m = start_d_dm / 10.0
        end_t = start_t + t_tenths
        end_m = interp_distance(end_t)
        interval_dur_s = int(round(t_tenths / 10.0))
        interval_dist_m = int(round(max(0.0, end_m - start_m)))

        iv_type = (iv.get("type") or "").lower()
        primary_unit = "s" if iv_type == "time" else "m"
        primary_target = (
            interval_dur_s if primary_unit == "s" else interval_dist_m
        )

        values: list = []
        if mode == "n_pieces":
            n = int(interval_sub.get("n", 4) or 0)
            if n > 0 and primary_target > 0:
                sizes = even_splits(primary_target, n)
                values = [{"u": primary_unit, "v": s} for s in sizes]
        elif mode == "custom":
            raw_values = interval_sub.get("values") or []
            for chip in raw_values:
                if not isinstance(chip, dict):
                    continue
                u = chip.get("u")
                if u not in ("m", "s"):
                    continue
                try:
                    v = int(chip.get("v", 0))
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    values.append({"u": u, "v": v})
        else:
            unit = interval_sub.get("unit", "m")
            try:
                value = int(interval_sub.get("value", 0) or 0)
            except (TypeError, ValueError):
                value = 0
            target = interval_dist_m if unit == "m" else interval_dur_s
            if value > 0 and target > 0:
                full = target // value
                rem = target - full * value
                values = [{"u": unit, "v": value} for _ in range(full)]
                if rem > 0:
                    values.append({"u": unit, "v": rem})

        if values:
            sub_rows = recalculate_splits(
                stitched, workout, values, start_t=start_t, start_m=start_m
            )
        else:
            sub_rows = []

        # Pin the cumulative end of the last sub-split at the canonical
        # interval distance reported by the device.  Without this,
        # ``interp_distance`` at the interval's nominal end time can lean
        # a few tenths into the rest period when the last work stroke
        # arrives slightly before the canonical mark, inflating the last
        # sub-split by a few metres.  The chip-list / interp model can't
        # recover the rounding the device applied, so trust the dict's
        # ``iv.distance``.
        canonical_dist = float(iv.get("distance") or 0)
        if sub_rows and canonical_dist > 0:
            actual_sum = sum(sr.get("distance", 0) or 0 for sr in sub_rows)
            diff = canonical_dist - actual_sum
            if abs(diff) > 0.5:
                last = sub_rows[-1]
                new_dist = max(0.0, (last.get("distance") or 0) + diff)
                last["distance"] = new_dist
                t_t = last.get("time_tenths") or 0
                last["pace_tenths"] = (
                    (t_t * 500.0 / new_dist) if new_dist > 0 else None
                )
        out.append(sub_rows)

        rest_tenths = iv.get("rest_time") or 0
        if rest_tenths > 0:
            out.append([])

    return out


# ---------------------------------------------------------------------------
# Best contiguous segment ("ranked events" support)
# ---------------------------------------------------------------------------


def _window_metrics(window_strokes: list, dist_m: float, dur_tenths: float) -> dict:
    """Aggregate per-stroke metrics inside a window into the same shape used
    by ``recalculate_splits``, plus avg/max watts for the ranked-events table.
    """
    spm_vals = [s.get("spm") for s in window_strokes if s.get("spm")]
    hr_vals = [s.get("hr") for s in window_strokes if s.get("hr")]
    watts_vals = [
        compute_watts(s["p"] / 10.0)
        for s in window_strokes
        if s.get("p") and s["p"] > 0
    ]
    pace_tenths_v = (dur_tenths * 500.0 / dist_m) if dist_m > 0 else None
    return {
        "distance": dist_m,
        "time_tenths": dur_tenths,
        "pace_tenths": pace_tenths_v,
        "spm": (sum(spm_vals) / len(spm_vals)) if spm_vals else None,
        "hr_avg": (sum(hr_vals) / len(hr_vals)) if hr_vals else None,
        "hr_max": max(hr_vals) if hr_vals else None,
        "watts_avg": (round(compute_watts(pace_tenths_v / 10.0))
                      if pace_tenths_v else None),
        "watts_max": max(watts_vals) if watts_vals else None,
    }


def _monotonic_by_d(strokes: list) -> list:
    """Return strokes filtered to a non-decreasing ``d`` sequence.

    Concept2's sanitiser keeps the occasional backward-t stroke that's
    flagged as a genuine section reset (used for interval boundaries).
    For ranked-event computation we don't want those resets — even on
    steady-state workouts, the last-stroke ``d`` can drop to 0 if the PM5
    emits an end-marker that the sanitiser interprets as a reset.  This
    helper produces a clean monotone view so the sliding window operates
    on a sensible distance axis.
    """
    out = []
    max_d = -1
    for s in strokes:
        d = s.get("d", 0) or 0
        if d < max_d:
            continue
        max_d = d
        out.append(s)
    return out


def _monotonic_by_t(strokes: list) -> list:
    """Same as ``_monotonic_by_d`` but along the time axis."""
    out = []
    max_t = -1
    for s in strokes:
        t = s.get("t", 0) or 0
        if t < max_t:
            continue
        max_t = t
        out.append(s)
    return out


def best_window_distance(
    strokes: list, workout: dict, target_m: int
) -> Optional[dict]:
    """Return the metrics of the fastest contiguous ``target_m``-metre
    segment in ``strokes``, or None when the workout doesn't fit a window
    of that length.

    Candidate left edges are stroke positions (O(N) sweep, with one
    interp_time call per candidate → O(N log N) total).  The right edge is
    never extended past the last *real* stroke, so the synthetic sentinel
    added by ``build_interp`` doesn't fabricate impossibly-fast tails.

    Returns ``None`` when ``target_m`` is within 2m of the workout total
    (the workout header already surfaces that result) or when no usable
    window exists.
    """
    if not strokes or target_m <= 0:
        return None

    mono = _monotonic_by_d(strokes)
    if not mono:
        return None
    last_real_d_m = mono[-1].get("d", 0) / 10.0
    if target_m > last_real_d_m - 2.0:
        return None

    interp_time, _ = build_interp(mono, workout)
    if interp_time is None:
        return None

    d_m_arr = [s.get("d", 0) / 10.0 for s in mono]
    t_t_arr = [s.get("t", 0) for s in mono]

    best_window_t = float("inf")
    best_left_d = 0.0
    best_right_d = 0.0
    best_left_t = 0.0
    best_right_t = 0.0

    for i in range(len(mono)):
        left_d = d_m_arr[i]
        right_d = left_d + target_m
        if right_d > last_real_d_m + 0.5:
            break
        left_t = float(t_t_arr[i])
        right_t = interp_time(right_d)
        window_t = right_t - left_t
        if window_t < best_window_t:
            best_window_t = window_t
            best_left_d = left_d
            best_right_d = right_d
            best_left_t = left_t
            best_right_t = right_t

    if best_window_t == float("inf"):
        return None

    window_strokes = [
        s
        for s, dm in zip(mono, d_m_arr)
        if best_left_d <= dm <= best_right_d
    ]
    out = _window_metrics(
        window_strokes, dist_m=float(target_m), dur_tenths=best_window_t
    )
    out["start_offset_tenths"] = best_left_t
    out["end_offset_tenths"] = best_right_t
    return out


def _first_work_interval(intervals: list) -> Optional[dict]:
    """Return the first interval if it is a work interval (i.e. not
    ``type == "rest"``) with non-zero time and distance.  ``None`` when the
    workout opens with a rest entry or the interval lacks usable metadata.
    """
    if not intervals:
        return None
    iv = intervals[0]
    if (iv.get("type") or "").lower() == "rest":
        return None
    if (iv.get("time") or 0) <= 0 or (iv.get("distance") or 0) <= 0:
        return None
    return iv


def _build_first_interval_interp(strokes: list, first_iv: dict):
    """Build ``(interp_time, interp_distance, window_pool)`` over the strokes
    that fall inside the first work interval, with a synthetic sentinel at
    the interval's reported ``(time, distance)`` so interpolation at the
    boundary matches the per-interval pace exactly.

    ``window_pool`` is the filtered stroke list (without the sentinel) for
    use by aggregation passes that need per-stroke ``spm``/``hr``/``p``.
    """
    iv_time = first_iv.get("time") or 0  # tenths-of-seconds
    iv_dist_m = first_iv.get("distance") or 0  # metres
    pool = [s for s in strokes if (s.get("t", 0) or 0) < iv_time]
    # Anchor the last interpolation point at the reported interval totals so
    # ``interp_distance(iv_time) == iv_dist_m`` and the mirror holds.  The
    # sentinel carries ``d`` in decimetres to match the stroke schema.
    sentinel = {"d": int(round(iv_dist_m * 10)), "t": iv_time}
    interp_time, interp_distance = build_interp(
        pool + [sentinel], {"time": iv_time, "distance": iv_dist_m}
    )
    return interp_time, interp_distance, pool


def from_start_distance(
    strokes: list, workout: dict, target_m: int
) -> Optional[dict]:
    """Return the metrics of the first ``target_m`` metres of the workout,
    measured from t=0, or None when the target doesn't fit.

    Counterpart to :func:`best_window_distance` for the "from start" mode of
    the ranked-events viewer.

    For interval workouts the segment is bounded by the first work
    interval: a target larger than that interval's distance returns
    ``None``, since "from start" describes one contiguous effort and the
    workout's next interval (or its rest) breaks that contiguity.  Within
    the bound, stroke-level interpolation is used so the pace reflects
    actual rowing (e.g. a rate ladder), with a synthetic sentinel at the
    interval's reported ``(time, distance)`` so interp is exact at the
    boundary.

    For continuous workouts, falls back to stroke interpolation over the
    whole stream.
    """
    if not strokes or target_m <= 0:
        return None

    intervals = (workout.get("workout") or {}).get("intervals") or []
    if workout.get("is_interval") and intervals:
        first_iv = _first_work_interval(intervals)
        if first_iv is None:
            return None
        iv_dist_m = first_iv["distance"]
        # "From start" must fit inside the first contiguous work effort.
        if target_m > iv_dist_m:
            return None
        interp_time, _, pool = _build_first_interval_interp(strokes, first_iv)
        if interp_time is None:
            return None
        end_t = interp_time(float(target_m))
        if end_t is None or end_t <= 0:
            return None
        window_strokes = [
            s for s in pool if (s.get("d", 0) / 10.0) <= float(target_m)
        ]
    else:
        mono = _monotonic_by_d(strokes)
        if not mono:
            return None
        last_real_d_m = mono[-1].get("d", 0) / 10.0
        if target_m > last_real_d_m - 2.0:
            return None
        interp_time, _ = build_interp(mono, workout)
        if interp_time is None:
            return None
        end_t = interp_time(float(target_m))
        if end_t is None or end_t <= 0:
            return None
        window_strokes = [
            s for s in mono if (s.get("d", 0) / 10.0) <= float(target_m)
        ]

    out = _window_metrics(
        window_strokes, dist_m=float(target_m), dur_tenths=float(end_t)
    )
    out["start_offset_tenths"] = 0.0
    out["end_offset_tenths"] = float(end_t)
    return out


def from_start_time(
    strokes: list, workout: dict, target_tenths: int
) -> Optional[dict]:
    """Return the metrics of the first ``target_tenths`` tenths-of-seconds of
    the workout, measured from t=0, or None when the target doesn't fit.

    Counterpart to :func:`best_window_time` for the "from start" mode of
    the ranked-events viewer.

    For interval workouts the segment is bounded by the first work
    interval: a target larger than that interval's time returns ``None``,
    since "from start" describes one contiguous effort and the workout's
    next interval (or its rest) breaks that contiguity.  Within the bound,
    stroke-level interpolation is used so the pace reflects actual rowing
    (e.g. a rate ladder), with a synthetic sentinel at the interval's
    reported ``(time, distance)`` so interp is exact at the boundary.

    For continuous workouts, falls back to stroke interpolation over the
    whole stream.
    """
    if not strokes or target_tenths <= 0:
        return None

    intervals = (workout.get("workout") or {}).get("intervals") or []
    if workout.get("is_interval") and intervals:
        first_iv = _first_work_interval(intervals)
        if first_iv is None:
            return None
        iv_time = first_iv["time"]
        # "From start" must fit inside the first contiguous work effort.
        if target_tenths > iv_time:
            return None
        _, interp_distance, pool = _build_first_interval_interp(
            strokes, first_iv
        )
        if interp_distance is None:
            return None
        end_d = interp_distance(float(target_tenths))
        if end_d is None or end_d <= 0:
            return None
        window_strokes = [
            s for s in pool if (s.get("t", 0) or 0) <= target_tenths
        ]
    else:
        mono = _monotonic_by_t(strokes)
        if not mono:
            return None
        last_real_t = mono[-1].get("t", 0)
        if target_tenths > last_real_t - 1:
            return None
        _, interp_distance = build_interp(mono, workout)
        if interp_distance is None:
            return None
        end_d = interp_distance(float(target_tenths))
        if end_d is None or end_d <= 0:
            return None
        window_strokes = [
            s for s in mono if (s.get("t", 0) or 0) <= target_tenths
        ]

    out = _window_metrics(
        window_strokes,
        dist_m=float(end_d),
        dur_tenths=float(target_tenths),
    )
    out["start_offset_tenths"] = 0.0
    out["end_offset_tenths"] = float(target_tenths)
    return out


def best_window_time(
    strokes: list, workout: dict, target_tenths: int
) -> Optional[dict]:
    """Mirror of ``best_window_distance``, sliding a duration target.

    Returns the metrics of the longest-distance contiguous window of
    ``target_tenths`` tenths-of-seconds.  Candidate left edges are stroke
    positions; the right edge interpolates the distance reached at that
    time.  Returns ``None`` when ``target_tenths`` is within 1 tenth of
    the workout's total elapsed time.
    """
    if not strokes or target_tenths <= 0:
        return None

    mono = _monotonic_by_t(strokes)
    if not mono:
        return None
    last_real_t = mono[-1].get("t", 0)
    if target_tenths > last_real_t - 1:
        return None

    _, interp_distance = build_interp(mono, workout)
    if interp_distance is None:
        return None

    d_m_arr = [s.get("d", 0) / 10.0 for s in mono]
    t_t_arr = [s.get("t", 0) for s in mono]

    best_window_d = -1.0
    best_left_t = 0.0
    best_right_t = 0.0
    best_left_d = 0.0
    best_right_d = 0.0

    for i in range(len(mono)):
        left_t = float(t_t_arr[i])
        right_t = left_t + target_tenths
        if right_t > last_real_t + 1:
            break
        left_d = d_m_arr[i]
        right_d = interp_distance(right_t)
        window_d = right_d - left_d
        if window_d > best_window_d:
            best_window_d = window_d
            best_left_t = left_t
            best_right_t = right_t
            best_left_d = left_d
            best_right_d = right_d

    if best_window_d <= 0:
        return None

    window_strokes = [
        s
        for s, tt in zip(mono, t_t_arr)
        if best_left_t <= tt <= best_right_t
    ]
    out = _window_metrics(
        window_strokes,
        dist_m=float(best_window_d),
        dur_tenths=float(target_tenths),
    )
    out["start_offset_tenths"] = best_left_t
    out["end_offset_tenths"] = best_right_t
    return out
