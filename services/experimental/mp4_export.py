"""
MP4 export for the time-progression simulation.

This module is intentionally unused for now — the MP4 export feature is
parked here for future re-enablement.  The render logic (generate_mp4) and
the HyperDiv UI wiring (render_mp4_export_button) are kept together so the
feature can be re-enabled by importing and calling render_mp4_export_button
from power_curve_page.py and wiring mp4_task = hd.task() back in.

To re-enable:
  1. In power_curve_page.py, add `mp4_task = hd.task()` inside power_curve_page().
  2. Call render_mp4_export_button(state, mp4_task, sim_config) in the
     chart-settings row where the commented-out MP4 block currently sits.
  3. Build sim_config from the current state variables (see docstring on
     render_mp4_export_button for the required keys).
"""

from __future__ import annotations

import colorsys
from datetime import date
from pathlib import Path
from typing import Optional

from services.rowing_utils import (
    RANKED_DIST_VALUES,
    SEASON_PALETTE,
    PACE_MIN,
    PACE_MAX,
    compute_pace,
    compute_watts,
    get_season,
    workout_cat_key,
    apply_best_only,
    apply_season_best_only,
    pauls_law_pace,
    loglog_fit,
    loglog_predict_pace,
)
from services.ranked_filters import workouts_before_date


# ---------------------------------------------------------------------------
# Internal colour helper (mirrors the one in simulation.py)
# ---------------------------------------------------------------------------


def _hsl_to_rgb(h_deg: float, s_pct: float, l_pct: float) -> tuple:
    """HSL (0-360, 0-100, 0-100) → (r, g, b) floats 0-1."""
    h, s, l = h_deg / 360.0, s_pct / 100.0, l_pct / 100.0
    r, g, b = colorsys.hls_to_rgb(h, l, s)  # note: colorsys uses HLS order
    return (r, g, b)


# ---------------------------------------------------------------------------
# generate_mp4
# ---------------------------------------------------------------------------


def generate_mp4(
    rankable_efforts: list,
    sim_dates: list,  # list of date objects, one per frame
    config: dict,  # see keys below
    output_path,  # str or Path
    rower_name: str = "",
    on_progress=None,  # callable(frame_idx, total_frames)
) -> tuple[Optional[str], Optional[str]]:
    """
    Render the time-progression simulation as an MP4.

    config keys:
      selected_dists, selected_times, excluded_seasons, best_filter,
      show_watts, show_lifetime_line, predictor, season_lines (set),
      all_seasons (list), is_dark, log_x, log_y,
      x_bounds (x_min, x_max), y_bounds (y_min, y_max)

    Returns (output_path_str, error_str).  One of the two will be None.
    Requires: matplotlib  (pip install matplotlib)
    Requires: ffmpeg on PATH
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.animation as manim
    except ImportError:
        return None, "matplotlib not installed — run: pip install matplotlib"

    selected_dists = config["selected_dists"]
    selected_times = config["selected_times"]
    excluded_seasons = config["excluded_seasons"]
    best_filter = config["best_filter"]
    show_watts = config.get("show_watts", False)
    show_lb_line = config.get("show_lifetime_line", True)
    predictor = config.get("predictor", "None")
    season_lines = config.get("season_lines", set())
    all_seasons = config.get("all_seasons", [])
    is_dark = config.get("is_dark", False)
    log_x = config.get("log_x", True)
    log_y = config.get("log_y", False)
    x_bounds = config.get("x_bounds")
    y_bounds = config.get("y_bounds")

    sorted_seasons = sorted(all_seasons)
    season_idx = {s: i for i, s in enumerate(sorted_seasons)}

    # Colours
    bg = "#1c1c2e" if is_dark else "#ffffff"
    ax_bg = "#12122a" if is_dark else "#f8f9fa"
    fg = "#eeeeee" if is_dark else "#222222"
    grid_c = (1, 1, 1, 0.08) if is_dark else (0, 0, 0, 0.12)
    pb_color = (0.94, 0.94, 0.94) if is_dark else (0.16, 0.16, 0.16)
    pred_c = (0.6, 0.6, 0.6, 0.55)

    fig, ax = plt.subplots(figsize=(14, 8), facecolor=bg)
    ax.set_facecolor(ax_bg)

    def _y(pace: float) -> float:
        return compute_watts(pace) if show_watts else pace - 60.0

    y_label = "Watts" if show_watts else "Pace − 1:00 (sec/500m)"

    def _draw(frame_idx: int) -> None:
        ax.clear()
        ax.set_facecolor(ax_bg)

        if on_progress:
            on_progress(frame_idx, len(sim_dates))

        sim_date = sim_dates[frame_idx]
        wkts = workouts_before_date(
            rankable_efforts,
            sim_date,
            selected_dists,
            selected_times,
            excluded_seasons,
            best_filter,
        )

        # Compute bests
        lb: dict = {}
        lb_anchor: dict = {}
        sb: dict = {}
        sb_anchor: dict = {}
        for w in wkts:
            p, c, d = compute_pace(w), workout_cat_key(w), w.get("distance")
            if p is None or c is None or not d:
                continue
            s = get_season(w.get("date", ""))
            if c not in lb or p < lb[c]:
                lb[c] = p
                lb_anchor[c] = d
            k = (s, c)
            if k not in sb or p < sb[k]:
                sb[k] = p
                sb_anchor[k] = d

        # --- Scatter (per season) ---
        season_buckets: dict = {}  # season -> [(dist, y_val, pace, cat)]
        for w in wkts:
            p, c, d = compute_pace(w), workout_cat_key(w), w.get("distance")
            if p is None or c is None or not d or not (PACE_MIN <= p <= PACE_MAX):
                continue
            s = get_season(w.get("date", ""))
            season_buckets.setdefault(s, []).append((d, _y(p), p, c))

        for season, pts in season_buckets.items():
            if not pts:
                continue
            idx = season_idx.get(season, 0)
            h, sat, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
            base_rgb = _hsl_to_rgb(h, sat, l)
            lb_d, lb_y, reg_d, reg_y = [], [], [], []
            for dist, yv, pace, cat in pts:
                if cat in lb and abs(pace - lb[cat]) < 1e-9:
                    lb_d.append(dist)
                    lb_y.append(yv)
                else:
                    reg_d.append(dist)
                    reg_y.append(yv)
            if reg_d:
                ax.scatter(
                    reg_d,
                    reg_y,
                    color=base_rgb,
                    alpha=0.55,
                    s=22,
                    zorder=2,
                    linewidths=0,
                )
            if lb_d:
                ax.scatter(
                    lb_d,
                    lb_y,
                    color=base_rgb,
                    alpha=1.0,
                    s=55,
                    edgecolors=pb_color,
                    linewidths=1.5,
                    zorder=3,
                )

        # --- PB line ---
        if show_lb_line and lb:
            lb_pts = sorted(
                [(lb_anchor[c], _y(p)) for c, p in lb.items() if lb_anchor.get(c)],
                key=lambda pt: pt[0],
            )
            if len(lb_pts) >= 2:
                xs, ys = zip(*lb_pts)
                ax.plot(
                    xs,
                    ys,
                    color=pb_color,
                    linewidth=3.0,
                    alpha=0.88,
                    zorder=1,
                    solid_capstyle="round",
                )

        # --- Season best lines ---
        for season in season_lines:
            if season not in season_idx:
                continue
            idx = season_idx[season]
            h, sat, l = SEASON_PALETTE[idx % len(SEASON_PALETTE)]
            base_rgb = _hsl_to_rgb(h, sat, l)
            s_pts = sorted(
                [
                    (sb_anchor[(season, c)], _y(p))
                    for (s2, c), p in sb.items()
                    if s2 == season and sb_anchor.get((season, c))
                ],
                key=lambda pt: pt[0],
            )
            if len(s_pts) >= 2:
                xs, ys = zip(*s_pts)
                ax.plot(xs, ys, color=base_rgb, linewidth=1.5, alpha=0.75, zorder=1)

        # --- Prediction line ---
        if predictor == "pauls_law" and lb:
            for cat, pb_pace in lb.items():
                anchor = lb_anchor.get(cat)
                if not anchor:
                    continue
                pts_pred = []
                for d in RANKED_DIST_VALUES:
                    p_pred = pauls_law_pace(pb_pace, anchor, d)
                    if PACE_MIN <= p_pred <= PACE_MAX:
                        pts_pred.append((d, _y(p_pred)))
                if len(pts_pred) >= 2:
                    xs, ys = zip(*pts_pred)
                    ax.plot(
                        xs,
                        ys,
                        color=pred_c[:3],
                        alpha=pred_c[3],
                        linewidth=1.0,
                        zorder=0,
                    )

        elif predictor == "loglog" and lb:
            fit = loglog_fit(lb, lb_anchor)
            if fit:
                slope, intercept = fit
                pts_pred = []
                for d in RANKED_DIST_VALUES:
                    p_pred = loglog_predict_pace(slope, intercept, d)
                    if PACE_MIN <= p_pred <= PACE_MAX:
                        pts_pred.append((d, _y(p_pred)))
                if len(pts_pred) >= 2:
                    xs, ys = zip(*pts_pred)
                    ax.plot(
                        xs,
                        ys,
                        color=pred_c[:3],
                        alpha=pred_c[3],
                        linewidth=1.5,
                        zorder=0,
                    )

        # --- Axes styling ---
        ax.set_xscale("log" if log_x else "linear")
        ax.set_yscale("log" if log_y else "linear")
        if x_bounds:
            ax.set_xlim(x_bounds)
        if y_bounds:
            ax.set_ylim(y_bounds)

        ax.set_xlabel("Distance (m)", color=fg, fontsize=11)
        ax.set_ylabel(y_label, color=fg, fontsize=11)
        ax.tick_params(colors=fg, which="both")
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("bottom", "left"):
            ax.spines[spine].set_color(fg)
        ax.grid(True, alpha=0.18, color=grid_c[:3], linewidth=0.6)

        # --- Date overlay ---
        date_str = f"{sim_date.strftime('%B')} {sim_date.day}, {sim_date.year}"
        ax.text(
            0.99,
            0.97,
            date_str,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=17,
            fontweight="bold",
            color=fg,
            alpha=0.9,
        )

        if rower_name:
            ax.text(
                0.01,
                0.97,
                rower_name,
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=11,
                color=fg,
                alpha=0.65,
            )

    total = len(sim_dates)
    fps = max(6, min(15, total // 20 + 1)) if total > 20 else 6  # ~15-25s target

    ani = manim.FuncAnimation(fig, _draw, frames=total, interval=1000 // fps)

    try:
        writer = manim.FFMpegWriter(
            fps=fps,
            bitrate=2500,
            metadata={"title": rower_name or "Rowing Simulation"},
        )
        output_path = Path(output_path)
        ani.save(str(output_path), writer=writer, dpi=120)
        plt.close(fig)
        return str(output_path), None
    except Exception as e:
        plt.close(fig)
        return None, str(e)


# ---------------------------------------------------------------------------
# HyperDiv UI wiring (parked — uncomment and call from power_curve_page to re-enable)
# ---------------------------------------------------------------------------


def render_mp4_export_button(state, mp4_task, sim_config: dict) -> None:
    """
    Render the MP4 export icon button and its status indicators.

    Call this from the chart-settings row in power_curve_page() after adding
    `mp4_task = hd.task()` to the state setup.

    sim_config must contain:
      rankable_efforts, sim_dates (list[date]), selected_dists, selected_times,
      excluded_seasons, best_filter, show_watts, show_lifetime_line, predictor,
      season_lines (set), all_seasons (list), is_dark, log_x, log_y,
      x_bounds, y_bounds, output_path (str|Path), rower_name (str)
    """
    import hyperdiv as hd

    if mp4_task.running:
        hd.spinner()
    elif mp4_task.done and mp4_task.result:
        _mp4_path, _mp4_err = mp4_task.result
        if _mp4_err:
            with hd.tooltip(f"MP4 error: {_mp4_err}"):
                hd.icon("exclamation-triangle", font_color="danger")
        else:
            with hd.tooltip(f"Saved: {_mp4_path}"):
                hd.icon("check-circle", font_color="success-600")
    else:
        with hd.tooltip("Export MP4"):
            if hd.icon_button("film", font_size="small").clicked:
                rankable_efforts = sim_config["rankable_efforts"]
                sim_dates = sim_config["sim_dates"]
                output_path = sim_config["output_path"]
                rower_name = sim_config.get("rower_name", "")
                config = {
                    k: v
                    for k, v in sim_config.items()
                    if k
                    not in (
                        "rankable_efforts",
                        "sim_dates",
                        "output_path",
                        "rower_name",
                    )
                }
                mp4_task.run(
                    generate_mp4,
                    rankable_efforts,
                    sim_dates,
                    config,
                    output_path,
                    rower_name,
                )
