"""
Erg Nerd — main application entry point.

Run with:  python app.py
The app opens at http://localhost:8888
"""

import json
import os
from config import SYNTHETIC_MODE


# Load .env before importing services so credentials are available.
def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


_load_dotenv()

import hyperdiv as hd
from services.concept2 import (
    Concept2Client,
    clear_token,
    exchange_code,
    extract_c2_profile,
    get_authorization_url,
    get_client,
    get_server_url,
    parse_callback_query,
    save_token,
)
from services.local_storage_compression import compress_workouts, decompress_workouts
from components.intervals_page import intervals_page
from components.profile_page import profile_page
from components.power_curve_page import power_curve_page
from components.race_page import race_page
from components.ergnerd_animation import ergnerd_animation
from components.workout_page import workout_page
from components.sessions_page import sessions_page
from components.volume_page import volume_page
from components.concept2_sync import concept2_sync
from components.view_context import (
    build_owner_context,
    build_public_context,
)
from components.public_banner import public_banner
from services import public_profiles


# ---------------------------------------------------------------------------
# OAuth callback view
# ---------------------------------------------------------------------------


def _oauth_callback_view(query_args: str, app_state) -> None:
    task = hd.task()
    params = parse_callback_query(query_args)

    if "error" in params:
        with hd.box(gap=2, padding=4, align="center"):
            hd.icon("x-circle", font_color="danger", font_size=3)
            hd.h3("Authorization failed")
            hd.text(
                params.get("error_description", params["error"]),
                font_color="neutral-600",
            )
            if hd.button("Try again", variant="primary").clicked:
                hd.location().go(path="/")
        return

    code = params.get("code")
    if not code:
        hd.alert(
            "No authorization code received from Concept2.",
            variant="danger",
            opened=True,
        )
        return

    def do_exchange(code: str):
        token_data = exchange_code(code)
        # Build a temporary client to look up the user ID and profile
        temp_client = Concept2Client(token_data["access_token"])
        user_data = temp_client.get_user()["data"]
        user_id = str(user_data["id"])
        save_token(token_data, user_id)
        profile = extract_c2_profile(user_data)
        return user_id, profile

    task.run(do_exchange, code)

    with hd.box(gap=3, padding=4, align="center"):
        if task.running:
            hd.spinner()
            hd.text("Connecting to Concept2…", font_color="neutral-600")
        elif task.error:
            hd.icon("x-circle", font_color="danger", font_size=3)
            hd.h3("Connection failed")
            hd.text(str(task.error), font_color="neutral-600")
            if hd.button("Try again", variant="primary").clicked:
                hd.location().go(path="/")
        else:
            # Task done — stash user_id and Concept2 profile for the next render
            user_id, c2_profile = task.result
            app_state.pending_user_id = user_id
            app_state.pending_profile = c2_profile
            hd.location().go(path="/")


# ---------------------------------------------------------------------------
# Login view
# ---------------------------------------------------------------------------


def _login_view() -> None:
    try:
        auth_url = get_authorization_url()
        missing_credentials = False
    except EnvironmentError:
        auth_url = ""
        missing_credentials = True

    _theme = hd.theme()

    with hd.box(
        height="100vh",
        align="center",
        justify="center",
        background_color="neutral-300",
    ):
        with hd.box(
            gap=4,
            padding=6,
            align="center",
            border_radius="large",
            background_color="neutral-50",
            border="1px solid neutral-100",
            width=34,
        ):
            with hd.box(align="center", gap=1):
                hd.h1("Erg Nerd", font_color="primary")
                hd.text(
                    "Fancy Concept2 data visuals to help you procrastinate your next workout",
                    text_align="center",
                    font_color="neutral-700",
                )

            ergnerd_animation(width=22, theme="dark" if _theme.is_dark else "light")

            with hd.box(gap=1, align="center"):
                if missing_credentials:
                    hd.alert(
                        "CONCEPT2_CLIENT_ID and CONCEPT2_CLIENT_SECRET are not set. "
                        "Copy .env.example to .env and fill in your credentials, "
                        "then restart the app.",
                        variant="warning",
                        opened=True,
                    )
                else:
                    hd.link(
                        "Connect with Concept2",
                        href=auth_url,
                        target="_self",
                        underline=False,
                        font_color="neutral-0",
                        background_color="primary",
                        padding=1,
                        border_radius="medium",
                        font_weight="semibold",
                    )
                hd.text(
                    "Your data stored in your browser, not on our server.",
                    font_color="neutral-500",
                    font_size="small",
                )


# ---------------------------------------------------------------------------
# Page routing
# ---------------------------------------------------------------------------

# Maps page name → URL path and back.  "/" falls back to the default page.
_PAGES_ROUTES: dict[str, str] = {
    "Sessions": "/sessions",
    "Volume": "/volume",
    "Intervals": "/intervals",
    "Power Curve": "/power_curve",
    "Race": "/race",
    "Profile": "/profile",
}
_ROUTES_PAGES: dict[str, str] = {v: k for k, v in _PAGES_ROUTES.items()}
_DEFAULT_PAGE = "Power Curve"


# ---------------------------------------------------------------------------
# Scroll-to-top on SPA navigation
# ---------------------------------------------------------------------------

_SCROLL_TO_TOP_JS = """
(function () {
    if (window.__scrollToTopInstalled) return;
    window.__scrollToTopInstalled = true;
    var _orig = history.pushState;
    history.pushState = function () {
        _orig.apply(this, arguments);
        window.scrollTo({ top: 0, behavior: "instant" });
    };
    window.addEventListener("popstate", function () {
        window.scrollTo({ top: 0, behavior: "instant" });
    });
})();
"""


class _ScrollToTop(hd.Plugin):
    _name = "ScrollToTop"
    _assets = [hd.Plugin.js(_SCROLL_TO_TOP_JS)]


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def _app_footer() -> None:
    import datetime

    year = datetime.date.today().year

    with hd.box(
        padding=2, background_color="neutral-700", margin_top=2, align="center", gap=2
    ):
        with hd.hbox(gap=2, align="start", justify="space-between", max_width="750px"):
            with hd.box(gap=1.5, grow=True):
                ergnerd_animation(
                    width=15, theme="light" if hd.theme().is_dark else "dark"
                )

                with hd.link(href="https://www.buymeacoffee.com/ergnerd") as bmc:
                    hd.image(
                        src="https://img.buymeacoffee.com/button-api/?text=Support Erg Nerd&emoji=☕&slug=ergnerd&button_colour=5F7FFF&font_colour=ffffff&font_family=Lato&outline_colour=000000&coffee_colour=FFDD00"
                    )

            # with hd.box(
            #     gap=0.5,
            #     grow=True,
            #     border_left="1px solid neutral-500",
            #     margin_top=1,
            #     padding_left=0.5,
            # ):
            #     for page_name, path in _PAGES_ROUTES.items():
            #         with hd.scope(f"footer_{page_name}"):
            #             hd.link(
            #                 page_name,
            #                 href=path,
            #                 target="_self",
            #                 font_color="neutral-300",
            #                 font_size="small",
            #                 underline=False,
            #             )

        with hd.box(gap=0.3, align="center"):
            with hd.hbox(align="center", gap=1):
                with hd.hbox(gap=0.2):
                    hd.text(
                        "Built by ",
                        font_color="neutral-300",
                        font_size="small",
                    )
                    hd.link(
                        "Travis Kriplean",
                        href="https://traviskriplean.com",
                        font_size="small",
                        font_color="primary-300",
                    )

                hd.text("|", font_color="neutral-500")

                hd.icon_link(
                    "github",
                    "Open source code",
                    href="https://github.com/tkriplean/Erg-Nerd",
                    background_color="neutral-700",
                    font_color="neutral-200",
                    hover_background_color="neutral-700",
                )

            with hd.hbox(align="center"):
                hd.text(
                    f"© {year} Travis Kriplean. All rights reserved.",
                    font_color="neutral-400",
                    font_size="small",
                )


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------


def _global_filter_ui(gstate, all_seasons: list, machine_types: list) -> None:
    """
    Render the global Season and Machine filter controls.
    Called from the nav-bar row in _dashboard_view.

    gstate.excluded_seasons  tuple[str]  — seasons hidden globally
    gstate.machine           str         — "All" or a machine type string
    all_seasons              list[str]   — sorted newest-first
    machine_types            list[str]   — unique machine types across all workouts
    """
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

        with hd.scope("global_season_dd"):
            with hd.dropdown() as _se_dd:
                _se_btn = hd.button(
                    _seas_btn_lbl,
                    caret=True,
                    size="small",
                    variant="neutral",
                    slot=_se_dd.trigger,
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
                                        and len(_excl) == max(0, len(all_seasons) - _n)
                                        and all(s in _excl for s in all_seasons[_n:])
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
        with hd.scope("global_machine_sel"):
            from services.formatters import machine_label

            machine_sel = hd.select(value=gstate.machine, size="small")
            with machine_sel:
                hd.option("All Machines", value="All")
                for mt in machine_types:
                    with hd.scope(mt):
                        hd.option(machine_label(mt), value=mt)
            if machine_sel.changed:
                gstate.machine = machine_sel.value


def _dashboard_view(ctx, app_state, path_suffix: str | None = None) -> None:
    _ScrollToTop()

    is_public = ctx.mode == "public"

    # Owner mode: fetch user profile for the display-name link. Public mode:
    # ctx.display_name is pre-populated from the scrubbed public profile.
    user_task = hd.task() if not is_public else None
    if user_task is not None:

        def fetch_user():
            return ctx.client.get_user().get("data", {})

        user_task.run(fetch_user)

    _theme = hd.theme()
    loc = hd.location()

    # Active path used to dispatch pages. In owner mode we read ``loc.path``
    # directly; in public mode we get the suffix (e.g. "/sessions") stripped
    # from "/u/{uid}/sessions" by the caller.
    active_path = path_suffix if path_suffix is not None else loc.path

    # Public-mode navigation links prepend "/u/{uid}" so SPA nav stays within
    # the public dashboard.
    def nav_href(path: str) -> str:
        if is_public:
            # "/" default page has no suffix under /u/{uid}
            suffix = "" if path == "/" else path
            return f"/u/{ctx.user_id}{suffix}"
        return path

    # ── Global filter state ────────────────────────────────────────────────
    # Shared across all pages; lives here so it persists across tab switches.
    gfilter = hd.state(
        excluded_seasons=(),  # tuple[str] of "YYYY-YY" seasons to hide
        machine="All",  # "All" or a machine-type string
    )

    # Determine the full season list and machine types from localStorage workouts
    # so the filter UI can render even before any page has fetched data.
    # We do a lightweight read here; concept2_sync() on the active page handles
    # the full data load.
    _ls_wkts_meta = hd.local_storage.get_item("workouts")
    _all_seasons_for_filter: list = []
    _machine_types_for_filter: list = []
    if _ls_wkts_meta.done and _ls_wkts_meta.result:
        try:
            from services.rowing_utils import get_season

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
            _all_seasons_for_filter = sorted(_season_set, reverse=True)
            # In synthetic mode the augmented machines (skierg, bike) are never
            # written to localStorage, so inject them manually here.
            if SYNTHETIC_MODE:
                _mtype_set.update({"skierg", "bike"})
            _machine_types_for_filter = sorted(_mtype_set)
        except Exception:
            pass

    # Derive active page from URL; unknown/session paths fall back to default.
    in_session = active_path.startswith("/session/")
    current_page = _ROUTES_PAGES.get(active_path, None if in_session else _DEFAULT_PAGE)

    # Public mode: hide Profile tab (no public settings page) AND Race tab is
    # kept — strokes gracefully degrade when uncached. Profile is the only
    # route that 404s in public mode.
    _hidden_nav_pages = {"Profile"}

    public_banner(ctx)

    with hd.box(padding=2, gap=1, padding_top=0):
        with hd.hbox(gap=2, align="center"):
            ergnerd_animation(width=10, theme="dark" if _theme.is_dark else "light")
            with hd.nav(direction="horizontal", gap=0, align="end"):
                for page_name, path in _PAGES_ROUTES.items():
                    if page_name in _hidden_nav_pages:
                        continue
                    with hd.scope(f"{page_name, active_path}"):
                        is_active = page_name == current_page
                        hd.link(
                            page_name,
                            href=nav_href(path),
                            target="_self",
                            underline=False,
                            font_size="medium",
                            font_color="primary" if is_active else "neutral-600",
                            border_bottom=(
                                "2px solid primary"
                                if is_active
                                else "2px solid neutral-200"
                            ),
                            padding=(1, 1.25, 1, 1.25),
                            hover_background_color="neutral-50",
                        )

            with hd.box(grow=True):
                pass

            # ── Global filters (season + machine) ──────────────────────────
            with hd.hbox(gap=1, align="center", padding_bottom=1):
                _global_filter_ui(
                    gfilter,
                    _all_seasons_for_filter,
                    _machine_types_for_filter,
                )

            with hd.hbox(gap=1, align="center", padding_bottom=1):
                if SYNTHETIC_MODE:
                    hd.badge("SYNTHETIC DATA", variant="warning")

                if is_public:
                    hd.text(
                        ctx.display_name,
                        font_color="primary",
                        font_size="small",
                        font_weight="semibold",
                    )
                else:
                    if user_task.done and user_task.result:
                        user = user_task.result
                        display_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
                            "username", ""
                        )
                        hd.link(
                            display_name,
                            href="profile",
                            font_color="primary",
                            font_size="small",
                        )

                    if hd.button("Disconnect", variant="neutral", size="small").clicked:
                        # Also tear down any published public data — single
                        # source of truth is the token file.
                        try:
                            public_profiles.unpublish(ctx.user_id)
                        except Exception as _exc:
                            print(f"[disconnect] unpublish failed: {_exc}")
                        clear_token(ctx.user_id)
                        hd.local_storage.remove_item("c2_user_id")
                        hd.local_storage.remove_item("workouts")
                        hd.local_storage.remove_item("profile")
                        loc.go(path="/")

        # ── Session detail overlay ─────────────────────────────────────────
        if in_session:
            try:
                session_id = int(active_path.split("/")[2])
            except (IndexError, ValueError):
                session_id = None
            if session_id is not None:
                with hd.scope(session_id):
                    workout_page(session_id, ctx)
        elif current_page == "Volume":
            volume_page(
                ctx,
                excluded_seasons=gfilter.excluded_seasons,
                machine=gfilter.machine,
            )
        elif current_page == "Sessions":
            sessions_page(
                ctx,
                excluded_seasons=gfilter.excluded_seasons,
                machine=gfilter.machine,
            )
        elif current_page == "Intervals":
            intervals_page(
                ctx,
                excluded_seasons=gfilter.excluded_seasons,
                machine=gfilter.machine,
            )
        elif current_page == "Power Curve":
            power_curve_page(
                ctx,
                excluded_seasons=gfilter.excluded_seasons,
                machine=gfilter.machine,
            )
        elif current_page == "Race":
            race_page(
                ctx,
                excluded_seasons=gfilter.excluded_seasons,
                machine=gfilter.machine,
            )
        else:
            # Profile page is owner-only; public-mode requests render 404.
            if is_public:
                _public_404_view(ctx.user_id)
            else:
                profile_page(ctx)

    _app_footer()


def _public_404_view(user_id: str | None = None) -> None:
    """Friendly card shown when ``/u/{uid}`` has no published data, or
    when an owner-only route is hit in public mode."""
    with hd.box(
        height="80vh",
        align="center",
        justify="center",
        gap=2,
        padding=4,
    ):
        with hd.box(
            gap=2,
            padding=4,
            align="center",
            border="1px solid neutral-200",
            border_radius="large",
            background_color="neutral-50",
            max_width=30,
        ):
            hd.icon("question-circle", font_size=3, font_color="neutral-400")
            hd.h3("Nothing to see here")
            if user_id:
                hd.text(
                    f"No public profile is published for user {user_id}.",
                    font_color="neutral-600",
                    text_align="center",
                )
            else:
                hd.text(
                    "This page isn't available on public profiles.",
                    font_color="neutral-600",
                    text_align="center",
                )
            hd.link(
                "Go to Erg Nerd",
                href="/",
                target="_self",
                font_color="primary",
            )


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app_state = hd.state(pending_user_id=None, pending_profile=None)
    loc = hd.location()

    # OAuth callback — handled before localStorage gate
    if loc.path == "/oauth/callback":
        _oauth_callback_view(loc.query_args or "", app_state)
        return

    # Public profile dispatch — `/u/{uid}/...` bypasses the login gate. The
    # segment after `{uid}` (if any) becomes the page path used internally
    # by _dashboard_view.
    if loc.path.startswith("/u/"):
        parts = loc.path.split("/", 3)  # ["", "u", "{uid}", "rest"]
        public_uid = parts[2] if len(parts) > 2 else ""
        suffix = "/" + parts[3] if len(parts) > 3 and parts[3] else "/"
        if not public_uid:
            _public_404_view()
            return
        ctx = build_public_context(public_uid)
        if ctx is None:
            _public_404_view(public_uid)
            return
        _dashboard_view(ctx, app_state, path_suffix=suffix)
        return

    # Flush any user_id set by the OAuth callback into browser localStorage
    if app_state.pending_user_id:
        hd.local_storage.set_item("c2_user_id", app_state.pending_user_id)
        app_state.pending_user_id = None

    # Pre-fill profile from Concept2 data (only on first login — skipped if
    # a profile already exists in localStorage).
    if app_state.pending_profile is not None:
        ls_existing_profile = hd.local_storage.get_item("profile")
        if not ls_existing_profile.done:
            with hd.box(height="100vh", align="center", justify="center"):
                hd.spinner()
            return
        if not ls_existing_profile.result:
            hd.local_storage.set_item("profile", json.dumps(app_state.pending_profile))
        app_state.pending_profile = None

    # Async gate — read user_id from localStorage
    ls_uid = hd.local_storage.get_item("c2_user_id")
    if not ls_uid.done:
        with hd.box(height="100vh", align="center", justify="center"):
            hd.spinner()
        return

    # No user_id → show login
    if not ls_uid.result:
        _login_view()
        return

    # Load token and run the app
    user_id = ls_uid.result
    client = get_client(user_id)
    if client is None:
        # Token file missing or corrupt — clear stale user_id and show login
        hd.local_storage.remove_item("c2_user_id")
        _login_view()
        return

    ctx = build_owner_context(client, user_id)
    _dashboard_view(ctx, app_state)


_BASE_URL = get_server_url()


plausible = (
    """<!-- Privacy-friendly analytics by Plausible -->
              <script async src="https://plausible.io/js/pa-qqiXIjSXpsHb8fbWbYtHz.js"></script>
              <script>
                window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};
                plausible.init()
              </script>"""
    if "https" in _BASE_URL
    else ""
)

hd.run(
    main,
    index_page=hd.index_page(
        title="Erg Nerd",
        description="Personal Concept2 rowing analytics — power curves, predictions, interval browser, and workout history.",
        keywords=["rowing", "Concept2", "erg", "Power Curve", "analytics", "training"],
        url=_BASE_URL,
        image=f"{_BASE_URL}/assets/static_logo.png",
        favicon="/assets/nerdemoji.png",
        raw_head_content=(
            f"""
                {plausible}

              <script data-name="BMC-Widget" data-cfasync="false" src="https://cdnjs.buymeacoffee.com/1.0.0/widget.prod.min.js" data-id="ergnerd" data-description="Support me on Buy me a coffee!" data-message="Help cover Erg Nerd's server and development costs!" data-color="#5F7FFF" data-position="Right" data-x_margin="18" data-y_margin="18"></script>
            """
        ),
    ),
)
