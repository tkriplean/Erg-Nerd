import hyperdiv as hd

from services.predictions import PREDICTORS, PREDICTORS_BY_KEY
from components.hyperdiv_extensions import grid_box

from services.rowing_utils import (
    ranked_distances,
    ranked_times,
)


def prediction_table(
    pred_rows: list,
    accuracy: dict,
    rl_available: bool = True,
    pauls_k: float = 5.0,
    machine: str = "rower",
) -> None:
    """
    Renders the multi-model prediction grid (Your PB, CP, Log-Log, Paul's Law,
    RowingLevel, Average) plus an accuracy footer row.  Pure renderer —
    ``pred_rows`` and ``accuracy`` are computed upstream by
    ``buildprediction_table_data`` (via the bundle lookup during animation
    or the slow-path snapshot when paused).

    Only renders when at least one row has any data.
    """

    if not any(
        r.get("pb_pace", None)
        or r.get("cp_pace", None)
        or r.get("loglog_pace", None)
        or r.get("pl_pace", None)
        or r.get("rl_pace", None)
        for r in pred_rows
    ):
        return

    from components.power_curve_page import PowerCurveState

    state = PowerCurveState()

    from components.app_context import your as _your

    _poss = _your()
    _poss_lower = _your(capitalize=False)
    _pl_tip = (
        f"Predicts +{pauls_k:.1f} s/500m for each doubling of distance "
        f"({_poss_lower} personalised value), applied from each anchor PB and averaged."
    )
    _PRED_COLS = [("pb", f"{_poss} PB", f"{_poss} personal best for each event.")]
    for _p in PREDICTORS:
        if _p.key == "none":
            continue
        if _p.key == "rowinglevel" and not rl_available:
            continue
        _tip = _pl_tip if _p.key == "pauls_law" else _p.extended_description
        _label = "Average" if _p.key == "average" else _p.name
        _PRED_COLS.append((_p.key, _label, _tip))

    _HEADER_BG = "neutral-100"
    _ACC_BG = "neutral-100"

    # CSS Grid: fixed Event column + one 1fr column per prediction model
    _col_template = "8rem " + " ".join(["1fr"] * len(_PRED_COLS))

    with grid_box(
        grid_template_columns=_col_template,
        border="1px solid neutral-200",
        border_radius="medium",
        width="100%",
        overflow="hidden",
    ):
        # ── header row ────────────────────────────────────────────────────
        with hd.box(
            padding=1,
            background_color=_HEADER_BG,
            border_right="1px solid neutral-200",
            border_bottom="1px solid neutral-200",
        ):
            hd.text("Event", font_weight="semibold", font_size="small")

        for col_key, col_label, col_tip in _PRED_COLS:
            with hd.scope(col_key):
                draw_header_cell(col_label, col_tip, _HEADER_BG)

        # ── data rows ─────────────────────────────────────────────────────
        for _ri, _row in enumerate(pred_rows):
            with hd.scope(_row["label"]):
                _row_bg = "neutral-50" if _ri % 2 == 0 else "neutral-0"
                _pb_raw = _row.get("pb_raw")

                if _row["event_type"] == "dist":
                    _ev_idx = next(
                        (
                            i
                            for i, (d, _) in enumerate(ranked_distances(machine))
                            if d == _row["event_value"]
                        ),
                        None,
                    )
                    _ev_enabled = (
                        state.dist_enabled[_ev_idx] if _ev_idx is not None else False
                    )
                else:
                    _ev_idx = next(
                        (
                            i
                            for i, (t, _) in enumerate(ranked_times(machine))
                            if t == _row["event_value"]
                        ),
                        None,
                    )
                    _ev_enabled = (
                        state.time_enabled[_ev_idx] if _ev_idx is not None else False
                    )

                # Event cell
                with hd.box(
                    padding=1,
                    background_color=_row_bg,
                    border_top="1px solid neutral-200",
                    border_right="1px solid neutral-200",
                ):
                    with hd.hbox(gap=0.5, align="center"):
                        with hd.tooltip(
                            "Include this event's PB in prediction "
                            "calculations? More accurate predictions "
                            "when you include only current, max-effort results."
                        ):
                            _ev_sw = hd.switch("", checked=_ev_enabled, size="small")
                        if _ev_sw.changed:
                            if _row["event_type"] == "dist":
                                _flags = list(state.dist_enabled)
                                _flags[_ev_idx] = _ev_sw.checked
                                state.dist_enabled = tuple(_flags)
                            else:
                                _flags = list(state.time_enabled)
                                _flags[_ev_idx] = _ev_sw.checked
                                state.time_enabled = tuple(_flags)
                        hd.text(
                            _row["label"],
                            font_weight="semibold",
                            font_size="small",
                            font_color="neutral-600" if _ev_enabled else "neutral-400",
                        )

                # Prediction cells
                for col_key, col_label, _tip in _PRED_COLS:
                    with hd.scope(col_key):
                        _pace_val = _row.get(f"{col_key}_pace")
                        _result_val = _row.get(f"{col_key}_result")
                        _pred_raw = _row.get(f"{col_key}_raw")

                        draw_prediction_cell(
                            col_key,
                            col_label,
                            _pb_raw,
                            _pace_val,
                            _result_val,
                            _pred_raw,
                            _tip,
                            _row_bg,
                            _ev_enabled,
                        )

        # ── accuracy row ──────────────────────────────────────────────────
        # Accuracy label cell
        with hd.box(
            padding=1,
            background_color=_ACC_BG,
            border_top="2px solid neutral-300",
            border_right="1px solid neutral-200",
        ):
            with hd.hbox(gap=0.5, align="center"):
                hd.text(
                    "Accuracy",
                    font_size="small",
                    font_weight="semibold",
                    font_color="neutral-600",
                )
                with hd.tooltip(
                    "RMSE (root mean square error) in sec/500m and R² "
                    "across enabled events where both a prediction and "
                    "a PB exist. Lower RMSE and higher R² are better. "
                    "Disabled events (toggled off) are excluded."
                ):
                    hd.icon(
                        "question-circle",
                        font_size="small",
                        font_color="neutral-600",
                    )

        # PB column accuracy cell (always —)
        with hd.box(
            padding=0.5,
            background_color=_ACC_BG,
            border_top="2px solid neutral-300",
        ):
            hd.text("—", font_size="small", font_color="neutral-600")

        # Other model accuracy cells
        for _ck, _cl, _ct in _PRED_COLS[1:]:
            with hd.scope(f"acc_{_ck}"):
                _av = accuracy.get(_ck, {"rmse": None, "r2": None, "n": 0})
                with hd.box(
                    padding=0.5,
                    background_color=_ACC_BG,
                    border_top="2px solid neutral-300",
                ):
                    if _av["rmse"] is not None:
                        hd.text(
                            f"{_av['rmse']:.2f}s",
                            font_size="large",
                            font_weight="semibold",
                            font_color="neutral-800",
                        )
                        if _av["r2"] is not None:
                            hd.text(
                                f"R²={_av['r2']:.3f}",
                                font_size="small",
                                font_color="neutral-600",
                            )
                        hd.text(
                            f"n={_av['n']}",
                            font_size="small",
                            font_color="neutral-600",
                        )
                    else:
                        hd.text("—", font_size="small", font_color="neutral-600")


@hd.cached
def draw_header_cell(col_label, col_tip, background_color):
    with hd.box(
        padding=(0.75, 0.5),
        background_color=background_color,
        border_bottom="1px solid neutral-200",
    ):
        with hd.hbox(gap=0.5, align="center"):
            hd.text(col_label, font_weight="semibold", font_size="small")
            with hd.tooltip(col_tip):
                hd.icon(
                    "question-circle",
                    font_size="small",
                    font_color="neutral-500",
                )


@hd.cached
def draw_prediction_cell(
    col_key,
    col_label,
    _pb_raw,
    _pace_val,
    _result_val,
    _pred_raw,
    _tip,
    _row_bg,
    _ev_enabled,
):
    has_delta = col_key != "pb" and _pred_raw is not None and _pb_raw is not None
    if has_delta:
        _delta = _pred_raw - _pb_raw
        _delta_s = f"{_delta:+.1f}s"
        _delta_color = (
            "success-600"
            if _delta < 0
            else "danger-600"
            if _delta > 0
            else "neutral-500"
        )
    _is_pb_col = col_key == "pb"
    _pace_color = "neutral-300" if _is_pb_col and not _ev_enabled else "neutral-900"
    _result_color = "neutral-300" if _is_pb_col and not _ev_enabled else "neutral-500"
    with hd.box(
        padding=0.5,
        background_color=_row_bg,
        border_top="1px solid neutral-200",
    ):
        if _pace_val:
            with hd.hbox(gap=0.5):
                hd.text(
                    _pace_val,
                    font_size="large",
                    font_weight="semibold",
                    font_color=_pace_color,
                )
                if has_delta:
                    hd.text(
                        _delta_s,
                        font_size="small",
                        font_color=_delta_color,
                    )
            hd.text(
                _result_val or "",
                font_size="x-small",
                font_color=_result_color,
            )
        else:
            hd.text(
                "—",
                font_size="small",
                font_color="neutral-300",
            )
