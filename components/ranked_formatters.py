"""
Formatting helpers and the shared result-table renderer for ranked workouts.

Exported:
  _MACHINE_LABELS     — dict mapping machine type strings to display labels
  _fmt_date()         — ISO date string → "Mon DD, YYYY"
  fmt_split()         — tenths-of-a-second → "M:SS.t"
  _pace_tenths()      — compute pace tenths from a workout dict
  _fmt_distance()     — meters → "N,NNNm"
  _fmt_hr()           — heart-rate dict → "NNN bpm"
  _machine_label()    — machine type string → human label
  _fmt_watts()        — compute and format watts from a workout dict
  result_table()      — HyperDiv table renderer with per-row view links.
                        Each row gets a link to /session/{id} so the browser
                        can navigate, bookmark, or open in a new tab.
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

_ROWS_PER_PAGE = 25
_COL_WIDTHS = {
    "Date": 10,
    "Type": 7,
    "Distance": 7,
    "Time": 7,
    "Pace /500m": 7,
    "Watts": 5,
    "Drag": 5,
    "SPM": 4,
    "HR": 8,
}


def result_table(
    results: list,
    *,
    paginate: bool = True,
) -> None:
    """
    Render a data table of workout results.

    Each row has a link to /session/{id} for drill-down navigation.
    """
    if not results:
        hd.text("No results.", font_color="neutral-500", font_size="small")
        return

    page_state = hd.state(page=0)
    total = len(results)
    per_page = _ROWS_PER_PAGE if paginate else total
    total_pages = max(1, (total + per_page - 1) // per_page)
    # clamp page if results changed
    if page_state.page >= total_pages:
        page_state.page = total_pages - 1

    page_start = page_state.page * per_page
    page_rows = results[page_start : page_start + per_page]

    types = {r.get("type") for r in results}
    multi_type = len(types) > 1

    _theme = hd.theme()
    # row_hover_bg = "neutral-100"
    header_color = "neutral-500"
    text_size = "small"

    with hd.box(
        border="1px solid neutral-200",
        border_radius="medium",
        # overflow="hidden"
    ):
        # Header row
        with hd.hbox(
            padding=(0.4, 0.75),
            background_color="neutral-50",
            border_bottom="1px solid neutral-200",
            gap=1,
        ):
            hd.text(
                "Date",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Date"],
            )
            if multi_type:
                hd.text(
                    "Type",
                    font_color=header_color,
                    font_size=text_size,
                    font_weight="semibold",
                    width=_COL_WIDTHS["Type"],
                )
            hd.text(
                "Distance",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Distance"],
            )
            hd.text(
                "Time",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Time"],
            )
            hd.text(
                "Pace /500m",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Pace /500m"],
            )
            hd.text(
                "Watts",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Watts"],
            )
            hd.text(
                "Drag",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["Drag"],
            )
            hd.text(
                "SPM",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["SPM"],
            )
            hd.text(
                "HR",
                font_color=header_color,
                font_size=text_size,
                font_weight="semibold",
                width=_COL_WIDTHS["HR"],
            )
            hd.box(width=2.5)  # view link column

        # Data rows
        for r in page_rows:
            with hd.scope(r.get("id", id(r))):
                with hd.hbox(
                    padding=(0.4, 0.75),
                    gap=1,
                    align="center",
                    border_bottom="1px solid neutral-100",
                    # hover_background_color=row_hover_bg,
                ):
                    hd.text(
                        _fmt_date(r.get("date", "")),
                        font_size=text_size,
                        width=_COL_WIDTHS["Date"],
                    )
                    if multi_type:
                        hd.text(
                            _machine_label(r.get("type", "")),
                            font_size=text_size,
                            width=_COL_WIDTHS["Type"],
                        )
                    hd.text(
                        _fmt_distance(r.get("distance")),
                        font_size=text_size,
                        width=_COL_WIDTHS["Distance"],
                    )
                    tf = r.get("time_formatted") or (
                        format_time(r["time"]) if r.get("time") else "—"
                    )
                    hd.text(tf, font_size=text_size, width=_COL_WIDTHS["Time"])
                    hd.text(
                        fmt_split(_pace_tenths(r)),
                        font_size=text_size,
                        width=_COL_WIDTHS["Pace /500m"],
                    )
                    hd.text(
                        _fmt_watts(r), font_size=text_size, width=_COL_WIDTHS["Watts"]
                    )
                    hd.text(
                        str(r.get("drag_factor") or "—"),
                        font_size=text_size,
                        width=_COL_WIDTHS["Drag"],
                    )
                    hd.text(
                        str(r.get("stroke_rate") or "—"),
                        font_size=text_size,
                        width=_COL_WIDTHS["SPM"],
                    )
                    hd.text(
                        _fmt_hr(r.get("heart_rate")),
                        font_size=text_size,
                        width=_COL_WIDTHS["HR"],
                    )

                    hd.link(
                        "↗",
                        href=f"/session/{r.get('id')}",
                        font_size="small",
                        font_color="neutral-400",
                        underline=False,
                        width=2.5,
                        text_align="center",
                    )

    # Pagination controls
    if paginate and total_pages > 1:
        with hd.hbox(gap=1, align="center", padding=(0.5, 0), justify="center"):
            prev_btn = hd.icon_button(
                "chevron-left",
                font_size="small",
                disabled=(page_state.page == 0),
            )
            if prev_btn.clicked and page_state.page > 0:
                page_state.page -= 1

            hd.text(
                f"{page_state.page + 1} / {total_pages}",
                font_color="neutral-500",
                font_size="small",
            )

            next_btn = hd.icon_button(
                "chevron-right",
                font_size="small",
                disabled=(page_state.page >= total_pages - 1),
            )
            if next_btn.clicked and page_state.page < total_pages - 1:
                page_state.page += 1
