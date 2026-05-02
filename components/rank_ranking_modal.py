"""
Rankings-browser modal for the Rank Page.

Renders a paginated dialog listing the ranking pool for one event, with the
user's row highlighted. Opens to the page containing the logged-in (or
public-profile) rower's rank by default; users can step through pages via
the navigation controls.
"""

from __future__ import annotations

from typing import Optional

import hyperdiv as hd

from services.formatters import fmt_split, format_time, fmt_distance


def _season_label(season) -> str:
    """Format a season as 'YYYY-YY'.

    Accepts an int season-end year (Concept2 ranking entries) or an existing
    'YYYY-YY' string (passthrough for the user's own entry).
    """
    if season is None or season == "":
        return ""
    if isinstance(season, str):
        return season if "-" in season else ""
    try:
        y = int(season)
    except (TypeError, ValueError):
        return ""
    return f"{y - 1}-{str(y)[2:]}"


def _pace_tenths_for(
    event_kind: str, event_value: int, value_tenths: int
) -> Optional[int]:
    """Return pace in tenths-of-a-second per 500m for a ranking entry."""
    if value_tenths is None or value_tenths <= 0:
        return None
    if event_kind == "dist":
        # value_tenths = tenths of a second for event_value meters
        return int(round(value_tenths * 500 / event_value))
    # time event — event_value = tenths of a second, value_tenths = meters
    if value_tenths <= 0:
        return None
    return int(round(event_value * 500 / value_tenths))


def _result_str(event_kind: str, event_value: int, value_tenths: int) -> str:
    if value_tenths is None:
        return "—"
    if event_kind == "dist":
        return format_time(value_tenths)
    return fmt_distance(value_tenths)


def rank_dialog_title(r: dict) -> str:

    event_name = f"{r["event_label"]} trials"
    wc = "Hwt " if r["weight_class"] == "H" else "Lwt " if r["weight_class"] else ""
    g = 'Male' if r['gender'] == "M" else "Female"

    return f"Ranked {event_name} for {wc}{g}s, Age {r['age']}"


def render_rankings_modal(
    dialog: hd.dialog,
    *,
    pool: list[dict],
    event_kind: str,
    event_value: int,
    user_rank: int,
    user_value_tenths: int,
    user_row_label: str,
    user_age: int = 0,
    user_date_label: str = "",
    user_season: Optional[str] = None,
) -> None:
    """Populate ``dialog`` with a rankings table. Call while dialog is open."""
    # Sort pool best → worst for display.
    if event_kind == "dist":
        ordered = sorted(pool, key=lambda e: e.get("value_tenths") or 10**12)
    else:
        ordered = sorted(pool, key=lambda e: -(e.get("value_tenths") or 0))

    # Splice the user's own performance into the displayed list at their rank.
    user_entry = {
        "name": "You",
        "age": user_age,
        "country": "",
        "value_tenths": user_value_tenths,
        "verified": "",
        "season": user_season,
        "_is_user": True,
    }
    insert_idx = max(0, min(len(ordered), user_rank - 1))
    ordered.insert(insert_idx, user_entry)
    total = len(ordered)

    page_size = 100
    total_pages = max(1, (total + page_size - 1) // page_size)
    user_idx = insert_idx  # 0-based position of user's row in ordered list
    user_page = user_idx // page_size  # 0-based page containing the user

    # Per-event modal state: when the (event_kind, event_value, total) signature
    # changes (modal re-opened on a different row), reset the page to the user's
    # page so we always land on the rower being viewed.
    page_state = hd.state(sig=None, page=0)
    sig = (event_kind, event_value, total, user_idx)
    if page_state.sig != sig:
        page_state.sig = sig
        page_state.page = user_page

    cur_page = max(0, min(total_pages - 1, page_state.page))
    page_lo = cur_page * page_size
    page_hi = min(total, page_lo + page_size)

    with dialog:
        with hd.box(gap=0.5, width="100%"):
            with hd.hbox(gap=3, align="center", justify="center"):
                hd.text(
                    f"{total:,} ranked performances",
                    font_color="neutral-500",
                    font_size="small",
                )

                _render_pagination(page_state, cur_page, total_pages, user_page)

            with hd.box(
                direction="horizontal",
                gap=0,
                padding=0.3,
                background_color="neutral-50",
                font_weight="bold",
                font_size="small",
            ):
                hd.text("Rank", width="4rem")
                hd.text("Season", width="5rem")
                hd.text("Name", width="14rem")
                hd.text("Age", width="3rem")
                hd.text("Country", width="5rem")
                hd.text("Pace", width="5rem")
                hd.text("Result", width="6rem")
                hd.text("Verified", width="5rem")

            with hd.box(
                max_height="60vh",
                gap=0,
            ):
                for i in range(page_lo, page_hi):
                    with hd.scope(f"row_{i}"):
                        entry = ordered[i]
                        rank_1b = i + 1
                        is_user = bool(entry.get("_is_user"))
                        bg = "primary-100" if is_user else None
                        v = entry.get("value_tenths") or 0
                        pt = _pace_tenths_for(event_kind, event_value, v)
                        result = _result_str(event_kind, event_value, v)
                        with hd.box(
                            direction="horizontal",
                            padding=0.3,
                            background_color=bg,
                            font_size="small",
                            border_bottom="1px solid neutral-100",
                        ):
                            hd.text(f"{rank_1b:,}", width="4rem")
                            hd.text(
                                _season_label(entry.get("season")),
                                width="5rem",
                            )
                            hd.text(entry.get("name", ""), width="14rem")
                            hd.text(str(entry.get("age", "")), width="3rem")
                            hd.text(entry.get("country", ""), width="5rem")
                            hd.text(fmt_split(pt) if pt else "—", width="5rem")
                            hd.text(result, width="6rem")
                            hd.text(entry.get("verified", ""), width="5rem")


def _render_pagination(
    page_state, cur_page: int, total_pages: int, user_page: int
) -> None:
    """Render Prev / page numbers / Next controls. Pages are 0-based internally,
    1-based in the UI. Highlights both the current page and the user's page.
    """
    if total_pages <= 1:
        return

    with hd.hbox(
        gap=0.25,
        align="center",
        justify="center",
        padding_top=0.5,
    ):
        prev_btn = hd.button(
            "‹ Prev",
            size="small",
            variant="text",
            disabled=cur_page == 0,
        )
        if prev_btn.clicked and cur_page > 0:
            page_state.page = cur_page - 1

        # Compact page-number list: first, last, current ±2, plus the user's
        # page.  Ellipses fill the gaps so the bar stays a constant width.
        anchors = {0, total_pages - 1, cur_page, user_page}
        for d in (-2, -1, 1, 2):
            anchors.add(cur_page + d)
        visible = sorted(p for p in anchors if 0 <= p < total_pages)

        prev_p = -1
        for p in visible:
            if p > prev_p + 1:
                with hd.scope(f"gap_{p}"):
                    hd.text("…", font_color="neutral-400", font_size="small")
            with hd.scope(f"page_{p}"):
                is_cur = p == cur_page
                is_user = p == user_page and p != cur_page
                btn = hd.button(
                    str(p + 1),
                    size="small",
                    variant=(
                        "primary" if is_cur else ("default" if is_user else "text")
                    ),
                )
                if btn.clicked:
                    page_state.page = p
            prev_p = p

        next_btn = hd.button(
            "Next ›",
            size="small",
            variant="text",
            disabled=cur_page >= total_pages - 1,
        )
        if next_btn.clicked and cur_page < total_pages - 1:
            page_state.page = cur_page + 1
