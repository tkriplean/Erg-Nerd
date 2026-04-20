"""
Scraper for rowinglevel.com 500m-split predictions.

For a given user profile (gender, age, weight) and a known ranked performance
(distance + time), uses a headless browser (Playwright) to fill the form on
rowinglevel.com and return a dict of { dist_m: pace_sec_per_500m } for all
distances the site predicts.

Results are cached in .rowinglevel_cache.json so the site is only hit once per
unique (profile × performance) combination.  A minimum interval of 3 s is
enforced between outbound requests.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from services.rowing_utils import (
    profile_complete,
    compute_pace,
    workout_cat_key,
    age_from_dob,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SITE_URL = "https://rowinglevel.com"
_CACHE_PATH = Path(".rowinglevel_cache.json")
_MIN_INTERVAL = 3.0  # seconds between requests

_last_request_at: float = 0.0


# Standard lightweight upper limits in kg (open/elite categories)
_LW_LIMIT_KG = {"Male": 72.5, "Female": 59.0}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(data: dict) -> None:
    _CACHE_PATH.write_text(json.dumps(data, indent=2))


def _cache_key(
    gender: str, age: int, weight_kg: float, dist_m: int, time_tenths: int
) -> str:
    return f"{gender.lower()}|{age}|{weight_kg:.1f}|{dist_m}|{time_tenths}"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def _rate_limit() -> None:
    global _last_request_at
    wait = _MIN_INTERVAL - (time.time() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.time()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Ordered so longer/more-specific patterns are checked first.
# Half marathon MUST come before marathon — "half marathon" contains "marathon"
# and would incorrectly match the 42195 pattern otherwise.
_DIST_PATTERNS: list[tuple[str, int]] = [
    (r"half[\s\-]?marathon|½\s*marathon|21[,.]?097", 21097),
    (r"marathon(?!\s*\d)", 42195),
    (r"10[,\s]?000\s*m\b|10\s*k\b", 10000),
    (r"6[,\s]?000\s*m\b|6\s*k\b", 6000),
    (r"5[,\s]?000\s*m\b|5\s*k\b", 5000),
    (r"3[,\s]?000\s*m\b|3\s*k\b", 3000),
    (r"2[,\s]?000\s*m\b|2\s*k\b", 2000),
    (r"1[,\s]?000\s*m\b|1\s*k\b", 1000),
    (r"\b500\s*m\b", 500),
    (r"\b100\s*m\b", 100),
]


def _parse_dist_label(text: str) -> Optional[int]:
    t = text.strip().lower()
    for pattern, meters in _DIST_PATTERNS:
        if re.search(pattern, t):
            return meters
    return None


def _pace_str_to_seconds(text: str) -> Optional[float]:
    """'M:SS' or 'M:SS.t' → total seconds (float)."""
    m = re.match(r"(\d+):(\d+(?:\.\d+)?)", text.strip())
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    return None


# ---------------------------------------------------------------------------
# Result parser (BeautifulSoup)
# ---------------------------------------------------------------------------


def _parse_500m_split_tab(soup: BeautifulSoup) -> Optional[dict]:
    """
    Locate the '500m Split' tab content inside 'Race Prediction' and
    return { dist_m: pace_sec_per_500m }.
    """
    tab_panel = None
    for el in soup.find_all(string=re.compile(r"500\s*m\s*split", re.I)):
        parent = el.find_parent()
        if parent is None:
            continue
        panel_id = parent.get("aria-controls") or (
            parent.get("href", "").lstrip("#") or None
        )
        if panel_id:
            tab_panel = soup.find(id=panel_id)
            if tab_panel:
                break
        for _ in range(6):
            if parent is None:
                break
            panels = parent.find_all(
                True,
                attrs={"role": re.compile(r"tabpanel", re.I)},
            )
            if panels:
                tab_panel = panels[0]
                break
            sibling_panel = parent.find_next_sibling(
                True, attrs={"role": re.compile(r"tabpanel", re.I)}
            )
            if sibling_panel:
                tab_panel = sibling_panel
                break
            parent = parent.parent
        if tab_panel:
            break

    search_root = tab_panel or soup

    results: dict = {}

    # Look for tables first
    for table in search_root.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                dist = _parse_dist_label(cells[0].get_text(strip=True))
                pace = _pace_str_to_seconds(cells[-1].get_text(strip=True))
                if dist and pace:
                    results[dist] = pace

    # Fallback: look for structured lists / definition lists
    if not results:
        for item in search_root.find_all(["li", "dt", "dd", "p", "span", "div"]):
            text = item.get_text(" ", strip=True)
            m = re.match(r"^(.{2,25}?)\s*[:\-–]\s*(\d+:\d+(?:\.\d+)?)", text)
            if m:
                dist = _parse_dist_label(m.group(1))
                pace = _pace_str_to_seconds(m.group(2))
                if dist and pace:
                    results[dist] = pace

    return results if results else None


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------


def _fill_field_by_label(page, label_text: str, value: str) -> bool:
    """Try to fill an input associated with a label containing label_text."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        page.get_by_label(re.compile(label_text, re.I)).first.fill(value, timeout=3000)
        return True
    except Exception:
        return False


def _select_radio_by_label_and_value(page, label_text: str, value: str) -> bool:
    """Click a radio button whose associated label matches label_text+value."""
    try:
        # Try: label contains the value text near the field group label
        radios = page.locator(f"input[type=radio]").all()
        for r in radios:
            try:
                v = r.get_attribute("value") or ""
                if value.lower() in v.lower() or v.lower() in value.lower():
                    # Check if this radio is near the expected section
                    r.check(timeout=2000)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def async_fetch_rowinglevel(state, profile: dict, chart_workouts: list) -> dict:
    """
    Launch (or resume) the background RowingLevel scrape.
    Only fires when at_today and profile_complete; otherwise returns {}.
    Uses a scope key derived from profile + PB hash so the task re-fires only
    when its inputs change.
    Returns rl_predictions (a dict — possibly empty).
    """

    import hyperdiv as hd

    if not profile_complete(profile):
        return {}

    weight_kg = (
        profile["weight"] * 0.453592
        if profile["weight_unit"] == "lbs"
        else profile["weight"]
    )
    lbest: dict = {}
    lbest_anchor: dict = {}
    lbest_dates: dict = {}
    for w in chart_workouts:
        p = compute_pace(w)
        c = workout_cat_key(w)
        d = w.get("distance")
        if p is None or c is None or not d:
            continue

        if c not in lbest or p < lbest[c]:
            lbest[c] = p
            lbest_anchor[c] = d
            lbest_dates[c] = w.get("date", "")

    lbest_hash = hashlib.md5(
        json.dumps(sorted((str(k), round(v, 2)) for k, v in lbest.items())).encode()
    ).hexdigest()[:8]

    key = (
        profile.get("gender", ""),
        age_from_dob(profile.get("dob", "")),
        profile.get("weight", 0.0),
        profile.get("weight_unit", "kg"),
    )
    key = hashlib.md5(json.dumps(key).encode()).hexdigest()[:10]

    scope_key = f"rl_{key}_{lbest_hash}"

    rl_predictions = {}
    with hd.scope(scope_key):
        rl_task = hd.task()

        def _do_scrape(gender, current_age, wkg, lb, lb_anchor, lb_dates):
            return fetch_all_pb_predictions(
                [], lb, lb_anchor, gender, current_age, wkg, lbest_dates=lb_dates
            )

        rl_task.run(
            _do_scrape,
            profile["gender"],
            age_from_dob(profile.get("dob", "")),
            weight_kg,
            lbest,
            lbest_anchor,
            lbest_dates,
        )
        if rl_task.done and rl_task.result:
            rl_predictions = rl_task.result

    return rl_predictions


def fetch_predictions(
    gender: str,
    age: int,
    weight_kg: float,
    dist_m: int,
    time_tenths: int,
) -> Optional[dict]:
    """
    Return { dist_m: pace_sec_per_500m } from rowinglevel.com, using cache
    when available.  Returns None on failure.

    Uses Playwright headless Chromium because the form uses action="#results"
    (a client-side JS anchor) — a plain HTTP POST returns the homepage.
    """
    cache = _load_cache()
    key = _cache_key(gender, age, weight_kg, dist_m, time_tenths)
    if key in cache:
        return cache[key].get("predictions")

    _rate_limit()

    total_sec = time_tenths / 10.0
    minutes = int(total_sec // 60)
    seconds = total_sec % 60
    gender_norm = gender.lower()  # "male" or "female"

    print(
        f"[rowinglevel] Fetching: dist={dist_m}m  time={minutes}:{seconds:04.1f}  "
        f"gender={gender}  age={age}  weight={weight_kg:.1f}kg"
    )

    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        print(
            "[rowinglevel] playwright not installed — run: pip install playwright && python -m playwright install chromium"
        )
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(_SITE_URL, wait_until="domcontentloaded", timeout=30000)

            # ----------------------------------------------------------------
            # Fill gender
            # ----------------------------------------------------------------
            # Try radio buttons first (value = "male"/"female" or "Male"/"Female")
            gender_filled = False
            for radio in page.locator("input[type=radio]").all():
                try:
                    val = (radio.get_attribute("value") or "").lower()
                    if val == gender_norm or (gender_norm in val and len(val) < 10):
                        radio.check(timeout=2000)
                        gender_filled = True
                        break
                except Exception:
                    continue

            if not gender_filled:
                # Try a select
                try:
                    sel = (
                        page.locator("select")
                        .filter(has_text=re.compile(r"male|female", re.I))
                        .first
                    )
                    sel.select_option(label=re.compile(gender, re.I), timeout=3000)
                    gender_filled = True
                except Exception:
                    pass

            # ----------------------------------------------------------------
            # Fill age
            # ----------------------------------------------------------------
            age_filled = False
            try:
                age_input = page.get_by_label(re.compile(r"\bage\b", re.I)).first
                age_input.fill(str(age), timeout=3000)
                age_filled = True
            except Exception:
                pass

            if not age_filled:
                # Fallback: input[name*=age], input[id*=age], input[placeholder*=age]
                for sel in [
                    "input[name*='age']",
                    "input[id*='age']",
                    "input[placeholder*='age' i]",
                ]:
                    try:
                        page.locator(sel).first.fill(str(age), timeout=2000)
                        age_filled = True
                        break
                    except Exception:
                        continue

            # ----------------------------------------------------------------
            # Fill weight (always submit in kg)
            # ----------------------------------------------------------------
            weight_filled = False

            # First try to set the unit to kg via radio
            for radio in page.locator("input[type=radio]").all():
                try:
                    val = (radio.get_attribute("value") or "").lower()
                    if val == "kg":
                        radio.check(timeout=2000)
                        break
                except Exception:
                    continue

            try:
                weight_input = page.get_by_label(
                    re.compile(r"weight|bodyweight|mass", re.I)
                ).first
                weight_input.fill(str(round(weight_kg, 1)), timeout=3000)
                weight_filled = True
            except Exception:
                pass

            if not weight_filled:
                for sel in [
                    "input[name*='weight' i]",
                    "input[id*='weight' i]",
                    "input[placeholder*='weight' i]",
                ]:
                    try:
                        page.locator(sel).first.fill(
                            str(round(weight_kg, 1)), timeout=2000
                        )
                        weight_filled = True
                        break
                    except Exception:
                        continue

            # ----------------------------------------------------------------
            # Fill distance
            # ----------------------------------------------------------------
            dist_filled = False
            # Try a select dropdown first (most likely)
            try:
                dist_select = page.locator(
                    "select[name*='dist' i], select[name*='event' i], select[name*='meter' i], select[id*='dist' i]"
                ).first
                # Find closest option value
                options = dist_select.locator("option").all()
                best_val, best_diff = None, float("inf")
                for opt in options:
                    try:
                        raw = re.sub(r"[^\d]", "", opt.get_attribute("value") or "")
                        if raw:
                            diff = abs(int(raw) - dist_m)
                            if diff < best_diff:
                                best_diff, best_val = diff, opt.get_attribute("value")
                    except Exception:
                        continue
                if best_val:
                    dist_select.select_option(value=best_val, timeout=3000)
                    dist_filled = True
            except Exception:
                pass

            if not dist_filled:
                try:
                    dist_input = page.get_by_label(
                        re.compile(r"dist|event|meter", re.I)
                    ).first
                    dist_input.fill(str(dist_m), timeout=3000)
                    dist_filled = True
                except Exception:
                    pass

            # ----------------------------------------------------------------
            # Fill time: minutes + seconds (separate fields, or combined)
            # ----------------------------------------------------------------
            time_filled = False
            try:
                min_input = page.get_by_label(re.compile(r"minute|^min$", re.I)).first
                min_input.fill(str(minutes), timeout=3000)
                sec_input = page.get_by_label(re.compile(r"second|^sec$", re.I)).first
                sec_input.fill(f"{seconds:.1f}", timeout=3000)
                time_filled = True
            except Exception:
                pass

            if not time_filled:
                # Try name-based selectors
                for min_sel in [
                    "input[name*='minute' i]",
                    "input[name='min']",
                    "input[id*='minute' i]",
                ]:
                    try:
                        page.locator(min_sel).first.fill(str(minutes), timeout=2000)
                        time_filled = True
                        break
                    except Exception:
                        continue
                for sec_sel in [
                    "input[name*='second' i]",
                    "input[name='sec']",
                    "input[id*='second' i]",
                ]:
                    try:
                        page.locator(sec_sel).first.fill(f"{seconds:.1f}", timeout=2000)
                        break
                    except Exception:
                        continue

            if not time_filled:
                # Try combined time field
                for sel in ["input[name*='time' i]", "input[id*='time' i]"]:
                    try:
                        page.locator(sel).first.fill(
                            f"{minutes}:{seconds:04.1f}", timeout=2000
                        )
                        time_filled = True
                        break
                    except Exception:
                        continue

            # ----------------------------------------------------------------
            # Submit form
            # ----------------------------------------------------------------
            submitted = False
            try:
                page.locator("input[type=submit], button[type=submit]").first.click(
                    timeout=5000
                )
                submitted = True
            except Exception:
                pass

            if not submitted:
                try:
                    page.locator("button").filter(
                        has_text=re.compile(r"calculat|predict|submit", re.I)
                    ).first.click(timeout=5000)
                    submitted = True
                except Exception:
                    pass

            # ----------------------------------------------------------------
            # Wait for results to render
            # ----------------------------------------------------------------
            # rowinglevel.com uses action="#results" — a pure client-side JS
            # anchor with no outbound network request.  "networkidle" resolves
            # immediately before JS has rendered anything.  "wait_for_selector
            # (#results)" never fires because the element pre-exists (hidden).
            # Instead, poll the DOM until a prediction table actually has rows.
            try:
                page.wait_for_function(
                    """() => {
                        const tables = document.querySelectorAll("table");
                        for (const t of tables) {
                            if (t.querySelectorAll("tr").length > 3) return true;
                        }
                        return false;
                    }""",
                    timeout=15000,
                )
            except PWTimeout:
                pass  # Best-effort — parse whatever is there

            # ----------------------------------------------------------------
            # Click the '500m Split' tab if present
            # ----------------------------------------------------------------
            try:
                split_tab = page.get_by_text(re.compile(r"500\s*m\s*split", re.I)).first
                split_tab.click(timeout=5000)
                # Wait for the tab panel to update
                page.wait_for_function(
                    """() => {
                        const tables = document.querySelectorAll("[role=tabpanel] table, [class*='tab'] table");
                        for (const t of tables) {
                            if (t.querySelectorAll("tr").length > 3) return true;
                        }
                        return false;
                    }""",
                    timeout=5000,
                )
            except Exception:
                pass  # No tab to click, or already visible

            # ----------------------------------------------------------------
            # Parse results from rendered HTML
            # ----------------------------------------------------------------
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")
        predictions = _parse_500m_split_tab(soup)

        if not predictions:
            print("[rowinglevel] Could not parse predictions from response.")
            _debug_dump_sections(soup)
            return None

        # Strip distances that are excluded as prediction points (e.g. 100m)
        predictions = {
            d: p for d, p in predictions.items() if d not in _RL_EXCLUDED_PRED_DISTS
        }

        cache[key] = {"predictions": predictions}
        _save_cache(cache)
        print(f"[rowinglevel] Got {len(predictions)} predictions for dist={dist_m}m")
        return predictions

    except Exception as e:
        print(f"[rowinglevel] Playwright scrape failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Categories excluded as RL prediction anchors (too short / unreliable).
_RL_EXCLUDED_CATS: set = {
    ("dist", 100),  # 100m
    ("time", 600),  # 1 min
    ("time", 2400),  # 4 min
}

# Distances (meters) to strip from every prediction dict before returning.
# 100m predictions are too noisy to be useful on the chart.
_RL_EXCLUDED_PRED_DISTS: set = {100}


def fetch_all_pb_predictions(
    workouts: list,
    lifetime_best: dict,
    lifetime_best_anchor: dict,
    gender: str,
    age: int,
    weight_kg: float,
    lbest_dates: dict | None = None,
) -> dict:
    """
    For each category in lifetime_best, fetch RowingLevel predictions.
    Returns { cat: {dist_m: pace_sec} }.  Rate-limited and cached.
    Skips categories in _RL_EXCLUDED_CATS (e.g. 100m, 1min, 4min).

    If lbest_dates is provided ({cat: ISO-date-string}), the age submitted to
    RowingLevel is adjusted to match how old the user was when that performance
    happened (assuming `age` is the user's current age).
    """
    from datetime import date as _date

    _today = _date.today()

    all_predictions: dict = {}
    for cat, pb_pace in lifetime_best.items():
        if cat in _RL_EXCLUDED_CATS:
            continue
        anchor_dist = lifetime_best_anchor.get(cat)
        if anchor_dist is None:
            continue

        # Adjust age to what it was when this performance happened
        perf_age = age
        if lbest_dates:
            date_str = lbest_dates.get(cat, "")
            if date_str:
                try:
                    perf_date = _date.fromisoformat(date_str[:10])
                    years_ago = (_today - perf_date).days // 365
                    perf_age = max(1, age - years_ago)
                except Exception:
                    pass

        # Back-calculate the PB time in tenths from pace and distance
        # pace = (time/10) / (dist/500)  →  time = pace * dist / 500 * 10
        time_tenths = round(pb_pace * anchor_dist / 500.0 * 10)
        preds = fetch_predictions(gender, perf_age, weight_kg, anchor_dist, time_tenths)
        if preds:
            all_predictions[str(cat)] = preds
    return all_predictions


# ---------------------------------------------------------------------------
# Debug helpers (printed server-side only)
# ---------------------------------------------------------------------------


def _debug_dump_sections(soup: BeautifulSoup) -> None:
    print("[rowinglevel] DEBUG — headings in response:")
    for h in soup.find_all(re.compile(r"h[1-6]")):
        print(f"  {h.name}: {h.get_text(strip=True)!r}")
