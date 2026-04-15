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
    critical_power_model,
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

# Duration gridlines (seconds) for the x-axis in duration mode.
# Ranked times are a subset; extras give context at 10 s, 2 min, and 2 hr.
# Passed to JS via config._ranked_durations so JS doesn't hardcode these.
_DURATION_GRIDLINES: list[int] = [10, 60, 120, 240, 600, 1800, 3600, 7200]


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
    featured_workouts: list,
    sim_start: date,
    included_seasons: list,
    best_filter: str = "SBs",
) -> list:
    """
    Return annotation dicts for the DateSlider timeline dots.
    Each dict: {day: int, label: str, color: str}

    featured_workouts — pre-computed by compute_featured_workouts(); the
                        workouts that ever set a new PB or SB at the time
                        performed, sorted newest-first.
    """
    sorted_seasons = sorted(included_seasons)
    s_idx = {s: i for i, s in enumerate(sorted_seasons)}
    lbl = "PB" if best_filter == "PBs" else "SB"
    show_season = best_filter != "PBs"

    annotations = []
    for w in featured_workouts:
        dt = parse_date(w.get("date", ""))
        if dt == date.min:
            continue
        day = (dt - sim_start).days
        if day < 0:
            continue
        pace = compute_pace(w)
        cat = workout_cat_key(w)
        if pace is None or cat is None:
            continue
        season = get_season(w.get("date", ""))
        etype, evalue = cat
        if etype == "dist":
            time_tenths = round(pace * 10 * evalue / 500)
            dist_label = _DIST_LABELS.get(evalue, f"{evalue:,}m")
            label = f"{dist_label} {lbl} — {fmt_split(time_tenths)}"
        else:
            mins = evalue // 600
            label = f"{mins}min {lbl} — {w.get('distance', 0):,}m"
        if show_season:
            label += f" ({season})"
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


def _wc_scatter_dataset(
    wc_lb: dict, wc_lba: dict, _y, _use_duration: bool, is_dark: bool
) -> dict:
    """
    Build a Chart.js scatter dataset for individual WC record points.
    Uses green upward triangles to distinguish from user scatter.
    """
    color = "rgba(50,210,100,0.92)" if is_dark else "rgba(20,160,55,0.92)"
    pts = []
    for cat, pace in wc_lb.items():
        dist = wc_lba.get(cat, 0)
        if not dist or pace <= 0:
            continue
        x = round(dist * pace / 500.0, 2) if _use_duration else dist
        pts.append({"x": x, "y": _y(pace), "cat": list(cat)})
    pts.sort(key=lambda p: p["x"])
    return {
        "type": "scatter",
        "label": "World Records",
        "data": pts,
        "borderColor": color,
        "backgroundColor": color,
        "pointStyle": "triangle",
        "pointRadius": 6,
        "pointHoverRadius": 8,
        "pointHitRadius": 10,
        "order": 1,
        "isWCRecord": True,
    }


def _wc_pred_datasets(
    wc_data: dict,
    predictor: str,
    x_min: float,
    x_max: float,
    _y_min: float,
    _y_max: float,
    _y,
    show_watts: bool,
    is_dark: bool,
    x_fn,
    pauls_k: float = 5.0,
) -> list:
    """
    Build WC prediction datasets using the currently selected predictor applied
    to WC records (lb/lba/cp_params).  WC predictions use the same green colour
    and the same function as the user's prediction, making them the WC 'synthetic
    user'.

    rowinglevel has no WC equivalent — falls back to CP then loglog.
    """
    wc_color = "rgba(60,180,90,0.85)" if is_dark else "rgba(30,140,60,0.85)"
    lb, lba, cp = wc_data["lb"], wc_data["lba"], wc_data.get("cp_params")

    if predictor == "none":
        return []

    eff = predictor
    if eff == "rowinglevel":
        # Use WC RowingLevel predictions when available (scraped in _fetch_wc_data
        # using the WC 2k record as the reference for the user's demographics).
        wc_rl = wc_data.get("rl_predictions") or {}
        if wc_rl:
            ds = _rowinglevel_datasets(wc_rl, wc_color, _y, False, lba, x_fn=x_fn)
            for d in ds:
                d["label"] = "_wc_pred"
            return ds
        eff = "critical_power"  # fallback when WC RL predictions unavailable

    if eff == "critical_power":
        if cp is not None:
            ds_list, _ = _cp_datasets(
                cp,
                x_min,
                x_max,
                _y_min,
                _y_max,
                wc_color,
                _y,
                show_watts,
                False,
                is_dark,
                x_fn=x_fn,
            )
            for d in ds_list:
                d["label"] = "_wc_pred"
            return ds_list
        eff = "loglog"  # fallback when CP fit unavailable

    if eff == "loglog":
        ds = _loglog_dataset(lb, lba, wc_color, _y, x_fn=x_fn)
        for d in ds:
            d["label"] = "_wc_pred"
        return ds

    if eff == "pauls_law":
        ds = _pauls_law_datasets(lb, lba, wc_color, _y, False, pauls_k=5.0, x_fn=x_fn)
        for d in ds:
            d["label"] = "_wc_pred"
        return ds

    if eff == "average":
        ds = _average_datasets(
            lb,
            lba,
            cp,
            None,
            5.0,
            x_min,
            x_max,
            wc_color,
            _y,
            show_watts,
            show_components=False,
            x_fn=x_fn,
        )
        for d in ds:
            d["label"] = "_wc_pred"
        return ds

    return []


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
    rl_predictions, pred_color, y_fn, show_components, lifetime_best_anchor, x_fn=None
) -> list:
    """RowingLevel distance-weighted average curve + optional per-anchor component curves.

    At each target distance d_t the average is weighted by proximity of each anchor:
        weight = 1 / (|log₂(d_t / d_anchor)| + 0.5)
    This gives more influence to RL curves anchored close to the prediction point.

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    """
    if x_fn is None:
        x_fn = lambda d, p: d

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
            _avg_pace = sum(w * p for w, p in zip(_rl_ws, _rl_ps)) / total_w
            _rl_avg_pts.append({"x": x_fn(_d, _avg_pace), "y": y_fn(_avg_pace)})
    _rl_avg_pts.sort(key=lambda p: p["x"])
    if len(_rl_avg_pts) >= 2:
        out.append(
            _pred_dataset(
                "_rl_avg", _rl_avg_pts, pred_color, point_radius=1.5, border_width=2.0
            )
        )
    if show_components:
        _rl_dim = _with_alpha(pred_color, 0.55)
        for cat_key, preds in rl_predictions.items():
            pred_pts = []
            for dist_m, pace_sec in preds.items():
                d = int(dist_m)
                if d == 100 or not (PACE_MIN <= pace_sec <= PACE_MAX):
                    continue
                pred_pts.append({"x": x_fn(d, pace_sec), "y": y_fn(pace_sec)})
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
    x_fn=None,
) -> list:
    """Paul's Law average curve + optional per-anchor component curves.

    pauls_k is the personalised pace-increase constant (sec/500m per doubling).
    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    """
    if x_fn is None:
        x_fn = lambda d, p: d

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
    _pl_avg_pts = []
    for d in RANKED_DIST_VALUES:
        paces = _pl_by_dist.get(d)
        if paces:
            avg_p = sum(paces) / len(paces)
            _pl_avg_pts.append({"x": x_fn(d, avg_p), "y": y_fn(avg_p)})
    _pl_avg_pts.sort(key=lambda p: p["x"])
    if len(_pl_avg_pts) >= 2:
        out.append(
            _pred_dataset(
                "_pl_avg", _pl_avg_pts, pred_color, point_radius=1.5, border_width=2.0
            )
        )
    if show_components:
        _pl_dim = _with_alpha(pred_color, 0.55)
        for cat, cat_pts in _pl_per_anchor.items():
            pred_pts = [{"x": x_fn(d, p), "y": y_fn(p)} for d, p in cat_pts]
            pred_pts.sort(key=lambda p: p["x"])
            out.append(
                _pred_dataset(
                    f"_pred_{cat}", pred_pts, _pl_dim, point_radius=0, border_width=1.0
                )
            )
    return out


def _loglog_dataset(
    lifetime_best, lifetime_best_anchor, pred_color, y_fn, x_fn=None
) -> list:
    """Log-log power law fit curve.

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    """
    if x_fn is None:
        x_fn = lambda d, p: d

    fit = loglog_fit(lifetime_best, lifetime_best_anchor)
    if fit is None:
        return []
    slope, intercept = fit
    pred_pts = []
    for d in RANKED_DIST_VALUES:
        predicted = loglog_predict_pace(slope, intercept, d)
        if PACE_MIN <= predicted <= PACE_MAX:
            pred_pts.append({"x": x_fn(d, predicted), "y": y_fn(predicted)})
    pred_pts.sort(key=lambda p: p["x"])
    if len(pred_pts) < 2:
        return []
    return [_pred_dataset("_loglog_fit", pred_pts, pred_color, point_radius=3)]


def _cp_datasets(
    critical_power_params,
    x_min,
    x_max,
    y_min,
    y_max,
    pred_color,
    y_fn,
    show_watts,
    show_components,
    is_dark,
    x_fn=None,
) -> tuple[list, list]:
    """CP curve, event marker dots, optional crossover vline + text, optional fast/slow components.

    Returns (datasets, crossover_canvas_labels):
      datasets              — list of Chart.js dataset dicts
      crossover_canvas_labels — list of canvas-label dicts for the crossover annotation
                                (empty unless show_components=True and crossover found)

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    For duration mode, x_fn = lambda d, p: d*p/500 which equals t (since dist = t*500/pace).
    """
    if x_fn is None:
        x_fn = lambda d, p: d

    def _convert_pts(pts):
        """Re-map existing {x: dist, y: y_val} points through x_fn.
        Recovers pace from y (watts→pace or pace directly) to compute x_fn(dist, pace).
        """
        result = []
        for pt in pts:
            y_val = pt["y"]
            pace = watts_to_pace(y_val) if show_watts else y_val
            new_x = x_fn(pt["x"], pace)
            result.append({**pt, "x": new_x})
        return result

    out = []
    crossover_labels = []

    # Smooth curve — critical_power_curve_points produces x=distance, so we
    # use x_min/x_max in distance-space always, then convert x values after.
    # (x_min/x_max in duration mode are already pre-converted from power_curve_page.py,
    # so we fall back to large safe distance bounds for the CP curve generation itself.)
    _cp_x_min_dist = 100.0
    _cp_x_max_dist = 50_000.0
    cp_pts = critical_power_curve_points(
        critical_power_params,
        x_min=_cp_x_min_dist,
        x_max=_cp_x_max_dist,
        show_watts=show_watts,
    )
    cp_pts = _convert_pts(cp_pts)
    # After conversion, filter to the actual chart x range.
    cp_pts = [p for p in cp_pts if x_min <= p["x"] <= x_max]
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
    ev_pts = _convert_pts(ev_pts)
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

    # Crossover — only shown when show_components is enabled.
    # Rendered as a dashed vertical line + bottom-anchored text annotation.
    xo = crossover_point(critical_power_params, show_watts=show_watts)
    if show_components and xo is not None:
        # xo["x"] is distance; xo["t_seconds"] is duration.
        # In duration mode use t_seconds directly; in distance mode use xo["x"].
        xo_pace = watts_to_pace(xo["y"]) if show_watts else xo["y"]
        xo_x = x_fn(xo["x"], xo_pace)
        if x_min <= xo_x <= x_max:
            xo_color = (
                "rgba(20, 210, 190, 0.55)" if is_dark else "rgba(0, 160, 145, 0.55)"
            )
            xo_text_color = (
                "rgba(20, 210, 190, 0.90)" if is_dark else "rgba(0, 140, 128, 0.90)"
            )
            # Dashed vertical line spanning the full y range
            out.append(
                {
                    "type": "line",
                    "label": "_cp_crossover_vline",
                    "data": [
                        {"x": xo_x, "y": y_min},
                        {"x": xo_x, "y": y_max},
                    ],
                    "borderColor": xo_color,
                    "backgroundColor": "rgba(0,0,0,0)",
                    "borderWidth": 1.5,
                    "borderDash": [6, 4],
                    "pointRadius": 0,
                    "tension": 0,
                    "order": 4,
                }
            )
            # Text annotation at chart bottom (JS positions using _anchor: "bottom")
            crossover_labels.append(
                {
                    "x": xo_x,
                    "_anchor": "bottom",
                    "lines": [
                        f"Crossover: {xo['t_label']}",
                        "<- sprint | aerobic ->",
                    ],
                    "color": xo_text_color,
                }
            )

    # Fast/slow component curves (optional)
    if show_components:
        _cp_dim = _with_alpha(pred_color, 0.62)
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
            # x_fn(_dist, pace) = _dist * pace / 500 = _t, so this works for both modes.
            _xv = x_fn(_dist, _pace_combined)
            if not (x_min <= _xv <= x_max):
                continue
            _w_fast = Pow1 / (1.0 + _t / tau1)
            _w_slow = Pow2 / (1.0 + _t / tau2)
            if show_watts:
                _fast_pts.append({"x": round(_xv, 2), "y": round(_w_fast, 2)})
                _slow_pts.append({"x": round(_xv, 2), "y": round(_w_slow, 2)})
            else:
                _pf, _ps = watts_to_pace(_w_fast), watts_to_pace(_w_slow)
                if PACE_MIN <= _pf <= PACE_MAX:
                    _fast_pts.append({"x": round(_xv, 2), "y": round(_pf, 4)})
                if PACE_MIN <= _ps <= PACE_MAX:
                    _slow_pts.append({"x": round(_xv, 2), "y": round(_ps, 4)})
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

    return out, crossover_labels


# ---------------------------------------------------------------------------
# Average prediction line — ensemble mean of all available models
# ---------------------------------------------------------------------------


def _average_datasets(
    lifetime_best,
    lifetime_best_anchor,
    critical_power_params,
    rl_predictions,
    pauls_k,
    x_min,
    x_max,
    pred_color,
    y_fn,
    show_watts,
    show_components=False,
    x_fn=None,
) -> list:
    """Ensemble average of all available prediction models + optional component curves.

    Samples at ~80 log-spaced distances from x_min to x_max.  At each distance,
    computes available paces from loglog, Paul's Law (per-anchor avg), Critical Power,
    and RowingLevel (distance-weighted avg), then averages the non-None values.

    When show_components=True, also draws each individual model curve dimly so the
    user can see which models are pulling the average up or down.

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    In duration mode, samples are still generated at log-spaced distances but
    x values are converted via x_fn before the points are stored.
    """
    from scipy.optimize import brentq as _brentq

    if x_fn is None:
        x_fn = lambda d, p: d

    out = []

    # ── Log-Log fit ────────────────────────────────────────────────────────────
    _ll_fit = loglog_fit(lifetime_best, lifetime_best_anchor) if lifetime_best else None
    _ll_slope, _ll_intercept = _ll_fit if _ll_fit is not None else (None, None)

    # ── CP tuple ───────────────────────────────────────────────────────────────
    _cp = critical_power_params
    _cp_valid = _cp is not None and all(
        k in _cp for k in ("Pow1", "tau1", "Pow2", "tau2")
    )

    # ── RL string-keyed anchor lookup ─────────────────────────────────────────
    _str_lba = (
        {str(k): v for k, v in lifetime_best_anchor.items()}
        if lifetime_best_anchor
        else {}
    )

    # ── Sample distances ───────────────────────────────────────────────────────
    # Always sample in distance-space (100m to 42195m) so all models get a
    # consistent input domain.  x_fn converts to the correct chart x at the end.
    _n_pts = 80
    _sample_dists = list(np.logspace(math.log10(100.0), math.log10(42195.0), _n_pts))

    # Containers for per-model curves (used when show_components)
    _ll_pts, _pl_pts, _cp_pts_avg, _rl_pts_avg = [], [], [], []
    _avg_pts = []

    for _d in _sample_dists:
        _paces = []

        # Log-log
        _ll_p = None
        if _ll_slope is not None:
            _p = loglog_predict_pace(_ll_slope, _ll_intercept, _d)
            if _p is not None and PACE_MIN <= _p <= PACE_MAX:
                _ll_p = _p
                _paces.append(_p)

        # Paul's Law — per-anchor average
        _pl_p = None
        if lifetime_best:
            _pl_paces = []
            for cat, pb_pace in lifetime_best.items():
                anchor = lifetime_best_anchor.get(cat)
                if not anchor:
                    continue
                _pp = pauls_law_pace(pb_pace, anchor, _d, k=pauls_k)
                if PACE_MIN <= _pp <= PACE_MAX:
                    _pl_paces.append(_pp)
            if _pl_paces:
                _pl_p = sum(_pl_paces) / len(_pl_paces)
                _paces.append(_pl_p)

        # Critical Power — numerically solve for distance
        _cp_p = None
        if _cp_valid:
            Pow1, tau1, Pow2, tau2 = _cp["Pow1"], _cp["tau1"], _cp["Pow2"], _cp["tau2"]

            def _cp_resid(_t, _dist=_d):
                P = critical_power_model(_t, Pow1, tau1, Pow2, tau2)
                return (_dist - (_t * (500.0 / watts_to_pace(P)))) if P > 0 else -_dist

            try:
                _t_star = _brentq(_cp_resid, 10.0, 20_000.0, xtol=0.5)
                _w = critical_power_model(_t_star, Pow1, tau1, Pow2, tau2)
                if _w > 0:
                    _pace_cp = watts_to_pace(_w)
                    if PACE_MIN <= _pace_cp <= PACE_MAX:
                        _cp_p = _pace_cp
                        _paces.append(_cp_p)
            except Exception:
                pass

        # RowingLevel — distance-weighted average
        _rl_p = None
        if rl_predictions:
            _rl_ps, _rl_ws = [], []
            for cat_key, preds in rl_predictions.items():
                # Nearest available RL distance
                _p = preds.get(int(_d)) or preds.get(str(int(_d)))
                if _p is None:
                    # log-log interpolate within the RL curve for this anchor
                    from services.ranked_predictions import _rl_interp_pace as _rl_ip

                    _p = _rl_ip(preds, _d)
                if _p is not None and PACE_MIN <= _p <= PACE_MAX:
                    anchor_dist = _str_lba.get(cat_key)
                    _w = (
                        1.0 / (abs(math.log2(_d / anchor_dist)) + 0.5)
                        if anchor_dist and _d > 0
                        else 1.0
                    )
                    _rl_ps.append(_p)
                    _rl_ws.append(_w)
            if _rl_ps:
                total_w = sum(_rl_ws)
                _rl_p = sum(w * p for w, p in zip(_rl_ws, _rl_ps)) / total_w
                _paces.append(_rl_p)

        # Store per-model points for component display
        if _ll_p is not None:
            _ll_pts.append({"x": x_fn(_d, _ll_p), "y": y_fn(_ll_p)})
        if _pl_p is not None:
            _pl_pts.append({"x": x_fn(_d, _pl_p), "y": y_fn(_pl_p)})
        if _cp_p is not None:
            _cp_pts_avg.append({"x": x_fn(_d, _cp_p), "y": y_fn(_cp_p)})
        if _rl_p is not None:
            _rl_pts_avg.append({"x": x_fn(_d, _rl_p), "y": y_fn(_rl_p)})

        # Average
        if _paces:
            _avg_pace = sum(_paces) / len(_paces)
            _avg_pts.append({"x": x_fn(_d, _avg_pace), "y": y_fn(_avg_pace)})

    # Filter to chart x range and sort
    def _in_range(pts):
        return sorted(
            [p for p in pts if x_min <= p["x"] <= x_max], key=lambda p: p["x"]
        )

    _avg_pts = _in_range(_avg_pts)

    # Main averaged curve
    if len(_avg_pts) >= 2:
        out.append(
            _pred_dataset(
                "_avg_ensemble",
                _avg_pts,
                pred_color,
                point_radius=1.5,
                border_width=2.5,
            )
        )

    # Component curves at reduced opacity
    if show_components:
        _dim = _with_alpha(pred_color, 0.55)
        for label, pts in [
            ("_avg_ll", _ll_pts),
            ("_avg_pl", _pl_pts),
            ("_avg_cp", _cp_pts_avg),
            ("_avg_rl", _rl_pts_avg),
        ]:
            pts = _in_range(pts)
            if len(pts) >= 2:
                out.append(
                    _pred_dataset(label, pts, _dim, point_radius=0, border_width=1.0)
                )

    return out


def _lifetime_best_datasets(
    show_lifetime_line, lifetime_best, data_points, pb_color, y_fn, x_fn=None
) -> list:
    """Lifetime-best connecting line (order=3).

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    """
    if x_fn is None:
        x_fn = lambda d, p: d

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
            lb_pts.append(
                {
                    "x": x_fn(dp["dist"], dp["pace"]),
                    "y": y_fn(dp["pace"]),
                }
            )
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
    sorted_seasons, season_lines, data_points, season_best, season_idx, y_fn, x_fn=None
) -> list:
    """Per-season best connecting lines (order=2).

    x_fn(dist, pace) → x value; defaults to distance (meters) if None.
    """
    if x_fn is None:
        x_fn = lambda d, p: d

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
                s_pts.append(
                    {
                        "x": x_fn(dp["dist"], dp["pace"]),
                        "y": y_fn(dp["pace"]),
                    }
                )
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
    sorted_seasons,
    data_points,
    season_best,
    lifetime_best,
    season_idx,
    pb_color,
    y_fn,
    x_fn=None,
    excluded_data_points=(),
) -> list:
    """Per-season scatter point datasets (order=1, topmost).

    excluded_data_points — events filtered out by the event toggle, rendered at
    very low opacity (alpha ≈ 0.18) but not used for season/lifetime best logic.
    x_fn — optional transform applied to (dist, pace) → x value.  Defaults to
    the raw distance if None (distance mode).  Pass a function for duration mode.
    """
    if x_fn is None:
        x_fn = lambda dist, pace: dist

    by_season: dict = {}
    for dp in data_points:
        by_season.setdefault(dp["season"], []).append(dp)

    # Group excluded points by season too
    excl_by_season: dict = {}
    for dp in excluded_data_points:
        excl_by_season.setdefault(dp["season"], []).append(dp)

    all_seasons_with_pts = set(by_season) | set(excl_by_season)

    out = []
    for season in sorted_seasons:
        if season not in all_seasons_with_pts:
            continue
        pts = by_season.get(season, [])
        excl_pts = excl_by_season.get(season, [])
        if not pts and not excl_pts:
            continue
        idx = season_idx.get(season, 0)
        s_data, bg, border, bw, radii = [], [], [], [], []

        # Included points
        for dp in pts:
            c = dp["cat"]
            is_lb = abs(dp["pace"] - lifetime_best.get(c, float("inf"))) < 1e-9
            is_sb = abs(dp["pace"] - season_best.get((season, c), float("inf"))) < 1e-9
            alpha = 1.0 if (is_lb or is_sb) else 0.40
            s_data.append(
                {
                    "x": x_fn(dp["dist"], dp["pace"]),
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

        # Excluded points — rendered faintly, same season colour
        for dp in excl_pts:
            s_data.append(
                {
                    "x": x_fn(dp["dist"], dp["pace"]),
                    "y": y_fn(dp["pace"]),
                    "date": dp["date"],
                    "wtype": dp.get("wtype", ""),
                }
            )
            bg.append(_season_hsla(idx, 0, 0.18))
            border.append(_season_hsla(idx, -12, 0.25))
            bw.append(0.5)
            radii.append(4)

        if not s_data:
            continue
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


def _canvas_labels_list(overlay_labels, y_fn, crossover_labels=None) -> list:
    """Canvas annotation labels for simulation overlays and crossover annotations.

    Two entry formats are supported:

    1. Simulation overlay (PB/upcoming badges) — dict with y_raw, line_event, etc.
       JS positions relative to the data point.

    2. Crossover annotation — dict with _anchor="bottom" and a lines list.
       JS positions at a fixed offset from the chart bottom regardless of y data.
    """
    result = []

    # Simulation overlay entries (standard format)
    for _ol in overlay_labels or []:
        result.append(
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
        )

    # Crossover annotation entries (bottom-anchored format)
    for _cl in crossover_labels or []:
        result.append(
            {
                "x": _cl["x"],
                "y": None,  # ignored by JS when _anchor == "bottom"
                "_anchor": "bottom",
                "lines": _cl["lines"],
                "color": _cl["color"],
            }
        )

    return result


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
    excluded_workouts=(),  # workouts for deselected events — plotted faintly
    x_mode: str = "distance",  # "distance" | "duration"
    wc_data=None,  # dict {records, cp_params, lb, lba} from concept2_records
):
    """Build a Chart.js config dict for the ranked-workouts chart.

    x_mode="duration" switches the x-axis from distance (meters) to duration (seconds).
    excluded_workouts are plotted at very low opacity and do not influence bests/predictions.
    """
    _use_duration = x_mode == "duration"

    # ── Helper to compute x value for a data point ───────────────────────────
    # distance mode: x = distance in meters
    # duration mode: x = time in seconds (workout["time"] / 10)
    def _x_from_workout(w) -> float | None:
        if _use_duration:
            t = w.get("time")
            return t / 10.0 if t else None
        return w.get("distance")

    def _x_from_dp(dist: float, pace: float) -> float:
        """Convert (dist_m, pace_sec_per_500m) → x in current mode."""
        if _use_duration:
            return round(dist * pace / 500.0, 2)
        return dist

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
        x_val = _x_from_workout(w)
        if x_val is None:
            continue
        data_points.append(
            {
                "pace": pace,
                "dist": dist,
                "x": x_val,
                "date": date_str,
                "cat": cat,
                "season": get_season(w.get("date", "")),
                "wtype": w.get("workout_type", ""),
            }
        )

    if not data_points:
        return {}

    # ── Collect excluded data points (same validation, separate list) ─────────
    excluded_data_points = []
    for w in excluded_workouts:
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
        x_val = _x_from_workout(w)
        if x_val is None:
            continue
        excluded_data_points.append(
            {
                "pace": pace,
                "dist": dist,
                "x": x_val,
                "date": date_str,
                "cat": cat,
                "season": get_season(w.get("date", "")),
                "wtype": w.get("workout_type", ""),
            }
        )

    # ── X-axis bounds ─────────────────────────────────────────────────────────
    # Use caller-supplied override (simulation mode) so the axis stays fixed at the
    # end-state range; otherwise auto-compute from data (including excluded points).
    if x_bounds is not None:
        x_min, x_max = x_bounds
    else:
        _all_x = [dp["x"] for dp in data_points + excluded_data_points]
        # Include WC record x positions so their scatter points are always visible.
        if wc_data is not None:
            for cat, pace in wc_data["lb"].items():
                dist = wc_data["lba"].get(cat, 0)
                if dist and pace > 0:
                    _all_x.append(
                        round(dist * pace / 500.0, 2) if _use_duration else dist
                    )
        _x_min_raw, _x_max_raw = min(_all_x), max(_all_x)
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

    # Y bounds for crossover vertical line span
    _y_min = y_bounds[0] if y_bounds is not None else None
    _y_max = y_bounds[1] if y_bounds is not None else None
    if _y_min is None or _y_max is None:
        _all_y = [_y(dp["pace"]) for dp in data_points]
        if _all_y:
            _y_min = _y_min if _y_min is not None else min(_all_y)
            _y_max = _y_max if _y_max is not None else max(_all_y)
        else:
            _y_min, _y_max = 60.0, 200.0

    # ── Assemble datasets (back to front) ─────────────────────────────────────
    datasets: list = []
    _crossover_labels: list = []  # collected from _cp_datasets when show_components

    # x_fn: maps (dist_m, pace_sec_per_500m) → chart x value.
    # Distance mode: identity. Duration mode: dist * pace / 500 = time in seconds.
    _x_fn = _x_from_dp if _use_duration else None

    # 0. Prediction curves (order=4, furthest back)
    if predictor == "rowinglevel" and rl_predictions:
        datasets.extend(
            _rowinglevel_datasets(
                rl_predictions,
                pred_color,
                _y,
                show_components,
                lifetime_best_anchor or {},
                x_fn=_x_fn,
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
                x_fn=_x_fn,
            )
        )
    if predictor == "loglog":
        datasets.extend(
            _loglog_dataset(
                lifetime_best, lifetime_best_anchor, pred_color, _y, x_fn=_x_fn
            )
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
        _cp_ds, _cp_xover_labels = _cp_datasets(
            _cp_with_sel,
            x_min,
            x_max,
            _y_min,
            _y_max,
            pred_color,
            _y,
            show_watts,
            show_components,
            is_dark,
            x_fn=_x_fn,
        )
        datasets.extend(_cp_ds)
        _crossover_labels.extend(_cp_xover_labels)

    if predictor == "average":
        datasets.extend(
            _average_datasets(
                lifetime_best,
                lifetime_best_anchor,
                critical_power_params,
                rl_predictions,
                pauls_k,
                x_min,
                x_max,
                pred_color,
                _y,
                show_watts,
                show_components=show_components,
                x_fn=_x_fn,
            )
        )

    # 1. Lifetime-best line (order=3)
    datasets.extend(
        _lifetime_best_datasets(
            show_lifetime_line, lifetime_best, data_points, pb_color, _y, x_fn=_x_fn
        )
    )

    # 2. Season-best lines (order=2)
    datasets.extend(
        _season_line_datasets(
            sorted_seasons,
            season_lines,
            data_points,
            season_best,
            season_idx,
            _y,
            x_fn=_x_fn,
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
            x_fn=_x_from_dp if _use_duration else None,
            excluded_data_points=excluded_data_points,
        )
    )

    # 4. Simulation overlays (order=0, topmost)
    if sim_overlays:
        datasets.extend(
            _sim_overlay_datasets(sim_overlays, season_idx, _y, pb_color, is_dark)
        )

    # ── World-class overlay ───────────────────────────────────────────────────
    if wc_data is not None:
        # Add WC scatter points and WC prediction line.

        # WC scatter points (green triangles, drawn at order=1 like user scatter).
        _wc_scatter = _wc_scatter_dataset(
            wc_data["lb"], wc_data["lba"], _y, _use_duration, is_dark
        )
        datasets.append(_wc_scatter)

        # WC prediction line using the selected predictor (drawn behind everything).
        _wc_preds = _wc_pred_datasets(
            wc_data,
            predictor,
            x_min,
            x_max,
            _y_min,
            _y_max,
            _y,
            show_watts,
            is_dark,
            _x_fn,
            pauls_k,
        )
        datasets = _wc_preds + datasets

    # Canvas labels drawn by canvasLabelsPlugin in power_curve_chart_plugin.js
    canvas_labels = _canvas_labels_list(
        overlay_labels, _y, crossover_labels=_crossover_labels
    )

    # X-axis title and label mode
    _x_title = "Duration (s)" if _use_duration else "Distance (m)"

    # Y-axis config
    _y_axis = {
        "type": "logarithmic" if log_y else "linear",
        **(
            {"min": round(y_bounds[0], 2), "max": round(y_bounds[1], 2)}
            if y_bounds is not None
            else {}
        ),
        "title": {
            "display": False,
            "text": "Watts" if show_watts else "Pace (sec/500m)",
            "font": {"size": 14, "font-weight": "bold"},
        },
        "grid": {"color": "rgba(180,180,180,0.35)"},
        "beginAtZero": False,
    }

    return {
        "type": "scatter",
        "data": {"datasets": datasets},
        "_canvas_labels": canvas_labels,
        "_x_mode": x_mode,  # read by JS for tick formatter and gridline positions
        "_ranked_dists": RANKED_DIST_VALUES,      # gridline positions for distance mode
        "_ranked_durations": _DURATION_GRIDLINES,  # gridline positions for duration mode
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "scales": {
                "x": {
                    "type": "logarithmic" if log_x else "linear",
                    "min": round(x_min, 1),
                    "max": round(x_max, 1),
                    "title": {
                        "display": False,
                        "text": _x_title,
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
                "y": _y_axis,
            },
            "plugins": {"legend": {"display": False}},
            # Fixed padding so growing/shrinking point radii never shift the plot area.
            # 16px covers the largest dot (radius 11 + border 4 + 1px margin).
            "layout": {"padding": 16},
        },
    }
