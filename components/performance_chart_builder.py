"""
Chart configuration builder for the ranked-workouts view.

Exported:
  build_sb_annotations() — DateSlider timeline dot annotations (one per season SB)
  ol_event_line()        — format the first overlay-label line ("Event  time-or-dist")
  pcts()                 — compute (pct_pace, pct_watts) improvement between two paces
  compute_lifetime_bests() — derive lifetime-best dicts from a raw workout list
  build_chart_config()   — build the full Chart.js config dict

All satellite helpers (_season_hsla, _pred_dataset, etc.) are private to this module.
Prediction table data is built in components/ranked_predictions.py.
"""

from __future__ import annotations

import math
import re as _re
from datetime import date

import numpy as np

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_DIST_SET,
    RANKED_DIST_VALUES,
    SEASON_PALETTE,
    PACE_MIN,
    PACE_MAX,
    parse_date,
    compute_pace,
    compute_watts,
    watts_to_pace,
    get_season,
    workout_cat_key,
    pauls_law_pace,
    loglog_fit,
    loglog_predict_pace,
)
from services.critical_power_model import (
    critical_power_curve_points,
    critical_power_event_points,
    crossover_point,
)
from services.formatters import fmt_split

# ---------------------------------------------------------------------------
# Short display labels for ranked distances — derived from RANKED_DISTANCES so
# there is one source of truth for event names.
# ---------------------------------------------------------------------------

_DIST_LABELS: dict = {d: lbl for d, lbl in RANKED_DISTANCES}


# ---------------------------------------------------------------------------
# Season colour helpers
# ---------------------------------------------------------------------------


def _season_hsla(idx: int, lightness_offset: int, alpha: float) -> str:
    h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
    return f"hsla({h},{s}%,{max(l + lightness_offset, 0)}%,{alpha:.2f})"


# ---------------------------------------------------------------------------
# Timeline annotation helper
# ---------------------------------------------------------------------------


def build_sb_annotations(
    all_ranked_raw: list,
    sim_start: date,
    included_seasons: list,
    selected_dists: set,
    selected_times: set,
) -> list:
    """
    Return a list of SB annotation dicts for the DateSlider timeline dots.
    Each dict: {day: int, label: str, color: str}
    One dot per (season, event) combination — placed at the date the SB was set.
    """
    inc = set(included_seasons)

    # Workouts visible in the current event + season filter
    filtered = [
        w
        for w in all_ranked_raw
        if (w.get("distance") in selected_dists or w.get("time") in selected_times)
        and get_season(w.get("date", "")) in inc
    ]

    # Best pace per (season, cat) — lower pace = better for both dist and timed pieces
    season_best: dict = {}
    for w in filtered:
        pace = compute_pace(w)
        if pace is None:
            continue
        cat = workout_cat_key(w)
        if cat is None:
            continue
        season = get_season(w.get("date", ""))
        sk = (season, cat)
        if sk not in season_best or pace < season_best[sk]:
            season_best[sk] = pace

    # Earliest workout that achieved each SB pace
    sb_workout: dict = {}
    for w in sorted(filtered, key=lambda x: x.get("date", "")):
        pace = compute_pace(w)
        if pace is None:
            continue
        cat = workout_cat_key(w)
        if cat is None:
            continue
        season = get_season(w.get("date", ""))
        sk = (season, cat)
        if (
            sk in season_best
            and abs(pace - season_best[sk]) < 1e-9
            and sk not in sb_workout
        ):
            sb_workout[sk] = w

    # Season → color index (same ordering as build_chart_config)
    sorted_seasons = sorted(included_seasons)
    s_idx = {s: i for i, s in enumerate(sorted_seasons)}

    annotations = []
    for (season, cat), w in sb_workout.items():
        dt = parse_date(w.get("date", ""))
        if dt == date.min:
            continue
        day = (dt - sim_start).days
        if day < 0:
            continue

        pace = season_best[(season, cat)]
        etype, evalue = cat
        if etype == "dist":
            dist_m = evalue
            time_tenths = round(pace * 10 * dist_m / 500)
            time_str = fmt_split(time_tenths)
            dist_label = _DIST_LABELS.get(dist_m, f"{dist_m:,}m")
            label = f"{dist_label} SB — {time_str} ({season})"
        else:
            # Timed piece: evalue is tenths of seconds; show minutes + distance achieved
            mins = evalue // 600  # 600 tenths = 60 s = 1 min
            dist = w.get("distance", 0)
            label = f"{mins}min SB — {dist:,}m ({season})"

        color = _season_hsla(s_idx.get(season, 0), 0, 1.0)
        annotations.append({"day": day, "label": label, "color": color})

    return annotations


# ---------------------------------------------------------------------------
# Overlay label helpers
# ---------------------------------------------------------------------------


def ol_event_line(etype, evalue, pace, dist):
    """Format 'Event  time-or-dist' for the first overlay label line."""
    if etype == "dist":
        _t = fmt_split(round(pace * 10 * evalue / 500))
        return f"{_DIST_LABELS.get(evalue, f'{evalue:,}m')}  {_t}"
    else:
        return f"{evalue // 600}min  {dist:,}m"


def pcts(old_pace, new_pace):
    """Return (pct_pace, pct_watts) improvements; both 0.0 if no prior best."""
    if not old_pace or old_pace <= new_pace:
        return 0.0, 0.0
    pp = (old_pace - new_pace) / old_pace * 100
    pw = (
        (compute_watts(new_pace) - compute_watts(old_pace))
        / compute_watts(old_pace)
        * 100
    )
    return pp, pw


# ---------------------------------------------------------------------------
# Private prediction dataset builder (DRYs up the three similar blocks)
# ---------------------------------------------------------------------------


def _with_alpha(color: str, alpha: float) -> str:
    """Return a copy of an HSLA/RGBA color string with the alpha channel replaced."""
    return _re.sub(
        r"((?:hsla|rgba)\([^,]+,[^,]+,[^,]+,\s*)[^)]+\)",
        lambda m: f"{m.group(1)}{alpha})",
        color,
    )


def _pred_dataset(
    label: str,
    points: list,
    pred_color: str,
    point_radius: float = 1.5,
    border_width: float = 1.5,
) -> dict:
    """Build a Chart.js prediction-line dataset dict."""
    return {
        "type": "line",
        "label": label,
        "data": points,
        "borderColor": pred_color,
        "backgroundColor": "rgba(0,0,0,0)",
        "borderWidth": border_width,
        "borderDash": [5, 4],
        "pointRadius": point_radius,
        "pointHoverRadius": point_radius + 1.0,
        "pointHitRadius": 8,
        "pointBackgroundColor": pred_color,
        "tension": 0,
        "order": 4,
        "isPrediction": True,
    }


# ---------------------------------------------------------------------------
# Prediction table helpers
# ---------------------------------------------------------------------------


def compute_lifetime_bests(workouts: list) -> tuple[dict, dict]:
    """
    Return (lifetime_best, lifetime_best_anchor) derived from the given workout list.

    Applies the same validity filter as build_chart_config() (pace in PACE_MIN…PACE_MAX,
    non-None distance, known category) but operates on raw workout dicts rather than
    the pre-processed data_points format used internally by build_chart_config().

    lifetime_best        — {cat_key: best_pace_sec_per_500m}
    lifetime_best_anchor — {cat_key: distance_m_of_the_best_performance}
    """
    lifetime_best: dict = {}
    lifetime_best_anchor: dict = {}
    for w in workouts:
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        dist = w.get("distance")
        if not dist:
            continue
        cat = workout_cat_key(w)
        if cat is None:
            continue
        if cat not in lifetime_best or pace < lifetime_best[cat]:
            lifetime_best[cat] = pace
            lifetime_best_anchor[cat] = dist
    return lifetime_best, lifetime_best_anchor


# ---------------------------------------------------------------------------
# Main chart config builder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dataset sub-builders  (each returns list[dict] — Chart.js dataset objects)
# ---------------------------------------------------------------------------


def _rowinglevel_datasets(
    rl_predictions, pred_color, y_fn, show_components, lifetime_best_anchor
) -> list:
    """RowingLevel distance-weighted average curve + optional per-anchor component curves.

    At each target distance d_t the average is weighted by proximity of each anchor:
        weight = 1 / (|log₂(d_t / d_anchor)| + 0.5)
    This gives more influence to RL curves anchored close to the prediction point.
    """
    out = []
    _rl_all_dists = sorted(
        {int(d) for preds in rl_predictions.values() for d in preds if int(d) != 100}
    )
    # rl_predictions keys are str(tuple) e.g. "('dist', 2000)"; normalise once.
    _str_lba = {str(k): v for k, v in lifetime_best_anchor.items()}
    _rl_avg_pts = []
    for _d in _rl_all_dists:
        _rl_ps = []
        _rl_ws = []
        for cat_key, _preds in rl_predictions.items():
            _p = _preds.get(_d) or _preds.get(str(_d))
            if _p and PACE_MIN <= _p <= PACE_MAX:
                anchor_dist = _str_lba.get(cat_key)
                w = (
                    1.0 / (abs(math.log2(_d / anchor_dist)) + 0.5)
                    if anchor_dist
                    else 1.0
                )
                _rl_ps.append(_p)
                _rl_ws.append(w)
        if _rl_ps:
            total_w = sum(_rl_ws)
            _rl_avg_pts.append(
                {
                    "x": _d,
                    "y": y_fn(sum(w * p for w, p in zip(_rl_ws, _rl_ps)) / total_w),
                }
            )
    _rl_avg_pts.sort(key=lambda p: p["x"])
    if len(_rl_avg_pts) >= 2:
        out.append(
            _pred_dataset(
                "_rl_avg", _rl_avg_pts, pred_color, point_radius=1.5, border_width=2.0
            )
        )
    if show_components:
        _rl_dim = _with_alpha(pred_color, 0.30)
        for cat_key, preds in rl_predictions.items():
            pred_pts = []
            for dist_m, pace_sec in preds.items():
                d = int(dist_m)
                if d == 100 or not (PACE_MIN <= pace_sec <= PACE_MAX):
                    continue
                pred_pts.append({"x": d, "y": y_fn(pace_sec)})
            if len(pred_pts) < 2:
                continue
            pred_pts.sort(key=lambda p: p["x"])
            out.append(
                _pred_dataset(
                    f"_rl_{cat_key}",
                    pred_pts,
                    _rl_dim,
                    point_radius=0,
                    border_width=1.0,
                )
            )
    return out


def _pauls_law_datasets(
    lifetime_best,
    lifetime_best_anchor,
    pred_color,
    y_fn,
    show_components,
    pauls_k: float = 5.0,
) -> list:
    """Paul's Law average curve + optional per-anchor component curves.

    pauls_k is the personalised pace-increase constant (sec/500m per doubling).
    """
    out = []
    _pl_by_dist: dict = {}
    _pl_per_anchor: dict = {}
    for cat, pb_pace in lifetime_best.items():
        anchor_dist = lifetime_best_anchor.get(cat)
        if not anchor_dist:
            continue
        cat_pts = []
        for d in RANKED_DIST_VALUES:
            predicted = pauls_law_pace(pb_pace, anchor_dist, d, k=pauls_k)
            if PACE_MIN <= predicted <= PACE_MAX:
                _pl_by_dist.setdefault(d, []).append(predicted)
                cat_pts.append((d, predicted))
        if len(cat_pts) >= 2:
            _pl_per_anchor[cat] = cat_pts
    _pl_avg_pts = [
        {"x": d, "y": y_fn(sum(paces) / len(paces))}
        for d in RANKED_DIST_VALUES
        if (paces := _pl_by_dist.get(d))
    ]
    _pl_avg_pts.sort(key=lambda p: p["x"])
    if len(_pl_avg_pts) >= 2:
        out.append(
            _pred_dataset(
                "_pl_avg", _pl_avg_pts, pred_color, point_radius=1.5, border_width=2.0
            )
        )
    if show_components:
        _pl_dim = _with_alpha(pred_color, 0.30)
        for cat, cat_pts in _pl_per_anchor.items():
            pred_pts = [{"x": d, "y": y_fn(p)} for d, p in cat_pts]
            pred_pts.sort(key=lambda p: p["x"])
            out.append(
                _pred_dataset(
                    f"_pred_{cat}", pred_pts, _pl_dim, point_radius=0, border_width=1.0
                )
            )
    return out


def _loglog_dataset(lifetime_best, lifetime_best_anchor, pred_color, y_fn) -> list:
    """Log-log power law fit curve."""
    fit = loglog_fit(lifetime_best, lifetime_best_anchor)
    if fit is None:
        return []
    slope, intercept = fit
    pred_pts = []
    for d in RANKED_DIST_VALUES:
        predicted = loglog_predict_pace(slope, intercept, d)
        if PACE_MIN <= predicted <= PACE_MAX:
            pred_pts.append({"x": d, "y": y_fn(predicted)})
    pred_pts.sort(key=lambda p: p["x"])
    if len(pred_pts) < 2:
        return []
    return [_pred_dataset("_loglog_fit", pred_pts, pred_color, point_radius=3)]


def _cp_datasets(
    critical_power_params,
    x_min,
    x_max,
    pred_color,
    y_fn,
    show_watts,
    show_components,
    is_dark,
) -> list:
    """CP curve, event marker dots, crossover dot, and optional fast/slow components."""
    out = []

    # Smooth curve
    cp_pts = critical_power_curve_points(
        critical_power_params, x_min=x_min, x_max=x_max, show_watts=show_watts
    )
    if len(cp_pts) >= 2:
        out.append(_pred_dataset("_critical_power", cp_pts, pred_color, point_radius=0))

    # Event marker dots — active event sets are injected by build_chart_config as
    # "_sel_dists" / "_sel_times" keys derived from the current lifetime_best filter.
    _cp_sel_dists_key = critical_power_params.get("_sel_dists", set())
    _cp_sel_times_key = critical_power_params.get("_sel_times", set())
    ev_pts = critical_power_event_points(
        critical_power_params,
        selected_dists=_cp_sel_dists_key,
        selected_times=_cp_sel_times_key,
        show_watts=show_watts,
    )
    ev_pts = [p for p in ev_pts if x_min <= p["x"] <= x_max]
    if ev_pts:
        out.append(
            {
                "type": "scatter",
                "label": "_cp_event_markers",
                "data": ev_pts,
                "backgroundColor": pred_color,
                "borderColor": pred_color,
                "borderWidth": 1,
                "pointRadius": 4,
                "pointHoverRadius": 7,
                "pointHitRadius": 12,
                "order": 4,
                "isPrediction": True,
            }
        )

    # Crossover dot
    xo = crossover_point(critical_power_params, show_watts=show_watts)
    if xo is not None and x_min <= xo["x"] <= x_max:
        xo_color = "rgba(20, 210, 190, 0.95)" if is_dark else "rgba(0, 160, 145, 0.95)"
        out.append(
            {
                "type": "scatter",
                "label": "_cp_crossover",
                "data": [
                    {
                        "x": xo["x"],
                        "y": xo["y"],
                        "_cp_crossover": True,
                        "_t_label": xo["t_label"],
                    }
                ],
                "backgroundColor": xo_color,
                "borderColor": xo_color,
                "borderWidth": 2,
                "pointRadius": 8,
                "pointHoverRadius": 11,
                "pointHitRadius": 14,
                "order": 4,
                "isPrediction": True,
            }
        )

    # Fast/slow component curves (optional)
    if show_components:
        _cp_dim = _with_alpha(pred_color, 0.35)
        Pow1, tau1 = critical_power_params["Pow1"], critical_power_params["tau1"]
        Pow2, tau2 = critical_power_params["Pow2"], critical_power_params["tau2"]
        _fast_pts, _slow_pts = [], []
        for _t in np.logspace(math.log10(10.0), math.log10(10_800.0), 200):
            _w_combined = Pow1 / (1.0 + _t / tau1) + Pow2 / (1.0 + _t / tau2)
            if _w_combined <= 0:
                continue
            _pace_combined = watts_to_pace(_w_combined)
            if not (PACE_MIN <= _pace_combined <= PACE_MAX):
                continue
            _dist = _t * (500.0 / _pace_combined)
            if not (x_min <= _dist <= x_max):
                continue
            _w_fast = Pow1 / (1.0 + _t / tau1)
            _w_slow = Pow2 / (1.0 + _t / tau2)
            if show_watts:
                _fast_pts.append({"x": round(_dist, 1), "y": round(_w_fast, 2)})
                _slow_pts.append({"x": round(_dist, 1), "y": round(_w_slow, 2)})
            else:
                _pf, _ps = watts_to_pace(_w_fast), watts_to_pace(_w_slow)
                if PACE_MIN <= _pf <= PACE_MAX:
                    _fast_pts.append({"x": round(_dist, 1), "y": round(_pf, 4)})
                if PACE_MIN <= _ps <= PACE_MAX:
                    _slow_pts.append({"x": round(_dist, 1), "y": round(_ps, 4)})
        if len(_fast_pts) >= 2:
            out.append(
                _pred_dataset(
                    "_cp_fast", _fast_pts, _cp_dim, point_radius=0, border_width=1.0
                )
            )
        if len(_slow_pts) >= 2:
            out.append(
                _pred_dataset(
                    "_cp_slow", _slow_pts, _cp_dim, point_radius=0, border_width=1.0
                )
            )

    return out


def _lifetime_best_datasets(
    show_lifetime_line, lifetime_best, data_points, pb_color, y_fn
) -> list:
    """Lifetime-best connecting line (order=3)."""
    if not show_lifetime_line or not lifetime_best:
        return []
    seen: set = set()
    lb_pts = []
    for dp in data_points:
        c = dp["cat"]
        if (
            c not in seen
            and abs(dp["pace"] - lifetime_best.get(c, float("inf"))) < 1e-9
        ):
            lb_pts.append({"x": dp["dist"], "y": y_fn(dp["pace"])})
            seen.add(c)
    lb_pts.sort(key=lambda p: p["x"])
    if not lb_pts:
        return []
    return [
        {
            "type": "line",
            "label": "Lifetime Bests",
            "data": lb_pts,
            "borderColor": pb_color,
            "backgroundColor": "rgba(0,0,0,0)",
            "borderWidth": 7,
            "pointRadius": 0,
            "tension": 0.15,
            "order": 3,
        }
    ]


def _season_line_datasets(
    sorted_seasons, season_lines, data_points, season_best, season_idx, y_fn
) -> list:
    """Per-season best connecting lines (order=2)."""
    out = []
    for season in sorted_seasons:
        if season not in season_lines:
            continue
        seen: set = set()
        s_pts = []
        for dp in data_points:
            c = dp["cat"]
            if dp["season"] != season or c in seen:
                continue
            if abs(dp["pace"] - season_best.get((season, c), float("inf"))) < 1e-9:
                s_pts.append({"x": dp["dist"], "y": y_fn(dp["pace"])})
                seen.add(c)
        if not s_pts:
            continue
        s_pts.sort(key=lambda p: p["x"])
        idx = season_idx.get(season, 0)
        out.append(
            {
                "type": "line",
                "label": f"Season {season}",
                "data": s_pts,
                "borderColor": _season_hsla(idx, 0, 0.90),
                "backgroundColor": "rgba(0,0,0,0)",
                "borderWidth": 1.5,
                "pointRadius": 0,
                "tension": 0.15,
                "order": 2,
            }
        )
    return out


def _scatter_datasets(
    sorted_seasons, data_points, season_best, lifetime_best, season_idx, pb_color, y_fn
) -> list:
    """Per-season scatter point datasets (order=1, topmost)."""
    by_season: dict = {}
    for dp in data_points:
        by_season.setdefault(dp["season"], []).append(dp)
    out = []
    for season in sorted_seasons:
        pts = by_season.get(season, [])
        if not pts:
            continue
        idx = season_idx.get(season, 0)
        s_data, bg, border, bw, radii = [], [], [], [], []
        for dp in pts:
            c = dp["cat"]
            is_lb = abs(dp["pace"] - lifetime_best.get(c, float("inf"))) < 1e-9
            is_sb = abs(dp["pace"] - season_best.get((season, c), float("inf"))) < 1e-9
            alpha = 1.0 if (is_lb or is_sb) else 0.40
            s_data.append(
                {
                    "x": dp["dist"],
                    "y": y_fn(dp["pace"]),
                    "date": dp["date"],
                    "wtype": dp["wtype"],
                }
            )
            bg.append(_season_hsla(idx, 0, alpha))
            if is_lb:
                border.append(pb_color)
                bw.append(2.5)
                radii.append(6)
            else:
                border.append(_season_hsla(idx, -12, min(alpha + 0.15, 1.0)))
                bw.append(1)
                radii.append(5)
        out.append(
            {
                "type": "scatter",
                "label": f"Season {season}",
                "data": s_data,
                "backgroundColor": bg,
                "borderColor": border,
                "borderWidth": bw,
                "pointRadius": radii,
                "pointHoverRadius": 8,
                "order": 1,
            }
        )
    return out


def _sim_overlay_datasets(sim_overlays, season_idx, y_fn, pb_color, is_dark) -> list:
    """Ghost dots and threat arrows for the simulation overlay (order=0)."""
    out = []
    _ghost_pts = [
        gp
        for gp in sim_overlays.get("ghost_pts", [])
        if PACE_MIN < gp["pace"] < PACE_MAX
    ]
    if _ghost_pts:
        _g_bg, _g_border = [], []
        for gp in _ghost_pts:
            _g_idx = season_idx.get(gp.get("season", ""), 0)
            _g_bg.append(_season_hsla(_g_idx, 0, 0.22))
            _g_border.append(_season_hsla(_g_idx, -12, 0.45))
        out.append(
            {
                "type": "scatter",
                "label": "_ghost",
                "data": [{"x": gp["dist"], "y": y_fn(gp["pace"])} for gp in _ghost_pts],
                "backgroundColor": _g_bg,
                "borderColor": _g_border,
                "borderWidth": 1,
                "pointRadius": 5,
                "pointHoverRadius": 7,
                "order": 0,
            }
        )
    _arrow_color = "rgba(240,240,240,0.35)" if is_dark else "rgba(40,40,40,0.35)"
    for arr in sim_overlays.get("arrows", []):
        out.append(
            {
                "type": "line",
                "label": "_arrow",
                "data": [
                    {"x": arr["from_dist"], "y": y_fn(arr["from_pace"])},
                    {"x": arr["to_dist"], "y": y_fn(arr["to_pace"])},
                ],
                "borderColor": _arrow_color,
                "backgroundColor": "rgba(0,0,0,0)",
                "borderWidth": 1.5,
                "borderDash": [5, 4],
                "pointRadius": 0,
                "tension": 0,
                "order": 0,
            }
        )
    return out


def _canvas_labels_list(overlay_labels, y_fn) -> list:
    """Canvas annotation labels for simulation overlay text."""
    if not overlay_labels:
        return []
    return [
        {
            "x": _ol["x"],
            "y": y_fn(_ol["y_raw"]),
            "line_event": _ol["line_event"],
            "pct_pace": round(_ol.get("pct_pace", 0.0), 1),
            "pct_watts": round(_ol.get("pct_watts", 0.0), 1),
            "line_label": _ol["line_label"],
            "color": _ol["color"],
            "bold": _ol.get("bold", False),
        }
        for _ol in overlay_labels
    ]


# ---------------------------------------------------------------------------
# Main chart config builder — orchestrates the sub-builders above
# ---------------------------------------------------------------------------


def build_chart_config(
    workouts,
    *,
    log_x=True,
    log_y=False,
    show_lifetime_line=True,
    show_watts=False,
    is_dark=False,
    predictor="None",
    rl_predictions=None,  # {str(cat): {dist_m: pace_sec}} from RowingLevel
    critical_power_params=None,  # fitted param dict from fit_critical_power(), or None
    season_lines,
    all_seasons,
    x_bounds=None,  # (x_min, x_max) override; skips auto-computation when set
    y_bounds=None,  # (y_min, y_max) explicit axis limits
    sim_overlays=None,  # dict of ghost/arrow/threatened/new-arrival overlay data
    overlay_labels=None,  # list of {x, y_raw, text, color, bold} drawn on canvas
    show_components=False,  # show per-anchor/component curves alongside the average
    lifetime_best=None,  # pre-computed from compute_lifetime_bests(); derived if None
    lifetime_best_anchor=None,  # pre-computed; derived alongside lifetime_best
    pauls_k: float = 5.0,  # personalised Paul's Law constant (sec/500m per doubling)
):
    """Build a Chart.js config dict for the ranked-workouts chart."""

    # ── Collect valid data points ─────────────────────────────────────────────
    data_points = []
    for w in workouts:
        pace = compute_pace(w)
        if pace is None or pace < PACE_MIN or pace > PACE_MAX:
            continue
        dist = w.get("distance")
        if not dist:
            continue
        date_str = (w.get("date") or "")[:10]
        if len(date_str) < 10:
            continue
        cat = workout_cat_key(w)
        if cat is None:
            continue
        data_points.append(
            {
                "pace": pace,
                "dist": dist,
                "date": date_str,
                "cat": cat,
                "season": get_season(w.get("date", "")),
                "wtype": w.get("workout_type", ""),
            }
        )

    if not data_points:
        return {}

    # ── X-axis bounds ─────────────────────────────────────────────────────────
    # Use caller-supplied override (simulation mode) so the axis stays fixed at the
    # end-state range; otherwise auto-compute from data.
    if x_bounds is not None:
        x_min, x_max = x_bounds
    else:
        _dists = [dp["dist"] for dp in data_points]
        _x_min_raw, _x_max_raw = min(_dists), max(_dists)
        if log_x:
            _pad = 1.45
            x_min, x_max = _x_min_raw / _pad, _x_max_raw * _pad
        else:
            _pad = max((_x_max_raw - _x_min_raw) * 0.1, _x_min_raw * 0.1)
            x_min, x_max = max(0, _x_min_raw - _pad), _x_max_raw + _pad

    # ── Lifetime and season bests ─────────────────────────────────────────────
    _lb_provided = lifetime_best is not None and lifetime_best_anchor is not None
    if not _lb_provided:
        lifetime_best, lifetime_best_anchor = {}, {}
    season_best: dict = {}
    for dp in data_points:
        c, s, p = dp["cat"], dp["season"], dp["pace"]
        if not _lb_provided:
            if c not in lifetime_best or p < lifetime_best[c]:
                lifetime_best[c] = p
                lifetime_best_anchor[c] = dp["dist"]
        sk = (s, c)
        if sk not in season_best or p < season_best[sk]:
            season_best[sk] = p

    sorted_seasons = sorted(all_seasons)
    season_idx = {s: i for i, s in enumerate(sorted_seasons)}

    def _y(pace: float) -> float:
        return round(compute_watts(pace), 1) if show_watts else round(pace, 3)

    pb_color = "rgba(240,240,240,0.92)" if is_dark else "rgba(40,40,40,0.88)"
    # Warm amber — clearly distinct from gray gridlines and colored prediction curves.
    pred_color = "rgba(220,160,55,0.80)" if is_dark else "rgba(185,120,20,0.80)"

    # ── Assemble datasets (back to front) ─────────────────────────────────────
    datasets: list = []

    # 0. Prediction curves (order=4, furthest back)
    if predictor == "rowinglevel" and rl_predictions:
        datasets.extend(
            _rowinglevel_datasets(
                rl_predictions,
                pred_color,
                _y,
                show_components,
                lifetime_best_anchor or {},
            )
        )
    if predictor == "pauls_law":
        datasets.extend(
            _pauls_law_datasets(
                lifetime_best,
                lifetime_best_anchor,
                pred_color,
                _y,
                show_components,
                pauls_k=pauls_k,
            )
        )
    if predictor == "loglog":
        datasets.extend(
            _loglog_dataset(lifetime_best, lifetime_best_anchor, pred_color, _y)
        )
    if predictor == "critical_power" and critical_power_params is not None:
        # Inject active event sets so _cp_datasets can pass them to critical_power_event_points
        _cp_with_sel = dict(critical_power_params)
        _cp_with_sel["_sel_dists"] = {
            cat[1] for cat in lifetime_best if cat[0] == "dist"
        }
        _cp_with_sel["_sel_times"] = {
            cat[1] for cat in lifetime_best if cat[0] == "time"
        }
        datasets.extend(
            _cp_datasets(
                _cp_with_sel,
                x_min,
                x_max,
                pred_color,
                _y,
                show_watts,
                show_components,
                is_dark,
            )
        )

    # 1. Lifetime-best line (order=3)
    datasets.extend(
        _lifetime_best_datasets(
            show_lifetime_line, lifetime_best, data_points, pb_color, _y
        )
    )

    # 2. Season-best lines (order=2)
    datasets.extend(
        _season_line_datasets(
            sorted_seasons, season_lines, data_points, season_best, season_idx, _y
        )
    )

    # 3. Scatter (order=1)
    datasets.extend(
        _scatter_datasets(
            sorted_seasons,
            data_points,
            season_best,
            lifetime_best,
            season_idx,
            pb_color,
            _y,
        )
    )

    # 4. Simulation overlays (order=0, topmost)
    if sim_overlays:
        datasets.extend(
            _sim_overlay_datasets(sim_overlays, season_idx, _y, pb_color, is_dark)
        )

    # Canvas labels drawn by canvasLabelsPlugin in performance_chart.js
    canvas_labels = _canvas_labels_list(overlay_labels, _y)

    return {
        "type": "scatter",
        "data": {"datasets": datasets},
        "_canvas_labels": canvas_labels,
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "scales": {
                "x": {
                    "type": "logarithmic" if log_x else "linear",
                    "min": round(x_min, 1),
                    "max": round(x_max, 1),
                    "title": {
                        "display": True,
                        "text": "Distance (m)",
                        "font": {"size": 12},
                    },
                    "grid": {"color": "rgba(180,180,180,0.35)"},
                    "ticks": {
                        "maxTicksLimit": 10,
                        "autoSkip": True,
                        "minRotation": 0,
                        "maxRotation": 0,
                    },
                },
                "y": {
                    "type": "logarithmic" if log_y else "linear",
                    **(
                        {"min": round(y_bounds[0], 2), "max": round(y_bounds[1], 2)}
                        if y_bounds is not None
                        else {}
                    ),
                    "title": {
                        "display": True,
                        "text": "Watts" if show_watts else "Pace (sec/500m)",
                        "font": {"size": 14, "font-weight": "bold"},
                    },
                    "grid": {"color": "rgba(180,180,180,0.35)"},
                    "beginAtZero": False,
                },
            },
            "plugins": {"legend": {"display": False}},
            # Fixed padding so growing/shrinking point radii never shift the plot area.
            # 16px covers the largest dot (radius 11 + border 4 + 1px margin).
            "layout": {"padding": 16},
        },
    }
