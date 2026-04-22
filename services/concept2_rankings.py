"""
Offline scraper + cache for Concept2 public RowErg rankings.

Concept2's logbook API has no rankings endpoint, so rankings are read from the
public HTML pages at ``https://log.concept2.com/rankings/{year}/rower/{event_id}``.
This module is pure Python (no HyperDiv) and is intended to be driven by the
CLI in ``bin/sync_c2_rankings.py``.

Cache layout — one JSON per fetched HTML page, plain directory at repo root:

    .c2_rankings/
      2025_rower_2000_age=40-49_weight=H_gender=M_page_1.json
      2025_rower_2000_age=40-49_weight=H_gender=M_page_2.json
      ...
      2025_rower_2000_age=0-12_gender=M_page_1.json          # youth (no weight)

Progress state is the file system itself: if a page file exists, that page is
considered done; re-running the script skips it. A crash loses at most one
in-flight page. No manifest, no in-progress map.

Scraping strategy — ``scrape_all`` iterates with events as the outer loop and
seasons descending (newest → oldest) as the inner loop. After all age / weight
/ gender combos for a given (event, season) have been scraped or confirmed
cached, the module checks whether every combo for that season was empty (i.e.
the event did not exist that year). If so, the event is discontinued for that
season and all earlier ones. This avoids fetching years of blank pages for
events that were only added to the rankings recently (e.g. 100m).

Per-entry schema kept lean (position, name, age, country, value_tenths,
verified). Location and club are on the rendered HTML but omitted from the
cache.

Event coordinates:
  * Distance events — the URL's ``rower/{id}`` is the distance in meters, and
    the displayed value is a time string (``M:SS.T``). Stored as tenths of a
    second in ``value_tenths``.
  * Time events — the URL's ``rower/{id}`` is the duration in minutes (e.g.
    ``rower/30`` = 30 min), and the displayed value is a plain integer in
    meters. Stored as that integer in ``value_tenths`` (field name preserved
    for schema uniformity; disambiguation is by ``event_kind``).

Rate limit: minimum 3.0 s between outbound requests, plus a small random
jitter, matching ``services/rowinglevel.py``. Exponential back-off on
429/5xx.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from services.rowing_utils import RANKED_DISTANCES, RANKED_TIMES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://log.concept2.com/rankings"
CACHE_DIR = Path(".c2_rankings")
FAILURES_LOG = CACHE_DIR / "_failures.log"
SCHEMA_VERSION = 1

MIN_INTERVAL = 3.0  # seconds between requests
JITTER = 0.5  # random extra delay
EARLIEST_SEASON = 2002

# URL age-band values. These differ from ``services/concept2_records.py``'s
# ``_AGE_BANDS`` (which splits youth into 17-18 / 15-16 / 13-14 / 12-under).
# The public rankings page uses 0-12 and 13-18 as single youth buckets with
# no weight filter.
AGE_BANDS: list[str] = [
    "0-12",
    "13-18",
    "19-29",
    "30-39",
    "40-49",
    "50-54",
    "55-59",
    "60-64",
    "65-69",
    "70-74",
    "75-79",
    "80-84",
    "85-89",
    "90-94",
    "95-99",
    "100",
]
GENDERS: list[str] = ["M", "F"]
WEIGHT_CLASSES_ADULT: list[str] = ["H", "L"]
YOUTH_BANDS: frozenset[str] = frozenset({"0-12", "13-18"})

# Canonical event table: (kind, tenths_or_meters, url_id)
# * Distance events — url_id is the distance in meters (same as the event value).
# * Time events — url_id is the duration in minutes (verified against the live
#   site with ``curl .../rower/30?age=40-49&weight=H&gender=M``).
EVENT_IDS_DIST: dict[int, int] = {d: d for d, _ in RANKED_DISTANCES}
EVENT_IDS_TIME: dict[int, int] = {
    600: 1,  # 1 min
    2400: 4,  # 4 min
    18000: 30,  # 30 min
    36000: 60,  # 60 min
}

USER_AGENT = (
    "Erg Nerd rankings sync (personal use; https://github.com/tkriplean/Erg-Nerd)"
)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Category:
    """One (season × event × age × weight × gender) bucket to scrape.

    ``weight`` is ``None`` for youth bands (0-12 / 13-18) which Concept2
    presents as a single-weight bucket.
    """

    season: int
    event_kind: str  # "dist" | "time"
    event_value: int  # meters (dist) or tenths (time) — matches RANKED_*
    event_id: int  # the value used in the URL path
    age_band: str
    weight: Optional[str]  # "H" | "L" | None
    gender: str  # "M" | "F"


# ---------------------------------------------------------------------------
# Season helpers
# ---------------------------------------------------------------------------


def latest_complete_season(today: Optional[date] = None) -> int:
    """Return the integer season-end year of the most recently finished season.

    Concept2 seasons run May 1 → Apr 30. The URL uses the end year as the
    season label (e.g. ``/rankings/2025/`` = season May 2024 → Apr 2025).
    On any day from May 1 year Y onwards, season Y is complete.
    """
    d = today or date.today()
    return d.year - 1 if (d.month, d.day) < (5, 1) else d.year


def all_past_seasons(today: Optional[date] = None) -> list[int]:
    """All season-end years from EARLIEST_SEASON through the most recent complete one."""
    return list(range(EARLIEST_SEASON, latest_complete_season(today) + 1))


# ---------------------------------------------------------------------------
# Category enumeration
# ---------------------------------------------------------------------------


def _event_list() -> list[tuple[str, int, int]]:
    """All 13 ranked events as (kind, event_value, url_id) tuples, distances first."""
    out: list[tuple[str, int, int]] = []
    for d, _ in RANKED_DISTANCES:
        out.append(("dist", d, EVENT_IDS_DIST[d]))
    for t, _ in RANKED_TIMES:
        out.append(("time", t, EVENT_IDS_TIME[t]))
    return out


def _combos_for(
    ev_id: int,
    kind: str,
    ev_value: int,
    season: int,
    age_list: list[str],
    weight_list: list[str],
    gender_list: list[str],
    explicit_weights: bool,
) -> list[Category]:
    """Return all Category objects for one (event, season) pair.

    ``explicit_weights=True`` suppresses youth bands (which have no weight
    split) when the caller has filtered to a specific weight class.
    """
    out: list[Category] = []
    for age in age_list:
        if age in YOUTH_BANDS:
            if explicit_weights:
                continue
            for gender in gender_list:
                out.append(Category(season, kind, ev_value, ev_id, age, None, gender))
            continue
        for weight in weight_list:
            for gender in gender_list:
                out.append(Category(season, kind, ev_value, ev_id, age, weight, gender))
    return out


def read_page1_payload(cat: Category) -> Optional[dict]:
    """Read and return the parsed JSON payload of page 1 for a category.

    Returns ``None`` if the file does not exist or cannot be parsed.  The
    caller uses ``payload.get('empty', False)`` to test whether the category
    had zero entries.
    """
    path = page_filename(cat, 1)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def iter_all_categories(
    *,
    seasons: Optional[list[int]] = None,
    event_ids: Optional[list[int]] = None,
    age_bands: Optional[list[str]] = None,
    weights: Optional[list[str]] = None,
    genders: Optional[list[str]] = None,
    today: Optional[date] = None,
) -> Iterator[Category]:
    """Yield every Category matching the (optional) filter kwargs.

    Ordering: season (newest first) → event → age band → weight → gender.
    Intended for query / read-side use; ``scrape_all`` uses a different
    event-outer ordering with early per-event cutoff.
    Empty/None filter lists mean "include all".
    """
    season_list = sorted(
        seasons if seasons is not None else all_past_seasons(today),
        reverse=True,  # newest first
    )
    events = [
        (k, v, u)
        for (k, v, u) in _event_list()
        if (event_ids is None or u in event_ids)
    ]
    age_list = age_bands if age_bands is not None else AGE_BANDS
    gender_list = genders if genders is not None else GENDERS
    weight_list = weights if weights is not None else WEIGHT_CLASSES_ADULT
    explicit_weights = weights is not None

    for season in season_list:
        for kind, ev_value, ev_id in events:
            for cat in _combos_for(
                ev_id,
                kind,
                ev_value,
                season,
                age_list,
                weight_list,
                gender_list,
                explicit_weights,
            ):
                yield cat


# ---------------------------------------------------------------------------
# URL + filename
# ---------------------------------------------------------------------------


def build_url(cat: Category, page: int) -> str:
    """Construct the rankings page URL for a (category, page) pair."""
    params = [("age", cat.age_band)]
    if cat.weight is not None:
        params.append(("weight", cat.weight))
    params.append(("gender", cat.gender))
    params.append(("rower", "rower"))
    params.append(("page", str(page)))
    return f"{BASE_URL}/{cat.season}/rower/{cat.event_id}?{urlencode(params)}"


def page_filename(cat: Category, page: int) -> Path:
    """Return the on-disk JSON path for a (category, page) tuple.

    Filename mirrors the URL query string so ``ls .c2_rankings/ | sort`` groups
    pages by category naturally.
    """
    bits = [
        f"{cat.season}_rower_{cat.event_id}",
        f"age={cat.age_band}",
    ]
    if cat.weight is not None:
        bits.append(f"weight={cat.weight}")
    bits.append(f"gender={cat.gender}")
    bits.append(f"page_{page}")
    return CACHE_DIR / ("_".join(bits) + ".json")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_last_request_at: float = 0.0
_min_interval_override: Optional[float] = None


def set_rate_limit(seconds: float) -> None:
    """Override MIN_INTERVAL at runtime (used by the ``--rate-limit`` CLI flag)."""
    global _min_interval_override
    _min_interval_override = max(0.0, float(seconds))


def _rate_limit() -> None:
    global _last_request_at
    interval = (
        _min_interval_override if _min_interval_override is not None else MIN_INTERVAL
    )
    wait = interval - (time.time() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    # Add a small jitter on top to avoid a perfectly regular cadence.
    time.sleep(random.uniform(0.0, JITTER))
    _last_request_at = time.time()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class RankingsFetchError(Exception):
    """Raised after retries are exhausted on HTTP errors."""

    def __init__(self, url: str, status: Optional[int], reason: str):
        self.url = url
        self.status = status
        self.reason = reason
        super().__init__(f"{status} {reason} :: {url}")


_BACKOFF_SCHEDULE = (30.0, 120.0, 480.0)  # 30 s → 2 min → 8 min
_client: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )
    return _client


def _fetch_html(url: str) -> str:
    """Fetch a URL with rate limiting and exponential back-off on 429/5xx.

    Raises RankingsFetchError after 3 retries, or immediately on other 4xx.
    A 404 is surfaced so the caller can treat the category as empty without
    triggering the back-off path.
    """
    client = _get_client()
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for attempt, delay in enumerate([0.0, *_BACKOFF_SCHEDULE]):
        if delay:
            time.sleep(delay)
        _rate_limit()
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            last_exc = exc
            continue
        last_status = resp.status_code
        if 200 <= resp.status_code < 300:
            return resp.text
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            # Retry with back-off
            continue
        # Other 4xx: raise immediately
        raise RankingsFetchError(
            url, resp.status_code, resp.reason_phrase or "HTTP error"
        )
    raise RankingsFetchError(
        url, last_status, str(last_exc) if last_exc else "retries exhausted"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _parse_time_to_tenths(text: str) -> Optional[int]:
    """Convert a displayed time string to tenths of a second.

    Accepts "H:MM:SS.T", "MM:SS.T", "M:SS.T", or bare "SS.T".
    Returns None if the string cannot be parsed.
    """
    s = text.strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 3:
            total = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            total = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1:
            total = float(parts[0])
        else:
            return None
    except ValueError:
        return None
    return int(round(total * 10))


def _parse_distance_to_meters(text: str) -> Optional[int]:
    """Parse a plain integer-meters distance (may contain commas)."""
    s = text.strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


_VERIFIED_MAP = {"Yes": "Y", "No": "N", "Race": "R"}


def parse_rankings_page(html: str, event_kind: str) -> dict:
    """Parse a rankings HTML page into a structured payload.

    Returns a dict with keys:
      * entries: list[dict]  — one per row, in page order
      * total_count: int|None — from the "Total people" stat block; None for empties
      * is_last_page: bool  — true when there is no "next" page link
      * empty: bool  — true when the page reports "no ranking results"
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Empty category page ----------------------------------------------
    content = soup.find("section", class_="content")
    if content is not None:
        empty_p = content.find(
            "p",
            string=lambda s: bool(s) and "no ranking results were found" in s.lower(),
        )
        # Fallback — the text is the only <p> inside content and contains our phrase.
        if empty_p is None:
            for p in content.find_all("p"):
                if p.get_text(strip=True).lower().startswith("sorry, no ranking"):
                    empty_p = p
                    break
        if empty_p is not None:
            return {
                "entries": [],
                "total_count": 0,
                "is_last_page": True,
                "empty": True,
            }

    # --- Total people stat ------------------------------------------------
    total_count: Optional[int] = None
    for stat in soup.find_all("div", class_="stat"):
        name_div = stat.find("div", class_="stat__name")
        if not name_div or name_div.get_text(strip=True).lower() != "total people":
            continue
        fig = stat.find("div", class_="stat__figure")
        if fig is None:
            continue
        span = fig.find("span")
        if span is None:
            continue
        text = span.get_text(strip=True).replace(",", "")
        if text.isdigit():
            total_count = int(text)
        break

    # --- Rankings table ---------------------------------------------------
    # There can be multiple <table class="table"> on the page (stats sidebar
    # has one). The rankings table is the one inside <section class="content">
    # whose first <th> is "Pos.".
    rankings_table = None
    tables = (content or soup).find_all("table", class_="table")
    for tbl in tables:
        first_th = tbl.find("th")
        if first_th and first_th.get_text(strip=True).startswith("Pos"):
            rankings_table = tbl
            break

    entries: list[dict] = []
    if rankings_table is not None:
        tbody = rankings_table.find("tbody")
        if tbody is not None:
            for tr in tbody.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 8:
                    continue
                try:
                    position = int(tds[0].get_text(strip=True))
                except ValueError:
                    continue
                name_a = tds[1].find("a")
                name = (
                    name_a.get_text(strip=True)
                    if name_a
                    else tds[1].get_text(strip=True)
                )
                try:
                    age_val = int(tds[2].get_text(strip=True))
                except ValueError:
                    age_val = 0
                # tds[3] = location, tds[5] = club — intentionally dropped
                country = tds[4].get_text(strip=True)
                raw_value = tds[6].get_text(strip=True)
                if event_kind == "dist":
                    value = _parse_time_to_tenths(raw_value)
                else:
                    value = _parse_distance_to_meters(raw_value)
                if value is None:
                    continue
                verified_raw = tds[7].get_text(strip=True)
                verified = _VERIFIED_MAP.get(verified_raw, verified_raw[:1] or "?")
                entries.append(
                    {
                        "position": position,
                        "name": name,
                        "age": age_val,
                        "country": country,
                        "value_tenths": value,
                        "verified": verified,
                    }
                )

    # --- Pagination -------------------------------------------------------
    # is_last_page = True when there's no <a rel="next"> link inside the
    # pagination <ul>. Empty pagination (no block at all) also means last.
    pagination = soup.find("ul", class_="pagination")
    is_last_page = True
    if pagination is not None:
        next_link = pagination.find("a", rel="next")
        if next_link is not None:
            is_last_page = False

    return {
        "entries": entries,
        "total_count": total_count
        if total_count is not None
        else (0 if not entries else None),
        "is_last_page": is_last_page,
        "empty": not entries and total_count in (None, 0),
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def _write_page_file(cat: Category, page: int, url: str, parsed: dict) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "url": url,
        "fetched_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "season": cat.season,
        "event_id": cat.event_id,
        "event_kind": cat.event_kind,
        "event_value": cat.event_value,
        "age_band": cat.age_band,
        "weight": cat.weight,
        "gender": cat.gender,
        "page": page,
        "total_count": parsed.get("total_count"),
        "is_last_page": parsed.get("is_last_page", True),
        "empty": parsed.get("empty", False),
        "entries": parsed.get("entries", []),
    }
    path = page_filename(cat, page)
    _atomic_write_json(path, payload)
    return path


def _log_failure(url: str, err: Exception) -> None:
    _ensure_cache_dir()
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{url}\t{type(err).__name__}: {err}\n"
    with FAILURES_LOG.open("a") as f:
        f.write(line)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


ProgressCB = Callable[[dict], None]
AbortCheck = Callable[[], bool]


def _read_is_last(path: Path) -> Optional[bool]:
    """Return the stored ``is_last_page`` flag for an existing page file, or None."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return bool(data.get("is_last_page", False))


def scrape_category(
    cat: Category,
    *,
    force: bool = False,
    max_pages: Optional[int] = None,
    abort_check: AbortCheck = lambda: False,
    on_progress: Optional[ProgressCB] = None,
) -> dict:
    """Scrape one category, writing one JSON per page. Resumable.

    * Starts at page 1. If ``page_N.json`` exists (and not ``force``), reads
      its ``is_last_page`` to decide whether to continue.
    * Stops after the first page with ``is_last_page = true`` or when an
      empty/404 page indicates the category has no entries.
    """
    _ensure_cache_dir()
    pages_written = 0
    pages_skipped = 0
    failures = 0
    reached_end = False

    def _emit(kind: str, **kw) -> None:
        if on_progress is not None:
            on_progress({"kind": kind, "category": cat, **kw})

    _emit("category_start")

    page = 1
    while True:
        if abort_check():
            _emit("aborted")
            return {
                "pages_written": pages_written,
                "pages_skipped": pages_skipped,
                "failures": failures,
                "reached_end": reached_end,
                "aborted": True,
            }
        if max_pages is not None and page > max_pages:
            _emit("category_capped", page=page - 1)
            break

        path = page_filename(cat, page)
        if path.exists() and not force:
            is_last = _read_is_last(path)
            pages_skipped += 1
            _emit("page_skipped", page=page, is_last=is_last)
            if is_last:
                reached_end = True
                break
            page += 1
            continue

        url = build_url(cat, page)
        _emit("page_start", page=page, url=url)
        try:
            html = _fetch_html(url)
        except RankingsFetchError as exc:
            failures += 1
            _log_failure(url, exc)
            _emit("page_failed", page=page, url=url, error=str(exc))
            # On 404, treat as empty category and stop (Concept2 404s
            # bucketings that don't exist, e.g. some age+event combos).
            if exc.status == 404:
                reached_end = True
                break
            # Other fatal error — stop this category; script continues.
            break

        try:
            parsed = parse_rankings_page(html, cat.event_kind)
        except Exception as exc:  # noqa: BLE001 — defensive
            failures += 1
            _log_failure(url, exc)
            _emit("page_failed", page=page, url=url, error=f"parse error: {exc}")
            break

        _write_page_file(cat, page, url, parsed)
        pages_written += 1
        _emit(
            "page_done",
            page=page,
            url=url,
            n_entries=len(parsed.get("entries") or []),
            total_count=parsed.get("total_count"),
            is_last=parsed.get("is_last_page", True),
            empty=parsed.get("empty", False),
        )

        if parsed.get("empty") or parsed.get("is_last_page"):
            reached_end = True
            break
        if not parsed.get("entries"):
            # Defensive: non-empty page with no parsable rows — bail to avoid loop.
            reached_end = True
            break
        page += 1

    _emit(
        "category_done",
        pages_written=pages_written,
        pages_skipped=pages_skipped,
        failures=failures,
        reached_end=reached_end,
    )
    return {
        "pages_written": pages_written,
        "pages_skipped": pages_skipped,
        "failures": failures,
        "reached_end": reached_end,
        "aborted": False,
    }


def scrape_all(
    *,
    seasons: Optional[list[int]] = None,
    event_ids: Optional[list[int]] = None,
    age_bands: Optional[list[str]] = None,
    weights: Optional[list[str]] = None,
    genders: Optional[list[str]] = None,
    force: bool = False,
    max_pages: Optional[int] = None,
    abort_check: AbortCheck = lambda: False,
    on_progress: Optional[ProgressCB] = None,
    today: Optional[date] = None,
) -> dict:
    """Scrape all matching categories, iterating event-outer / season-descending.

    Iteration order: for each event (oldest-to-newest season is NOT used),
    seasons are walked newest → oldest. After all (age × weight × gender)
    combos for a given (event, season) are resolved, the function checks
    whether every combo confirmed empty (i.e. the event had no participants
    that year). If so, scraping stops for that event — there is no point
    fetching even older seasons.

    A combo counts as "confirmed empty" only when its ``page_1.json`` exists
    *and* has ``"empty": true``. A missing file (fetch error) is treated as
    inconclusive, preventing premature cutoff.

    Returns a totals dict with keys: categories_total, categories_done,
    pages_written, pages_skipped, failures, events_discontinued, aborted.
    """
    season_list = sorted(
        seasons if seasons is not None else all_past_seasons(today), reverse=True
    )
    events_filtered = [
        (k, v, u) for (k, v, u) in _event_list() if event_ids is None or u in event_ids
    ]
    age_list = age_bands if age_bands is not None else AGE_BANDS
    gender_list = genders if genders is not None else GENDERS
    weight_list = weights if weights is not None else WEIGHT_CLASSES_ADULT
    explicit_weights = weights is not None

    # Upper-bound on category count (early cutoff will reduce the actual count).
    n_combos_per_season = sum(
        (len(gender_list) if age in YOUTH_BANDS and not explicit_weights else 0)
        + (len(weight_list) * len(gender_list) if age not in YOUTH_BANDS else 0)
        for age in age_list
    )
    max_cats = len(events_filtered) * len(season_list) * n_combos_per_season

    if on_progress is not None:
        on_progress({"kind": "run_start", "total_categories_max": max_cats})

    totals: dict = {
        "categories_total": max_cats,
        "categories_done": 0,
        "pages_written": 0,
        "pages_skipped": 0,
        "failures": 0,
        "events_discontinued": 0,
        "aborted": False,
    }
    cat_index = 0

    for kind, ev_value, ev_id in events_filtered:
        for season in season_list:
            if abort_check():
                totals["aborted"] = True
                if on_progress is not None:
                    on_progress({"kind": "run_aborted", "completed": cat_index})
                if on_progress is not None:
                    on_progress({"kind": "run_done", "totals": totals})
                return totals

            combos = _combos_for(
                ev_id,
                kind,
                ev_value,
                season,
                age_list,
                weight_list,
                gender_list,
                explicit_weights,
            )

            for cat in combos:
                cat_index += 1
                if on_progress is not None:
                    on_progress(
                        {
                            "kind": "category_index",
                            "index": cat_index,
                            "total": max_cats,
                            "category": cat,
                        }
                    )
                result = scrape_category(
                    cat,
                    force=force,
                    max_pages=max_pages,
                    abort_check=abort_check,
                    on_progress=on_progress,
                )
                totals["categories_done"] += 1
                totals["pages_written"] += result["pages_written"]
                totals["pages_skipped"] += result["pages_skipped"]
                totals["failures"] += result["failures"]
                if result.get("aborted"):
                    totals["aborted"] = True
                    if on_progress is not None:
                        on_progress({"kind": "run_done", "totals": totals})
                    return totals

            # ── Early-cutoff check ────────────────────────────────────────
            # Inspect page-1 files for every combo in this (event, season).
            # Only discontinue if ALL combos have confirmed-empty page-1 files.
            # A missing file means we couldn't determine emptiness — be conservative.
            all_confirmed_empty = bool(combos)  # True unless combos is somehow empty
            for cat in combos:
                payload = read_page1_payload(cat)
                if payload is None or not payload.get("empty", False):
                    all_confirmed_empty = False
                    break

            if all_confirmed_empty:
                ev_label = f"ev_id={ev_id}"
                if on_progress is not None:
                    on_progress(
                        {
                            "kind": "event_discontinued",
                            "event_id": ev_id,
                            "event_kind": kind,
                            "event_value": ev_value,
                            "at_season": season,
                        }
                    )
                totals["events_discontinued"] += 1
                break  # move on to the next event

    if on_progress is not None:
        on_progress({"kind": "run_done", "totals": totals})
    return totals


# ---------------------------------------------------------------------------
# Query helpers (used by the Rank Page)
# ---------------------------------------------------------------------------

# C2 rankings weight classes ("H" / "L") vs WR API ("Hwt" / "Lwt"). The Rank
# Page normalises both to the C2 rankings style at the call site.

# Canonical adult age-band bucketing used by the C2 rankings pages. Must stay
# in sync with the URL path values in ``AGE_BANDS``.
_RANKINGS_AGE_BANDS_ADULT: list[tuple[int, int, str]] = [
    (19, 29, "19-29"),
    (30, 39, "30-39"),
    (40, 49, "40-49"),
    (50, 54, "50-54"),
    (55, 59, "55-59"),
    (60, 64, "60-64"),
    (65, 69, "65-69"),
    (70, 74, "70-74"),
    (75, 79, "75-79"),
    (80, 84, "80-84"),
    (85, 89, "85-89"),
    (90, 94, "90-94"),
    (95, 99, "95-99"),
    (100, 200, "100"),
]


def rankings_age_band(age: int) -> str:
    """Return the C2-rankings URL age_band for a given whole-year age.

    Youth band splits: 0-12 and 13-18.
    """
    if age <= 12:
        return "0-12"
    if age <= 18:
        return "13-18"
    for lo, hi, label in _RANKINGS_AGE_BANDS_ADULT:
        if lo <= age <= hi:
            return label
    return "100"


@dataclass(frozen=True)
class RankingModifiers:
    """Optional pool-restriction filters used by the Rank Page."""

    must_have_event_kinds: frozenset = frozenset()
    exclude_unverified: bool = False
    min_ranked_performances: int = 1
    # Optional precomputed {name: count} from a cross-event sweep. If None,
    # ``min_ranked_performances`` and ``must_have_event_kinds`` are skipped.
    name_counts: Optional[dict] = None
    name_event_sets: Optional[dict] = None


def _apply_modifiers(entries: list[dict], modifiers: Optional[RankingModifiers]) -> list[dict]:
    if modifiers is None:
        return entries
    out = entries
    if modifiers.exclude_unverified:
        out = [e for e in out if e.get("verified") == "Y"]
    if modifiers.min_ranked_performances > 1 and modifiers.name_counts is not None:
        nc = modifiers.name_counts
        mn = modifiers.min_ranked_performances
        out = [e for e in out if nc.get(e.get("name", ""), 0) >= mn]
    if modifiers.must_have_event_kinds and modifiers.name_event_sets is not None:
        nes = modifiers.name_event_sets
        required = modifiers.must_have_event_kinds
        out = [e for e in out if required.issubset(nes.get(e.get("name", ""), frozenset()))]
    return out


def filter_matched_rankings(
    entries: list[dict],
    *,
    target_age: int,
    k: int = 0,
    gender: str,
    weight_class: Optional[str],
    modifiers: Optional[RankingModifiers] = None,
) -> list[dict]:
    """Age-matched pool: rows within ``target_age ± k`` of the given age.

    ``gender`` is ``"M"`` / ``"F"``; ``weight_class`` is ``"H"`` / ``"L"`` /
    ``None`` (youth). Entries must have the expected fields from the index
    (``gender``, ``weight``, ``age``).
    """
    lo, hi = target_age - k, target_age + k
    out: list[dict] = []
    for e in entries:
        if e.get("gender") != gender:
            continue
        if e.get("weight") != weight_class:
            continue
        a = e.get("age", -1)
        if a < lo or a > hi:
            continue
        out.append(e)
    return _apply_modifiers(out, modifiers)


def age_group_matched_rankings(
    entries: list[dict],
    *,
    age_band: str,
    gender: str,
    weight_class: Optional[str],
    modifiers: Optional[RankingModifiers] = None,
) -> list[dict]:
    """Age-group pool: rows whose cached ``age_band`` matches exactly."""
    out: list[dict] = []
    for e in entries:
        if e.get("gender") != gender:
            continue
        if e.get("weight") != weight_class:
            continue
        if e.get("age_band") != age_band:
            continue
        out.append(e)
    return _apply_modifiers(out, modifiers)


def rank_in_pool(
    pool: list[dict], user_value_tenths: int, event_kind: str
) -> tuple[int, int, float]:
    """Return (rank, total, percentile) for a user's value against ``pool``.

    * Distance events: lower ``value_tenths`` (time) = better.
    * Time events: higher ``value_tenths`` (meters) = better.
    * percentile = 100 * (total - rank + 1) / total  (100 ≈ top).
    * rank counts the number of pool entries strictly better than the user
      plus 1. If the pool is empty, returns (0, 0, 0.0).
    """
    total = len(pool)
    if total == 0:
        return 0, 0, 0.0
    if event_kind == "dist":
        better = sum(1 for e in pool if e.get("value_tenths", 0) < user_value_tenths)
    else:
        better = sum(1 for e in pool if e.get("value_tenths", 0) > user_value_tenths)
    rank = better + 1
    pct = 100.0 * (total - rank + 1) / total
    return rank, total, pct


def histogram_watts(
    pool: list[dict], event_kind: str, event_value: int, bins: int = 30
) -> tuple[list[int], float, float]:
    """Bin ``pool`` values as watts. Returns (counts, min_watts, max_watts).

    Converts each entry's ``value_tenths`` → pace → watts via
    ``services.rowing_utils.compute_watts``. Returns zeros + (0,0) on empty.
    """
    from services.rowing_utils import compute_watts

    watts_list: list[float] = []
    for e in pool:
        v = e.get("value_tenths")
        if v is None or v <= 0:
            continue
        if event_kind == "dist":
            # v = tenths of a second for event_value meters
            t_sec = v / 10.0
            dist_m = event_value
        else:
            # v = meters covered in event_value tenths
            t_sec = event_value / 10.0
            dist_m = v
        if dist_m <= 0 or t_sec <= 0:
            continue
        pace = t_sec / (dist_m / 500.0)
        w = compute_watts(pace)
        if w is None:
            continue
        try:
            if not (w > 0):
                continue
        except TypeError:
            continue
        watts_list.append(float(w))

    if not watts_list:
        return [0] * bins, 0.0, 0.0
    wmin, wmax = min(watts_list), max(watts_list)
    if wmax <= wmin:
        counts = [0] * bins
        counts[0] = len(watts_list)
        return counts, wmin, wmax
    width = (wmax - wmin) / bins
    counts = [0] * bins
    for w in watts_list:
        idx = int((w - wmin) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    return counts, wmin, wmax


# ---------------------------------------------------------------------------
# Self-test (run with ``python -m services.concept2_rankings``)
# ---------------------------------------------------------------------------


def _run_parser_self_test() -> int:
    fixture_dir = Path("references/rankings_fixtures")
    expectations = [
        (
            "2025_rower_2000_age=40-49_weight=H_gender=M_page_1.html",
            "dist",
            {
                "min_entries": 45,
                "total_at_least": 1000,
                "is_last_page": False,
                "empty": False,
            },
        ),
        (
            "2025_rower_30_age=40-49_weight=H_gender=M_page_1.html",
            "time",
            {
                "min_entries": 45,
                "total_at_least": 500,
                "is_last_page": False,
                "empty": False,
            },
        ),
        (
            "2005_rower_500_age=95-99_weight=H_gender=F_page_1.html",
            "dist",
            {"min_entries": 0, "empty": True, "is_last_page": True},
        ),
        (
            "2025_rower_2000_age=0-12_gender=M_page_1.html",
            "dist",
            {"min_entries": 30, "empty": False},
        ),
    ]

    failures = 0
    for fname, kind, expect in expectations:
        path = fixture_dir / fname
        if not path.exists():
            print(f"SKIP {fname} — fixture missing")
            continue
        parsed = parse_rankings_page(path.read_text(), kind)
        n = len(parsed["entries"])
        ok = True
        if n < expect.get("min_entries", 0):
            print(
                f"FAIL {fname}: expected >={expect.get('min_entries')} entries, got {n}"
            )
            ok = False
        if "total_at_least" in expect:
            tc = parsed.get("total_count") or 0
            if tc < expect["total_at_least"]:
                print(f"FAIL {fname}: total_count {tc} < {expect['total_at_least']}")
                ok = False
        for key in ("is_last_page", "empty"):
            if key in expect and parsed.get(key) != expect[key]:
                print(f"FAIL {fname}: {key}={parsed.get(key)} expected {expect[key]}")
                ok = False
        if ok and not expect.get("empty") and parsed["entries"]:
            # Spot-check the first row on every non-empty fixture.
            e0 = parsed["entries"][0]
            if not (
                isinstance(e0.get("position"), int)
                and isinstance(e0.get("age"), int)
                and isinstance(e0.get("value_tenths"), int)
                and e0.get("verified") in {"Y", "N", "R"}
            ):
                print(f"FAIL {fname}: first row looks wrong: {e0}")
                ok = False
        if ok:
            print(
                f"OK   {fname}: {n} entries, total_count={parsed.get('total_count')}, "
                f"is_last={parsed.get('is_last_page')}, empty={parsed.get('empty')}"
            )
        else:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_parser_self_test())
