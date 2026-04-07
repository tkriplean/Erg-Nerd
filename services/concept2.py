"""
Concept2 Logbook API — auth and client module.

OAuth flow:
  1. Call get_authorization_url() to get the Concept2 auth page URL.
  2. User authorizes → Concept2 redirects to localhost/oauth/callback?code=...
  3. Call exchange_code(code) to swap the code for tokens; tokens are saved to disk.
  4. Call get_client() to get a ready-to-use Concept2Client (handles refresh automatically).

Workout history caching:
  get_all_results() uses a local JSON file (.workouts.json) keyed by workout ID.
  It fetches API pages from newest to oldest, stops as soon as an overlap with the
  local cache is found, and rate-limits requests between pages. This means the
  initial run fetches everything, and subsequent runs only fetch the new pages.

Register your app at: https://log.concept2.com/developers
Set CONCEPT2_CLIENT_ID and CONCEPT2_CLIENT_SECRET in your .env file.
"""

import json
import os
import time
from typing import Optional
from urllib.parse import parse_qs

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_AUTH_BASE = "https://log.concept2.com"
_AUTHORIZE_URL = f"{_AUTH_BASE}/oauth/authorize"
_TOKEN_URL = f"{_AUTH_BASE}/oauth/access_token"
API_BASE = f"{_AUTH_BASE}/api"

SCOPES = "user:read,results:read"

# Seconds to wait between page requests when fetching history.
# Keeps us polite to the Concept2 API.
_PAGE_DELAY = 1.0

# Credentials are read lazily (inside functions) so that load_dotenv() in
# app.py has always run before they are needed, regardless of import order.
def _client_id() -> str:
    return os.environ.get("CONCEPT2_CLIENT_ID", "")

def _client_secret() -> str:
    return os.environ.get("CONCEPT2_CLIENT_SECRET", "")

_ROOT = os.path.join(os.path.dirname(__file__), "..")

# OAuth token — contains access + refresh tokens.
_TOKEN_FILE = os.path.join(_ROOT, ".concept2_token.json")

# Persistent workout history cache — dict of {str(id): result_dict}.
# All workouts are stored here (including intervals). Dashboards filter as needed.
_WORKOUTS_FILE = os.path.join(_ROOT, ".workouts.json")

# Short-lived response cache for lightweight calls (user profile, single pages).
_CACHE_DIR = os.path.join(_ROOT, ".cache")
_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def get_redirect_uri() -> str:
    """Build the redirect URI using the configured HD_PORT (default 8888)."""
    port = os.environ.get("HD_PORT", "8888")
    return f"http://localhost:{port}/oauth/callback"


def get_authorization_url() -> str:
    """
    Return the URL the user must visit to authorize this app with Concept2.
    Open this in the browser; after approval Concept2 redirects to
    get_redirect_uri() with ?code=... appended.
    """
    if not _client_id():
        raise EnvironmentError(
            "CONCEPT2_CLIENT_ID is not set. "
            "Copy .env.example to .env and fill in your credentials."
        )
    # Build the URL without urlencode so colons and the redirect URI are
    # not percent-encoded — Concept2 requires them in their literal form.
    return (
        f"{_AUTHORIZE_URL}"
        f"?client_id={_client_id()}"
        f"&scope={SCOPES}"
        f"&response_type=code"
        f"&redirect_uri={get_redirect_uri()}"
    )


def parse_callback_query(query_args: str) -> dict:
    """
    Parse the raw query string from hd.location().query_args after the
    OAuth redirect. Returns a dict with at least one of:
      {"code": "...", "state": "..."}  on success
      {"error": "...", ...}            on failure
    """
    parsed = parse_qs(query_args or "")
    return {k: v[0] for k, v in parsed.items()}


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def load_token() -> Optional[dict]:
    """Load cached OAuth token from disk. Returns None if absent or corrupt."""
    try:
        with open(_TOKEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(token_data: dict) -> None:
    """Persist token (with a saved_at timestamp) to disk."""
    token_data = dict(token_data)
    token_data["saved_at"] = time.time()
    with open(_TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def clear_token() -> None:
    """Delete the saved token (triggers re-authentication on next run)."""
    try:
        os.remove(_TOKEN_FILE)
    except FileNotFoundError:
        pass


def is_token_expired(token_data: dict) -> bool:
    """Return True if the access token has expired (with a 60s safety buffer)."""
    saved_at = token_data.get("saved_at", 0)
    expires_in = token_data.get("expires_in", 0)
    return time.time() > (saved_at + expires_in - 60)


# ---------------------------------------------------------------------------
# Token exchange / refresh
# ---------------------------------------------------------------------------

def exchange_code(code: str) -> dict:
    """
    Exchange an authorization code for access + refresh tokens.
    Saves the token to disk and returns the token dict.
    """
    response = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": get_redirect_uri(),
        },
    )
    response.raise_for_status()
    token_data = response.json()
    save_token(token_data)
    return token_data


def _refresh_token(token_data: dict) -> Optional[dict]:
    """
    Use the stored refresh token to obtain a new access token.
    Returns None (and clears stored token) if the refresh is rejected,
    so the caller can fall back to showing the login screen.
    """
    try:
        response = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"[concept2] Token refresh failed ({exc.response.status_code}) — "
            "clearing stored token, re-authentication required."
        )
        clear_token()
        return None
    new_token = response.json()
    save_token(new_token)
    return new_token


def get_valid_token() -> Optional[dict]:
    """
    Return a valid token dict, refreshing automatically if expired.
    Returns None if no token is stored or the refresh fails.
    """
    token_data = load_token()
    if token_data is None:
        return None
    if is_token_expired(token_data):
        token_data = _refresh_token(token_data)
    return token_data


# ---------------------------------------------------------------------------
# Persistent workout history cache
# ---------------------------------------------------------------------------

def load_local_workouts() -> dict:
    """
    Load the persistent workout cache from disk.
    Returns a dict of {str(workout_id): result_dict}.
    """
    try:
        with open(_WORKOUTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_local_workouts(workouts: dict) -> None:
    """Write the workout dict to disk."""
    with open(_WORKOUTS_FILE, "w") as f:
        json.dump(workouts, f)


def clear_local_workouts() -> None:
    """Delete the local workout cache (forces a full re-fetch on next call)."""
    try:
        os.remove(_WORKOUTS_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Short-lived response cache (for user profile and single-page fetches)
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    safe_key = key.replace("/", "_").replace("?", "_").replace("&", "_")
    return os.path.join(_CACHE_DIR, f"{safe_key}.json")


def _read_cache(key: str) -> Optional[dict]:
    try:
        with open(_cache_path(key)) as f:
            entry = json.load(f)
        if time.time() < entry["expires_at"]:
            return entry["data"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        pass
    return None


def _write_cache(key: str, data: dict) -> None:
    with open(_cache_path(key), "w") as f:
        json.dump({"expires_at": time.time() + _CACHE_TTL_SECONDS, "data": data}, f)


def clear_cache() -> None:
    """Delete all short-lived cached API responses."""
    import shutil
    if os.path.isdir(_CACHE_DIR):
        shutil.rmtree(_CACHE_DIR)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class Concept2Client:
    """
    Authenticated client for the Concept2 Logbook REST API.

    Usage:
        client = get_client()
        if client is None:
            # user needs to authenticate
            ...
        user = client.get_user()
        results = client.get_all_results()
    """

    def __init__(self, access_token: str):
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.c2logbook.v1+json",
            },
            timeout=30,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None, *, cache_key: Optional[str] = None) -> dict:
        """GET with optional short-lived disk cache."""
        if cache_key:
            cached = _read_cache(cache_key)
            if cached is not None:
                return cached

        response = self._http.get(path, params=params)
        response.raise_for_status()
        data = response.json()

        if cache_key:
            _write_cache(cache_key, data)
        return data

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    def get_user(self) -> dict:
        """
        Return the authenticated user's profile (cached for 5 minutes).

        Response shape:
            {
              "data": {
                "id": 123,
                "username": "...",
                "first_name": "...",
                "last_name": "...",
                "email": "...",
                "gender": "M" | "F",
                "dob": "YYYY-MM-DD",
                "weight": <decigrams>,
                "weight_class": "H" | "L",
                ...
              }
            }
        """
        return self._get("/users/me", cache_key="users_me")

    # ------------------------------------------------------------------
    # Results — single page
    # ------------------------------------------------------------------

    def get_results(
        self,
        *,
        page: int = 1,
        type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> dict:
        """
        Return one page of workout results (up to 50 per page), cached for 5 minutes.

        Args:
            page:       Page number, starting at 1.
            type:       Machine type — "rower" | "skierg" | "bike" | etc.
            from_date:  Earliest date, ISO format "YYYY-MM-DD".
            to_date:    Latest date, ISO format "YYYY-MM-DD".
        """
        params: dict = {"page": page}
        if type:
            params["type"] = type
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        cache_key = "results_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
        return self._get("/users/me/results", params=params, cache_key=cache_key)

    # ------------------------------------------------------------------
    # Results — full history with smart local cache
    # ------------------------------------------------------------------

    def get_all_results(self, on_progress=None) -> list:
        """
        Load the full workout history using a persistent local cache and
        a smart incremental API sync.

        Algorithm:
          1. Load all locally cached workouts (keyed by workout ID).
          2. Fetch the first API page to learn the total number of results.
             If the cache already holds that many workouts, return immediately.
          3. Fetch pages from newest to oldest, waiting _PAGE_DELAY seconds
             between each page after the first (rate limiting).
          4. For each page, add every unseen workout to the local cache.
          5. Stop fetching once an overlap is found AND the local cache size
             matches the reported total — everything older is already stored.
             (Overlap alone is not enough: a previous partial run may have
             cached only the first page, causing a false early-stop.)
          6. Save the updated cache to disk after each page (so a mid-run
             interruption doesn't lose progress).
          7. Return all cached workouts sorted newest-first.

        On the first run this fetches the entire history page by page.
        On subsequent runs it typically fetches only 1–2 pages.
        Interval workouts are stored — dashboards filter them as needed.

        Args:
            on_progress: optional callable(pages_fetched, workouts_cached)
                         called after each page is processed. Safe to call
                         from a background thread (e.g. mutate an hd.state).
        """
        local = load_local_workouts()  # {str(id): result_dict}
        print(f"[concept2] Starting sync. Local cache: {len(local)} workouts.")

        api_total: Optional[int] = None  # learned from first page's meta
        page = 1
        while True:
            if page > 1:
                time.sleep(_PAGE_DELAY)

            # Bypass short-lived cache — we manage freshness ourselves.
            response = self._http.get("/users/me/results", params={"page": page})
            response.raise_for_status()
            data = response.json()
            page_results = data.get("data", [])
            pagination = data.get("meta", {}).get("pagination", {})

            # Learn the authoritative total on the first page.
            if api_total is None:
                api_total = pagination.get("total")

            print(
                f"[concept2] Page {page}: {len(page_results)} results | "
                f"api_total={api_total} | local={len(local)} | "
                f"has_next={bool(pagination.get('links', {}).get('next'))}"
            )

            if not page_results:
                print("[concept2] Empty page — done.")
                break

            overlap_found = False
            for result in page_results:
                rid = str(result.get("id", ""))
                if not rid:
                    continue
                if rid in local:
                    overlap_found = True
                    # Keep going through this page — there may be newer
                    # workouts interleaved (e.g. after a date edit).
                else:
                    local[rid] = result

            # Persist after every page so partial progress is never lost.
            save_local_workouts(local)

            if on_progress:
                on_progress(page, len(local))

            # Only stop on overlap once the cache is complete.
            # A previous partial run could have cached only page 1, which
            # would cause every subsequent fetch to overlap immediately and
            # stop early — leaving the rest of history unfetched.
            cache_complete = api_total is None or len(local) >= api_total
            print(
                f"[concept2] overlap={overlap_found} | "
                f"cache_complete={cache_complete} | "
                f"local_after={len(local)}"
            )
            if overlap_found and cache_complete:
                print("[concept2] Overlap + complete — stopping.")
                break

            if not pagination.get("links", {}).get("next"):
                print("[concept2] No next page link — done.")
                break

            page += 1

        print(f"[concept2] Sync done. Returning {len(local)} workouts.")

        workouts = list(local.values())
        workouts.sort(key=lambda r: r.get("date", ""), reverse=True)
        return workouts

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Concept2Client":
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def get_client() -> Optional[Concept2Client]:
    """
    Return an authenticated Concept2Client using the stored token,
    refreshing it automatically if expired.
    Returns None if the user has not authenticated yet.
    """
    token_data = get_valid_token()
    if token_data is None:
        return None
    return Concept2Client(token_data["access_token"])
