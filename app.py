"""
Erg Nerd — main application entry point.

Run with:  python app.py
The app opens at http://localhost:8888
"""

import json
import os


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
    parse_callback_query,
    save_token,
)
from services.local_storage_compression import compress_workouts, decompress_workouts
from components.intervals_page import intervals_page
from components.profile_page import profile_page
from components.performance_page import performance_page
from components.event_page import event_page
from components.ergnerd_animation import ergnerd_animation
from components.workout_page import workout_page
from components.sessions_page import sessions_page
from components.volume_page import volume_page
from components.concept2_sync import concept2_sync


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
    "Performance": "/performance",
    "Race": "/race",
    "Profile": "/profile",
}
_ROUTES_PAGES: dict[str, str] = {v: k for k, v in _PAGES_ROUTES.items()}
_DEFAULT_PAGE = "Performance"


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------


def _dashboard_view(client, user_id: str, app_state) -> None:
    user_task = hd.task()

    def fetch_user():
        return client.get_user().get("data", {})

    user_task.run(fetch_user)

    _theme = hd.theme()
    loc = hd.location()

    # Derive active page from URL; unknown/session paths fall back to default.
    in_session = loc.path.startswith("/session/")
    current_page = _ROUTES_PAGES.get(loc.path, None if in_session else _DEFAULT_PAGE)

    with hd.box(padding=2, gap=1, padding_top=0):
        with hd.hbox(
            gap=2,
        ):
            ergnerd_animation(width=10, theme="dark" if _theme.is_dark else "light")
            with hd.nav(direction="horizontal", gap=0, align="end"):
                for page_name, path in _PAGES_ROUTES.items():
                    if page_name in ["Profile"]:
                        continue
                    with hd.scope(f"{page_name, loc.path}"):
                        is_active = page_name == current_page
                        hd.link(
                            page_name,
                            href=path,
                            target="_self",
                            underline=False,
                            font_size="medium",
                            font_color="primary" if is_active else "neutral-600",
                            # font_weight="semibold" if is_active else "normal",
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
                    clear_token(user_id)
                    hd.local_storage.remove_item("c2_user_id")
                    hd.local_storage.remove_item("workouts")
                    hd.local_storage.remove_item("profile")
                    loc.go(path="/")

        # ── Session detail overlay ─────────────────────────────────────────
        if in_session:
            try:
                session_id = int(loc.path.split("/")[2])
            except (IndexError, ValueError):
                session_id = None
            if session_id is not None:
                with hd.scope(session_id):
                    workout_page(
                        session_id,
                        client,
                        user_id,
                    )
        elif current_page == "Volume":
            volume_page(client, user_id)
        elif current_page == "Sessions":
            sessions_page(client, user_id)
        elif current_page == "Intervals":
            intervals_page(client, user_id)
        elif current_page == "Performance":
            performance_page(client, user_id)
        elif current_page == "Race":
            event_page(client, user_id)
        else:
            profile_page()


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

    _dashboard_view(client, user_id, app_state)


_PORT = int(os.environ.get("HD_PORT", 8888))

hd.run(
    main,
    index_page=hd.index_page(
        title="Erg Nerd",
        description="Personal Concept2 rowing analytics — performance charts, fitness level predictions, and workout history.",
        keywords=["rowing", "Concept2", "erg", "performance", "analytics", "training"],
        url=f"http://localhost:{_PORT}",
        image=f"http://localhost:{_PORT}/assets/static_logo.png",
        favicon="/assets/nerdemoji.png",
    ),
)
