"""
Concept2 world-record lookup service.

Fetches world records from the public Concept2 JSON API:
  GET https://log.concept2.com/api/records/rower/world

Returns all 2700+ records in one unauthenticated call.  We cache the raw
payload for 7 days and filter locally by gender, age category, and weight
class to produce a dict of {(etype, evalue): float} matching our ranked
event definitions.

Public API
----------
  get_age_group_records(gender, age, weight_kg)
      → {("dist", 2000): 347.8, ("time", 18000): 9207, ...}
        distance events  → seconds (float)
        time events      → meters (float)

  records_to_cp_input(records)
      → [{"duration_s": float, "watts": float}, ...]
        suitable for fit_critical_power()

  records_to_lbest(records)
      → (lb, lba) dicts compatible with loglog_fit(), _loglog_dataset(), etc.
        lb  {(etype, evalue): pace_sec_per_500m}
        lba {(etype, evalue): anchor_distance_meters}

  fetch_wr_data(gender_api, age, weight_kg)
      → dict{"records", "cp_params", "lb", "lba", "rl_predictions"} or None
        Blocking: fetch records, fit CP, optionally fetch RL predictions.
        Intended to run inside hd.task().
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from pathlib import Path

from services.rowing_utils import (
    RANKED_DISTANCES,
    RANKED_TIMES,
    compute_watts,
    age_from_dob,
    profile_complete,
)
from services.critical_power_model import fit_critical_power
from services.rowinglevel import fetch_predictions as rl_fetch_predictions

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_URL = "https://log.concept2.com/api/records/rower/world"
_CACHE_PATH = Path(".concept2_records_cache.json")
_CACHE_TTL = 7 * 24 * 3600  # 7 days in seconds

# Lightweight thresholds (kg).  If at or under, weight class is "Lwt".
_LWT_M_KG = 75.0
_LWT_F_KG = 61.5

# Concept2 API age_category string lookup, keyed by lower bound of each band.
# Built once at import time.
_AGE_BANDS: list[tuple[int, str]] = [
    (100, "100"),
    (95, "95-99"),
    (90, "90-94"),
    (85, "85-89"),
    (80, "80-84"),
    (75, "75-79"),
    (70, "70-74"),
    (65, "65-69"),
    (60, "60-64"),
    (55, "55-59"),
    (50, "50-54"),
    (40, "40-49"),
    (30, "30-39"),
    (19, "19-29"),
    (17, "17-18"),
    (15, "15-16"),
    (13, "13-14"),
    (0, "12 and Under"),
]

# Distance event: value = meters, matches RANKED_DISTANCES[*][0]
_RANKED_DIST_SET = {d for d, _ in RANKED_DISTANCES}
# Time event: value = tenths of a second.  Concept2 API uses minutes.
# minutes = tenths // 600
_RANKED_TIME_BY_MINUTES: dict[int, int] = {t // 600: t for t, _ in RANKED_TIMES}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def age_category(age: int) -> str:
    """Map integer age → Concept2 API age_category string."""
    for lower, label in _AGE_BANDS:
        if age >= lower:
            return label

    return "12 and Under"


def weight_class_str(weight_kg: float, gender: str, age: int) -> str | None:
    """
    Return 'Lwt', 'Hwt', or None (youth categories < 17 have no weight class).
    gender: 'M' or 'F' (as stored by the Concept2 API).
    """
    if age < 17:
        return None
    threshold = _LWT_M_KG if gender == "M" else _LWT_F_KG
    return "Lwt" if weight_kg <= threshold else "Hwt"


def _parse_result(result_str: str, event_type: str) -> float | None:
    """
    Parse a Concept2 API result string to a float.

    Distance events: time string → seconds
      "5:47.8"      → 347.8
      "0:12.5"      → 12.5
      "1:11:04.2"   → 4264.2
    Time events: distance string → meters (float)
      "423"         → 423.0
      "1,387"       → 1387.0
      "17,994"      → 17994.0
    """
    try:
        result_str = str(result_str).strip()
        if event_type == "time":
            return float(result_str.replace(",", ""))
        # distance event — parse M:SS.T or H:MM:SS.T
        parts = result_str.split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60.0 + seconds
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600.0 + minutes * 60.0 + seconds
    except (ValueError, IndexError):
        pass
    return None


def _ranked_event_for(event: int, event_type: str) -> tuple | None:
    """
    Map API event/event_type → (etype, evalue) as used in RANKED_DISTANCES /
    RANKED_TIMES, or None if the event is not in our ranked set.
    """
    if event_type == "distance":
        return ("dist", event) if event in _RANKED_DIST_SET else None
    if event_type == "time":
        tenths = _RANKED_TIME_BY_MINUTES.get(event)
        return ("time", tenths) if tenths is not None else None
    return None


def wr_category_label(profile: dict) -> str | None:
    """Return the WR category label string for the given profile, or None if incomplete."""
    if not profile:
        return ""

    if not profile_complete(profile):
        return None

    gender_raw = profile.get("gender", "")
    gender_api = "M" if gender_raw == "Male" else "F"
    _age = age_from_dob(profile.get("dob", ""))
    _wt = profile.get("weight") or 0.0
    _wt_unit = profile.get("weight_unit", "kg")
    _wt_kg = _wt * 0.453592 if _wt_unit == "lbs" else float(_wt)

    _age_cat = age_category(_age)
    _wt_cls = weight_class_str(_wt_kg, gender_api, _age)
    if _age < 17:
        return f"{gender_api} {_age_cat}"
    return f"{gender_api} {_age_cat} {_wt_cls}"


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
    try:
        _CACHE_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_raw_records_from_api() -> list[dict]:
    """HTTP GET the Concept2 world records API.  Raises on failure."""
    req = urllib.request.Request(
        _API_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["data"]


def _filter_records(
    raw: list[dict], gender: str, age_cat: str, wt_class: str | None
) -> dict:
    """
    Filter the raw API payload and return {(etype, evalue): best_result} for
    RowErg world records matching the specified gender/age/weight.
    """
    best: dict[tuple, float] = {}
    for r in raw:
        if r.get("type") != "RowErg":
            continue
        if r.get("class") != "World":
            continue
        if r.get("adaptive") is not None:
            continue
        if r.get("gender") != gender:
            continue
        if r.get("age_category") != age_cat:
            continue
        r_wt = r.get("weight_class")
        if wt_class is not None and r_wt != wt_class:
            continue
        # For youth (no weight class), skip records that have a weight class
        if wt_class is None and r_wt is not None:
            continue

        key = _ranked_event_for(r.get("event"), r.get("event_type", ""))
        if key is None:
            continue
        parsed = _parse_result(r.get("result", ""), r.get("event_type", ""))
        if parsed is None:
            continue

        etype = key[0]
        if key not in best:
            best[key] = parsed
        else:
            # For distance events: lower time = better
            # For time events: higher meters = better
            if etype == "dist" and parsed < best[key]:
                best[key] = parsed
            elif etype == "time" and parsed > best[key]:
                best[key] = parsed

    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_age_group_records(gender: str, age: int, weight_kg: float) -> dict:
    """
    Fetch (or retrieve from 7-day cache) Concept2 RowErg world records for
    the given age group and weight class.

    Parameters
    ----------
    gender    : 'M' or 'F'  (Concept2 API format)
    age       : integer years
    weight_kg : body weight in kilograms

    Returns
    -------
    dict keyed by ranked-event tuple, e.g.:
        {("dist", 2000): 347.8,   # seconds
         ("time", 18000): 9207.0} # meters
    Empty dict if the API is unreachable and no cache is available.
    """
    age_cat = age_category(age)

    wt_class = weight_class_str(weight_kg, gender, age)
    filter_key = f"{gender}|{age_cat}|{wt_class}"

    cache = _load_cache()
    now = time.time()

    # Return filtered records from cache if still fresh.
    if filter_key in cache:
        entry = cache[filter_key]
        if now - entry.get("_ts", 0) < _CACHE_TTL:
            return {
                tuple(k.split("|")[:1] + [int(k.split("|")[1])]): v
                for k, v in entry.get("records", {}).items()
            }

    # Load raw payload — reuse if cached and fresh, otherwise re-fetch.
    raw_entry = cache.get("_raw", {})
    if now - raw_entry.get("_ts", 0) < _CACHE_TTL and "data" in raw_entry:
        raw = raw_entry["data"]
    else:
        try:
            raw = _fetch_raw_records_from_api()
            cache["_raw"] = {"_ts": now, "data": raw}
        except Exception:
            # API unavailable: return whatever filtered records we have (may be stale).
            if filter_key in cache:
                entry = cache[filter_key]
                return {
                    tuple(k.split("|")[:1] + [int(k.split("|")[1])]): v
                    for k, v in entry.get("records", {}).items()
                }
            return {}

    # Filter and cache.
    filtered = _filter_records(raw, gender, age_cat, wt_class)
    # Serialize keys as "dist|2000" strings for JSON.
    cache[filter_key] = {
        "_ts": now,
        "records": {f"{etype}|{evalue}": v for (etype, evalue), v in filtered.items()},
    }
    _save_cache(cache)
    return filtered


def records_to_cp_input(records: dict) -> list[dict]:
    """
    Convert a records dict (from get_age_group_records) to a list of
    {duration_s, watts} dicts suitable for fit_critical_power().

    Excludes entries that produce non-finite or non-positive watts.
    Returns the list sorted by duration_s ascending.
    """
    result = []
    for (etype, evalue), value in records.items():
        if etype == "dist":
            # value = time in seconds for this distance
            dist_m = evalue
            t_sec = value
            if t_sec <= 0:
                continue
            pace = t_sec / (dist_m / 500.0)  # sec/500m
            duration_s = t_sec
        elif etype == "time":
            # value = meters covered in this duration
            tenths = evalue
            duration_s = tenths / 10.0
            dist_m = value
            if duration_s <= 0 or dist_m <= 0:
                continue
            pace = duration_s / (dist_m / 500.0)  # sec/500m
        else:
            continue

        watts = compute_watts(pace)
        if not math.isfinite(watts) or watts <= 0:
            continue
        result.append({"duration_s": duration_s, "watts": watts})

    result.sort(key=lambda x: x["duration_s"])
    return result


def records_to_lbest(records: dict) -> tuple[dict, dict]:
    """
    Convert a records dict (from get_age_group_records) to
    (lifetime_best, lifetime_best_anchor) format, compatible with
    loglog_fit(), _loglog_dataset(), _pauls_law_datasets(), and
    _average_datasets() in the chart builder.

    lb  keys: same (etype, evalue) tuples as *records*
         values: pace in sec/500m

    lba keys: same tuples
         values: anchor distance in meters (the canonical event distance for
                 distance events, or the meters-covered for time events)
    """
    lb: dict = {}
    lba: dict = {}
    for (etype, evalue), value in records.items():
        if etype == "dist":
            dist_m = evalue
            t_sec = value  # value = seconds for this distance
            if t_sec <= 0 or dist_m <= 0:
                continue
            pace = t_sec / (dist_m / 500.0)
            lb[(etype, evalue)] = pace
            lba[(etype, evalue)] = dist_m
        elif etype == "time":
            tenths = evalue
            dist_m = value  # value = meters covered in this duration
            duration_s = tenths / 10.0
            if duration_s <= 0 or dist_m <= 0:
                continue
            pace = duration_s / (dist_m / 500.0)
            lb[(etype, evalue)] = pace
            lba[(etype, evalue)] = dist_m
    return lb, lba


# ---------------------------------------------------------------------------
# Composite world-class fetch (records + CP fit + RL predictions)
# ---------------------------------------------------------------------------


def fetch_wr_data(gender_api: str, age: int, weight_kg: float) -> dict | None:
    """
    Blocking function — intended to run inside hd.task().
    Fetches Concept2 world records for the given gender/age/weight,
    fits the CP model (when enough data), builds lb/lba dicts, and
    optionally fetches RowingLevel predictions using the WC 2k record
    as the reference performance.

    Returns a dict {"records", "cp_params", "lb", "lba", "rl_predictions"}
    or None if the API returned no records at all.
    """
    records = get_age_group_records(gender_api, age, weight_kg)
    if not records:
        return None
    cp_input = records_to_cp_input(records)
    cp_params = fit_critical_power(cp_input) if len(cp_input) >= 5 else None
    lb, lba = records_to_lbest(records)

    # RowingLevel predictions: use WC record at best available dist event as
    # the reference performance (prefer 2k, the canonical RL anchor).
    rl_predictions: dict = {}
    gender_rl = "Male" if gender_api == "M" else "Female"
    _ref_dist, _ref_time_s = None, None
    for _d in [2000, 1000, 5000, 6000, 10000, 500, 21097]:
        _t = records.get(("dist", _d))
        if _t:
            _ref_dist, _ref_time_s = _d, _t
            break
    if _ref_dist is not None and _ref_time_s is not None:
        time_tenths = round(_ref_time_s * 10)
        preds = rl_fetch_predictions(gender_rl, age, weight_kg, _ref_dist, time_tenths)
        if preds:
            rl_predictions[str(("dist", _ref_dist))] = preds

    return {
        "records": records,
        "cp_params": cp_params,
        "lb": lb,
        "lba": lba,
        "rl_predictions": rl_predictions,
    }
