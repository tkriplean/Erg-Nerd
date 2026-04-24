"""
services/interval_utils.py

Pure-Python utilities for interval workout parsing, description, and pace
computation.

No HyperDiv dependency — safe to import anywhere.
Energy-system classification and breakdown-bar generation are handled by
services/volume_bins.py (workout_bin_meters, bin_bar_svg, Z1/Z2/Z3_BINS).

Exported:
  fmt_tenths(tenths)                → "M:SS" string
  wrap_parts(parts, sep, per_row)   → list of wrapped lines
  build_interval_lines(r)           → list of human-readable description lines
  interval_totals(work_m, rest_m)   → "Xm work  ·  Ym rest" footer string
  avg_workpace_tenths(r)           → tenths/500m (r["time"]*500/r["distance"])
  avg_work_spm(r)                   → work-weighted average stroke rate
  interval_structure_label(r)       → canonical one-line structure string
  interval_structure_key(r)         → structure label with leading "N × " stripped
"""

from __future__ import annotations
from services.formatters import fmt_tenths

# ---------------------------------------------------------------------------
# Formatting helpers  (moved verbatim from sessions_chart_builder.py)
# ---------------------------------------------------------------------------


def wrap_parts(parts: list[str], sep: str, per_row: int = 6) -> list[str]:
    """Join parts into rows of up to *per_row* items."""
    if len(parts) <= per_row:
        return [sep.join(parts)]
    rows = []
    for i in range(0, len(parts), per_row):
        rows.append(sep.join(parts[i : i + per_row]))
    return rows


# ---------------------------------------------------------------------------
# Pattern detection helpers
# ---------------------------------------------------------------------------


def _abbreviate_arithmetic(values: list[int], min_len: int = 5) -> list[int] | None:
    """
    If ``values`` is a long arithmetic progression (constant, non-zero step),
    return a 4-element summary [first, second, second-to-last, last] that the
    caller can render as "A–B–...–Y–Z".  Returns None otherwise, so callers
    fall back to the full list.
    """
    if len(values) < min_len:
        return None
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    if len(set(diffs)) != 1 or diffs[0] == 0:
        return None
    return [values[0], values[1], values[-2], values[-1]]


def _interval_signature(iv: dict) -> tuple:
    """Hashable signature of a single interval for super-block equality."""
    return (
        (iv.get("type") or "").lower(),
        iv.get("time") or 0,
        iv.get("distance") or 0,
        iv.get("rest_time") or 0,
    )


def _block_sig_ignore_last_rest(b: list[dict]) -> tuple:
    """Signature of a block that ignores the trailing interval's rest_time.

    This lets super-block detection treat the rest between super-reps as
    "outer" rest (which may be 0 on the very last rep) while still comparing
    the rest of the block structure.
    """
    parts = []
    for i, iv in enumerate(b):
        if i == len(b) - 1:
            parts.append(
                (
                    (iv.get("type") or "").lower(),
                    iv.get("time") or 0,
                    iv.get("distance") or 0,
                )
            )
        else:
            parts.append(_interval_signature(iv))
    return tuple(parts)


def _detect_super_block(
    blocks: list[list[dict]],
) -> tuple[int, list[list[dict]], int] | None:
    """
    If ``blocks`` is made of ``n_reps`` repetitions of a shorter sub-sequence
    (ignoring the final interval's rest_time, which is the "outer" rest
    between super-reps), return ``(n_reps, inner_blocks, outer_rest)``.

    ``outer_rest`` is the rest_time separating super-reps (0 if the workout
    had no explicit outer rest, e.g. when only the last rep terminates).

    Used to collapse e.g. 15 single-interval blocks forming 3 × (5 × …) into
    a single "N × (…)" description.
    """
    n = len(blocks)
    if n < 4:
        return None
    # Prefer the longest viable inner length so that e.g. 15 blocks forming
    # 3 × (5 × …) aren't mis-detected as 5 × (3 × …).  Skip inner_len == 1:
    # those cases are handled better by step 3a ("N × …").
    for inner_len in range(n // 2, 1, -1):
        if n % inner_len != 0:
            continue
        n_reps = n // inner_len
        if n_reps < 2:
            continue
        inner = blocks[:inner_len]
        inner_sig = tuple(_block_sig_ignore_last_rest(b) for b in inner)
        ok = True
        for rep in range(1, n_reps):
            rep_blocks = blocks[rep * inner_len : (rep + 1) * inner_len]
            rep_sig = tuple(_block_sig_ignore_last_rest(b) for b in rep_blocks)
            if rep_sig != inner_sig:
                ok = False
                break
        if not ok:
            continue
        # Determine outer rest: final interval rest_time of each rep's last block.
        # All non-final reps should share the same value; the final rep's value
        # is often 0 (workout ends) — tolerate that.
        rest_vals = []
        for rep in range(n_reps):
            last_block = blocks[(rep + 1) * inner_len - 1]
            rest_vals.append(last_block[-1].get("rest_time") or 0)
        non_final = rest_vals[:-1]
        if non_final and len(set(non_final)) != 1:
            continue
        outer_rest = non_final[0] if non_final else (rest_vals[-1] or 0)
        # Require the outer rest to be strictly longer than any rest that
        # appears within the inner pattern.  Otherwise the "super-block" is
        # spurious — e.g. 6 × 500m / 2'r would match 2 × (3 × 500m / 2'r) /
        # 2'r even though it's just six uniform reps.
        within_rests: list[int] = []
        for bi, b in enumerate(inner):
            for ii, iv in enumerate(b):
                if bi == len(inner) - 1 and ii == len(b) - 1:
                    continue  # skip the rest slot that becomes outer_rest
                within_rests.append(iv.get("rest_time") or 0)
        max_within = max(within_rests) if within_rests else 0
        if outer_rest <= max_within:
            continue
        return n_reps, inner, outer_rest
    return None


# ---------------------------------------------------------------------------
# Interval description  (moved verbatim from sessions_chart_builder.py)
# ---------------------------------------------------------------------------


def build_interval_lines(r: dict, compact: bool = False) -> list[str]:
    """
    Return a list of lines that describe the exact interval structure.

    The Concept2 API attaches rest (rest_time / rest_distance) to the interval
    that PRECEDES the rest.  An interval with rest_time == 0 flows directly into
    the next with no prescribed recovery.

    Parameters
    ----------
    compact:
        If True, use abbreviated time strings and
        shorten the rest suffix from " rest" to "r".  Intended for compact
        labels in the interval browser.  Default False preserves original
        output used by the session chart detail view.

    Algorithm
    ---------
    1.  Partition the interval list into *blocks* by scanning for rest_time > 0.
        Each such interval closes a block; the final trailing group is also a block.

    2a. If every block is a single interval:
          – uniform work + uniform rest  → "N × Xm  /  Y:TT rest"
          – variable work + uniform rest → "d1–d2–...m  /  Y:TT rest"
          – any rest == 0 or variable    → inline per-interval: "Xm/Y – Xm – Xm/Z"
            (wrap to multiple lines if there are > 6 intervals)

    2b. If any block has multiple intervals (complex structure):
          One line per block → "d1+d2+d3m  /  Y:TT" or "d1+d2+d3m" (no-rest block)

    Examples (compact=False / compact=True)
    ----------------------------------------
    "36 × 1:00  /  1:00 rest"   →  "36 × 1'  /  1'r"
    "6 × 500m  /  2:00 rest"    →  "6 × 500m  /  2'r"
    "600–500–400m  /  2:00 rest" → "600–500–400m  /  2'r"
    "800+250+200+2000m  /  8:00" → "800+250+200+2000m  /  8'"
    "4:00+3:00+0:30"             → "4'+3'+30\""
    """
    raw = (r.get("workout") or {}).get("intervals") or []
    if not raw:
        return []

    # Fold "rest"-type intervals into the preceding work interval's rest_time.
    # Some C2 workout programmings store the long recovery between super-reps
    # as a standalone rest-type interval rather than as rest on the preceding
    # work interval; without folding, that rest looks like a 4'+20"+… work
    # block and breaks super-block / ladder detection.
    intervals: list[dict] = []
    for iv in raw:
        t = (iv.get("type") or "").lower()
        if t == "rest" and intervals:
            prev = dict(intervals[-1])
            added = (iv.get("time") or 0) + (iv.get("rest_time") or 0)
            prev["rest_time"] = (prev.get("rest_time") or 0) + added
            intervals[-1] = prev
            continue
        intervals.append(iv)
    if not intervals:
        return []

    n = len(intervals)
    iv_type = (intervals[0].get("type") or "").lower()

    # Choose time formatter and rest suffix based on compact flag
    def _ft(t):
        return fmt_tenths(t, compact=compact)

    _rest_suffix = "r" if compact else " rest"

    # ── Step 1: build blocks ──────────────────────────────────────────────────
    blocks: list[list[dict]] = []
    current: list[dict] = []
    for iv in intervals:
        current.append(iv)
        if (iv.get("rest_time") or 0) > 0:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    # ── Step 2: super-block detection (e.g. 3 × (5 × 20"/10"r) / 4'r) ────────
    super_pat = _detect_super_block(blocks)
    if super_pat is not None:
        n_reps, inner_blocks, outer_rest = super_pat
        # Zero out the final interval's rest_time in the last inner block so
        # the inner rendering doesn't also claim the outer rest.
        inner_for_render = []
        for bi, b in enumerate(inner_blocks):
            if bi == len(inner_blocks) - 1:
                modified = [dict(iv) for iv in b]
                modified[-1]["rest_time"] = 0
                inner_for_render.append(modified)
            else:
                inner_for_render.append(b)
        inner_lines = _format_blocks(inner_for_render, iv_type, _ft, _rest_suffix)
        if inner_lines is not None and len(inner_lines) == 1:
            inner_str = inner_lines[0]
            if outer_rest > 0:
                return [
                    f"{n_reps} × ({inner_str})  /  " f"{_ft(outer_rest)}{_rest_suffix}"
                ]
            return [f"{n_reps} × ({inner_str})"]

    # ── Step 3a: all single-interval blocks ──────────────────────────────────
    if all(len(b) == 1 for b in blocks):
        single_lines = _format_blocks(blocks, iv_type, _ft, _rest_suffix)
        if single_lines is not None:
            return single_lines

    # ── Step 3b: at least one multi-interval block ───────────────────────────
    lines = []
    for b in blocks:
        rest_t = b[-1].get("rest_time") or 0
        if iv_type == "distance":
            work_str = "+".join(f"{iv.get('distance') or 0:,}m" for iv in b)
        else:
            work_str = "+".join(_ft(iv.get("time") or 0) for iv in b)
        lines.append(f"{work_str}  /  {_ft(rest_t)}" if rest_t else work_str)
    return lines


def _format_blocks(
    blocks: list[list[dict]],
    iv_type: str,
    _ft,
    _rest_suffix: str,
) -> list[str] | None:
    """
    Format a list of single-interval blocks as one or more description
    lines.  Handles uniform-work, variable-work (with arithmetic-progression
    abbreviation), and inline-per-interval fallbacks.  Returns None when the
    blocks aren't all single-interval (caller falls through).
    """
    if not all(len(b) == 1 for b in blocks):
        return None

    n = len(blocks)

    def _abbrev_time(vals: list[int]) -> str | None:
        a = _abbreviate_arithmetic(vals)
        if a is None:
            return None
        return f"{_ft(a[0])}–{_ft(a[1])}–...–{_ft(a[2])}–{_ft(a[3])}"

    def _abbrev_dist(vals: list[int]) -> str | None:
        a = _abbreviate_arithmetic(vals)
        if a is None:
            return None
        return f"{a[0]:,}–{a[1]:,}–...–{a[2]:,}–{a[3]:,}m"

    if iv_type == "time":
        times = [b[0].get("time") or 0 for b in blocks]
        rests = [b[0].get("rest_time") or 0 for b in blocks]
        nz = [rt for rt in rests if rt > 0]
        uniform_t = len(set(times)) == 1 and times[0] > 0
        uniform_r = len(set(nz)) == 1

        if uniform_t and uniform_r and nz:
            return [f"{n} × {_ft(times[0])}  /  {_ft(nz[0])}{_rest_suffix}"]
        if uniform_t and not nz:
            return [f"{n} × {_ft(times[0])}"]

        if uniform_r and nz:
            abbrev = _abbrev_time(times)
            if abbrev is not None:
                return [f"{abbrev}  /  {_ft(nz[0])}{_rest_suffix}"]
            time_str = "–".join(_ft(t) for t in times)
            return [f"{time_str}  /  {_ft(nz[0])}{_rest_suffix}"]
        if not nz:
            abbrev = _abbrev_time(times)
            if abbrev is not None:
                return [abbrev]
            return ["–".join(_ft(t) for t in times)]

        # Variable rest: inline per-interval
        parts = []
        for b in blocks:
            t = b[0].get("time") or 0
            rt = b[0].get("rest_time") or 0
            parts.append(f"{_ft(t)}/{_ft(rt)}" if rt else _ft(t))
        return wrap_parts(parts, sep="  –  ")

    # "distance" or unknown
    dists = [b[0].get("distance") or 0 for b in blocks]
    rests = [b[0].get("rest_time") or 0 for b in blocks]
    nz = [rt for rt in rests if rt > 0]
    uniform_d = len(set(dists)) == 1 and dists[0] > 0
    uniform_r = len(set(nz)) == 1

    if uniform_d and uniform_r and nz:
        return [f"{n} × {dists[0]:,}m  /  {_ft(nz[0])}{_rest_suffix}"]
    if uniform_d and not nz:
        return [f"{n} × {dists[0]:,}m"]

    if uniform_r and nz:
        abbrev = _abbrev_dist(dists)
        if abbrev is not None:
            return [f"{abbrev}  /  {_ft(nz[0])}{_rest_suffix}"]
        dist_str = "–".join(f"{d:,}" for d in dists) + "m"
        return [f"{dist_str}  /  {_ft(nz[0])}{_rest_suffix}"]
    if not nz:
        abbrev = _abbrev_dist(dists)
        if abbrev is not None:
            return [abbrev]
        dist_str = "–".join(f"{d:,}" for d in dists) + "m"
        return [dist_str]

    # Variable rest: inline per-interval
    parts = []
    for b in blocks:
        d = b[0].get("distance") or 0
        rt = b[0].get("rest_time") or 0
        parts.append(f"{d:,}m/{_ft(rt)}" if rt else f"{d:,}m")
    return wrap_parts(parts, sep="  –  ")


def interval_totals(work_m: int, rest_m: int) -> str:
    """Short 'Xm work  ·  Ym rest' summary string."""
    if rest_m > 0:
        return f"{work_m:,}m work  ·  {rest_m:,}m rest"
    return f"{work_m:,}m work"


# ---------------------------------------------------------------------------
# Pace computation
# ---------------------------------------------------------------------------


def avg_workpace_tenths(r: dict) -> float | None:
    """
    Average WORK pace in tenths-of-a-second per 500 m.

    For interval workouts both ``r["time"]`` (tenths) and ``r["distance"]``
    (meters) are work-only values, so the pace is simply:
        time_tenths * 500 / distance_m

    Returns None if either field is unavailable.
    """
    wt = r.get("time")
    d = r.get("distance")
    if not wt or not d:
        return None
    return wt * 500 / d


def avg_work_spm(r: dict) -> float | None:
    """
    Work-weighted average stroke rate across all work intervals.

    The top-level ``stroke_rate`` is averaged over the entire piece including
    rest (where SPM = 0), so it reads artificially low.  This function weights
    each work interval's ``spm`` by its work volume (distance if available,
    otherwise time) and returns the weighted mean.

    Returns None if no per-interval SPM data is available.
    """
    intervals = (r.get("workout") or {}).get("intervals") or []
    work_ivs = [iv for iv in intervals if (iv.get("type") or "").lower() != "rest"]

    weighted_sum = 0.0
    weight_total = 0.0
    for iv in work_ivs:
        spm = iv.get("spm") or iv.get("stroke_rate")
        if not spm:
            continue
        # Prefer distance weight; fall back to time weight
        weight = (iv.get("distance") or 0) or ((iv.get("time") or 0) / 10)
        if weight > 0:
            weighted_sum += spm * weight
            weight_total += weight

    if weight_total > 0:
        return weighted_sum / weight_total
    return None


# ---------------------------------------------------------------------------
# Structure label
# ---------------------------------------------------------------------------


def interval_structure_label(r: dict, compact: bool = False) -> str:
    """
    Return a canonical one-line structure string for display.

    Uses the first line from ``build_interval_lines()``.  Falls back to the
    ``workout_type`` field if no interval data is present.

    Pass ``compact=True`` for abbreviated labels (e.g. "36 × 1'  /  1'r"
    instead of "36 × 1:00  /  1:00 rest").
    """
    lines = build_interval_lines(r, compact=compact)
    return lines[0] if lines else (r.get("workout_type") or "Unknown")


def interval_structure_key(r: dict, compact: bool = False) -> str:
    """
    Return a grouping key for the interval structure, stripping the leading
    rep count so that e.g. "3 × 15'  /  1'r" and "4 × 15'  /  1'r" both
    map to the same key "15'  /  1'r".

    Complex/mixed structures (no "N × " prefix) are returned unchanged.
    """
    label = interval_structure_label(r, compact=compact)
    # Strip leading "N × " (e.g. "6 × 500m  /  2'r" → "500m  /  2'r")
    if " × " in label:
        _, _, rest = label.partition(" × ")
        return rest
    return label
