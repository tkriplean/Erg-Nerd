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
    intervals = (r.get("workout") or {}).get("intervals") or []
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

    # ── Step 2a: all single-interval blocks ───────────────────────────────────
    if all(len(b) == 1 for b in blocks):
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

            # Inline per-interval
            parts = []
            for b in blocks:
                t = b[0].get("time") or 0
                rt = b[0].get("rest_time") or 0
                parts.append(f"{_ft(t)}/{_ft(rt)}" if rt else _ft(t))
            return wrap_parts(parts, sep="  –  ")

        else:  # "distance" or unknown
            dists = [b[0].get("distance") or 0 for b in blocks]
            rests = [b[0].get("rest_time") or 0 for b in blocks]
            nz = [rt for rt in rests if rt > 0]
            uniform_d = len(set(dists)) == 1 and dists[0] > 0
            uniform_r = len(set(nz)) == 1

            if uniform_d and uniform_r and nz:
                return [f"{n} × {dists[0]:,}m  /  {_ft(nz[0])}{_rest_suffix}"]
            if uniform_d and not nz:
                return [f"{n} × {dists[0]:,}m"]

            dist_str = "–".join(f"{d:,}" for d in dists) + "m"
            if uniform_r and nz:
                return [f"{dist_str}  /  {_ft(nz[0])}{_rest_suffix}"]
            if not nz:
                return [dist_str]

            # Variable rest: inline per-interval
            parts = []
            for b in blocks:
                d = b[0].get("distance") or 0
                rt = b[0].get("rest_time") or 0
                parts.append(f"{d:,}m/{_ft(rt)}" if rt else f"{d:,}m")
            return wrap_parts(parts, sep="  –  ")

    # ── Step 2b: at least one multi-interval block ────────────────────────────
    lines = []
    for b in blocks:
        rest_t = b[-1].get("rest_time") or 0
        if iv_type == "distance":
            work_str = "+".join(f"{iv.get('distance') or 0:,}m" for iv in b)
        else:
            work_str = "+".join(_ft(iv.get("time") or 0) for iv in b)
        lines.append(f"{work_str}  /  {_ft(rest_t)}" if rest_t else work_str)
    return lines


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
