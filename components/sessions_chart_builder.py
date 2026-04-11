"""
Builder and component function for the sessions pace-vs-date chart.

Public surface:
  sessions_chart(workouts)  — HyperDiv component; call from sessions_page.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime

import hyperdiv as hd

from scipy.optimize import brentq

from services.rowing_utils import (
    INTERVAL_WORKOUT_TYPES,
    RANKED_DIST_SET,
    RANKED_TIME_SET,
    compute_pace,
    compute_watts,
    get_season,
)

from services.formatters import fmt_date

from services.interval_utils import (
    wrap_parts as _wrap_parts,
    build_interval_lines as _build_interval_lines,
    interval_totals as _interval_totals,
)
from services.critical_power_model import (
    critical_power_model,
    fit_critical_power,
)
from components.sessions_chart_plugin import SessionsChart
from components.workout_table import result_table


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

# 12-colour palette (H, S%, L%).  Balanced saturation; readable on both themes.
_PALETTE = [
    (210, 75, 55),  # cornflower blue
    (18, 82, 57),  # burnt orange
    (163, 58, 44),  # seafoam teal
    (338, 68, 58),  # watermelon pink
    (44, 88, 50),  # golden amber
    (122, 54, 44),  # sage green
    (278, 54, 60),  # soft violet
    (196, 71, 50),  # sky blue
    (28, 74, 54),  # terra cotta
    (252, 60, 62),  # periwinkle
    (82, 63, 47),  # olive
    (312, 58, 57),  # mauve
]

_MS_PER_DAY = 86_400_000

# Window sizes selectable by the user.
WINDOW_OPTIONS = ("Week", "Month", "Quarter", "Year", "2 Years", "All")
_WINDOW_DAYS = {
    "Week": 7,
    "Month": 30,
    "Quarter": 91,
    # "Season": 183,
    "Year": 365,
    "2 Years": 2 * 365,
    "All": 99 * 365,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hsla(h: int, s: int, l_: int, a: float) -> str:
    return f"hsla({h},{s}%,{l_}%,{a:.2f})"


def _dot_r(meters: float) -> float:
    """Outer dot radius in px: ½ √meters."""
    return 0.25 * math.sqrt(max(0.0, meters))


def _date_to_ms(date_str: str) -> int:
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return int(dt.timestamp() * 1_000)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Outlier filter — Critical Power model
# ---------------------------------------------------------------------------

_CP_MIN_DIST_M = 500  # exclude sub-500m events from the CP fit (too sprint-y)
_OUTLIER_FACTOR = 1.75  # drop sessions whose pace > this × predicted 2k pace
_MIN_DIST_M = 500  # hard floor: anything shorter is never plotted

# Duration bracket for brentq when solving for the 2k time [seconds].
_SOLVE_T_MIN = 10.0
_SOLVE_T_MAX = 10_800.0


def _build_pb_list(workouts: list) -> list:
    """
    Collect the best non-interval performance at each ranked event and return
    a list of {'duration_s': float, 'watts': float} dicts suitable for
    fit_critical_power().

    - Distance events ≥ _CP_MIN_DIST_M: best = lowest elapsed time.
    - Timed events: best = greatest distance (highest wattage) at that duration.
    """
    bests: dict = {}  # event_key → workout dict
    for r in workouts:
        if r.get("workout_type") in INTERVAL_WORKOUT_TYPES:
            continue
        dist = r.get("distance") or 0
        time_tenths = r.get("time") or 0
        if not dist or not time_tenths:
            continue
        if dist in RANKED_DIST_SET and dist >= _CP_MIN_DIST_M:
            key = ("dist", dist)
            T = time_tenths / 10.0
            prev = bests.get(key)
            if prev is None or T < (prev["time"] / 10.0):
                bests[key] = r
        elif time_tenths in RANKED_TIME_SET:
            key = ("time", time_tenths)
            prev = bests.get(key)
            if prev is None or dist > (prev.get("distance") or 0):
                bests[key] = r

    pb_list = []
    for r in bests.values():
        pace = compute_pace(r)
        if pace is None or pace <= 0:
            continue
        time_tenths = r.get("time") or 0
        dist = r.get("distance") or 0
        # duration: for distance events use elapsed time; for timed events use
        # the event definition (both happen to be time_tenths / 10.0 here).
        duration_s = time_tenths / 10.0
        watts = compute_watts(pace)
        if duration_s > 0 and watts > 0:
            pb_list.append({"duration_s": duration_s, "watts": watts})

    return pb_list


def _predict_2k_pace_from_params(params: dict) -> float | None:
    """
    Given fitted CP params, predict the 2000m time by solving:

        (P(t) / 2.80)^(1/3) · t  =  2000

    where P(t) is the four-parameter critical_power_model.
    Returns pace in sec/500m, or None if brentq fails or pace is out of range.
    """
    Pow1, tau1, Pow2, tau2 = (
        params["Pow1"],
        params["tau1"],
        params["Pow2"],
        params["tau2"],
    )

    def _residual(t):
        P = critical_power_model(t, Pow1, tau1, Pow2, tau2)
        if P <= 0:
            return -2000.0
        return (P / 2.80) ** (1.0 / 3.0) * t - 2000.0

    try:
        t_2k = brentq(_residual, _SOLVE_T_MIN, _SOLVE_T_MAX, xtol=0.1)
    except Exception:
        return None

    pace = t_2k * 500.0 / 2000.0  # sec/500m
    return pace if 60.0 <= pace <= 420.0 else None


def _apply_outlier_filter(workouts: list) -> list:
    """
    Remove sessions that are clearly warm-ups, aborted pieces, or erroneous rows.

    Primary method — CP curve:
      1. Build a pb_list from ranked non-interval bests (distance and duration).
      2. Fit the four-parameter veloclinic model via fit_critical_power().
      3. Solve for the predicted 2k pace using the fitted curve.
      4. Drop any session whose pace > _OUTLIER_FACTOR × predicted_2k_pace.

    Fallback (CP fit unavailable — fewer than 5 ranked bests, poor R², etc.):
      Keep any session ≥ _MIN_DIST_M meters.
    """
    # Hard length floor first.
    candidates = [r for r in workouts if (r.get("distance") or 0) >= _MIN_DIST_M]

    pb_list = _build_pb_list(candidates)
    params = fit_critical_power(pb_list)
    if params is None:
        return candidates

    pace_2k = _predict_2k_pace_from_params(params)
    if pace_2k is None:
        return candidates

    cutoff = pace_2k * _OUTLIER_FACTOR
    return [r for r in candidates if (compute_pace(r) or 0.0) <= cutoff]


# ---------------------------------------------------------------------------
# Season-best detection
# ---------------------------------------------------------------------------


def compute_sb_ids(workouts: list) -> set:
    """
    Return the set of workout IDs that are a season best for their
    (season, ranked event) combination.

    Only non-interval sessions at a ranked distance (100m … marathon) or
    ranked timed duration (1 min … 1 hr) are eligible for SB status.
    """
    bests: dict = {}  # (season, event_key) → (best_pace, rid)
    for r in workouts:
        # Intervals cannot be season-bests.
        if r.get("workout_type") in INTERVAL_WORKOUT_TYPES:
            continue
        dist = r.get("distance") or 0
        time = r.get("time") or 0
        # Must be a ranked event.
        if dist in RANKED_DIST_SET:
            event_key = ("dist", dist)
        elif time in RANKED_TIME_SET:
            event_key = ("time", time)
        else:
            continue
        pace = compute_pace(r)
        if pace is None:
            continue
        season = get_season(r.get("date", ""))
        key = (season, event_key)
        rid = r.get("id")
        if key not in bests or pace < bests[key][0]:
            bests[key] = (pace, rid)
    return {rid for _, rid in bests.values() if rid is not None}


# ---------------------------------------------------------------------------
# Point preparation
# ---------------------------------------------------------------------------


def prepare_points(workouts: list, sb_ids: set) -> list:
    """
    Convert raw workout dicts into the compact point dicts expected by the JS plugin.
    Returns list sorted largest-dist-first so big dots render behind small ones.

    Each dict has:
      x         — ms timestamp
      y         — pace (sec/500m), rounded to 2dp
      r         — outer dot radius (px)  = ½√total_m
      r2        — inner fill radius (px); equals r for non-intervals
      c         — full-opacity HSLA colour string
      c33       — 33% opacity (regular dot fill)
      c25       — 25% opacity (hatch tile background, work area)
      c60       — 60% opacity (interval circle border)
      cHatch    — 60% opacity (hatch stripe colour; independent from c60)
      c70       — 70% opacity (overview in-window dots)
      ivl       — bool: is interval workout
      sb        — bool: is season best
      dist      — total meters (work + rest) — used for draw-order sort
      work_m    — work meters
      rest_m    — rest meters (0 for non-intervals)
      ivl_desc  — list[str]: one tooltip line per structural block
      rest_desc — totals summary string ("Xm work  ·  Ym rest")
      date_str  — formatted date for tooltip
      dist_str  — formatted distance for tooltip (total meters for intervals)
    """
    pts = []
    for r in workouts:
        x = _date_to_ms(r.get("date", ""))
        if not x:
            continue
        pace = compute_pace(r)
        if pace is None or not (70.0 <= pace <= 420.0):
            continue

        dist = r.get("distance") or 0
        is_ivl = r.get("workout_type") in INTERVAL_WORKOUT_TYPES
        rid = r.get("id")

        # Outer radius: ½√(total meters including rest)
        # For interval workouts r["distance"] = work meters only;
        # r["rest_distance"] = rest meters (top-level field from the API).
        if is_ivl:
            work_m = dist  # work meters only (API field)
            rest_m = r.get("rest_distance") or 0  # rest meters (top-level API field)
            total_m = work_m + rest_m
            radius = round(_dot_r(total_m), 2)
            radius2 = round(radius * (work_m / total_m), 2) if total_m > 0 else radius

            ivl_desc = _build_interval_lines(r)  # list[str], one line per block
            rest_desc = _interval_totals(round(work_m), round(rest_m))
            dist_str = f"{total_m:,}m" if total_m else ""
        else:
            work_m = dist
            rest_m = 0
            total_m = dist
            radius = round(_dot_r(dist), 2)
            radius2 = radius
            ivl_desc = []
            rest_desc = ""
            dist_str = f"{dist:,}m" if dist else ""

        # Deterministic colour from session ID
        idx = int(hashlib.md5(str(rid).encode()).hexdigest(), 16) % len(_PALETTE)
        h, s, l_ = _PALETTE[idx]

        pts.append(
            {
                "x": x,
                "y": round(pace, 2),
                "r": radius,
                "r2": radius2,
                # Colour variants — each serves a specific visual role;
                # keep them separate so they can be tuned independently.
                "c": _hsla(h, s, l_, 1.00),  # full opacity (outlines)
                "c33": _hsla(h, s, l_, 0.33),  # regular dot fill
                "c25": _hsla(h, s, l_, 0.25),  # hatch tile background (work area)
                "c60": _hsla(h, s, l_, 1.00),  # interval circle border
                "cHatch": _hsla(h, s, l_, 0.60),  # hatch stripe colour
                "c70": _hsla(h, s, l_, 0.70),  # overview in-window dots
                # Metadata
                "ivl": is_ivl,
                "sb": rid in sb_ids,
                "dist": total_m,  # used for draw-order sort
                "work_m": round(work_m),
                "rest_m": round(rest_m),
                "ivl_desc": ivl_desc,  # list[str] — one line per block
                "rest_desc": rest_desc,  # totals summary string
                "date_str": fmt_date(r.get("date", "")),
                "dist_str": dist_str,
            }
        )

    pts.sort(key=lambda p: p["dist"], reverse=True)
    return pts


# ---------------------------------------------------------------------------
# Window calculation
# ---------------------------------------------------------------------------


def window_bounds_ms(all_ms: list, window_size: str, window_end_ms: int) -> tuple:
    """
    Return (start_ms, end_ms) for the current view window.

    window_end_ms is the right edge; the left edge is derived from window_size.
    If window_end_ms is 0 (uninitialised), default to the latest session.
    """

    days = _WINDOW_DAYS.get(window_size, 183)
    window_ms = days * _MS_PER_DAY

    if not all_ms:
        now_ms = int(datetime.now().timestamp() * 1_000)
        return now_ms - window_ms, now_ms

    min_ms = min(all_ms)
    max_ms = max(all_ms)

    end_ms = window_end_ms if window_end_ms else max_ms
    # Clamp so the window stays within history
    end_ms = min(end_ms, max_ms)
    end_ms = max(end_ms, min_ms + window_ms)
    start_ms = max(end_ms - window_ms, min_ms)

    return start_ms, end_ms


def step_ms(all_ms: list, window_size: str) -> int:
    """75% of the window width — used for ◄/► button steps."""
    days = _WINDOW_DAYS.get(window_size, 183)
    return int(days * _MS_PER_DAY * 0.75)


# ---------------------------------------------------------------------------
# HyperDiv component
# ---------------------------------------------------------------------------


def sessions_chart(workouts: list) -> None:
    """
    Render the pace-vs-date focus+context chart with brush navigator,
    session filters, and an in-window workouts table.
    """
    state = hd.state(
        window_size="Season",
        window_end_ms=0,  # 0 = uninitialised → defaults to latest session
        last_change_id=0,
        filter_10k=False,
        filter_ivl="All",  # "All" | "Intervals Only" | "No Intervals"
    )

    workouts = _apply_outlier_filter(workouts)

    # ── Controls ───────────────────────────────────────────────────────────────

    with hd.hbox(gap=2, align="center", wrap="wrap", padding_bottom=1):
        with hd.scope("ws"):
            with hd.radio_group(value=state.window_size) as rg:
                hd.radio_button("Week", size="small")
                hd.radio_button("Month", size="small")
                hd.radio_button("Quarter", size="small")
                hd.radio_button("Season", size="small")
                hd.radio_button("Year", size="small")
                hd.radio_button("2 Years", size="small")
                hd.radio_button("All", size="small")

            if rg.changed:
                state.window_size = rg.value
                state.window_end_ms = 0  # snap to latest when window size changes

        with hd.scope("ivl_filter"):
            with hd.radio_group(value=state.filter_ivl) as ivl_rg:
                hd.radio_button("All", size="small")
                hd.radio_button("Intervals Only", size="small")
                hd.radio_button("No Intervals", size="small")
            if ivl_rg.changed:
                state.filter_ivl = ivl_rg.value
        with hd.scope("filter_10k"):
            cb_10k = hd.checkbox("10k+", checked=state.filter_10k)
            if cb_10k.changed:
                state.filter_10k = cb_10k.checked

    # ── Apply filters ──────────────────────────────────────────────────────────

    filtered = workouts

    if state.filter_10k:
        filtered = [
            r
            for r in filtered
            if (r.get("distance") or 0) + (r.get("rest_distance") or 0) >= 10_000
        ]

    if state.filter_ivl == "Intervals Only":
        filtered = [
            r for r in filtered if r.get("workout_type") in INTERVAL_WORKOUT_TYPES
        ]
    elif state.filter_ivl == "No Intervals":
        filtered = [
            r for r in filtered if r.get("workout_type") not in INTERVAL_WORKOUT_TYPES
        ]

    sb_ids = compute_sb_ids(filtered)
    pts = prepare_points(filtered, sb_ids)

    if not pts:
        hd.text("No sessions match the selected filters.", font_color="neutral-500")
        return

    all_ms = [p["x"] for p in pts]

    # ── Compute target window ─────────────────────────────────────────────────
    target_start, target_end = window_bounds_ms(
        all_ms, state.window_size, state.window_end_ms
    )

    # ── Plugin ────────────────────────────────────────────────────────────────
    chart = SessionsChart(
        points=pts,
        target_window_start=target_start,
        target_window_end=target_end,
        is_dark=hd.theme().is_dark,
        height="75vh",
    )

    # ── Sync window_end_ms from brush drags ───────────────────────────────────
    if chart.change_id != state.last_change_id:
        state.last_change_id = chart.change_id
        state.window_end_ms = chart.brush_end

    # ── Workouts-in-view table ────────────────────────────────────────────────
    in_window = [
        r
        for r in filtered
        if target_start <= _date_to_ms(r.get("date", "")) <= target_end
    ]
    in_window.sort(key=lambda r: r.get("date", ""), reverse=True)
    if in_window:
        with hd.box(padding=(2, 0, 0, 0)):
            hd.h3(f"Workouts in View  ({len(in_window)})")
            result_table(in_window[:250])
