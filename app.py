"""
Erg Nerd — main application entry point.

Run with:  python app.py
The app opens at http://localhost:8888
"""

import json
import os
from config import SYNTHETIC_MODE, PROFILE_ENABLE


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

from services.rowinglevel import init_cache as _init_rowinglevel_cache

_init_rowinglevel_cache()

import hyperdiv as hd
from services.profiling import profile_block
from services.concept2 import (
    Concept2Client,
    clear_token,
    exchange_code,
    extract_c2_profile,
    get_authorization_url,
    get_server_url,
    parse_callback_query,
    save_token,
)
from components.experimental_page import experimental_page
from components.intervals_page import intervals_page
from components.profile_page import profile_page
from components.power_curve_page import power_curve_page
from components.race_page import race_page
from components.rank_page import rank_page
from components.ergnerd_animation import ergnerd_animation
from components.workout_page import workout_page
from components.workouts_page import workouts_page
from components.volume_page import volume_page
from components.concept2_sync import concept2_sync
from components.indexed_db import mount_indexed_db, IDB_STORES
from components.app_context import (
    AppContext,
    get_profile,
    populate_owner,
    populate_public,
    refresh_profile_if_stale,
    write_profile_ls,
    clear_profile_ls,
    _SESSION_LS_KEY,
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


@hd.cached
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
    "Workouts": "/workouts",
    "Volume": "/volume",
    "Intervals": "/intervals",
    "Power Curve": "/power_curve",
    "Race": "/race",
    "Rank": "/rank",
    "Experimental": "/experimental",
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


# @hd.cached
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

                with hd.link(
                    href="https://www.buymeacoffee.com/ergnerd",
                    background_color="primary-500",
                    border_radius="large",
                ) as bmc:
                    hd.image(
                        src="https://img.buymeacoffee.com/button-api/?text=Support Erg Nerd&emoji=☕&slug=ergnerd&button_color=5F7FFF&font_color=ffffff&font_family=Lato&outline_color=000000&coffee_color=FFDD00"
                    )

        with hd.box(gap=0.3, align="center"):
            with hd.hbox(align="center", gap=1):
                with hd.hbox(gap=0.2):
                    hd.text(
                        "Built by ",
                        font_color="neutral-300",
                        font_size="small",
                    )
                    hd.link(
                        "Redline Rower",
                        href="https://log.concept2.com/team/7119",
                        font_size="small",
                        font_color="primary-300",
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


@hd.cached
def _app_header(current_page, is_public):
    # Public mode: hide Profile tab (no public settings page) AND Race tab is
    # kept — strokes gracefully degrade when uncached. Profile is the only
    # route that 404s in public mode.
    _hidden_nav_pages = {"Profile"}
    ctx = AppContext()

    # Public-mode navigation links prepend "/u/{uid}" so SPA nav stays within
    # the public dashboard.
    def nav_href(path: str) -> str:
        if is_public:
            # "/" default page has no suffix under /u/{uid}
            suffix = "" if path == "/" else path
            return f"/u/{ctx.user_id}{suffix}"
        return path

    _theme = hd.theme()
    with hd.hbox(gap=2, align="center"):
        ergnerd_animation(width=10, theme="dark" if _theme.is_dark else "light")
        with hd.nav(direction="horizontal", gap=0, align="end"):
            for page_name, path in _PAGES_ROUTES.items():
                if page_name in _hidden_nav_pages:
                    continue
                with hd.scope(f"{page_name, current_page}"):
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
                profile = get_profile()
                display_name = (
                    (profile.get("display_name") or "").strip()
                    or (ctx.display_name or "").strip()
                    or "Rower"
                )
                profile_image = profile.get("profile_image") or ""

                with hd.dropdown(placement="bottom-end") as _user_dd:
                    with hd.button(
                        display_name,
                        caret=True,
                        variant="text",
                        size="large",
                        slot=_user_dd.trigger,
                        font_color="primary",
                    ) as _user_btn:
                        if profile_image:
                            hd.avatar(
                                image=profile_image,
                                assistive_label=display_name,
                                size=2.5,
                                slot=_user_btn.prefix,
                            )
                    with hd.menu() as _user_menu:
                        _profile_mi = hd.menu_item("Profile", prefix_icon="person")
                        _disconnect_mi = hd.menu_item(
                            "Disconnect", prefix_icon="box-arrow-right"
                        )
                    if _user_btn.clicked or _user_menu.selected_item:
                        _user_dd.opened = not _user_dd.opened
                    if _profile_mi.clicked:
                        loc = hd.location()
                        loc.go(path="/profile")
                    if _disconnect_mi.clicked:
                        # Also tear down any published public data — single
                        # source of truth is the token file.
                        try:
                            public_profiles.unpublish(ctx.user_id)
                        except Exception as _exc:
                            print(f"[disconnect] unpublish failed: {_exc}")
                        clear_token(ctx.user_id)
                        clear_profile_ls()
                        # Drop client-side workout + stroke caches so the next
                        # user logging in on this browser starts clean.
                        from components import indexed_db as _idb

                        _idb.clear(_idb.WORKOUTS_STORE)
                        _idb.clear(_idb.STROKES_STORE)
                        _idb.clear(_idb.META_STORE)
                        loc = hd.location()
                        loc.go(path="/")


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------


def _dashboard_view(app_state, path_suffix: str | None = None) -> None:
    _ScrollToTop()

    ctx = AppContext()
    is_public = ctx.mode == "public"

    loc = hd.location()

    # Active path used to dispatch pages. In owner mode we read ``loc.path``
    # directly; in public mode we get the suffix (e.g. "/workouts") stripped
    # from "/u/{uid}/workouts" by the caller.
    active_path = path_suffix if path_suffix is not None else loc.path

    # Derive active page from URL; unknown/workout paths fall back to default.
    in_workout = active_path.startswith("/workout/")
    current_page = _ROUTES_PAGES.get(active_path, None if in_workout else _DEFAULT_PAGE)

    public_banner()

    # Mount the IndexedDB bridge once per render in owner mode.  Public mode
    # reads workouts + strokes from server-side files, so it doesn't need
    # client-side storage.  Mounting here (above the page dispatch) ensures
    # the plugin is in the DOM whenever any consumer needs it.
    if not is_public:
        mount_indexed_db(IDB_STORES)

    with hd.box(padding=2, gap=1, padding_top=0):
        _app_header(current_page, is_public)
        # ── One-time workout sync ─────────────────────────────────────────
        # Owner mode: fire concept2_sync exactly once per session (it bails
        # immediately on subsequent renders once ctx.workouts_dict is set).
        # Public mode: populate_public has already filled the snapshot.
        if not is_public:
            concept2_sync(ctx.client)

        if not is_public and ctx.workouts_dict is None:
            # Loading / progress UI rendered inline by concept2_sync above —
            # skip the page dispatch until the snapshot is ready.
            pass
        # ── Session detail overlay ─────────────────────────────────────────
        elif in_workout:
            try:
                workout_id = int(active_path.split("/")[2])
            except (IndexError, ValueError):
                workout_id = None
            if workout_id is not None:
                workout_page(workout_id)
        elif current_page == "Volume":
            volume_page()
        elif current_page == "Workouts":
            workouts_page()
        elif current_page == "Intervals":
            intervals_page()
        elif current_page == "Power Curve":
            power_curve_page()
        elif current_page == "Race":
            race_page()
        elif current_page == "Rank":
            rank_page()
        elif current_page == "Experimental":
            experimental_page()
        else:
            # Profile page is owner-only; public-mode requests render 404.
            if is_public:
                _public_404_view(ctx.user_id)
            else:
                profile_page()

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
    from pyinstrument import Profiler

    if False and PROFILE_ENABLE:
        profiler = Profiler()
        profiler.start()

    with profile_block("main-render"):
        _main_body()

    if False and PROFILE_ENABLE:
        profiler.stop()
        profiler.print()


def _main_body() -> None:
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
        if not populate_public(public_uid):
            _public_404_view(public_uid)
            return
        _dashboard_view(app_state, path_suffix=suffix)
        return

    # ── Combined profile-key gate ────────────────────────────────────────────
    # One async fetch carries both ``user_id`` and ``profile`` so the boot
    # path doesn't pay for two LS round-trips on every render.
    ls_profile = hd.local_storage.get_item(_SESSION_LS_KEY)
    if not ls_profile.done:
        with hd.box(height="100vh", align="center", justify="center"):
            hd.spinner()
        return

    # Flush any pending OAuth-callback result into the combined profile key.
    # If a profile already exists for this user (e.g. logged out and back in
    # without clearing the LS), preserve its profile so user-edited values
    # survive the round-trip.
    if app_state.pending_user_id:
        new_uid = app_state.pending_user_id
        new_profile = app_state.pending_profile or {}
        if ls_profile.result:
            try:
                existing = json.loads(ls_profile.result)
                if existing.get("user_id") == new_uid and existing.get("profile"):
                    new_profile = existing["profile"]
            except Exception:
                pass
        write_profile_ls(new_uid, new_profile)
        app_state.pending_user_id = None
        app_state.pending_profile = None
        return  # next render will re-read the combined key

    if not ls_profile.result:
        _login_view()
        return

    # Have profile data — parse and populate AppContext.
    try:
        data = json.loads(ls_profile.result)
    except Exception:
        data = {}
    user_id = data.get("user_id") or ""
    profile = data.get("profile") or {}

    if not user_id:
        _login_view()
        return

    if not populate_owner(user_id, profile=profile):
        # Token file missing or corrupt — clear stale profile and show login
        clear_profile_ls()
        _login_view()
        return

    # If the cached profile is older than the refresh TTL, kick off one
    # keyed task to re-fetch /users/me and merge non-edited fields back in.
    refresh_profile_if_stale()

    _dashboard_view(app_state)


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
