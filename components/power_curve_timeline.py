"""
Timeline snapshot and payload builder for the power curve simulation.

Exported:
  compute_timeline_snapshot() — given workout lists at a timeline position,
      compute all derived model data: lb, pauls_k, CP fit, prediction datasets
      for the chart, and prediction table rows.  Pure Python, no HyperDiv.

  build_timeline_payload() — precompute the full JS chart payload (workout
      manifest + keyframes) and a Python-side prediction-table lookup across
      the entire training timeline.  Runs in a background thread via hd.task().
      No HyperDiv calls.  Returns (js_payload, pred_table_lookup).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SEPARATION OF CONCERNS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  power_curve_chart_builder.py  — static Chart.js configs and chart-data helpers
  power_curve_timeline.py       — time-varying data (snapshots + payload)
  power_curve_page.py           — HyperDiv orchestration: state, caching, layout

compute_timeline_snapshot is the single source of truth for per-keyframe model
computation; build_timeline_payload calls it for every PB keyframe and stores
the pred_table_rows in a Python-side lookup so the page can use it during
animation without re-running the models.
"""

from __future__ import annotations

from datetime import date

from services.rowing_utils import (
    compute_duration_s,
    RANKED_DISTANCES,
    RANKED_TIMES,
    SEASON_PALETTE,
    apply_best_only,
    apply_season_best_only,
    compute_pace,
    compute_watts,
    get_season,
    parse_date,
    workout_cat_key,
    compute_pauls_constant,
)


from services.critical_power_model import fit_critical_power
from services.ranked_predictions import build_prediction_table_data


from components.power_curve_chart_builder import (
    build_pred_datasets,
    compute_lifetime_bests,
    ol_event_line,
    pcts,
    build_wr_static_datasets,
)


# ---------------------------------------------------------------------------
# compute_timeline_snapshot
# ---------------------------------------------------------------------------


def compute_timeline_snapshot(
    *,
    sim_wkts: list,
    all_events_to_date: list,
    predictor: str,
    rl_predictions: dict | None,
    show_watts: bool,
    is_dark: bool,
    x_mode: str,
    x_bounds,
    y_bounds,
    show_components: bool,
    cp_fit_cache: dict | None = None,
) -> dict:
    """
    Given the workouts visible at a timeline position, compute all derived model data.

    sim_wkts       — selected-event workouts visible at this date, already
                     filtered by best_filter (apply_best_only / apply_season_best_only
                     applied by the caller as needed).
    all_events_to_date   — all-event workouts visible at this date (excluded-seasons
                     filter applied, but dist/time filter NOT applied); used for
                     lb_all so that disabled-event PBs still contribute to
                     Paul's Law and RowingLevel averaging.
    cp_fit_cache   — optional mutable dict {fit_key: cp_params} shared across
                     multiple keyframes by build_timeline_payload.  When None
                     (slow/static path), CP is recomputed fresh each call.

    Returns a dict with keys:
        lb              — selected-event lifetime bests {(etype, evalue): pace}
        lb_anchor       — selected-event lifetime bests in anchor format
        lb_all          — all-event lifetime bests (no dist/time gate)
        lb_all_anchor   — all-event lifetime bests in anchor format
        pauls_k_fit     — personalised Paul's constant, or None if < 2 PBs
        pauls_k         — pauls_k_fit or population default 5.0
        cp_params       — Critical Power fit dict, or None if insufficient data
        pred_datasets   — Chart.js dataset list for the active predictor
        pred_canvas_labels — canvas overlay label dicts
        pred_table_rows — list of prediction table row dicts
    """

    # ── Lifetime bests ────────────────────────────────────────────────────────
    lb, lb_anchor = compute_lifetime_bests(sim_wkts)
    lb_all, lb_all_anchor = compute_lifetime_bests(all_events_to_date)

    # ── Paul's constant ───────────────────────────────────────────────────────
    pauls_k_fit = compute_pauls_constant(lb, lb_anchor)
    pauls_k = pauls_k_fit if pauls_k_fit is not None else 5.0

    # ── Critical Power fit ────────────────────────────────────────────────────
    cp_params = None
    if predictor in ("critical_power", "average"):
        cp_pb_list = []
        for w in apply_best_only(sim_wkts):
            dur = compute_duration_s(w)
            pac = compute_pace(w)
            if dur and pac:
                cp_pb_list.append({"duration_s": dur, "watts": compute_watts(pac)})
        fit_key = str(
            sorted(
                (round(p["duration_s"], 1), round(p["watts"], 1)) for p in cp_pb_list
            )
        )
        if cp_fit_cache is not None:
            if fit_key not in cp_fit_cache:
                cp_fit_cache[fit_key] = fit_critical_power(cp_pb_list)
            cp_params = cp_fit_cache[fit_key]
        else:
            cp_params = fit_critical_power(cp_pb_list)

    # ── Prediction datasets (chart curves) ────────────────────────────────────
    pred_datasets, pred_canvas_labels = build_pred_datasets(
        predictor=predictor,
        lifetime_best=lb,
        lifetime_best_anchor=lb_anchor,
        critical_power_params=cp_params,
        rl_predictions=(
            rl_predictions if predictor in ("rowinglevel", "average") else None
        ),
        pauls_k=pauls_k,
        show_watts=show_watts,
        is_dark=is_dark,
        x_mode=x_mode,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        show_components=show_components,
    )

    # ── Prediction table rows ─────────────────────────────────────────────────
    pred_table_rows = build_prediction_table_data(
        lifetime_best=lb,
        lifetime_best_anchor=lb_anchor,
        all_lifetime_best=lb_all,
        all_lifetime_best_anchor=lb_all_anchor,
        critical_power_params=cp_params,
        rl_predictions=rl_predictions,
        pauls_k=pauls_k,
    )

    return {
        "lb": lb,
        "lb_anchor": lb_anchor,
        "lb_all": lb_all,
        "lb_all_anchor": lb_all_anchor,
        "pauls_k_fit": pauls_k_fit,
        "pauls_k": pauls_k,
        "cp_params": cp_params,
        "pred_datasets": pred_datasets,
        "pred_canvas_labels": pred_canvas_labels,
        "pred_table_rows": pred_table_rows,
    }


# ---------------------------------------------------------------------------
# build_timeline_payload
# ---------------------------------------------------------------------------


def build_timeline_payload(
    efforts_filtered_by_event: list,
    quality_efforts: list,
    featured_efforts: list,
    *,
    sim_start: date,
    total_days: int,
    best_filter: str,
    dist_enabled: tuple,
    time_enabled: tuple,
    show_watts: bool,
    is_dark: bool,
    x_mode: str,
    x_bounds,
    y_bounds,
    log_x: bool,
    predictor: str,
    draw_power_curves: str,
    show_components: bool,
    rl_predictions: dict,
    all_seasons: list,
    wr_data,
    bundle_key: str,
) -> tuple[dict, dict]:
    """
    Precompute the full JS chart payload and a Python-side prediction-table lookup.

    efforts_filtered_by_event — selected events, quality-filtered, newest-first.
    prefilt_excl   — all events (excluded-seasons only), newest-first.
    featured_efforts  — historical PB/SB workouts (PBs/SBs mode), newest-first.

    Returns (js_payload, pred_table_lookup) where:
      js_payload         — dict consumed by power_curve_chart_plugin.js
      pred_table_lookup  — dict[int, list] mapping keyframe_day → pred_table_rows
    """

    # ── Excluded categories ──────────────────────────────────────────────────
    excluded_cats = set()
    for i, (dist, _) in enumerate(RANKED_DISTANCES):
        if not dist_enabled[i]:
            excluded_cats.add(("dist", dist))
    for i, (tenths, _) in enumerate(RANKED_TIMES):
        if not time_enabled[i]:
            excluded_cats.add(("time", tenths))

    # ── Season metadata ──────────────────────────────────────────────────────
    sorted_seasons = sorted(all_seasons)
    season_idx_map = {s: i for i, s in enumerate(sorted_seasons)}

    def _hsla(idx, lightness_offset, alpha):
        h, s, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
        return f"hsla({h},{s}%,{max(l + lightness_offset, 0)}%,{alpha:.2f})"

    season_meta = [
        {
            "label": s,
            "color": _hsla(i, 0, 0.90),
            "dim_color": _hsla(i, 0, 0.40),
            "border_color": _hsla(i, -12, 1.0),
        }
        for i, s in enumerate(sorted_seasons)
    ]

    # ── X/Y helpers ──────────────────────────────────────────────────────────
    _use_duration = x_mode == "duration"
    pb_color = "rgba(240,240,240,0.92)" if is_dark else "rgba(40,40,40,0.88)"

    def _x_val(w):
        """Return x value in current x_mode, or None."""
        if _use_duration:
            t = w.get("time")
            if t:
                return t / 10.0
            p = compute_pace(w)
            d = w.get("distance")
            if p and d:
                return round(d * p / 500.0, 2)
            return None
        return w.get("distance")

    # ── Workout manifest (all workouts, oldest-first) ─────────────────────────
    manifest = []

    def _add_to_manifest(w, excluded: bool):
        p = compute_pace(w)
        d = w.get("distance")
        xv = _x_val(w)
        ck = workout_cat_key(w)
        if p is None or d is None or xv is None or ck is None:
            return
        dt = parse_date(w.get("date", ""))
        if dt < sim_start:
            return
        day = (dt - sim_start).days
        if day < 0 or day > total_days:
            return
        season = get_season(w.get("date", ""))
        s_idx = season_idx_map.get(season, 0)
        etype, evalue = ck
        manifest.append(
            {
                "day": day,
                "season_idx": s_idx,
                "cat_key_str": f"{etype}:{evalue}",
                "x": xv,
                "y_pace": round(p, 4),
                "y_watts": round(compute_watts(p), 1),
                "dist_m": d,
                "event_line": ol_event_line(etype, evalue, p, d),
                "date_label": dt.strftime("%b %d, %Y"),
                "wtype": w.get("workout_type", ""),
                "excluded": excluded,
            }
        )

    for w in efforts_filtered_by_event:
        _add_to_manifest(w, excluded=False)
    for w in quality_efforts:
        if workout_cat_key(w) in excluded_cats:
            _add_to_manifest(w, excluded=True)

    manifest.sort(key=lambda e: e["day"])

    # ── Keyframe builder ─────────────────────────────────────────────────────
    # Walk unique workout dates oldest→newest; emit a keyframe whenever the
    # lifetime-best dict changes (i.e. a new PB is set for any category).
    sorted_prefilt = sorted(efforts_filtered_by_event, key=lambda w: w.get("date", ""))
    sorted_featured = sorted(featured_efforts, key=lambda w: w.get("date", ""))
    # Sort excl oldest-first for per-keyframe lb_all slicing.
    sorted_prefilt_excl = sorted(quality_efforts, key=lambda w: w.get("date", ""))

    js_keyframes = [
        {
            "day": 0,
            "lifetime_best_pace": {},
            "lifetime_best_watts": {},
            "new_pbs": [],
            "new_pb_labels": [],
            "pred_datasets": [],
            "pred_canvas_labels": [],
        }
    ]
    pred_table_lookup: dict[int, list] = {0: []}  # keyframe_day → pred_table_rows
    prev_lb_str = {}  # cat_key_str -> pace
    cp_fit_cache: dict = {}  # fit_key -> cp_params (shared across keyframes)

    seen_dates = sorted(
        {w.get("date", "")[:10] for w in sorted_prefilt if w.get("date")}
    )

    for date_str in seen_dates:
        dt = parse_date(date_str)
        if dt < sim_start:
            continue
        day = (dt - sim_start).days
        if day < 0 or day > total_days:
            continue

        # Sim workouts up to this date
        if best_filter == "All":
            sim_wkts = [w for w in sorted_prefilt if w.get("date", "")[:10] <= date_str]
        else:
            in_time = [w for w in sorted_featured if w.get("date", "")[:10] <= date_str]
            sim_wkts = (
                apply_best_only(in_time)
                if best_filter == "PBs"
                else apply_season_best_only(in_time)
            )

        lb, lb_anchor = compute_lifetime_bests(sim_wkts)

        lb_str = {f"{k[0]}:{k[1]}": v for k, v in lb.items()}
        lb_anchor_str = {f"{k[0]}:{k[1]}": v for k, v in lb_anchor.items()}
        lb_watts_str = {ck: round(compute_watts(p), 1) for ck, p in lb_str.items()}

        if lb_str == prev_lb_str:
            continue  # nothing improved — no keyframe needed

        # Which categories got a new PB?
        new_pb_strs = [
            ck
            for ck, p in lb_str.items()
            if p < prev_lb_str.get(ck, float("inf")) - 1e-9
        ]

        # Build PB labels (canvas label dicts)
        new_pb_labels = []
        for ck_str in new_pb_strs:
            pace = lb_str[ck_str]
            dist = lb_anchor_str.get(ck_str, 0)
            etype, evalue_str = ck_str.split(":", 1)
            evalue = int(evalue_str)
            prev_pace = prev_lb_str.get(ck_str)
            pp, pw = pcts(prev_pace, pace) if prev_pace else (0.0, 0.0)
            new_pb_labels.append(
                {
                    "x": dist,
                    "y_pace": round(pace, 4),
                    "y_watts": round(compute_watts(pace), 1),
                    "line_event": ol_event_line(etype, evalue, pace, dist),
                    "pct_pace": round(pp, 1),
                    "pct_watts": round(pw, 1),
                    "line_label": "\u2746 New PB!",
                    "color": pb_color,
                    "bold": True,
                }
            )

        # All-event workouts for lb_all (Paul's Law / RowingLevel averaging).
        all_events_to_date = [
            w for w in sorted_prefilt_excl if w.get("date", "")[:10] <= date_str
        ]

        # Full snapshot: CP fit, pred datasets, pred table rows.
        _snap = compute_timeline_snapshot(
            sim_wkts=sim_wkts,
            all_events_to_date=all_events_to_date,
            predictor=predictor,
            rl_predictions=rl_predictions,
            show_watts=show_watts,
            is_dark=is_dark,
            x_mode=x_mode,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
            show_components=show_components,
            cp_fit_cache=cp_fit_cache,
        )

        js_keyframes.append(
            {
                "day": day,
                "lifetime_best_pace": lb_str,
                "lifetime_best_watts": lb_watts_str,
                "new_pbs": new_pb_strs,
                "new_pb_labels": new_pb_labels,
                "pred_datasets": _snap["pred_datasets"],
                "pred_canvas_labels": _snap["pred_canvas_labels"],
            }
        )
        pred_table_lookup[day] = _snap["pred_table_rows"]

        prev_lb_str = lb_str

    # ── Static datasets: WC records (time-invariant) ─────────────────────────
    full_lb, full_lb_anchor = compute_lifetime_bests(efforts_filtered_by_event)
    full_pauls_k = compute_pauls_constant(full_lb, full_lb_anchor) or 5.0

    static_datasets = (
        build_wr_static_datasets(
            wr_data,
            predictor=predictor,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
            show_watts=show_watts,
            is_dark=is_dark,
            x_mode=x_mode,
            pauls_k=full_pauls_k,
        )
        if wr_data
        else []
    )

    js_payload = {
        "workout_manifest": manifest,
        "keyframes": js_keyframes,
        "static_datasets": static_datasets,
        "season_meta": season_meta,
        "total_days": total_days,
        "pb_badge_lifetime_steps": 40,
        "bundle_key": bundle_key,
        "draw_lifetime_line": draw_power_curves == "PBs",
        "draw_season_lines": draw_power_curves == "SBs",
        "pb_color": pb_color,
        "is_dark": is_dark,
        "show_watts": show_watts,
        "x_mode": x_mode,
        "x_bounds": list(x_bounds) if x_bounds else None,
        "y_bounds": list(y_bounds) if y_bounds else None,
        "log_x": log_x,
    }

    return js_payload, pred_table_lookup
