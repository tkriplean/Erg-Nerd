"""
Offline scraper + cache for Concept2 public RowErg rankings.

Concept2's logbook API has no rankings endpoint, so rankings are read from the
public HTML pages at ``https://log.concept2.com/rankings/{year}/rower/{event_id}``.
This module is pure Python (no HyperDiv) and is intended to be driven by the
CLI in ``bin/sync_c2_rankings.py``.

Cache layout — one JSON per (season × event × age_band × weight × gender)
output category, plain directory at repo root:

    .c2_rankings/
      2025_rower_2000_age=40-49_weight=H_gender=M_page_1.json
      2025_rower_2000_age=50-54_weight=H_gender=M_page_1.json
      ...
      2025_rower_2000_age=0-12_gender=M_page_1.json          # youth (no weight)

Fetch strategy — adult queries omit the ``age`` URL parameter entirely; one
HTTP fetch per (season × event × weight × gender) returns every adult age
band in a single paginated stream.  After all pages are fetched, the entries
are partitioned by ``rankings_age_band(entry['age'])`` and one consolidated
``page_1.json`` is written per output age band.  Each per-age file therefore
holds the full ranking slice for that band and carries ``is_last_page=True``;
multi-page pagination is internal to the fetch and not surfaced in the cache.
Youth bands (0-12 and 13-18) keep the ``age`` parameter because Concept2 does
not weight-split youth categories — each youth fetch produces a single output
file with the same shape.

Progress state is the file system itself: if a category's ``page_1.json``
exists, that category is considered done; re-running the script skips it.
Mid-fetch crashes lose at most one fetch group's progress (re-fetched on the
next run).

Scraping strategy — ``scrape_all`` iterates with events as the outer loop and
seasons descending (newest → oldest) as the inner loop. After every fetch
group (each producing one or more output categories) for a given (event,
season) has resolved, the module checks whether every output category for
that season was empty (i.e. the event did not exist that year). If so, the
event is discontinued for that season and all earlier ones. This avoids
fetching years of blank pages for events that were only added to the rankings
recently (e.g. 100m).

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

from services.rowing_utils import ranked_distances, ranked_times, compute_watts

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

# Canonical event table: (kind, tenths_or_meters, url_id) — resolved per machine.
# * Distance events — url_id is the distance in meters (same as the event value).
# * Time events — url_id is the duration in minutes (verified against the live
#   site with ``curl .../rower/30?age=40-49&weight=H&gender=M``).
_EVENT_IDS_TIME_BY_MACHINE: dict[str, dict[int, int]] = {
    "rower": {
        600: 1,  # 1 min
        2400: 4,  # 4 min
        18000: 30,  # 30 min
        36000: 60,  # 60 min
    },
    "skierg": {
        600: 1,
        2400: 4,
        18000: 30,
        36000: 60,
    },
    "bikeerg": {
        600: 1,
        18000: 30,
        36000: 60,
    },
}


def event_ids_dist(machine: str = "rower") -> dict[int, int]:
    return {d: d for d, _ in ranked_distances(machine)}


def event_ids_time(machine: str = "rower") -> dict[int, int]:
    return _EVENT_IDS_TIME_BY_MACHINE.get(machine, _EVENT_IDS_TIME_BY_MACHINE["rower"])


USER_AGENT = (
    "Erg Nerd rankings sync (personal use; https://github.com/tkriplean/Erg-Nerd)"
)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Category:
    """One (season × event × age × weight × gender) output bucket.

    Each Category corresponds to exactly one ``page_1.json`` in the cache.
    ``weight`` is ``None`` for youth bands (0-12 / 13-18) which Concept2
    presents as a single-weight bucket.
    """

    season: int
    event_kind: str  # "dist" | "time"
    event_value: int  # meters (dist) or tenths (time) — matches the per-machine ranked tables
    event_id: int  # the value used in the URL path
    age_band: str
    weight: Optional[str]  # "H" | "L" | None
    gender: str  # "M" | "F"
    machine: str = "rower"  # "rower" | "skierg" | "bikeerg"


@dataclass(slots=True, frozen=True)
class FetchTarget:
    """Internal: one HTTP fetch group, possibly producing many output Categories.

    Adult targets carry ``weight`` and ``age_band=None`` — the URL omits the
    age parameter and yields entries spanning every adult age band.  Youth
    targets carry ``age_band`` (one of ``YOUTH_BANDS``) and ``weight=None`` —
    one URL per youth band, matching Concept2's youth-page structure.
    """

    season: int
    event_kind: str
    event_value: int
    event_id: int
    age_band: Optional[str]  # set for youth, None for adults
    weight: Optional[str]  # set for adults, None for youth
    gender: str
    machine: str


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


def _event_list(machine: str = "rower") -> list[tuple[str, int, int]]:
    """All ranked events for ``machine`` as (kind, event_value, url_id) tuples,
    distances first.
    """
    eid_dist = event_ids_dist(machine)
    eid_time = event_ids_time(machine)
    out: list[tuple[str, int, int]] = []
    for d, _ in ranked_distances(machine):
        out.append(("dist", d, eid_dist[d]))
    for t, _ in ranked_times(machine):
        out.append(("time", t, eid_time[t]))
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
    machine: str = "rower",
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
                out.append(
                    Category(season, kind, ev_value, ev_id, age, None, gender, machine)
                )
            continue
        for weight in weight_list:
            for gender in gender_list:
                out.append(
                    Category(
                        season, kind, ev_value, ev_id, age, weight, gender, machine
                    )
                )
    return out


def _targets_for(
    ev_id: int,
    kind: str,
    ev_value: int,
    season: int,
    weight_list: list[str],
    gender_list: list[str],
    include_youth: bool,
    machine: str = "rower",
) -> list[FetchTarget]:
    """Return all FetchTarget objects for one (event, season) pair.

    Adults: one target per (weight × gender), no age — a single URL covers
    every adult age band.  Youth: one target per (youth_band × gender),
    weight=None.
    """
    out: list[FetchTarget] = []
    for weight in weight_list:
        for gender in gender_list:
            out.append(
                FetchTarget(
                    season, kind, ev_value, ev_id, None, weight, gender, machine
                )
            )
    if include_youth:
        for age_band in sorted(YOUTH_BANDS):
            for gender in gender_list:
                out.append(
                    FetchTarget(
                        season, kind, ev_value, ev_id, age_band, None, gender, machine
                    )
                )
    return out


def output_categories_for(target: FetchTarget) -> list[Category]:
    """Return the output Categories produced by one fetch target.

    Adult target → one Category per adult age band (14 in total).
    Youth target → one Category for the target's youth band.
    """
    if target.age_band is not None:
        return [
            Category(
                target.season,
                target.event_kind,
                target.event_value,
                target.event_id,
                target.age_band,
                None,
                target.gender,
                target.machine,
            )
        ]
    return [
        Category(
            target.season,
            target.event_kind,
            target.event_value,
            target.event_id,
            ab,
            target.weight,
            target.gender,
            target.machine,
        )
        for ab in AGE_BANDS
        if ab not in YOUTH_BANDS
    ]


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


def iter_fetch_targets(
    *,
    seasons: Optional[list[int]] = None,
    event_ids: Optional[list[int]] = None,
    weights: Optional[list[str]] = None,
    genders: Optional[list[str]] = None,
    today: Optional[date] = None,
    machine: str = "rower",
) -> Iterator[FetchTarget]:
    """Yield every fetch target matching the (optional) filter kwargs.

    Ordering: season (newest first) → event → weight → gender, with youth
    targets appended after adult targets per (event, season).  Passing an
    explicit ``weights`` list suppresses youth targets.
    """
    season_list = sorted(
        seasons if seasons is not None else all_past_seasons(today),
        reverse=True,
    )
    events = [
        (k, v, u)
        for (k, v, u) in _event_list(machine)
        if (event_ids is None or u in event_ids)
    ]
    gender_list = genders if genders is not None else GENDERS
    weight_list = weights if weights is not None else WEIGHT_CLASSES_ADULT
    include_youth = weights is None

    for season in season_list:
        for kind, ev_value, ev_id in events:
            for target in _targets_for(
                ev_id,
                kind,
                ev_value,
                season,
                weight_list,
                gender_list,
                include_youth,
                machine,
            ):
                yield target


def iter_all_categories(
    *,
    seasons: Optional[list[int]] = None,
    event_ids: Optional[list[int]] = None,
    age_bands: Optional[list[str]] = None,
    weights: Optional[list[str]] = None,
    genders: Optional[list[str]] = None,
    today: Optional[date] = None,
    machine: str = "rower",
) -> Iterator[Category]:
    """Yield every output Category matching the (optional) filter kwargs.

    Ordering: season (newest first) → event → age band → weight → gender.
    Intended for query / read-side use (e.g. the index builder enumerating
    output files); ``scrape_all`` iterates ``FetchTarget`` objects internally
    with a different event-outer ordering and per-event early cutoff.
    Empty/None filter lists mean "include all".
    """
    season_list = sorted(
        seasons if seasons is not None else all_past_seasons(today),
        reverse=True,  # newest first
    )
    events = [
        (k, v, u)
        for (k, v, u) in _event_list(machine)
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
                machine,
            ):
                yield cat


# ---------------------------------------------------------------------------
# URL + filename
# ---------------------------------------------------------------------------


def build_fetch_url(target: FetchTarget, page: int) -> str:
    """Construct the actual rankings HTTP URL for a fetch target.

    Adult targets omit the ``age`` parameter; youth targets keep it.
    """
    params: list[tuple[str, str]] = []
    if target.age_band is not None:
        params.append(("age", target.age_band))
    if target.weight is not None:
        params.append(("weight", target.weight))
    params.append(("gender", target.gender))
    # Concept2's URL convention duplicates the machine slug as a query param.
    params.append((target.machine, target.machine))
    params.append(("page", str(page)))
    return f"{BASE_URL}/{target.season}/{target.machine}/{target.event_id}?{urlencode(params)}"


def page_filename(cat: Category, page: int) -> Path:
    """Return the on-disk JSON path for a (category, page) tuple.

    Filename mirrors the URL query string so ``ls .c2_rankings/ | sort`` groups
    pages by category naturally.  For machine="rower" the resulting filename
    matches the historical rower-only naming (``2025_rower_2000_age=...``), so
    pre-existing rower files keep working without migration.
    """
    bits = [
        f"{cat.season}_{cat.machine}_{cat.event_id}",
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
    print(path)
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


def scrape_fetch_target(
    target: FetchTarget,
    *,
    force: bool = False,
    max_pages: Optional[int] = None,
    abort_check: AbortCheck = lambda: False,
    on_progress: Optional[ProgressCB] = None,
) -> dict:
    """Fetch one URL group through pagination and write one consolidated
    ``page_1.json`` per output Category.

    All HTTP pages of the target's URL are fetched and accumulated in memory,
    then partitioned into output Categories by ``rankings_age_band(age)``
    (adults) or assigned in full to the single youth Category (youth targets).
    Resumability: if every output category's ``page_1.json`` already exists
    (and not ``force``), the fetch is skipped entirely.
    """
    _ensure_cache_dir()
    output_cats = output_categories_for(target)

    def _emit(kind: str, **kw) -> None:
        if on_progress is not None:
            on_progress({"kind": kind, "target": target, **kw})

    # Skip if every output file already exists and is complete. Old-format
    # multi-page per-age-band caches are converted to the consolidated layout
    # by ``bin/migrate_c2_rankings.py``; any remaining ``is_last_page=False``
    # file is a genuinely interrupted fetch and gets re-run here.
    if not force and all(
        (p := read_page1_payload(c)) is not None and p.get("is_last_page", False)
        for c in output_cats
    ):
        _emit("target_skipped", n_outputs=len(output_cats))
        return {
            "pages_written": 0,
            "pages_skipped": len(output_cats),
            "failures": 0,
            "reached_end": True,
            "aborted": False,
        }

    pages_fetched = 0
    failures = 0
    all_entries: list[dict] = []
    reached_end = False

    page = 1
    while True:
        if abort_check():
            _emit("aborted")
            return {
                "pages_written": 0,
                "pages_skipped": 0,
                "failures": failures,
                "reached_end": False,
                "aborted": True,
            }
        if max_pages is not None and page > max_pages:
            _emit("target_capped", page=page - 1)
            break

        url = build_fetch_url(target, page)
        _emit("page_start", page=page, url=url)
        try:
            html = _fetch_html(url)
        except RankingsFetchError as exc:
            failures += 1
            _log_failure(url, exc)
            _emit("page_failed", page=page, url=url, error=str(exc))
            if exc.status == 404:
                # Treat as empty target — write empty output files below.
                reached_end = True
                break
            # Hard failure mid-fetch — abandon this target without writing
            # partial outputs, so a future run can re-fetch cleanly.
            return {
                "pages_written": 0,
                "pages_skipped": 0,
                "failures": failures,
                "reached_end": False,
                "aborted": False,
            }

        try:
            parsed = parse_rankings_page(html, target.event_kind)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            _log_failure(url, exc)
            _emit("page_failed", page=page, url=url, error=f"parse error: {exc}")
            return {
                "pages_written": 0,
                "pages_skipped": 0,
                "failures": failures,
                "reached_end": False,
                "aborted": False,
            }

        pages_fetched += 1
        all_entries.extend(parsed.get("entries") or [])
        _emit(
            "page_done",
            page=page,
            url=url,
            n_entries=len(parsed.get("entries") or []),
            total_count=parsed.get("total_count"),
            is_last=parsed.get("is_last_page", True),
            empty=parsed.get("empty", False),
        )

        if parsed.get("empty"):
            reached_end = True
            break
        if parsed.get("is_last_page", True):
            reached_end = True
            break
        if not parsed.get("entries"):
            # Defensive: non-empty marker with no parsable rows.
            reached_end = True
            break
        page += 1

    # Bucket entries into output Categories and write one file each.
    representative_url = build_fetch_url(target, 1)
    if target.age_band is not None:
        # Youth: single output, all entries.
        buckets: dict[str, list[dict]] = {target.age_band: list(all_entries)}
    else:
        buckets = {c.age_band: [] for c in output_cats}
        for entry in all_entries:
            ab = rankings_age_band(int(entry.get("age") or 0))
            if ab in buckets:
                buckets[ab].append(entry)

    pages_written = 0
    for cat in output_cats:
        entries = buckets.get(cat.age_band, [])
        parsed_for_cat = {
            "entries": entries,
            "total_count": len(entries),
            "is_last_page": True,
            "empty": not entries,
        }
        _write_page_file(cat, 1, representative_url, parsed_for_cat)
        pages_written += 1

    _emit(
        "target_done",
        pages_fetched=pages_fetched,
        pages_written=pages_written,
        failures=failures,
        reached_end=reached_end,
    )
    return {
        "pages_written": pages_written,
        "pages_skipped": 0,
        "failures": failures,
        "reached_end": reached_end,
        "aborted": False,
    }


def scrape_all(
    *,
    seasons: Optional[list[int]] = None,
    event_ids: Optional[list[int]] = None,
    weights: Optional[list[str]] = None,
    genders: Optional[list[str]] = None,
    force: bool = False,
    max_pages: Optional[int] = None,
    abort_check: AbortCheck = lambda: False,
    on_progress: Optional[ProgressCB] = None,
    today: Optional[date] = None,
    machine: str = "rower",
) -> dict:
    """Scrape every matching fetch target, iterating event-outer / season-descending.

    Iteration order: for each event, seasons are walked newest → oldest.
    After every fetch target for a (event, season) is resolved, the function
    checks whether every output Category for that season is confirmed empty.
    If so, scraping stops for that event — there is no point fetching older
    seasons.

    A category counts as "confirmed empty" only when its ``page_1.json``
    exists *and* has ``"empty": true``. Missing files (fetch error) are
    inconclusive and prevent premature cutoff.

    Returns a totals dict with keys: targets_total, targets_done,
    pages_written, pages_skipped, failures, events_discontinued, aborted.
    """
    season_list = sorted(
        seasons if seasons is not None else all_past_seasons(today), reverse=True
    )
    events_filtered = [
        (k, v, u)
        for (k, v, u) in _event_list(machine)
        if event_ids is None or u in event_ids
    ]
    gender_list = genders if genders is not None else GENDERS
    weight_list = weights if weights is not None else WEIGHT_CLASSES_ADULT
    include_youth = weights is None

    n_youth = len(YOUTH_BANDS) if include_youth else 0
    n_targets_per_season = len(weight_list) * len(gender_list) + n_youth * len(
        gender_list
    )
    max_targets = len(events_filtered) * len(season_list) * n_targets_per_season

    if on_progress is not None:
        on_progress({"kind": "run_start", "total_targets_max": max_targets})

    totals: dict = {
        "targets_total": max_targets,
        "targets_done": 0,
        "pages_written": 0,
        "pages_skipped": 0,
        "failures": 0,
        "events_discontinued": 0,
        "aborted": False,
    }
    target_index = 0

    for kind, ev_value, ev_id in events_filtered:
        for season in season_list:
            if abort_check():
                totals["aborted"] = True
                if on_progress is not None:
                    on_progress({"kind": "run_aborted", "completed": target_index})
                    on_progress({"kind": "run_done", "totals": totals})
                return totals

            targets = _targets_for(
                ev_id,
                kind,
                ev_value,
                season,
                weight_list,
                gender_list,
                include_youth,
                machine,
            )

            for target in targets:
                target_index += 1
                if on_progress is not None:
                    on_progress(
                        {
                            "kind": "target_index",
                            "index": target_index,
                            "total": max_targets,
                            "target": target,
                        }
                    )
                result = scrape_fetch_target(
                    target,
                    force=force,
                    max_pages=max_pages,
                    abort_check=abort_check,
                    on_progress=on_progress,
                )
                totals["targets_done"] += 1
                totals["pages_written"] += result["pages_written"]
                totals["pages_skipped"] += result["pages_skipped"]
                totals["failures"] += result["failures"]
                if result.get("aborted"):
                    totals["aborted"] = True
                    if on_progress is not None:
                        on_progress({"kind": "run_done", "totals": totals})
                    return totals

            # Early-cutoff: every output Category for this (event, season)
            # must have a confirmed-empty page_1.json before we discontinue.
            output_cats = [c for t in targets for c in output_categories_for(t)]
            all_confirmed_empty = bool(output_cats)
            for cat in output_cats:
                payload = read_page1_payload(cat)
                if payload is None or not payload.get("empty", False):
                    all_confirmed_empty = False
                    break

            if all_confirmed_empty:
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


def _apply_modifiers(
    entries: list[dict], modifiers: Optional[RankingModifiers]
) -> list[dict]:
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
        out = [
            e for e in out if required.issubset(nes.get(e.get("name", ""), frozenset()))
        ]
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

    ``gender`` and ``weight_class`` are accepted for API symmetry, but
    ``load_event_slices`` already filters to a single (gender, weight) at the
    slice-file level — every entry passed in is guaranteed to match those, so
    only the ``age`` window needs to be re-checked here.
    """
    lo, hi = target_age - k, target_age + k
    out = [e for e in entries if lo <= e["age"] <= hi]
    return _apply_modifiers(out, modifiers)


def age_group_matched_rankings(
    entries: list[dict],
    *,
    age_band: str,
    gender: str,
    weight_class: Optional[str],
    modifiers: Optional[RankingModifiers] = None,
) -> list[dict]:
    """Age-group pool: rows whose cached ``age_band`` matches exactly.

    ``load_event_slices`` is called with ``k=0`` for this focus, so the input
    entries are already restricted to a single (gender, weight, age_band).
    The kwargs are kept for API symmetry but require no per-entry filtering.
    """
    return _apply_modifiers(entries, modifiers)


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
        better = sum(1 for e in pool if e["value_tenths"] < user_value_tenths)
    else:
        better = sum(1 for e in pool if e["value_tenths"] > user_value_tenths)
    rank = better + 1
    pct = 100.0 * (total - rank + 1) / total
    return rank, total, pct


def entry_watts(entry: dict, event_kind: str, event_value: int) -> Optional[float]:
    """Convert one ranking entry's ``value_tenths`` to watts.

    Returns ``None`` if the entry has no usable value or watts cannot be
    computed. Distance events: ``value_tenths`` is tenths-of-a-second for
    ``event_value`` metres. Time events: ``value_tenths`` is metres rowed in
    ``event_value`` tenths-of-a-second.
    """

    v = entry.get("value_tenths")
    if v is None or v <= 0:
        return None
    if event_kind == "dist":
        t_sec = v / 10.0
        dist_m = event_value
    else:
        t_sec = event_value / 10.0
        dist_m = v
    if dist_m <= 0 or t_sec <= 0:
        return None
    pace = t_sec / (dist_m / 500.0)
    w = compute_watts(pace)
    if w is None:
        return None
    try:
        if not (w > 0):
            return None
    except TypeError:
        return None
    return float(w)


def histogram_watts(
    pool: list[dict], event_kind: str, event_value: int, bins: int = 30
) -> tuple[list[int], float, float]:
    """Bin ``pool`` values as watts. Returns (counts, min_watts, max_watts).

    Reads each entry's precomputed ``_watts`` field (populated at slice-cache
    load time). For pools that bypass that path (e.g. tests passing raw
    dicts), falls back to ``entry_watts``. Returns zeros + (0,0) on empty.
    """
    watts_list: list[float] = []
    for e in pool:
        w = e.get("_watts")
        if w is None and "_watts" not in e:
            w = entry_watts(e, event_kind, event_value)
        if w is None:
            continue
        watts_list.append(w)

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
