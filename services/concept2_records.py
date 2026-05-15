"""
Concept2 world-record lookup service — per machine.

Fetches world records from the public Concept2 JSON API:
  GET https://log.concept2.com/api/records/{machine}/world      (machine ∈ {rower, skierg})

Bikeerg has no Concept2 world records; ``wr_machine_supported(machine)`` is
the public predicate callers should consult before invoking the fetch path.

Returns all records for one machine in one unauthenticated call.  We cache
the raw payload (under ``_raw_{machine}``) for 7 days and filter locally by
gender, age category, and weight class to produce a dict of
{(etype, evalue): float} matching our ranked event definitions.  Only the
machine's canonical record type is retained (``RowErg`` for rower, ``SkiErg``
for skierg) — the rower endpoint also publishes Dynamic / Slides records,
which we intentionally ignore.

Public API
----------
  WR_AVAILABLE_MACHINES, wr_machine_supported(machine)

  get_age_group_records(gender, age, weight_kg, machine="rower")
      → {("dist", 2000): {"result": 347.8, "name": "...",
                          "date": "2024-03-10",
                          "age_category": "30-39",
                          "weight_class": "Hwt" | None,
                          "gender": "M" | "F"}, ...}
        result is seconds (dist events) or meters (time events).

  records_to_pd_input(records)
      → [{"duration_s": float, "watts": float}, ...]
        suitable for fit_power_duration()

  records_to_lbest(records)
      → (lb, lba) dicts compatible with loglog_fit(), _loglog_dataset(), etc.
        lb  {(etype, evalue): pace_sec_per_500m}
        lba {(etype, evalue): anchor_distance_meters}

  fetch_wr_data(gender_api, age, weight_kg, machine="rower")
      → dict{"records", "pd_params", "lb", "lba", "rl_predictions"} or None
        Blocking: fetch records, fit CP, optionally fetch RL predictions
        (rower only).  Intended to run inside hd.task().
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from pathlib import Path

from services.rowing_utils import (
    ranked_distances,
    ranked_times,
    compute_watts,
    age_from_dob,
    profile_complete,
)
from services.power_duration_model import fit_power_duration
from services.rowinglevel import fetch_predictions as rl_fetch_predictions

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-machine WR endpoints. Bikeerg has no Concept2 world records.
_API_URL_BY_MACHINE: dict[str, str] = {
    "rower": "https://log.concept2.com/api/records/rower/world",
    "skierg": "https://log.concept2.com/api/records/skierg/world",
}

# Each machine has a single "type" string in the records payload that we treat
# as its WR. Other ``type`` values within an endpoint (e.g. Dynamic / Slides
# inside the rower payload) are intentionally ignored.
_RECORD_TYPE_BY_MACHINE: dict[str, str] = {
    "rower": "RowErg",
    "skierg": "SkiErg",
}

# Public contract: machines for which Concept2 publishes WR data.
WR_AVAILABLE_MACHINES: frozenset[str] = frozenset(_API_URL_BY_MACHINE)


def wr_machine_supported(machine: str) -> bool:
    """True iff ``machine`` has Concept2 world records available."""
    return machine in WR_AVAILABLE_MACHINES


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


def _ranked_dist_set_for(machine: str) -> set[int]:
    """Distance event values (meters) that Concept2 publishes WRs for."""
    return {d for d, _ in ranked_distances(machine)}


def _ranked_time_by_minutes(machine: str) -> dict[int, int]:
    """Map of API minutes → tenths-of-second event value, for time events.

    Concept2 publishes time-event distances under integer minutes; our internal
    representation uses tenths.  ``minutes = tenths // 600``.
    """
    return {t // 600: t for t, _ in ranked_times(machine)}


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


def _ranked_event_for(event: int, event_type: str, machine: str) -> tuple | None:
    """
    Map API event/event_type → (etype, evalue) as used in the per-machine
    ranked event tables, or None if the event is not in our ranked set.
    """
    if event_type == "distance":
        return ("dist", event) if event in _ranked_dist_set_for(machine) else None
    if event_type == "time":
        tenths = _ranked_time_by_minutes(machine).get(event)
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


def _fetch_raw_records_from_api(machine: str) -> list[dict]:
    """HTTP GET the Concept2 world records API for ``machine``.  Raises on failure.

    Caller must check ``wr_machine_supported(machine)`` first.
    """
    url = _API_URL_BY_MACHINE[machine]
    req = urllib.request.Request(
        url,
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
    raw: list[dict],
    gender: str,
    age_cat: str,
    wt_class: str | None,
    machine: str,
) -> dict:
    """
    Filter the raw API payload and return {(etype, evalue): metadata_dict} for
    world records matching the specified gender/age/weight on ``machine``.

    metadata_dict keys: result (float), name, date, age_category, weight_class,
    gender.  ``result`` is seconds for dist events, meters for time events.
    """
    record_type = _RECORD_TYPE_BY_MACHINE[machine]
    best: dict[tuple, dict] = {}
    for r in raw:
        if r.get("type") != record_type:
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

        key = _ranked_event_for(r.get("event"), r.get("event_type", ""), machine)
        if key is None:
            continue
        parsed = _parse_result(r.get("result", ""), r.get("event_type", ""))
        if parsed is None:
            continue

        entry = {
            "result": parsed,
            "name": r.get("name") or "",
            "date": r.get("date") or "",
            "age_category": r.get("age_category") or "",
            "weight_class": r.get("weight_class"),
            "gender": r.get("gender") or "",
        }

        etype = key[0]
        if key not in best:
            best[key] = entry
        else:
            # For distance events: lower time = better
            # For time events: higher meters = better
            cur = best[key]["result"]
            if etype == "dist" and parsed < cur:
                best[key] = entry
            elif etype == "time" and parsed > cur:
                best[key] = entry

    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_age_group_records(
    gender: str, age: int, weight_kg: float, machine: str = "rower"
) -> dict:
    """
    Fetch (or retrieve from 7-day cache) Concept2 world records for ``machine``
    in the given age group and weight class.

    Parameters
    ----------
    gender    : 'M' or 'F'  (Concept2 API format)
    age       : integer years
    weight_kg : body weight in kilograms
    machine   : "rower" or "skierg"; bikeerg is not supported (no Concept2 WRs)

    Returns
    -------
    dict keyed by ranked-event tuple, e.g.:
        {("dist", 2000): {"result": 347.8, "name": "...", "date": "...", ...},
         ("time", 18000): {"result": 9207.0, ...}}
    Empty dict if the API is unreachable and no cache is available, or if
    ``machine`` has no WRs available.
    """
    if not wr_machine_supported(machine):
        return {}

    age_cat = age_category(age)

    wt_class = weight_class_str(weight_kg, gender, age)
    filter_key = f"{machine}|{gender}|{age_cat}|{wt_class}"
    raw_key = f"_raw_{machine}"

    cache = _load_cache()
    now = time.time()

    # Return filtered records from cache if still fresh.
    if filter_key in cache:
        entry = cache[filter_key]
        if now - entry.get("_ts", 0) < _CACHE_TTL:
            return _deserialize_records(entry.get("records", {}))

    # Load raw payload — reuse if cached and fresh, otherwise re-fetch.
    raw_entry = cache.get(raw_key, {})
    if now - raw_entry.get("_ts", 0) < _CACHE_TTL and "data" in raw_entry:
        raw = raw_entry["data"]
    else:
        try:
            raw = _fetch_raw_records_from_api(machine)
            cache[raw_key] = {"_ts": now, "data": raw}
        except Exception:
            # API unavailable: return whatever filtered records we have (may be stale).
            if filter_key in cache:
                entry = cache[filter_key]
                return _deserialize_records(entry.get("records", {}))
            return {}

    # Filter and cache.
    filtered = _filter_records(raw, gender, age_cat, wt_class, machine)
    # Serialize keys as "dist|2000" strings for JSON.
    cache[filter_key] = {
        "_ts": now,
        "records": {f"{etype}|{evalue}": v for (etype, evalue), v in filtered.items()},
    }
    _save_cache(cache)
    return filtered


def _deserialize_records(raw: dict) -> dict:
    """Deserialize cached records."""
    out: dict = {}
    for k, v in raw.items():
        parts = k.split("|")
        if len(parts) != 2:
            continue
        key = (parts[0], int(parts[1]))
        out[key] = v
    return out


def wr_holder_names(raw: list[dict] | None, machine: str) -> set[str]:
    """Return casefolded names of every athlete who holds a Concept2 World
    record on ``machine`` (any event / age band / weight class).

    Used by the leaderboard index as a whitelist for the integrity cap: an
    athlete with a real WR on file is allowed to keep their other rankings
    entries even when the entry's watts exceed the slice WR by more than the
    integrity threshold (the published WR sometimes lags the actual best
    pace, and known top athletes are unlikely to be cheating their own
    rankings rows).
    """
    if raw is None or not wr_machine_supported(machine):
        return set()
    record_type = _RECORD_TYPE_BY_MACHINE[machine]
    names: set[str] = set()
    for r in raw:
        if r.get("type") != record_type:
            continue
        if r.get("class") != "World":
            continue
        if r.get("adaptive") is not None:
            continue
        name = (r.get("name") or "").strip().casefold()
        if name:
            names.add(name)
    return names


def ensure_raw_records_cached(machine: str) -> list[dict] | None:
    """Return the cached raw WR payload for ``machine``, fetching it if
    missing or stale.  Returns ``None`` when the machine has no WRs available
    or the API is unreachable and no cached payload exists.

    Used by the leaderboard-index rebuild to seed the WR cap before walking
    the rankings cache.
    """
    if not wr_machine_supported(machine):
        return None
    raw_key = f"_raw_{machine}"
    cache = _load_cache()
    now = time.time()
    raw_entry = cache.get(raw_key, {})
    if now - raw_entry.get("_ts", 0) < _CACHE_TTL and "data" in raw_entry:
        return raw_entry["data"]
    try:
        raw = _fetch_raw_records_from_api(machine)
    except Exception:
        return raw_entry.get("data")  # may be stale; better than nothing
    cache[raw_key] = {"_ts": now, "data": raw}
    _save_cache(cache)
    return raw


def _ranking_age_band_to_wr_age_cats(age_band: str) -> list[str]:
    """Map a rankings ``age_band`` (e.g. ``"30-39"``, ``"0-12"``, ``"13-18"``)
    to the WR API's ``age_category`` strings.

    Rankings collapses youth into ``0-12`` and ``13-18`` while WR splits youth
    into ``"12 and Under"`` / ``13-14`` / ``15-16`` / ``17-18``.  Adult bands
    (``19-29`` and up) match 1:1.
    """
    if age_band == "0-12":
        return ["12 and Under"]
    if age_band == "13-18":
        return ["13-14", "15-16", "17-18"]
    return [age_band]


def _result_to_watts(event_kind: str, event_value: int, result: float) -> float | None:
    """Convert a WR ``result`` (seconds for dist events, metres for time
    events) to watts via ``compute_watts``.  Returns ``None`` for non-finite
    or non-positive values.
    """
    if result is None or result <= 0:
        return None
    if event_kind == "dist":
        t_sec = float(result)
        dist_m = float(event_value)
    else:
        t_sec = event_value / 10.0
        dist_m = float(result)
    if t_sec <= 0 or dist_m <= 0:
        return None
    pace = t_sec / (dist_m / 500.0)
    w = compute_watts(pace)
    if w is None or not math.isfinite(w) or w <= 0:
        return None
    return float(w)


def wr_watts_for_slice(
    raw: list[dict] | None,
    machine: str,
    event_kind: str,
    event_value: int,
    gender: str,
    age_band: str,
    weight_sentinel: str,
) -> float | None:
    """Return the WR watts cap for a leaderboard slice, or ``None`` when no
    matching WR exists.

    ``weight_sentinel`` follows the index convention: ``"H"`` (heavyweight),
    ``"L"`` (lightweight), or ``"X"`` (youth — no weight split).  For youth
    rankings bands that span multiple WR sub-bands, the loosest (highest watts)
    WR across those sub-bands is used so we don't filter legitimate entries.
    """
    if raw is None or not wr_machine_supported(machine):
        return None
    wr_wt: str | None
    if weight_sentinel == "H":
        wr_wt = "Hwt"
    elif weight_sentinel == "L":
        wr_wt = "Lwt"
    else:
        wr_wt = None  # youth → no weight class
    best_w: float | None = None
    for age_cat in _ranking_age_band_to_wr_age_cats(age_band):
        recs = _filter_records(raw, gender, age_cat, wr_wt, machine)
        rec = recs.get((event_kind, event_value))
        if not rec:
            continue
        w = _result_to_watts(event_kind, event_value, rec.get("result"))
        if w is None:
            continue
        if best_w is None or w > best_w:
            best_w = w
    return best_w


def get_records_for_age(
    gender: str, age: int, weight_kg: float, machine: str = "rower"
) -> dict:
    """
    Return {(etype, evalue): value} for the WR age_category that contains
    ``age`` — like ``get_age_group_records`` but optimised for callers that
    iterate over many ages and would otherwise re-filter the cached raw
    payload repeatedly.

    Reuses the in-memory cached ``_raw_{machine}`` payload without re-fetching.
    Falls back to ``get_age_group_records`` if the raw payload is missing or
    stale (so the first call still triggers one network round-trip).
    """
    if not wr_machine_supported(machine):
        return {}

    age_cat = age_category(age)
    wt_class = weight_class_str(weight_kg, gender, age)
    raw_key = f"_raw_{machine}"

    cache = _load_cache()
    now = time.time()
    raw_entry = cache.get(raw_key, {})
    raw = raw_entry.get("data") if now - raw_entry.get("_ts", 0) < _CACHE_TTL else None
    if raw is None:
        # Trigger a normal fetch + cache via get_age_group_records, then re-read.
        get_age_group_records(gender, age, weight_kg, machine)
        cache = _load_cache()
        raw_entry = cache.get(raw_key, {})
        raw = raw_entry.get("data")
        if not raw:
            return {}
    return _filter_records(raw, gender, age_cat, wt_class, machine)


def records_to_pd_input(records: dict) -> list[dict]:
    """
    Convert a records dict (from get_age_group_records) to a list of
    {duration_s, watts} dicts suitable for fit_power_duration().

    Excludes entries that produce non-finite or non-positive watts.
    Returns the list sorted by duration_s ascending.
    """
    result = []
    for (etype, evalue), rec in records.items():
        value = rec["result"] if isinstance(rec, dict) else rec
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
    for (etype, evalue), rec in records.items():
        value = rec["result"] if isinstance(rec, dict) else rec
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


def fetch_wr_data(
    gender_api: str, age: int, weight_kg: float, machine: str = "rower"
) -> dict | None:
    """
    Blocking function — intended to run inside hd.task().
    Fetches Concept2 world records for the given gender/age/weight on
    ``machine``, fits the CP model (when enough data), builds lb/lba dicts,
    and (rower only) fetches RowingLevel predictions using the WC 2k record
    as the reference performance.

    Returns a dict {"records", "pd_params", "lb", "lba", "rl_predictions"}
    or None if the API returned no records at all (or ``machine`` has no WRs).
    """
    if not wr_machine_supported(machine):
        return None
    records = get_age_group_records(gender_api, age, weight_kg, machine)
    if not records:
        return None
    pd_input = records_to_pd_input(records)
    pd_params = fit_power_duration(pd_input) if len(pd_input) >= 5 else None
    lb, lba = records_to_lbest(records)

    # RowingLevel predictions: rower-only (RowingLevel.com is rower-only).
    rl_predictions: dict = {}
    if machine != "rower":
        return {
            "records": records,
            "pd_params": pd_params,
            "lb": lb,
            "lba": lba,
            "rl_predictions": rl_predictions,
        }

    # Use WC record at best available dist event as the reference performance
    # (prefer 2k, the canonical RL anchor).
    gender_rl = "Male" if gender_api == "M" else "Female"
    _ref_dist, _ref_time_s = None, None
    for _d in [2000, 1000, 5000, 6000, 10000, 500, 21097]:
        _rec = records.get(("dist", _d))
        if _rec:
            _ref_dist = _d
            _ref_time_s = _rec["result"] if isinstance(_rec, dict) else _rec
            break
    if _ref_dist is not None and _ref_time_s is not None:
        time_tenths = round(_ref_time_s * 10)
        preds = rl_fetch_predictions(gender_rl, age, weight_kg, _ref_dist, time_tenths)
        if preds:
            rl_predictions[str(("dist", _ref_dist))] = preds

    return {
        "records": records,
        "pd_params": pd_params,
        "lb": lb,
        "lba": lba,
        "rl_predictions": rl_predictions,
    }
