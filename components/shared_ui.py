import hyperdiv as hd
from services.local_storage_compression import decompress_workouts
from services.formatters import machine_label
from services.rowing_utils import get_season
from components.concept2_sync import sync_from_context


def global_filter_ui(gstate, ctx) -> None:
    """
    Render the global Season and Machine filter controls.
    Called from the nav-bar row in _dashboard_view.

    gstate.excluded_seasons  tuple[str]  — seasons hidden globally
    gstate.machine           str         — "All" or a machine type string
    all_seasons              list[str]   — sorted newest-first
    machine_types            list[str]   — unique machine types across all workouts
    """

    # Determine the full season list and machine types from localStorage workouts
    # so the filter UI can render even before any page has fetched data.
    # We do a lightweight read here; concept2_sync() on the active page handles
    # the full data load.
    _ls_wkts_meta = hd.local_storage.get_item("workouts")

    all_seasons: list = []
    machine_types: list = []
    if _ls_wkts_meta.done and _ls_wkts_meta.result:
        try:
            _wkts = decompress_workouts(_ls_wkts_meta.result)
            _season_set: set = set()
            _mtype_set: set = set()
            for _w in _wkts.values():
                _s = get_season(_w.get("date", ""))
                if _s != "Unknown":
                    _season_set.add(_s)
                _mt = _w.get("type", "rower")
                if _mt:
                    _mtype_set.add(_mt)
            all_seasons = sorted(_season_set, reverse=True)
            # In synthetic mode the augmented machines (skierg, bike) are never
            # written to localStorage, so inject them manually here.
            if SYNTHETIC_MODE:
                _mtype_set.update({"skierg", "bike"})
            machine_types = sorted(_mtype_set)
        except Exception:
            pass

    with hd.hbox(gap=1, align="center", min_height="30px"):
        # ── Season dropdown ────────────────────────────────────────────────────
        if all_seasons:
            _excl = set(gstate.excluded_seasons) & set(all_seasons)
            if not _excl:
                _seas_btn_lbl = "All Seasons"
            elif len(all_seasons) - len(_excl) == 1:
                _seas_btn_lbl = next(s for s in all_seasons if s not in _excl)
            else:
                _seas_btn_lbl = (
                    f"{len(all_seasons) - len(_excl)} of {len(all_seasons)} seasons"
                )

            with hd.hbox(gap=0, align="center"):
                hd.text("Across", font_size="small")

                with hd.dropdown() as _se_dd:
                    _se_btn = hd.button(
                        _seas_btn_lbl,
                        caret=True,
                        size="small",
                        variant="text",
                        slot=_se_dd.trigger,
                        font_weight="bold",
                        font_color="neutral-800",
                        font_size="small",
                    )
                    if _se_btn.clicked:
                        _se_dd.opened = not _se_dd.opened

                    with hd.box(
                        padding=1, gap=0.5, background_color="neutral-50", min_width=14
                    ):
                        # Convenience shortcuts
                        _shortcuts = [
                            ("All Seasons", 0),
                            ("Last Season", 1),
                            ("Last 2 Seasons", 2),
                            ("Last 5 Seasons", 5),
                        ]
                        with hd.box(gap=0.25, padding_bottom=0.5):
                            for _lbl, _n in _shortcuts:
                                if _n == 0 or len(all_seasons) >= _n:
                                    with hd.scope(f"shortcut_{_n}"):
                                        _active = (_n == 0 and not _excl) or (
                                            _n > 0
                                            and len(_excl)
                                            == max(0, len(all_seasons) - _n)
                                            and all(
                                                s in _excl for s in all_seasons[_n:]
                                            )
                                        )
                                        if hd.button(
                                            _lbl,
                                            size="small",
                                            variant="primary" if _active else "text",
                                            width="100%",
                                        ).clicked:
                                            if _n == 0:
                                                gstate.excluded_seasons = ()
                                            else:
                                                gstate.excluded_seasons = tuple(
                                                    sorted(all_seasons[_n:])
                                                )
                                            _se_dd.opened = False

                        hd.divider()

                        # Per-season checkboxes
                        with hd.box(gap=0.25, padding_top=0.5):
                            with hd.scope(str(gstate.excluded_seasons)):
                                for season in all_seasons:
                                    with hd.scope(f"gs_{season}"):
                                        _is_sel = season not in gstate.excluded_seasons
                                        cb = hd.checkbox(season, checked=_is_sel)
                                        if cb.changed:
                                            _e = set(gstate.excluded_seasons)
                                            if cb.checked:
                                                _e.discard(season)
                                            else:
                                                _e.add(season)
                                            gstate.excluded_seasons = tuple(sorted(_e))
                                        if cb.checked != _is_sel:
                                            cb.checked = _is_sel

        # ── Machine selector (only when >1 type) ───────────────────────────────
        if len(machine_types) > 1:
            with hd.hbox(gap=0, align="center"):
                hd.text("On", font_size="small")

                machine_sel = hd.select(
                    value=gstate.machine,
                    size="small",
                    combobox_style=hd.style(
                        border="none",
                        font_size="small",
                        font_color="neutral-800",
                        font_weight="bold",
                    ),
                    display_input_style=hd.style(width="86px"),
                    expand_icon_style=hd.style(margin_left="8px", font_size="x-small"),
                )
                with machine_sel:
                    hd.option("All Machines", value="All")
                    for mt in machine_types:
                        with hd.scope(mt):
                            hd.option(machine_label(mt), value=mt)
                if machine_sel.changed:
                    gstate.machine = machine_sel.value


"""Render a single h1-sized dropdown for header state fields."""


def header_dropdown(
    state,
    *,
    key: str,
    labels: dict[str, str],
    current_value: str,
    field: str,
) -> None:
    cur_label = labels.get(current_value, current_value)
    with hd.scope(key):
        with hd.dropdown() as dd:
            btn = hd.button(
                cur_label,
                caret=True,
                size="small",
                font_color="neutral-800",
                font_size=2,
                font_weight="bold",
                border="none",
                slot=dd.trigger,
                hover_background_color="neutral-0",
                label_style=hd.style(padding_right=0),
            )
            if btn.clicked:
                dd.opened = not dd.was_opened
            with hd.box(gap=0.1, background_color="neutral-0", min_width=20):
                for val, lbl in labels.items():
                    with hd.scope(f"{key}_{val}"):
                        item = hd.button(
                            lbl,
                            size="small",
                            variant="primary" if current_value == val else "text",
                            width="100%",
                            border_radius="small",
                            font_size="medium",
                            font_color="neutral-0"
                            if current_value == val
                            else "neutral-800",
                            label_style=hd.style(padding_top=0.5, padding_bottom=0.5),
                            hover_background_color="neutral-100",
                        )
                        if item.clicked:
                            setattr(state, field, val)
                            dd.opened = False
