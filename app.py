"""
Erg Nerd — main application entry point.

Run with:  python app.py
The app opens at http://localhost:8888
"""

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
    clear_token,
    exchange_code,
    get_authorization_url,
    get_client,
    parse_callback_query,
)
from components.interval_tab import interval_tab
from components.profile_tab import profile_tab
from components.ranked_tab import ranked_tab
from components.rowing_animation import rowing_animation
from components.sessions_tab import sessions_tab
from components.volume_tab import volume_tab


# ---------------------------------------------------------------------------
# OAuth callback view
# ---------------------------------------------------------------------------


def _oauth_callback_view(query_args: str) -> None:
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
        exchange_code(code)

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
            rowing_animation(width=22, theme="dark" if _theme.is_dark else "light")

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
                    "Your data stays in your browser, not our server.",
                    font_color="neutral-500",
                    font_size="small",
                )


# ---------------------------------------------------------------------------
# Dashboard view
# ---------------------------------------------------------------------------


def _dashboard_view() -> None:
    user_task = hd.task()

    def fetch_user():
        client = get_client()
        return client.get_user().get("data", {}) if client else {}

    user_task.run(fetch_user)

    _theme = hd.theme()

    # Tabs
    with hd.box(padding=2, gap=1, padding_top=0):
        with hd.hbox(gap=2, align="end"):
            rowing_animation(width=10, theme="dark" if _theme.is_dark else "light")
            with hd.tab_group() as tabs:
                hd.tab("Profile", font_size="medium")
                hd.tab("Volume", font_size="medium")
                hd.tab("Sessions", font_size="medium")
                hd.tab("Intervals", font_size="medium")
                hd.tab("Ranking Events", font_size="medium", active=True)

            with hd.box(grow=True):
                pass

            with hd.box(padding_bottom=3, align="start"):
                with hd.hbox(gap=1, align="center"):
                    if user_task.done and user_task.result:
                        user = user_task.result
                        name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
                            "username", ""
                        )
                        hd.text(name, font_color="neutral-400", font_size="small")

                    if hd.button("Disconnect", variant="neutral", size="small").clicked:
                        clear_token()
                        hd.location().go(path="/")

        if tabs.active == "Volume":
            volume_tab()
        elif tabs.active == "Sessions":
            sessions_tab()
        elif tabs.active == "Intervals":
            interval_tab()
        elif tabs.active == "Ranking Events":
            ranked_tab()
        else:
            profile_tab()


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------


def main() -> None:
    loc = hd.location()

    if loc.path == "/oauth/callback":
        _oauth_callback_view(loc.query_args or "")
    elif get_client() is not None:
        _dashboard_view()
    else:
        _login_view()


hd.run(
    main,
    index_page=hd.index_page(title="Erg Nerd", favicon="/assets/nerdemoji.png"),
)
