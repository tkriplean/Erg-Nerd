"""
Residual Python-side helpers for the Power Curve chart after Stage 3 of the
refactor.  Scatter, best-lines, prediction curves and axis options are now
built in JS (see components/chart_assets/power_curve_chart_plugin.js).

This module keeps:

  compute_axis_bounds()    — stable x/y bounds from all-time PBs.  Called once
                             per render in power_curve_page and passed as a
                             prop so the chart doesn't rescale when the user
                             toggles individual events.

  _season_hsla / _pred_dataset / _with_alpha / _wr_scatter_dataset /
  _wr_pred_datasets / _rowinglevel_datasets / _pauls_law_datasets /
  _loglog_dataset / _cp_datasets / _average_datasets
                           — shared with components/power_curve_animation.py,
                             which builds the world-record overlay datasets
                             baked into every timeline snapshot.  These stay
                             in Python for now because the WR overlay isn't
                             time-varying (it's rebuilt only on identity_key
                             changes) and the full CP/ensemble math is still
                             easier in Python.  A later stage may port them.
"""

from __future__ import annotations

import math
import re as _re

import numpy as np

from services.rowing_utils import (
    RANKED_DIST_VALUES,
    SEASON_PALETTE,
    PACE_MIN,
    PACE_MAX,
    apply_best_only,
    compute_pace,
    compute_watts,
    watts_to_pace,
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


# ---------------------------------------------------------------------------
# Season colour helpers
# ---------------------------------------------------------------------------


def _season_hsla(idx: int, lightness_offset: int, alpha: float) -> str:
    h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
    return f"hsla({h},{s}%,{max(l + lightness_offset, 0)}%,{alpha:.2f})"


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


def _wr_scatter_dataset(
    wr_lb: dict, wr_lba: dict, _y, _use_duration: bool, is_dark: bool
) -> dict:
    """
    Build a Chart.js scatter dataset for individual WC record points.
    Uses green upward triangles to distinguish from user scatter.
    """
    color = "rgba(50,210,100,0.92)" if is_dark else "rgba(20,160,55,0.92)"
    pts = []
    for cat, pace in wr_lb.items():
        dist = wr_lba.get(cat, 0)
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


def _wr_pred_datasets(
    wr_data: dict,
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
    wr_color = "rgba(60,180,90,0.85)" if is_dark else "rgba(30,140,60,0.85)"
    lb, lba, cp = wr_data["lb"], wr_data["lba"], wr_data.get("cp_params")

    if predictor == "none":
        return []

    eff = predictor
    if eff == "rowinglevel":
        # Use WC RowingLevel predictions when available (scraped in _fetch_wr_data
        # using the WC 2k record as the reference for the user's demographics).
        wr_rl = wr_data.get("rl_predictions") or {}
        if wr_rl:
            ds = _rowinglevel_datasets(wr_rl, wr_color, _y, False, lba, x_fn=x_fn)
            for d in ds:
                d["label"] = "_wr_pred"
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
                wr_color,
                _y,
                show_watts,
                False,
                is_dark,
                x_fn=x_fn,
            )
            for d in ds_list:
                d["label"] = "_wr_pred"
            return ds_list
        eff = "loglog"  # fallback when CP fit unavailable

    if eff == "loglog":
        ds = _loglog_dataset(lb, lba, wr_color, _y, x_fn=x_fn)
        for d in ds:
            d["label"] = "_wr_pred"
        return ds

    if eff == "pauls_law":
        ds = _pauls_law_datasets(lb, lba, wr_color, _y, False, pauls_k=5.0, x_fn=x_fn)
        for d in ds:
            d["label"] = "_wr_pred"
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
            wr_color,
            _y,
            show_watts,
            show_components=False,
            x_fn=x_fn,
        )
        for d in ds:
            d["label"] = "_wr_pred"
        return ds

    return []


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

    # Event marker dots — active event sets are injected by the caller
    # (build_pred_curves_for_snapshot in power_curve_animation, or _wr_pred_datasets
    # for WC overlay) as "_sel_dists" / "_sel_times" keys on critical_power_params.
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
                    from services.predictions import _rl_interp_pace as _rl_ip

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


# ───────────────────────────────────────────────────────────────────────────
# Axis bounds — stable x/y bounds from all-time PBs
# ───────────────────────────────────────────────────────────────────────────


def compute_axis_bounds(
    quality_efforts: list,
    show_watts: bool,
    use_duration: bool,
    log_x: bool,
) -> tuple:
    """Stable x/y bounds from all-time PBs so the chart doesn't rescale when
    the user toggles individual events.  Returns (x_bounds, y_bounds);
    either may be None if data is insufficient."""
    bests = apply_best_only(quality_efforts)
    if not bests:
        return None, None
    bp = [p for w in bests if (p := compute_pace(w)) and 60 < p < 400]
    if use_duration:
        bx = [
            w.get("distance") * p / 500
            for w in bests
            if w.get("distance") and (p := compute_pace(w)) and 60 < p < 400
        ]
    else:
        bx = [w.get("distance") for w in bests if w.get("distance")]
    if not bp or not bx:
        return None, None
    xr, xR = min(bx), max(bx)
    x_bounds = (
        (xr / 1.45, xR * 1.45)
        if log_x
        else (
            max(0, xr - max((xR - xr) * 0.1, xr * 0.1)),
            xR + max((xR - xr) * 0.1, xr * 0.1),
        )
    )
    by = [compute_watts(p) if show_watts else p for p in bp]
    yr, yR = min(by), max(by)
    ypad = max((yR - yr) * 0.15, 5 if not show_watts else 2)
    return x_bounds, (yr - ypad, yR + ypad)
