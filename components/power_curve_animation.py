"""
Animation layer for the Power Curve page — top to bottom.

Three cohesive sections:

1. Snapshot helpers — per-keyframe model computation.
     ol_event_line()              format the overlay "Event  time-or-dist" line.
     compute_timeline_snapshot()  given the workouts visible at a timeline
                                  position, compute all derived model data
                                  (lifetime bests, Paul's K, CP fit, prediction
                                  datasets, prediction-table rows + accuracy).
                                  Pure Python, no HyperDiv.

2. Keyframe building — heavy precomputation of the full animation payload.
     build_sb_annotations()       DateSlider timeline dot annotations
                                  (one per PB / season SB).
     build_wr_static_datasets()   World-class record overlay datasets
                                  (time-invariant — computed once per bundle).
     build_keyframes()            Precompute the workout manifest, per-PB
                                  keyframes (scatter + prediction datasets),
                                  static datasets, and the prediction-table
                                  lookup across the whole training timeline.
                                  Runs in a background thread via hd.task().
                                  Returns (bundle_data, pred_table_lookup).
     wrap_payload()               Wrap cached ``bundle_data`` with the
                                  style-only fields Chart.js consumes
                                  (log_x/log_y, overlay toggles, bounds,
                                  bundle_key).  O(1); no model work.

3. Bundle lifecycle — HyperDiv plumbing (the only non-pure part).
     lookup_bundle_entry()        Latest pred_table_lookup entry at or before
                                  a given day.
     manage_animation_bundle()    Three-key caching of per-selection snapshot
                                  sets in ``state.sim_snapshot_cache`` (LRU
                                  bounded to ``_SNAPSHOT_CACHE_MAX``);
                                  launches the background build_keyframes
                                  task; returns sim_command.

        Key split:
          identity_key  — workout identity + predictor + x_mode + show_components
                          + show_watts + best_filter + excluded_seasons + WC
                          fetch state.  Change → invalidate the entire LRU
                          cache.
          selection_key — hash of (selected_dists, selected_times).  Inner
                          dimension of the cache; different selections share
                          the same identity but have their own snapshot set.
          style_key     — log_x, log_y, overlay_bests, x_bounds, y_bounds.
                          Change → only re-wrap the cached bundle_data (O(1)
                          — no task).  Bounds live on the style side because
                          they depend on log_x and are injected by wrap_payload.

        The combined (identity_key, selection_key, style_key) forms the
        ``bundle_key`` the JS compares to decide whether to re-apply the
        bundle.  The transported ``timeline_snapshots`` prop is a two-level
        dict ``{selection_key: {day: entry}}`` merged from every cached
        selection, so JS can switch to a previously-seen selection without a
        Python round-trip.

Why sections 1–2 live next to the HyperDiv bundle lifecycle in section 3:
    The animation story has its own cohesive invariant set (bundle_key, task
    scope names, sim_command semantics, pauls_k_fit travelling with
    keyframes).  compute_timeline_snapshot is the single source of truth for
    per-keyframe model computation; build_keyframes calls it for every PB
    keyframe and stores pred_table_rows + pauls_k_fit in a Python-side lookup
    so the page can read both during animation without re-running the models.

    The animation-only chart helpers (ol_event_line,
    build_sb_annotations, build_wr_static_datasets) live here alongside the
    keyframe builder.  A handful of dataset-shaping privates
    (``_wr_scatter_dataset``, ``_wr_pred_datasets``, ``_season_hsla``) still
    live in ``power_curve_chart_config`` and are imported here by their
    underscored names because the world-record overlay composition is shared
    across that module and this one.

    pauls_k_fit travels inside ``pred_table_lookup`` entries so the bundle
    reader can pull it atomically with pred_rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import hyperdiv as hd

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    SEASON_PALETTE,
    apply_best_only,
    apply_season_best_only,
    compute_duration_s,
    compute_lifetime_bests,
    compute_pace,
    compute_pauls_constant,
    compute_watts,
    get_season,
    loglog_fit,
    parse_date,
    workout_cat_key,
)
from services.formatters import fmt_split
from services.critical_power_model import fit_critical_power
from services.predictions import build_prediction_table_data

from components.power_curve_workouts import WorkoutView
from components.power_curve_chart_config import (
    # Private WR-overlay helpers that live in chart_config; animation imports
    # them by underscored name because the two modules are tightly-coupled
    # siblings for WR overlay composition.
    _season_hsla,
    _wr_pred_datasets,
    _wr_scatter_dataset,
)


# Short display labels for ranked distances — derived from RANKED_DISTANCES so
# there is one source of truth for event names.
_DIST_LABELS: dict = {d: lbl for d, lbl in RANKED_DISTANCES}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Snapshot helpers
# ═══════════════════════════════════════════════════════════════════════════


def ol_event_line(etype, evalue, pace, dist):
    """Format 'Event  time-or-dist' for the first overlay label line."""
    if etype == "dist":
        _t = fmt_split(round(pace * 10 * evalue / 500))
        return f"{_DIST_LABELS.get(evalue, f'{evalue:,}m')}  {_t}"
    else:
        return f"{evalue // 600}min  {dist:,}m"


def compute_timeline_snapshot(
    *,
    sim_wkts: list,
    all_events_to_date: list,
    rl_predictions: dict | None,
    selected_dist_set: set | None = None,
    selected_time_set: set | None = None,
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
    selected_dist_set / selected_time_set — enabled event sets for the
                     per-model accuracy (RMSE / R²) computation in the
                     prediction table.  None means "every event counts".
    cp_fit_cache   — optional mutable dict {fit_key: cp_params} shared across
                     multiple keyframes by build_keyframes.  When None
                     (slow/static path), CP is recomputed fresh each call.

    Returns a dict with keys:
        lb              — selected-event lifetime bests {(etype, evalue): pace}
        lb_anchor       — selected-event lifetime bests in anchor format
        lb_all          — all-event lifetime bests (no dist/time gate)
        lb_all_anchor   — all-event lifetime bests in anchor format
        pauls_k_fit     — personalised Paul's constant, or None if < 2 PBs
        pauls_k         — pauls_k_fit or population default 5.0
        cp_params       — Critical Power fit dict, or None if insufficient data
        ll_slope        — log-log slope (None if < 2 PBs) — JS samples curve
        ll_intercept    — log-log intercept paired with ll_slope
        pred_table_rows — list of prediction table row dicts
        accuracy        — dict keyed by Predictor.key ("average",
                          "critical_power", "loglog", "pauls_law",
                          "rowinglevel") → {"rmse", "r2", "n"} over
                          enabled events
    """

    # ── Lifetime bests ────────────────────────────────────────────────────────
    lb, lb_anchor = compute_lifetime_bests(sim_wkts)
    lb_all, lb_all_anchor = compute_lifetime_bests(all_events_to_date)

    # ── Paul's constant ───────────────────────────────────────────────────────
    pauls_k_fit = compute_pauls_constant(lb, lb_anchor)
    pauls_k = pauls_k_fit if pauls_k_fit is not None else 5.0

    # ── Critical Power fit ────────────────────────────────────────────────────
    # Always fit: the prediction table's CP column needs cp_params regardless
    # of which predictor's chart curve is drawn. cp_fit_cache makes repeat
    # fits free across keyframes during animation.
    cp_pb_list = []
    for w in apply_best_only(sim_wkts):
        dur = compute_duration_s(w)
        pac = compute_pace(w)
        if dur and pac:
            cp_pb_list.append({"duration_s": dur, "watts": compute_watts(pac)})
    fit_key = str(
        sorted((round(p["duration_s"], 1), round(p["watts"], 1)) for p in cp_pb_list)
    )
    if cp_fit_cache is not None:
        if fit_key not in cp_fit_cache:
            cp_fit_cache[fit_key] = fit_critical_power(cp_pb_list)
        cp_params = cp_fit_cache[fit_key]
    else:
        cp_params = fit_critical_power(cp_pb_list)

    # ── Log-log fit (JS samples pred curves from these params) ───────────────
    _ll = loglog_fit(lb, lb_anchor)
    ll_slope, ll_intercept = (_ll if _ll else (None, None))

    # ── Prediction table rows + accuracy ──────────────────────────────────────
    _pred = build_prediction_table_data(
        lifetime_best=lb,
        lifetime_best_anchor=lb_anchor,
        all_lifetime_best=lb_all,
        all_lifetime_best_anchor=lb_all_anchor,
        critical_power_params=cp_params,
        rl_predictions=rl_predictions,
        pauls_k=pauls_k,
        selected_dist_set=selected_dist_set,
        selected_time_set=selected_time_set,
    )

    return {
        "lb": lb,
        "lb_anchor": lb_anchor,
        "lb_all": lb_all,
        "lb_all_anchor": lb_all_anchor,
        "pauls_k_fit": pauls_k_fit,
        "pauls_k": pauls_k,
        "cp_params": cp_params,
        "ll_slope": ll_slope,
        "ll_intercept": ll_intercept,
        "pred_table_rows": _pred["rows"],
        "accuracy": _pred["accuracy"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# 2. Keyframe building
# ═══════════════════════════════════════════════════════════════════════════


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


def build_wr_static_datasets(
    wr_data: dict,
    *,
    predictor: str,
    x_bounds: tuple,
    y_bounds: tuple,
    show_watts: bool,
    is_dark: bool,
    x_mode: str,
    pauls_k: float = 5.0,
) -> list:
    """Build the world-class scatter + prediction datasets for the sim bundle.

    These are time-invariant (WC records don't change during animation), so
    they are computed once and stored in bundle.static_datasets.

    Returns an empty list if wr_data is None or empty.
    """
    if not wr_data:
        return []
    _use_duration = x_mode == "duration"
    x_min, x_max = x_bounds if x_bounds else (100.0, 42195.0)
    y_min, y_max = y_bounds if y_bounds else (60.0, 250.0)

    def _y(pace: float) -> float:
        return round(compute_watts(pace), 1) if show_watts else round(pace, 3)

    x_fn = (lambda dist, pace: round(dist * pace / 500.0, 2)) if _use_duration else None

    scatter = _wr_scatter_dataset(
        wr_data["lb"], wr_data["lba"], _y, _use_duration, is_dark
    )
    preds = _wr_pred_datasets(
        wr_data,
        predictor,
        x_min,
        x_max,
        y_min,
        y_max,
        _y,
        show_watts,
        is_dark,
        x_fn,
        pauls_k,
    )
    return preds + [scatter]


def _bisect_date_desc(workouts: list, date_str: str) -> int:
    """
    Binary search on a newest-first workout list.
    Returns the first index i such that workouts[i:] are all dated <= date_str.
    O(log n) versus the O(n) linear scan.
    """
    lo, hi = 0, len(workouts)
    while lo < hi:
        mid = (lo + hi) // 2
        if (workouts[mid].get("date") or "")[:10] > date_str:
            lo = mid + 1
        else:
            hi = mid
    return lo


def build_keyframes(
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
    predictor: str,
    show_components: bool,
    rl_predictions: dict,
    all_seasons: list,
    wr_data,
    fast_only: bool = False,
) -> tuple[dict, dict]:
    """
    Heavy, data-dependent half of the animation bundle.

    Builds everything that depends on workout data, predictor choice, and the
    axis/metric configuration: the workout manifest, per-keyframe scatter +
    prediction datasets, static datasets (WC overlay), and the prediction
    table lookup.

    Returns (bundle_data, pred_table_lookup) where:
      bundle_data        — dict of the heavy fields the JS chart needs.
                           Does NOT include the cheap style-only fields
                           (log_x, log_y, draw_lifetime_line/season_lines,
                           bundle_key) — those are added by ``wrap_payload``.
      pred_table_lookup  — dict[int, dict]: keyframe_day →
                           {"pred_rows": [...], "pauls_k_fit": float|None,
                            "accuracy": {"cp": {...}, "loglog": {...}, ...}}

    ``fast_only=True`` skips the historical per-date walk and emits a single
    end-state snapshot stored under day=0, so the nearest-≤ lookup returns
    it for any ``currentDay``.  Used synchronously from
    ``manage_animation_bundle`` on cache-miss so the initial render isn't
    blank while the full historical build runs in the background.  The bg
    task result replaces the fast bundle entry when it completes.

    See ``wrap_payload`` for the cheap style wrapper that produces the final
    js_payload; ``manage_animation_bundle`` caches this result by a data_key
    so style-only toggles (log axes, overlay selection) don't re-run the
    heavy loop.
    """

    # ── Excluded categories + enabled event sets for accuracy ────────────────
    excluded_cats = set()
    selected_dist_set: set = set()
    selected_time_set: set = set()
    for i, (dist, _) in enumerate(RANKED_DISTANCES):
        if dist_enabled[i]:
            selected_dist_set.add(dist)
        else:
            excluded_cats.add(("dist", dist))
    for i, (tenths, _) in enumerate(RANKED_TIMES):
        if time_enabled[i]:
            selected_time_set.add(tenths)
        else:
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
    # Newest-first: ``_bisect_date_desc`` walks a descending-by-date list and
    # returns the first index whose workouts are all dated ≤ date_str, so the
    # per-keyframe slice ``list[i:]`` is "everything at or before this date."
    # A previous refactor accidentally sorted ascending here, which made the
    # bisect return empty slices for early dates (no predictions at the start
    # of the timeline) and full slices for late dates (predictions stale with
    # respect to the PBs-at-that-point because future workouts leaked in).
    sorted_efforts_by_event = sorted(
        efforts_filtered_by_event, key=lambda w: w.get("date", ""), reverse=True,
    )
    sorted_featured = sorted(
        featured_efforts, key=lambda w: w.get("date", ""), reverse=True,
    )
    sorted_quality_efforts = sorted(
        quality_efforts, key=lambda w: w.get("date", ""), reverse=True,
    )

    snapshots: dict[int, dict] = {
        0: {
            "snapshot": {
                "lb": {},
                "lb_anchor": {},
                "cp_params": None,
                "ll_slope": None,
                "ll_intercept": None,
                "pauls_k": 5.0,
            },
        }
    }
    # keyframe_day → {"pred_rows": [...], "pauls_k_fit": float|None}
    #
    # pauls_k_fit travels with the keyframe so the page's fast path can read
    # the personalised Paul's constant without a slow-path render having gone
    # first (which used to stash it in state._pauls_k_fit — a race-prone
    # bridge between the two code paths).
    pred_table_lookup: dict[int, dict] = {
        0: {"pred_rows": [], "pauls_k_fit": None, "accuracy": {}}
    }
    prev_lb_str = {}  # cat_key_str -> pace
    cp_fit_cache: dict = {}  # fit_key -> cp_params (shared across keyframes)

    # ── Fast path: single end-state snapshot ─────────────────────────────────
    # When fast_only=True, skip the per-date walk and emit one snapshot that
    # reflects the current (end-of-timeline) bests.  Stored under day=0 so the
    # JS nearest-≤ lookup returns it for any currentDay the user lands on
    # before the full historical build replaces this entry in the cache.
    if fast_only:
        if best_filter == "All":
            sim_wkts = list(efforts_filtered_by_event)
        else:
            sim_wkts = apply_best_only(
                featured_efforts, by_season=best_filter != "PBs"
            )
        _snap = compute_timeline_snapshot(
            sim_wkts=sim_wkts,
            all_events_to_date=quality_efforts,
            rl_predictions=rl_predictions,
            selected_dist_set=selected_dist_set,
            selected_time_set=selected_time_set,
        )
        lb_str = {f"{k[0]}:{k[1]}": v for k, v in _snap["lb"].items()}
        lb_anchor_str = {f"{k[0]}:{k[1]}": v for k, v in _snap["lb_anchor"].items()}
        snapshots[0] = {
            "snapshot": {
                "lb": lb_str,
                "lb_anchor": lb_anchor_str,
                "cp_params": _snap["cp_params"],
                "ll_slope": _snap["ll_slope"],
                "ll_intercept": _snap["ll_intercept"],
                "pauls_k": _snap["pauls_k"],
            },
        }
        pred_table_lookup[0] = {
            "pred_rows": _snap["pred_table_rows"],
            "pauls_k_fit": _snap["pauls_k_fit"],
            "accuracy": _snap["accuracy"],
        }
        # Skip the historical per-date walk; fall through to static_datasets
        # + bundle_data assembly below.
        seen_dates = []
    else:
        seen_dates = sorted(
            {w.get("date", "")[:10] for w in sorted_efforts_by_event if w.get("date")}
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
            sim_wkts = sorted_efforts_by_event[
                _bisect_date_desc(sorted_efforts_by_event, date_str) :
            ]
        else:
            in_time = sorted_featured[_bisect_date_desc(sorted_featured, date_str) :]
            sim_wkts = apply_best_only(in_time, by_season=best_filter != "PBs")

        # All-event workouts for lb_all (Paul's Law / RowingLevel averaging).
        all_events_to_date = sorted_quality_efforts[
            _bisect_date_desc(sorted_quality_efforts, date_str) :
        ]

        lb, lb_anchor = compute_lifetime_bests(sim_wkts)

        lb_str = {f"{k[0]}:{k[1]}": v for k, v in lb.items()}
        lb_anchor_str = {f"{k[0]}:{k[1]}": v for k, v in lb_anchor.items()}

        if lb_str == prev_lb_str:
            continue  # nothing improved — no keyframe needed

        # Full snapshot: CP fit, pred datasets, pred table rows, accuracy.
        _snap = compute_timeline_snapshot(
            sim_wkts=sim_wkts,
            all_events_to_date=all_events_to_date,
            rl_predictions=rl_predictions,
            selected_dist_set=selected_dist_set,
            selected_time_set=selected_time_set,
            cp_fit_cache=cp_fit_cache,
        )

        snapshots[day] = {
            "snapshot": {
                "lb": lb_str,
                "lb_anchor": lb_anchor_str,
                "cp_params": _snap["cp_params"],
                "ll_slope": _snap["ll_slope"],
                "ll_intercept": _snap["ll_intercept"],
                "pauls_k": _snap["pauls_k"],
            },
        }
        pred_table_lookup[day] = {
            "pred_rows": _snap["pred_table_rows"],
            "pauls_k_fit": _snap["pauls_k_fit"],
            "accuracy": _snap["accuracy"],
        }

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

    # Note: x_bounds / y_bounds are intentionally NOT baked into bundle_data.
    # They depend on log_x (a style-only knob) via compute_axis_bounds, so
    # wrap_payload injects the current values on every render — otherwise
    # toggling log_x would leave the JS chart scale & gridline filter stuck
    # on the bounds captured when the bundle was first built.
    bundle_data = {
        "workout_manifest": manifest,
        "snapshots": snapshots,
        "static_datasets": static_datasets,
        "season_meta": season_meta,
        "total_days": total_days,
        "pb_badge_lifetime_steps": 40,
        "pb_color": pb_color,
        "is_dark": is_dark,
        "show_watts": show_watts,
        "x_mode": x_mode,
        # JS samples pred curves per-keyframe from snapshot fit params; these
        # bundle-level props are the ones predOpts(bundle) reads and that do
        # not vary per keyframe.
        "predictor": predictor,
        "show_components": show_components,
        "rl_predictions": rl_predictions,
        # Gridline color.  Python is the single source of truth for this
        # colour (CLAUDE.md) — JS reads it from the bundle.
        "grid_color": "rgba(180,180,180,0.35)",
    }

    return bundle_data, pred_table_lookup


def wrap_payload(
    bundle_data: dict,
    *,
    log_x: bool,
    log_y: bool,
    overlay_bests: str,
    bundle_key: str,
    x_bounds,
    y_bounds,
    selection_key: str,
    snapshots_ready: bool,
    timeline_snapshots: dict,
) -> dict:
    """
    Wrap a cached ``bundle_data`` dict with the style-only fields the JS
    chart needs.  O(1) — no loops, no model work.

    ``x_bounds`` / ``y_bounds`` are injected here (not baked into
    ``bundle_data``) because they depend on ``log_x`` — a style-only knob.
    Re-wrapping on style-key change keeps the JS chart scale and the
    ranked-grid filter in sync with the current render's bounds.

    ``timeline_snapshots`` is the two-level dict {selection_key: {day: entry}}
    merged from the per-selection LRU cache — JS looks up the inner dict via
    ``selection_key`` and picks the nearest-≤ day.  This replaces the older
    single-level ``keyframes`` list; caching multiple selections lets the user
    toggle event sets without re-fitting.  ``snapshots_ready`` is true when
    the current selection's snapshots are fully built.

    The per-selection ``snapshots`` field on ``bundle_data`` is intentionally
    dropped from the transport payload — JS reads from ``timeline_snapshots``
    instead.

    Separating this from ``build_keyframes`` lets the animation layer cache
    snapshots by (identity_key, selection_key) and re-wrap on style-only
    toggles (log axes, overlay selection) without re-running the heavy
    keyframe loop.
    """
    out = {
        **bundle_data,
        "bundle_key": bundle_key,
        "log_x": log_x,
        "log_y": log_y,
        "draw_lifetime_line": overlay_bests == "PBs",
        "draw_season_lines": overlay_bests == "SBs",
        "x_bounds": list(x_bounds) if x_bounds else None,
        "y_bounds": list(y_bounds) if y_bounds else None,
        "selection_key": selection_key,
        "snapshots_ready": snapshots_ready,
        "timeline_snapshots": timeline_snapshots,
    }
    # The per-selection snapshots dict on bundle_data is inlined into the
    # two-level timeline_snapshots above; strip it from the transport payload
    # to avoid shipping the same data twice.
    out.pop("snapshots", None)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 3. Bundle lifecycle (HyperDiv)
# ═══════════════════════════════════════════════════════════════════════════


def lookup_bundle_entry(lookup: dict, day: int | None) -> dict:
    """
    Latest keyframe entry at or before ``day`` from ``pred_table_lookup``.

    Returns ``{"pred_rows": [...], "pauls_k_fit": float|None, "accuracy": {}}``
    — the empty dict when the lookup is empty or no keyframe at or before
    ``day`` exists.

    ``day=None`` means "end of timeline" — returns the final keyframe.
    """
    empty = {"pred_rows": [], "pauls_k_fit": None, "accuracy": {}}
    if not lookup:
        return empty
    if day is None:
        return lookup.get(max(lookup.keys()), empty)
    return lookup.get(max((d for d in lookup if d <= day), default=0), empty)


_SNAPSHOT_CACHE_MAX = 4


def manage_animation_bundle(
    state,
    *,
    workouts: WorkoutView,
    sim_start: date,
    total_days: int,
    selected_dists: set,
    selected_times: set,
    excluded_seasons: tuple,
    show_watts: bool,
    is_dark: bool,
    x_bounds,
    y_bounds,
    rl_predictions: dict,
    all_seasons: list,
    wr_data,
    at_today: bool,
) -> str:
    """
    Manage the animation bundle lifecycle.

    Returns ``sim_command``: ``"play"`` | ``"pause"`` | ``"stop"``.

    sim_command semantics:
      * ``"play"``   — playing, not at today, snapshots ready → JS ticks forward
      * ``"pause"``  — snapshots not ready yet (hold JS), or paused with a bundle
      * ``"stop"``   — at_today (timeline_day is None) or no bundle at all

    Caching strategy (three keys):
      * ``identity_key`` — workout identity + predictor + x_mode + show_components
        + show_watts + best_filter + excluded_seasons + WR fetch state.  Changing
        any of these invalidates the per-selection LRU cache entirely.
      * ``selection_key`` — hash of (selected_dists, selected_times).  Inner
        cache dimension so the user can toggle event selections without
        re-fitting if that selection has been seen recently.
      * ``style_key`` — log_x, log_y, overlay_bests, axis bounds.  Style-only
        toggles that bypass the task entirely (cheap ``wrap_payload`` re-run).

      ``state.sim_snapshot_cache`` maps ``selection_key → (bundle_data,
      pred_lookup)`` with LRU eviction at ``_SNAPSHOT_CACHE_MAX`` entries
      (insertion-ordered dict; oldest evicted on overflow).  The transported
      ``timeline_snapshots`` prop is the two-level merge of every cached
      selection's snapshots, so JS can look up previously-seen selections
      without a Python round-trip.
    """
    _identity_key = hashlib.md5(
        json.dumps(
            [
                state.chart_predictor,
                state.best_filter,
                sorted(list(excluded_seasons)),
                show_watts,
                state.chart_x_metric,
                state.chart_show_components,
                state.chart_compare_wc,
                # wr_data identity — rebuild once the WC fetch completes.
                state.wr_fetch_done,
                state.wr_fetch_key,
                # (hash(FilterSpec), workout_count) — workout pipeline identity.
                list(state._view_key),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]

    _selection_key = hashlib.md5(
        json.dumps(
            [sorted(list(selected_dists)), sorted(list(selected_times))],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:8]

    _style_key = hashlib.md5(
        json.dumps(
            [
                state.chart_log_x,
                state.chart_log_y,
                state.overlay_bests,
                # x_bounds / y_bounds are injected by wrap_payload, not baked
                # into bundle_data — include them here so the bundle re-wraps
                # when they change (e.g. log_x toggles → bounds recompute).
                list(x_bounds) if x_bounds else None,
                list(y_bounds) if y_bounds else None,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()[:8]

    # ── Identity change: blow away the LRU cache ─────────────────────────────
    if state.sim_identity_key != _identity_key:
        state.sim_snapshot_cache = {}
        state.sim_full_selections = frozenset()
        state.sim_identity_key = _identity_key
        state.sim_pred_lookup = {}
        state.sim_bundle = None
        state.sim_bundle_key = ""

    # ── Background task: full historical build ───────────────────────────────
    # Always inspect the task for this selection, even when the cache already
    # holds a fast bundle.  When the task completes, its result replaces the
    # fast bundle in the cache and ``state.sim_full_selections`` flips, which
    # promotes the bundle_key suffix from ``fast`` → ``full`` below and drives
    # the style-side re-wrap so JS swaps its cached bundle.
    _keyframes_kwargs = dict(
        sim_start=sim_start,
        total_days=total_days,
        best_filter=state.best_filter,
        dist_enabled=state.dist_enabled,
        time_enabled=state.time_enabled,
        show_watts=show_watts,
        is_dark=is_dark,
        x_mode=state.chart_x_metric,
        x_bounds=x_bounds,
        y_bounds=y_bounds,
        predictor=state.chart_predictor,
        show_components=state.chart_show_components,
        rl_predictions=rl_predictions,
        all_seasons=all_seasons,
        wr_data=wr_data,
    )

    if _selection_key not in state.sim_full_selections:
        # Scope by (identity, selection) so a still-running task for a prior
        # selection under the same identity doesn't swallow this selection's
        # build.
        with hd.scope(f"sim_bundle_{_identity_key}_{_selection_key}"):
            _bt = hd.task()
            if not _bt.running and not _bt.done:
                _bt.run(
                    build_keyframes,
                    workouts.efforts_filtered_by_event,
                    workouts.quality_efforts,
                    workouts.featured_efforts,
                    **_keyframes_kwargs,
                )
            if _bt.done:
                if _bt.result:
                    bundle_data, pred_lookup = _bt.result
                    # LRU insertion + eviction.  Plain dict preserves insertion
                    # order in Py3.7+; re-binding keeps HyperDiv's reactive
                    # state-change detection happy.
                    new_cache = dict(state.sim_snapshot_cache)
                    new_cache[_selection_key] = (bundle_data, pred_lookup)
                    while len(new_cache) > _SNAPSHOT_CACHE_MAX:
                        _oldest = next(iter(new_cache))
                        del new_cache[_oldest]
                    state.sim_snapshot_cache = new_cache
                    state.sim_full_selections = frozenset(
                        state.sim_full_selections | {_selection_key}
                    )
                elif _bt.error:
                    # Task failed — stop playing and surface the error.
                    state.sim_playing = False
                    hd.alert(
                        f"Animation bundle failed: {_bt.error}",
                        variant="danger",
                        closable=True,
                    )

    # ── Fast bundle: synchronous placeholder for initial render ──────────────
    # If nothing is cached for this selection yet, compute a single end-state
    # snapshot now so the chart has something to render while the full
    # historical task runs.  The bg task's result will overwrite this entry
    # on the render following its completion.
    if _selection_key not in state.sim_snapshot_cache:
        fast_data, fast_lookup = build_keyframes(
            workouts.efforts_filtered_by_event,
            workouts.quality_efforts,
            workouts.featured_efforts,
            fast_only=True,
            **_keyframes_kwargs,
        )
        new_cache = dict(state.sim_snapshot_cache)
        new_cache[_selection_key] = (fast_data, fast_lookup)
        while len(new_cache) > _SNAPSHOT_CACHE_MAX:
            _oldest = next(iter(new_cache))
            del new_cache[_oldest]
        state.sim_snapshot_cache = new_cache

    _snapshots_ready = _selection_key in state.sim_snapshot_cache

    # Expose the current selection's pred_lookup for the prediction table.
    if _snapshots_ready:
        state.sim_pred_lookup = state.sim_snapshot_cache[_selection_key][1]
    else:
        state.sim_pred_lookup = {}

    # Bundle key — include the fast/full suffix so JS sees a real bundle_key
    # change when the bg task replaces a fast bundle with the full historical
    # one (identity / selection / style are all unchanged across that swap, so
    # without this suffix ``applyBundle``'s early-out would keep the fast
    # bundle's single day-0 snapshot even after Python ships the full bundle,
    # which is what made mid-animation prediction curves appear frozen).
    _fullness = "full" if _selection_key in state.sim_full_selections else "fast"
    _bundle_key = f"{_identity_key}-{_selection_key}-{_style_key}-{_fullness}"

    # ── Style-side: cheap re-wrap when bundle key changes ────────────────────
    if _snapshots_ready and state.sim_bundle_key != _bundle_key:
        current_bundle_data = state.sim_snapshot_cache[_selection_key][0]
        # Merge snapshots across all cached selections so JS can toggle back
        # to a previously-seen selection without a Python round-trip.
        merged_snapshots = {
            sk: bd["snapshots"]
            for sk, (bd, _pl) in state.sim_snapshot_cache.items()
        }
        state.sim_bundle = wrap_payload(
            current_bundle_data,
            log_x=state.chart_log_x,
            log_y=state.chart_log_y,
            overlay_bests=state.overlay_bests,
            bundle_key=_bundle_key,
            x_bounds=x_bounds,
            y_bounds=y_bounds,
            selection_key=_selection_key,
            snapshots_ready=True,
            timeline_snapshots=merged_snapshots,
        )
        state.sim_bundle_key = _bundle_key

    # Derive sim_command.  Seeking is handled entirely in JS via the integrated
    # scrubber; Python only signals play / pause / stop.
    if state.sim_playing and not at_today and state.sim_bundle is not None:
        return "play"
    elif state.sim_playing and state.sim_bundle is None:
        return "pause"  # bundle not ready yet — hold JS
    elif at_today:
        return "stop"
    elif state.sim_bundle is not None:
        return "pause"
    else:
        return "stop"
