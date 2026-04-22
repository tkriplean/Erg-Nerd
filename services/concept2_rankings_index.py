"""
Per-event aggregated index over the ``.c2_rankings/`` scraper cache.

The scraper in ``services/concept2_rankings.py`` writes one JSON per fetched
page — a fine shape for resumable scraping but awkward to query when the Rank
Page wants every ranking entry for a single event (all seasons × ages ×
weights × genders) at once.

This module builds a per-event flat JSON under ``.c2_rankings_index/`` that
is derived purely from the scraper cache — no network I/O.

Layout:

    .c2_rankings_index/
      dist_2000.json
      dist_5000.json
      time_18000.json
      ...

Schema of each file:

    {
      "schema_version": 1,
      "built_at": "<iso8601>",
      "event_kind": "dist" | "time",
      "event_value": int,
      "entries": [
        {
          "season": int, "age_band": str, "weight": "H"|"L"|None,
          "gender": "M"|"F", "position": int, "name": str, "age": int,
          "country": str, "value_tenths": int, "verified": "Y"|"N"|"R"
        },
        ...
      ]
    }

Public API:

  * ``load_event_index(event_kind, event_value)`` — reads the index from disk,
    rebuilding from the scraper cache if missing.
  * ``rebuild_event_index(event_kind, event_value)`` — force rebuild from
    ``.c2_rankings/`` and return the entries.
  * ``rebuild_all_indices()`` — rebuild every ranked event's index.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.concept2_rankings import (
    CACHE_DIR as RANKINGS_CACHE_DIR,
    EVENT_IDS_DIST,
    EVENT_IDS_TIME,
    Category,
    iter_all_categories,
    page_filename,
    read_page1_payload,
)
from services.rowing_utils import RANKED_DISTANCES, RANKED_TIMES

INDEX_DIR = Path(".c2_rankings_index")
SCHEMA_VERSION = 1


def _index_path(event_kind: str, event_value: int) -> Path:
    return INDEX_DIR / f"{event_kind}_{event_value}.json"


def _url_id_for(event_kind: str, event_value: int) -> int:
    if event_kind == "dist":
        return EVENT_IDS_DIST[event_value]
    return EVENT_IDS_TIME[event_value]


def _walk_category_pages(cat: Category) -> list[dict]:
    """Return all entries for one category, following page files until last."""
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
                    "age_band": cat.age_band,
                    "weight": cat.weight,
                    "gender": cat.gender,
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


def rebuild_event_index(event_kind: str, event_value: int) -> list[dict]:
    """Walk the scraper cache and rewrite the per-event index file."""
    url_id = _url_id_for(event_kind, event_value)
    all_entries: list[dict] = []
    for cat in iter_all_categories(event_ids=[url_id]):
        if cat.event_kind != event_kind or cat.event_value != event_value:
            continue
        payload = read_page1_payload(cat)
        if payload is None:
            continue
        if payload.get("empty"):
            continue
        all_entries.extend(_walk_category_pages(cat))

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "event_kind": event_kind,
        "event_value": event_value,
        "entries": all_entries,
    }
    path = _index_path(event_kind, event_value)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    os.replace(tmp, path)
    return all_entries


def load_event_index(
    event_kind: str, event_value: int, rebuild_if_missing: bool = True
) -> list[dict]:
    """Load the per-event index, rebuilding on demand if the file is absent."""
    path = _index_path(event_kind, event_value)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get("schema_version") == SCHEMA_VERSION:
                return data.get("entries") or []
        except Exception:
            pass
    if rebuild_if_missing:
        return rebuild_event_index(event_kind, event_value)
    return []


def rebuild_all_indices() -> None:
    """Rebuild every ranked event's index from the scraper cache."""
    for d, _ in RANKED_DISTANCES:
        rebuild_event_index("dist", d)
    for t, _ in RANKED_TIMES:
        rebuild_event_index("time", t)


if __name__ == "__main__":
    rebuild_all_indices()
