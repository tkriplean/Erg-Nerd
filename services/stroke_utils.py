"""
Stroke-data helpers for the Event Race page.

All functions are pure Python — no HyperDiv, no I/O side-effects (except
fetch_strokes_batch which calls the Concept2 API and is designed to run inside
an hd.task()).

Exported:
    build_boat_label(workout, all_event_workouts) → str
        "Jan. 26th, 2019" style label (full date).

    normalize_strokes(raw_strokes) → list[dict]
        Convert Concept2 API stroke list (tenths/decimeters) to
        [{t: secs, d: meters}, …] sorted by t.

    synthesize_strokes(workout) → list[dict]
        Build synthetic [{t, d}] data from split-level information stored in
        the cached workout dict, for pieces that have no stroke-level data.

    fetch_strokes_batch(client, user_id, workouts, existing_cache) → dict
        Blocking function (call inside hd.task()).  Fetches stroke data for
        each workout not already in existing_cache; falls back to synthesis
        when the API returns an empty list.
        Returns a complete merged dict {str(id): [{t, d}, …]}.

    build_races_data(workouts, strokes_by_id, sorted_seasons) → list[dict]
        Assemble the full races payload ready for the RaceChart JS plugin.
        Each dict: {id, label, color, strokes, is_pb, season, finish_time_s}.
        Order is preserved from the input workouts list (Python caller sorts).

    season_color_hex(season, sorted_seasons) → str
        Return a CSS hex color for a season, consistent with the SEASON_PALETTE.
"""

from __future__ import annotations

import colorsys
import math
from datetime import datetime
from typing import Optional

from services.rowing_utils import SEASON_PALETTE, compute_pace


# ---------------------------------------------------------------------------
# Boat label
# ---------------------------------------------------------------------------

_MONTH_ABBR_LONG = [
    "Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
    "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.",
]


def _ordinal(n: int) -> str:
    """Return e.g. '1st', '2nd', '3rd', '26th'."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def build_boat_label(workout: dict, all_event_workouts: list[dict]) -> str:
    """
    Return a human-readable date label for a boat in the race view.

    Format: "Jan. 26th, 2019"
    """
    date_str = workout.get("date", "")
    try:
        dt = datetime.fromisoformat(date_str[:10])
    except Exception:
        return str(workout.get("id"))

    mon = _MONTH_ABBR_LONG[dt.month - 1]
    day = _ordinal(dt.day)
    year = str(dt.year)
    return f"{mon} {day}, {year}"


# ---------------------------------------------------------------------------
# Stroke normalisation
# ---------------------------------------------------------------------------


def normalize_strokes(raw_strokes: list[dict]) -> list[dict]:
    """
    Convert Concept2 API stroke records to internal format.

    Input:  [{t: tenths_of_sec, d: decimeters, p: ..., spm: ..., hr: ...}, …]
    Output: [{t: seconds (float), d: meters (float)}, …] sorted by t.

    Fields other than t and d are stripped from the races payload (they are
    retained in the raw cache via the full API response stored by
    fetch_strokes_batch).
    """
    out = []
    for s in raw_strokes:
        t_raw = s.get("t")
        d_raw = s.get("d")
        if t_raw is None or d_raw is None:
            continue
        try:
            out.append({"t": float(t_raw) / 10.0, "d": float(d_raw) / 10.0})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["t"])


# ---------------------------------------------------------------------------
# Stroke synthesis (for workouts without stroke-level data)
# ---------------------------------------------------------------------------


def synthesize_strokes(workout: dict) -> list[dict]:
    """
    Build synthetic [{t, d}] stroke data from the cached workout's split info.

    Uses the `splits` stored in workout["workout"]["splits"] — each split is
    typically 500m and carries elapsed time and distance at the split boundary.
    One interpolated interior point is added per split for smoother animation.

    Falls back to a single straight-line segment from (0,0) to (finish_t, finish_d)
    if no split data is available.
    """
    workout_data = workout.get("workout") or {}
    splits = workout_data.get("splits") or []

    total_time_s = (workout.get("time") or 0) / 10.0
    total_dist_m = workout.get("distance") or 0

    if not splits or not total_time_s:
        # Bare minimum: straight line
        return [{"t": 0.0, "d": 0.0}, {"t": total_time_s, "d": float(total_dist_m)}]

    points = [{"t": 0.0, "d": 0.0}]
    elapsed_t = 0.0
    elapsed_d = 0.0

    for split in splits:
        split_t = (split.get("time") or 0) / 10.0
        split_d = float(split.get("distance") or 0)

        if split_t <= 0 and split_d <= 0:
            continue

        end_t = elapsed_t + split_t
        end_d = elapsed_d + split_d

        # Interior midpoint
        mid_t = elapsed_t + split_t * 0.5
        mid_d = elapsed_d + split_d * 0.5
        points.append({"t": mid_t, "d": mid_d})
        points.append({"t": end_t, "d": end_d})

        elapsed_t = end_t
        elapsed_d = end_d

    # Ensure we end at the workout's actual finish
    if total_dist_m > 0 and abs(elapsed_d - total_dist_m) > 1:
        points.append({"t": total_time_s, "d": float(total_dist_m)})

    return sorted(points, key=lambda x: x["t"])


# ---------------------------------------------------------------------------
# Batch fetch (blocking — designed for hd.task())
# ---------------------------------------------------------------------------


def fetch_one_stroke(client, user_id: int, workout: dict) -> tuple[str, list]:
    """
    Fetch stroke data for a single workout.

    Designed to be called inside hd.task() so one workout is fetched per
    render cycle, allowing the caller to show a real progress bar.

    Returns (str_id, strokes_list) where strokes_list is normalised [{t,d}].
    Falls back to synthesized strokes when the API returns nothing.
    """
    wid = workout.get("id")
    if wid is None:
        return ("", [])

    if workout.get("stroke_data"):
        try:
            raw = client.get_strokes(user_id, wid)
            normalized = normalize_strokes(raw)
        except Exception:
            normalized = []
    else:
        normalized = []

    if not normalized:
        normalized = synthesize_strokes(workout)

    return (str(wid), normalized)


def fetch_strokes_batch(
    client,
    user_id: int,
    workouts: list[dict],
    existing_cache: dict,
) -> dict:
    """
    Fetch stroke data for any workout not already in existing_cache.

    Runs synchronously (blocking). Designed to be called inside hd.task() so
    the network I/O runs off the main render thread.

    Parameters
    ----------
    client        Concept2Client instance with a valid access token.
    user_id       Integer user ID for the Concept2 API call.
    workouts      List of workout dicts to ensure coverage for.
    existing_cache  Dict mapping str(workout_id) → [{t, d}] already in cache.

    Returns
    -------
    A merged dict {str(workout_id): [{t, d}]} covering every ID in `workouts`.
    Workouts already in existing_cache are returned as-is.
    Workouts whose API call returns an empty list are given synthesized data.
    """
    result = dict(existing_cache)

    for w in workouts:
        wid = w.get("id")
        if wid is None:
            continue
        key = str(wid)
        if key in result:
            continue

        if w.get("stroke_data"):
            try:
                raw = client.get_strokes(user_id, wid)
                normalized = normalize_strokes(raw)
            except Exception:
                normalized = []
        else:
            normalized = []

        if not normalized:
            normalized = synthesize_strokes(w)

        result[key] = normalized

    return result


# ---------------------------------------------------------------------------
# Season color
# ---------------------------------------------------------------------------


def season_color_hex(season: str, sorted_seasons: list[str]) -> str:
    """
    Return a CSS hex color for a season using the SEASON_PALETTE.

    sorted_seasons should be sorted chronologically (which is the same as
    lexicographic order for the "YYYY-YY" format).
    """
    idx = sorted_seasons.index(season) if season in sorted_seasons else 0
    h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
    # Convert HSL to RGB hex
    h_frac = h / 360.0
    s_frac = s / 100.0
    l_frac = l / 100.0
    r, g, b = colorsys.hls_to_rgb(h_frac, l_frac, s_frac)
    return "#{:02x}{:02x}{:02x}".format(
        round(r * 255), round(g * 255), round(b * 255)
    )


# ---------------------------------------------------------------------------
# Build races payload
# ---------------------------------------------------------------------------


def _ensure_finish_point(strokes: list[dict], finish_t: float, finish_d: float) -> list[dict]:
    """
    Guarantee that the stroke list ends at exactly (finish_t, finish_d).

    If the last stroke already reaches or exceeds those values, return as-is.
    Otherwise append a final point so JS interpolation produces the correct
    result and every boat crosses the finish line on time.
    """
    if not strokes:
        return [{"t": 0.0, "d": 0.0}, {"t": finish_t, "d": finish_d}]

    last = strokes[-1]
    # Already reaches the finish — no change needed
    if last["t"] >= finish_t and last["d"] >= finish_d:
        return strokes

    # Append the guaranteed endpoint
    return list(strokes) + [{"t": finish_t, "d": finish_d}]


def build_races_data(
    workouts: list[dict],
    strokes_by_id: dict,
    sorted_seasons: list[str],
) -> list[dict]:
    """
    Assemble the races payload for the RaceChart JS plugin.

    Parameters
    ----------
    workouts       Qualifying workout dicts for the selected event (all filtered).
                   **The order of the returned list mirrors this list exactly.**
                   Sort workouts before calling if lane order matters.
    strokes_by_id  Dict {str(id): [{t, d}]} — complete (no missing IDs).
    sorted_seasons Seasons sorted chronologically (used for color assignment).

    Returns
    -------
    List of boat dicts in the same order as `workouts`:
    [
        {
            "id":           int,
            "label":        "Jan. 26th, 2019",
            "color":        "#3a8fde",
            "strokes":      [{"t": float, "d": float}, …],
            "is_pb":        bool,
            "season":       "2025-26",
            "finish_time_s": float | None,  # official finish time (dist events only)
        },
        …
    ]

    For distance events each stroke list is guaranteed to end at the workout's
    official (time, distance) so that all boats visually cross the finish line.
    """
    if not workouts:
        return []

    # Determine event type from the first workout.
    # Timed workouts (e.g. 30-min) always carry a non-zero distance (meters rowed),
    # so we MUST check against RANKED_DIST_SET rather than truthiness of distance.
    from services.rowing_utils import RANKED_DIST_SET
    first = workouts[0]
    is_time_event = first.get("distance") not in RANKED_DIST_SET

    # Find the personal best for the event
    if is_time_event:
        pb_wkt = max(workouts, key=lambda w: w.get("distance") or 0, default=None)
    else:
        pb_wkt = min(
            (w for w in workouts if w.get("time")),
            key=lambda w: w.get("time"),
            default=None,
        )
    pb_workout_id = pb_wkt.get("id") if pb_wkt else None

    from services.rowing_utils import get_season

    boats = []
    for w in workouts:
        wid = w.get("id")
        key = str(wid) if wid is not None else None
        strokes = list(strokes_by_id.get(key, []) if key else [])

        if not strokes:
            strokes = synthesize_strokes(w)

        # Patch strokes so the last point is the official (time, distance) result.
        # For distance events this guarantees every boat crosses the finish line at
        # exactly the right moment even when stroke data falls short.
        # For time events it ensures the final distance is the authoritative one —
        # stroke data sometimes under-counts by a few metres.
        finish_time_s: Optional[float] = None
        finish_dist_m: Optional[float] = None
        raw_t = w.get("time")
        raw_d = w.get("distance")
        if raw_t and raw_d:
            finish_time_s = float(raw_t) / 10.0
            finish_dist_m = float(raw_d)
            strokes = _ensure_finish_point(strokes, finish_time_s, finish_dist_m)

        season = get_season(w.get("date", ""))
        color = season_color_hex(season, sorted_seasons)
        label = build_boat_label(w, workouts)

        boats.append(
            {
                "id": wid,
                "label": label,
                "color": color,
                "strokes": strokes,
                "is_pb": wid == pb_workout_id,
                "season": season,
                "finish_time_s": finish_time_s,  # dist events: official finish time (s)
                "finish_dist_m": finish_dist_m,  # time events: official final metres
            }
        )

    # Order is preserved — caller is responsible for sorting workouts first.
    return boats
