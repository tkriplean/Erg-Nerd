"""
Formatting helpers and the shared result-table renderer for ranked workouts.

Exported:
  MACHINE_LABELS     — dict mapping machine type strings to display labels
  fmt_date()         — ISO date string → "Mon DD, YYYY"
  fmt_split()         — tenths-of-a-second → "M:SS.t"
  pace_tenths()      — compute pace tenths from a workout dict
  fmt_distance()     — meters → "N,NNNm"
  fmt_hr()           — heart-rate dict → "NNN bpm"
  machine_label()    — machine type string → human label
  fmt_watts()        — compute and format watts from a workout dict
"""

from datetime import datetime

from services.rowing_utils import compute_pace, compute_watts

# ---------------------------------------------------------------------------
# Machine type labels
# ---------------------------------------------------------------------------

MACHINE_LABELS = {
    "rower": "Rower",
    "skierg": "SkiErg",
    "bike": "BikeErg",
    "dynamic": "Dynamic",
    "slides": "Slides",
    "paddle": "Paddle",
    "water": "Water",
    "snow": "Snow",
    "rollerski": "Roller Ski",
    "multierg": "MultiErg",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return date_str[:10] if date_str else "—"


def format_time(tenths: int) -> str:
    """
    Format a duration stored as tenths of a second into the same string the
    Concept2 API returns as 'time_formatted'.

    Examples:
        71      → '0:07.1'
        4608    → '7:40.8'
        84254   → '2:20:25.4'
    """
    t = int(tenths)
    frac = t % 10
    total_s = t // 10
    secs = total_s % 60
    total_m = total_s // 60
    mins = total_m % 60
    hours = total_m // 60
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}.{frac}"
    return f"{mins}:{secs:02d}.{frac}"


def fmt_split(tenths) -> str:
    """Tenths-of-a-second → M:SS.t string."""
    if not tenths:
        return "—"
    total = tenths / 10
    m = int(total // 60)
    s = total % 60
    return f"{m}:{s:04.1f}"


def fmt_tenths(tenths: int, compact: bool = False) -> str:
    """Convert tenths of seconds to 'M:SS' string.  E.g. 600 → '1:00'."""
    total_s = int(tenths) // 10
    mins, secs = divmod(total_s, 60)

    if compact:
        """
        Compact time string.

        Rules (chosen for brevity while remaining unambiguous):
          Whole minutes  →  "4'"       (e.g. 2400 tenths → "4'")
          Pure seconds   →  '30"'      (e.g.  300 tenths → '30"')
          Mixed          →  "1:30"     (unchanged — "1'30\"" would be harder to read)

        Examples:
          0:10  → '10"'
          0:30  → '30"'
          1:00  → "1'"
          1:30  → "1:30"
          4:00  → "4'"
          9:55  → "9:55"
        """
        if secs == 0:
            return f"{mins}'"
        if mins == 0:
            return f'{secs}"'

    return f"{mins}:{secs:02d}"


def pace_tenths(r: dict):
    """
    Compute pace in tenths-of-a-second per 500m from a workout dict.
    Returns None if time or distance are unavailable.
    """
    t = r.get("time")
    d = r.get("distance")
    if not t or not d:
        return None
    return t * 500 / d


def fmt_distance(meters) -> str:
    if not meters:
        return "—"
    return f"{meters:,}m"


def fmt_distance_label(workout: dict) -> str:
    d = workout.get("distance")
    if d:
        return fmt_distance(d)
    t = workout.get("time")
    if t:
        return format_time(t)
    return ""


def fmt_meters(m: float) -> str:
    """Format a meter count: ≥1000 → '10.5k', else '500m'."""
    v = round(m)
    if v >= 1000:
        k = v / 1000
        return (str(k) if k == int(k) else f"{k:.1f}") + "k"
    return f"{v}m"


def fmt_hr(hr) -> str:
    if not hr or not isinstance(hr, dict):
        return "—"
    avg = hr.get("average")
    return f"{avg} bpm" if avg else "—"


def machine_label(type_str: str) -> str:
    return MACHINE_LABELS.get(
        type_str.lower(), type_str.capitalize() if type_str else "—"
    )


def fmt_watts(r: dict) -> str:
    pace = compute_pace(r)
    if pace is None:
        return "—"
    return str(round(compute_watts(pace)))
