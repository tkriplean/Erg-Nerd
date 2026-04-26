"""
Per-(event × demographic) aggregated index over the ``.c2_rankings/``
scraper cache.

The scraper in ``services/concept2_rankings.py`` writes one JSON per fetched
page — a fine shape for resumable scraping but awkward to query when the Rank
Page wants a slice (one event, one gender, one age band, one weight class)
across all seasons at once.

This module builds one JSON file per slice under ``.c2_rankings_index/``,
derived purely from the scraper cache — no network I/O.

Layout:

    .c2_rankings_index/
      dist_2000_M_30-39_H.json
      dist_2000_M_30-39_L.json
      dist_2000_F_0-12_X.json
      time_18000_M_50-54_H.json
      ...

The trailing token is the weight class: ``H`` (heavyweight), ``L``
(lightweight), or ``X`` (youth — Concept2 does not split youth bands by
weight).

Schema of each file:

    {
      "schema_version": 2,
      "built_at": "<iso8601>",
      "event_kind": "dist" | "time",
      "event_value": int,
      "gender": "M" | "F",
      "age_band": str,
      "weight": "H" | "L" | "X",
      "entries": [
        { "season": int, "position": int, "name": str, "age": int,
          "country": str, "value_tenths": int, "verified": "Y"|"N"|"R" },
        ...
      ]
    }

Per-slice deduplication: during rebuild, each season's entries are passed
through a race-result dedup pass. For every entry with ``verified == "R"``,
any other entry in the same slice + season whose name matches
case-insensitively and whose watts are within 5% of the race entry is treated
as the same athlete; the cluster is collapsed to its best performance and
the rest are dropped. Each removal is printed to stdout.

Public API:

  * ``load_event_slices(event_kind, event_value, *, gender, target_age, k,
    weight)`` — concatenated entries for the demographic slice covering the
    age window ``[target_age - k, target_age + k]``. Rebuilds on demand.
  * ``iter_event_entries(event_kind, event_value)`` — streaming iterator
    over every entry of every slice for one event (used by cross-event
    name sweeps).
  * ``rebuild_event_index(event_kind, event_value)`` — force rebuild of
    every slice file for the event from ``.c2_rankings/``.
  * ``rebuild_all_indices()`` — rebuild every ranked event.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from services.concept2_rankings import (
    EVENT_IDS_DIST,
    EVENT_IDS_TIME,
    YOUTH_BANDS,
    Category,
    entry_watts,
    iter_all_categories,
    page_filename,
    rankings_age_band,
    read_page1_payload,
)
from services.rowing_utils import RANKED_DISTANCES, RANKED_TIMES

INDEX_DIR = Path(".c2_rankings_index")
SCHEMA_VERSION = 2
WEIGHT_NONE_SENTINEL = "X"
DEDUP_WATTS_TOLERANCE = 0.05


def _slice_path(
    event_kind: str,
    event_value: int,
    gender: str,
    age_band: str,
    weight_sentinel: str,
) -> Path:
    return (
        INDEX_DIR
        / f"{event_kind}_{event_value}_{gender}_{age_band}_{weight_sentinel}.json"
    )


def _legacy_path(event_kind: str, event_value: int) -> Path:
    """Path for the v1 single-file layout (one file per event)."""
    return INDEX_DIR / f"{event_kind}_{event_value}.json"


def _url_id_for(event_kind: str, event_value: int) -> int:
    if event_kind == "dist":
        return EVENT_IDS_DIST[event_value]
    return EVENT_IDS_TIME[event_value]


def _weight_sentinel(weight: Optional[str]) -> str:
    return weight if weight is not None else WEIGHT_NONE_SENTINEL


def _walk_category_pages(cat: Category) -> list[dict]:
    """Return all entries for one category. Slice metadata (gender/age_band/
    weight) is *not* attached — it lives at file level in the v2 layout.
    """
    entries: list[dict] = []
    page = 1
    while True:
        path = page_filename(cat, page)
        if not path.exists():
            break
        try:
            payload = json.loads(path.read_text())
        except Exception:
            break
        for e in payload.get("entries") or []:
            entries.append(
                {
                    "season": cat.season,
                    "position": e.get("position"),
                    "name": e.get("name", ""),
                    "age": e.get("age", 0),
                    "country": e.get("country", ""),
                    "value_tenths": e.get("value_tenths"),
                    "verified": e.get("verified", "?"),
                }
            )
        if payload.get("is_last_page", True):
            break
        page += 1
    return entries


def _is_better(event_kind: str, a: dict, b: dict) -> bool:
    """Return True if entry ``a`` is a better performance than ``b``."""
    av = a.get("value_tenths")
    bv = b.get("value_tenths")
    if av is None:
        return False
    if bv is None:
        return True
    if event_kind == "dist":
        return av < bv  # lower time = better
    return av > bv  # more metres = better


def _dedup_race_collisions(
    entries: list[dict],
    event_kind: str,
    event_value: int,
    slice_label: str,
) -> list[dict]:
    """Per-season race-result dedup. Returns a new list with duplicates removed.

    For every ``verified == "R"`` entry, find other entries in the same season
    with case-insensitive name match and watts within
    ``DEDUP_WATTS_TOLERANCE``; collapse the cluster to its best performance.
    Removals are printed to stdout.
    """
    by_season: dict[int, list[int]] = {}
    for i, e in enumerate(entries):
        by_season.setdefault(int(e.get("season") or 0), []).append(i)

    watts_cache: list[Optional[float]] = [
        entry_watts(e, event_kind, event_value) for e in entries
    ]
    removed: set[int] = set()

    for season, idxs in by_season.items():
        race_idxs = [i for i in idxs if entries[i].get("verified") == "R"]
        for ri in race_idxs:
            if ri in removed:
                continue
            r_watts = watts_cache[ri]
            if r_watts is None or r_watts <= 0:
                continue
            r_name = (entries[ri].get("name") or "").casefold()
            if not r_name:
                continue

            cluster = [ri]
            for i in idxs:
                if i == ri or i in removed:
                    continue
                if (entries[i].get("name") or "").casefold() != r_name:
                    continue
                w = watts_cache[i]
                if w is None:
                    continue
                if abs(w - r_watts) / r_watts > DEDUP_WATTS_TOLERANCE:
                    continue
                cluster.append(i)

            if len(cluster) <= 1:
                continue

            best = cluster[0]
            for i in cluster[1:]:
                if _is_better(event_kind, entries[i], entries[best]):
                    best = i

            for i in cluster:
                if i == best:
                    continue
                removed.add(i)
                dropped = entries[i]
                kept = entries[best]
                d_w = watts_cache[i]
                k_w = watts_cache[best]
                print(
                    f"[c2_rankings_index dedup] {event_kind}{event_value} "
                    f"season={season} {slice_label} drop "
                    f"name={dropped.get('name')!r} verified={dropped.get('verified')} "
                    f"value_tenths={dropped.get('value_tenths')} "
                    f"watts={d_w:.1f} -- kept "
                    f"name={kept.get('name')!r} verified={kept.get('verified')} "
                    f"value_tenths={kept.get('value_tenths')} "
                    f"watts={k_w:.1f}",
                    flush=True,
                )

    return [e for i, e in enumerate(entries) if i not in removed]


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)


def _delete_existing_slices(event_kind: str, event_value: int) -> None:
    """Remove all existing slice files for one event before a fresh write.

    Keeps the directory clean if a slice that previously existed is now empty.
    Also removes the v1 single-file legacy layout if present.
    """
    if not INDEX_DIR.exists():
        return
    pattern = f"{event_kind}_{event_value}_*.json"
    for p in INDEX_DIR.glob(pattern):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    legacy = _legacy_path(event_kind, event_value)
    if legacy.exists():
        try:
            legacy.unlink()
        except FileNotFoundError:
            pass


def rebuild_event_index(event_kind: str, event_value: int) -> None:
    """Walk the scraper cache and rewrite every slice file for the event."""
    url_id = _url_id_for(event_kind, event_value)

    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for cat in iter_all_categories(event_ids=[url_id]):
        if cat.event_kind != event_kind or cat.event_value != event_value:
            continue
        payload = read_page1_payload(cat)
        if payload is None:
            continue
        if payload.get("empty"):
            continue
        slice_key = (cat.gender, cat.age_band, _weight_sentinel(cat.weight))
        bucket = buckets.setdefault(slice_key, [])
        bucket.extend(_walk_category_pages(cat))

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _delete_existing_slices(event_kind, event_value)

    built_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    for (gender, age_band, weight_sentinel), entries in buckets.items():
        slice_label = f"{gender}/{age_band}/{weight_sentinel}"
        deduped = _dedup_race_collisions(
            entries, event_kind, event_value, slice_label
        )
        out = {
            "schema_version": SCHEMA_VERSION,
            "built_at": built_at,
            "event_kind": event_kind,
            "event_value": event_value,
            "gender": gender,
            "age_band": age_band,
            "weight": weight_sentinel,
            "entries": deduped,
        }
        _atomic_write_json(
            _slice_path(event_kind, event_value, gender, age_band, weight_sentinel),
            out,
        )


def _attach_slice_metadata(entries: list[dict], data: dict) -> list[dict]:
    """Re-add file-level (gender, age_band, weight) to each entry so downstream
    filters that read those keys (e.g. ``filter_matched_rankings``) keep
    working. ``weight`` is mapped from sentinel ``"X"`` back to ``None``."""
    gender = data.get("gender")
    age_band = data.get("age_band")
    weight_sentinel = data.get("weight")
    weight = None if weight_sentinel == WEIGHT_NONE_SENTINEL else weight_sentinel
    out: list[dict] = []
    for e in entries:
        e2 = dict(e)
        e2["gender"] = gender
        e2["age_band"] = age_band
        e2["weight"] = weight
        out.append(e2)
    return out


def _read_slice_file(path: Path) -> Optional[dict]:
    """Read one slice file. Returns None on missing / wrong schema / corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    return data


def _age_bands_for_window(target_age: int, k: int, *, youth: bool) -> list[str]:
    """Return canonical rankings age bands intersecting ``[target_age - k,
    target_age + k]``, restricted to youth-only or adult-only based on
    ``youth``."""
    lo = max(target_age - k, 0)
    hi = max(target_age + k, lo)
    bands: set[str] = set()
    for a in range(lo, hi + 1):
        bands.add(rankings_age_band(a))
    if youth:
        bands = {b for b in bands if b in YOUTH_BANDS}
    else:
        bands = {b for b in bands if b not in YOUTH_BANDS}
    return sorted(bands)


def load_event_slices(
    event_kind: str,
    event_value: int,
    *,
    gender: str,
    target_age: int,
    k: int = 0,
    weight: Optional[str],
    rebuild_if_missing: bool = True,
) -> list[dict]:
    """Load all entries for one event covering the demographic slice and the
    age window ``[target_age - k, target_age + k]``.

    ``weight`` follows the rankings convention: ``"H"``, ``"L"``, or ``None``
    for youth bands. When ``k > 0`` and the window crosses age-band
    boundaries, multiple slice files are concatenated.

    If any required slice file is missing (or has a stale schema) and
    ``rebuild_if_missing`` is true, the entire event is rebuilt and the read
    is retried once.
    """
    weight_sentinel = _weight_sentinel(weight)
    youth = weight_sentinel == WEIGHT_NONE_SENTINEL
    bands = _age_bands_for_window(target_age, k, youth=youth)

    out: list[dict] = []
    needs_rebuild = False
    for band in bands:
        path = _slice_path(
            event_kind, event_value, gender, band, weight_sentinel
        )
        data = _read_slice_file(path)
        if data is None:
            needs_rebuild = True
            break
        out.extend(_attach_slice_metadata(data.get("entries") or [], data))

    if needs_rebuild and rebuild_if_missing:
        rebuild_event_index(event_kind, event_value)
        return load_event_slices(
            event_kind,
            event_value,
            gender=gender,
            target_age=target_age,
            k=k,
            weight=weight,
            rebuild_if_missing=False,
        )
    return out


def iter_event_entries(
    event_kind: str, event_value: int, *, rebuild_if_missing: bool = True
) -> Iterator[dict]:
    """Yield every entry of every slice for ``(event_kind, event_value)``.

    Slice metadata (``gender``, ``age_band``, ``weight``) is attached to each
    yielded entry. If no slice files exist for the event, the index is
    rebuilt once before iteration."""
    pattern = f"{event_kind}_{event_value}_*.json"

    def _paths() -> list[Path]:
        if not INDEX_DIR.exists():
            return []
        return sorted(INDEX_DIR.glob(pattern))

    paths = _paths()
    if not paths and rebuild_if_missing:
        rebuild_event_index(event_kind, event_value)
        paths = _paths()

    for p in paths:
        data = _read_slice_file(p)
        if data is None:
            continue
        for e in _attach_slice_metadata(data.get("entries") or [], data):
            yield e


def rebuild_all_indices() -> None:
    """Rebuild every ranked event's slice files from the scraper cache."""
    for d, _ in RANKED_DISTANCES:
        rebuild_event_index("dist", d)
    for t, _ in RANKED_TIMES:
        rebuild_event_index("time", t)


if __name__ == "__main__":
    rebuild_all_indices()
