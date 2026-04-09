"""
Concept2 Logbook API — auth and client module.

OAuth flow:
  1. Call get_authorization_url() to get the Concept2 auth page URL.
  2. User authorizes → Concept2 redirects to localhost/oauth/callback?code=...
  3. Call exchange_code(code) to get the token dict (no disk save).
  4. Call client.get_user() to get the Concept2 numeric user ID.
  5. Call save_token(token_data, user_id) to persist the token server-side.
  6. Call get_client(user_id) on subsequent visits for a ready-to-use client.

Token storage:
  Each user's token is stored in a separate file keyed by Concept2 user ID:
    .concept2_token_{user_id}.json
  Token refresh is handled automatically and saves back to the same user file.

Workout caching:
  Workouts are stored in the browser's localStorage (not on disk).
  get_all_results(initial_workouts) accepts the pre-loaded workouts dict
  (provided by the caller from localStorage), syncs new pages from the API,
  and returns (updated_workouts_dict, sorted_list). The caller writes the
  dict back to localStorage.

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


def _get_server_url() -> str:
    port = os.environ.get("HD_PORT", "8888")
    return os.environ.get("server_url", f"http://localhost:{port}")


_ROOT = os.path.join(os.path.dirname(__file__), "..")

# Short-lived response cache for lightweight calls (user profile, single pages).
_CACHE_DIR = os.path.join(_ROOT, ".cache")
_CACHE_TTL_SECONDS = 5 * 60  # 5 minutes


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def get_redirect_uri() -> str:
    """Build the redirect URI using the configured HD_PORT (default 8888)."""
    return f"{_get_server_url()}/oauth/callback"


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
# Token persistence — server-side, keyed by Concept2 user ID
# ---------------------------------------------------------------------------


def _token_path(user_id: str) -> str:
    """Return the path to the token file for the given user ID."""
    return os.path.join(_ROOT, f".concept2_token_{user_id}.json")


def load_token(user_id: str) -> Optional[dict]:
    """Load cached OAuth token from disk for the given user. Returns None if absent or corrupt."""
    try:
        with open(_token_path(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(token_data: dict, user_id: str) -> None:
    """Persist token (with a saved_at timestamp) to the user-specific file on disk."""
    token_data = dict(token_data)
    token_data["saved_at"] = time.time()
    with open(_token_path(user_id), "w") as f:
        json.dump(token_data, f, indent=2)


def clear_token(user_id: str) -> None:
    """Delete the saved token for this user (triggers re-authentication on next run)."""
    try:
        os.remove(_token_path(user_id))
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
    Returns the token dict without saving to disk — the caller must call
    save_token(token_data, user_id) after obtaining the user ID.
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
    return response.json()


def _refresh_token(token_data: dict, user_id: str) -> Optional[dict]:
    """
    Use the stored refresh token to obtain a new access token.
    Saves the refreshed token to the user-specific file on success.
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
        clear_token(user_id)
        return None
    new_token = response.json()
    save_token(new_token, user_id)
    return new_token


def get_valid_token(user_id: str) -> Optional[dict]:
    """
    Return a valid token dict for the given user, refreshing automatically if expired.
    Returns None if no token is stored or the refresh fails.
    """
    token_data = load_token(user_id)
    if token_data is None:
        return None
    if is_token_expired(token_data):
        token_data = _refresh_token(token_data, user_id)
    return token_data


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
        client = get_client(user_id)
        if client is None:
            # user needs to authenticate
            ...
        user = client.get_user()
        workouts_dict, sorted_list = client.get_all_results(initial_workouts)
    """

    def __init__(self, access_token: str, user_id: str = ""):
        self._user_id = user_id
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

    def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        cache_key: Optional[str] = None,
    ) -> dict:
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

        The cache is keyed per user so simultaneous sessions stay isolated.
        Email is scrubbed before writing to disk.

        Response shape:
            {
              "data": {
                "id": 123,
                "username": "...",
                "first_name": "...",
                "last_name": "...",
                "gender": "M" | "F",
                "dob": "YYYY-MM-DD",
                "weight": <decagrams — divide by 100 for kg>,
                "max_heart_rate": <bpm or None>,
                ...
              }
            }
        """
        cache_key = f"users_me_{self._user_id}" if self._user_id else "users_me"
        cached = _read_cache(cache_key)
        if cached is not None:
            return cached

        response = self._http.get("/users/me")
        response.raise_for_status()
        data = response.json()

        # Scrub email before persisting to disk
        if isinstance(data.get("data"), dict) and "email" in data["data"]:
            data = {
                **data,
                "data": {k: v for k, v in data["data"].items() if k != "email"},
            }

        _write_cache(cache_key, data)
        return data

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
    # Results — full history with incremental sync
    # ------------------------------------------------------------------

    def get_all_results(
        self,
        initial_workouts: dict,
        on_progress=None,
    ) -> tuple:
        """
        Sync the full workout history against the Concept2 API.

        Takes the caller-provided initial workout dict (loaded from browser
        localStorage) and fetches any new pages from the API. Returns a
        (updated_workouts_dict, sorted_list) tuple. The caller is responsible
        for writing the dict back to localStorage.

        Algorithm:
          1. Start from the provided initial_workouts dict.
          2. Fetch the first API page to learn the total number of results.
             If the cache already holds that many workouts, return immediately.
          3. Fetch pages from newest to oldest, waiting _PAGE_DELAY seconds
             between each page after the first (rate limiting).
          4. For each page, add every unseen workout to the local dict.
          5. Stop fetching once an overlap is found AND the dict size matches
             the reported total — everything older is already stored.
          6. Return (updated_dict, sorted_list).

        On the first run this fetches the entire history page by page.
        On subsequent runs it typically fetches only 1–2 pages.

        Args:
            initial_workouts: dict of {str(id): result_dict} from localStorage.
            on_progress: optional callable(pages_fetched, workouts_cached)
                         called after each page. Safe to call from a background
                         thread (e.g. mutate an hd.state).
        """
        local = dict(initial_workouts)  # work on a copy
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

            if on_progress:
                on_progress(page, len(local))

            # Only stop on overlap once the cache is complete.
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
        return local, workouts

    # ------------------------------------------------------------------
    # Per-workout stroke data
    # ------------------------------------------------------------------

    def get_strokes(self, user_id: int, result_id: int) -> list:
        """
        Fetch the stroke-by-stroke data for a single workout.

        Calls ``GET /api/users/{user_id}/results/{result_id}/strokes``.
        Returns the ``data`` list directly (one dict per stroke):

            t   — elapsed time (tenths of a second)
            d   — elapsed distance (decimeters)
            p   — pace (tenths of sec/500m)
            spm — strokes per minute
            hr  — heart rate in bpm (0 when no HR monitor was worn)

        Returns an empty list when the response contains no data.
        Not cached — fetched on demand each time a session detail view opens.
        """
        path = f"/users/{user_id}/results/{result_id}/strokes"
        results = self._get(path)
        return results.get("data") or []

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


def get_client(user_id: str) -> Optional[Concept2Client]:
    """
    Return an authenticated Concept2Client for the given user ID, loading
    their token from .concept2_token_{user_id}.json and refreshing if expired.
    Returns None if no token file exists for this user.
    """
    token_data = get_valid_token(user_id)
    if token_data is None:
        return None
    return Concept2Client(token_data["access_token"], user_id=user_id)


def extract_c2_profile(user_data: dict) -> dict:
    """
    Convert a Concept2 /users/me 'data' sub-dict to an Erg Nerd profile dict.

    Weight from Concept2 is stored in decagrams (10 g each); dividing by 100
    gives kilograms.  The 'email' field is intentionally ignored here.

    weight_class is derived from weight + gender using the standard open/elite
    lightweight limits (men ≤ 72.5 kg, women ≤ 59.0 kg).
    """
    from services.rowinglevel import _LW_LIMIT_KG  # avoid top-level circular import

    gender_map = {"M": "Male", "F": "Female"}
    weight_raw = user_data.get("weight") or 0
    weight_kg = round(weight_raw / 100, 1) if weight_raw else 0.0
    max_hr = user_data.get("max_heart_rate") or None
    if max_hr is not None and not (50 <= int(max_hr) <= 220):
        max_hr = None

    gender = gender_map.get(user_data.get("gender", ""), "")
    limit = _LW_LIMIT_KG.get(gender)
    if limit is not None and weight_kg > 0:
        weight_class = "Lightweight" if weight_kg <= limit else "Heavyweight"
    else:
        weight_class = ""

    return {
        "gender": gender,
        "dob": user_data.get("dob", ""),
        "weight": weight_kg,
        "weight_unit": "kg",
        "weight_class": weight_class,
        "max_heart_rate": max_hr,
    }
