"""
Formatting helpers and the shared result-table renderer for ranked workouts.

Exported:
  _MACHINE_LABELS     — dict mapping machine type strings to display labels
  _fmt_date()         — ISO date string → "Mon DD, YYYY"
  _fmt_split()        — tenths-of-a-second → "M:SS.t"
  _pace_tenths()      — compute pace tenths from a workout dict
  _fmt_distance()     — meters → "N,NNNm"
  _fmt_hr()           — heart-rate dict → "NNN bpm"
  _machine_label()    — machine type string → human label
  _fmt_watts()        — compute and format watts from a workout dict
  result_table()      — HyperDiv data-table renderer; used by both tab views
"""

from datetime import datetime

import hyperdiv as hd

from services.rowing_utils import compute_pace, compute_watts, format_time

# ---------------------------------------------------------------------------
# Machine type labels
# ---------------------------------------------------------------------------

_MACHINE_LABELS = {
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


def _fmt_date(date_str: str) -> str:
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return date_str[:10] if date_str else "—"


def fmt_split(tenths) -> str:
    """Tenths-of-a-second → M:SS.t string."""
    if not tenths:
        return "—"
    total = tenths / 10
    m = int(total // 60)
    s = total % 60
    return f"{m}:{s:04.1f}"


def _pace_tenths(r: dict):
    """
    Compute pace in tenths-of-a-second per 500m from a workout dict.
    Returns None if time or distance are unavailable.
    """
    t = r.get("time")
    d = r.get("distance")
    if not t or not d:
        return None
    return t * 500 / d


def _fmt_distance(meters) -> str:
    if not meters:
        return "—"
    return f"{meters:,}m"


def _fmt_hr(hr) -> str:
    if not hr or not isinstance(hr, dict):
        return "—"
    avg = hr.get("average")
    return f"{avg} bpm" if avg else "—"


def _machine_label(type_str: str) -> str:
    return _MACHINE_LABELS.get(type_str, type_str.capitalize() if type_str else "—")


def _fmt_watts(r: dict) -> str:
    pace = compute_pace(r)
    if pace is None:
        return "—"
    return str(round(compute_watts(pace)))


# ---------------------------------------------------------------------------
# Shared result table renderer
# ---------------------------------------------------------------------------


def result_table(results: list, *, paginate: bool = True) -> None:
    """Render a data table of workout results. Used by both tab views."""
    types = {r.get("type") for r in results}
    cols: dict = {"Date": tuple(_fmt_date(r.get("date", "")) for r in results)}
    if len(types) > 1:
        cols["Type"] = tuple(_machine_label(r.get("type", "")) for r in results)
    cols["Distance"] = tuple(_fmt_distance(r.get("distance")) for r in results)
    cols["Time"] = tuple(r.get("time_formatted") or (format_time(r["time"]) if r.get("time") else "—") for r in results)
    cols["Pace /500m"] = tuple(fmt_split(_pace_tenths(r)) for r in results)
    cols["Watts"] = tuple(_fmt_watts(r) for r in results)
    cols["Drag Factor"] = tuple(str(r.get("drag_factor") or "—") for r in results)
    cols["SPM"] = tuple(str(r.get("stroke_rate") or "—") for r in results)
    cols["Heart Rate"] = tuple(_fmt_hr(r.get("heart_rate")) for r in results)
    hd.data_table(cols, rows_per_page=25 if paginate else len(results) or 1)
