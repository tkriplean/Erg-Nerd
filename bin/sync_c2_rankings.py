#!/usr/bin/env python3
"""
Offline sync of Concept2 public RowErg rankings into ``c2_rankings/``.

This is a thin CLI wrapper over ``services.concept2_rankings.scrape_all``. The
heavy lifting — URL building, HTTP, parsing, atomic writes, resumability —
lives in the service module. This file owns argument parsing and terminal
status printing only.

Typical use::

    # Smoke-test the pipeline on the sparsest possible category, 2 pages max.
    python bin/sync_c2_rankings.py --season 2005 --event 500 \\
        --age 95-99 --weight H --gender F --max-pages 1

    # See the full work plan without touching the network.
    python bin/sync_c2_rankings.py --dry-run

    # Sync everything missing (may take days). Safe to Ctrl-C and resume.
    python bin/sync_c2_rankings.py

Status output goes to stdout. Failures are appended to
``c2_rankings/_failures.log``.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

# Make the repo root importable whether the script is run as
# ``python bin/sync_c2_rankings.py`` or ``./bin/sync_c2_rankings.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.concept2_rankings import (  # noqa: E402
    AGE_BANDS,
    CACHE_DIR,
    Category,
    EVENT_IDS_DIST,
    EVENT_IDS_TIME,
    GENDERS,
    WEIGHT_CLASSES_ADULT,
    all_past_seasons,
    iter_all_categories,
    latest_complete_season,
    scrape_all,
    set_rate_limit,
)
from services.rowing_utils import RANKED_DISTANCES, RANKED_TIMES  # noqa: E402

RATE_LIMIT = 3.0

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _event_label(cat: Category) -> str:
    """Human label like '2k' or '30min' for a Category."""
    if cat.event_kind == "dist":
        for d, label in RANKED_DISTANCES:
            if d == cat.event_value:
                return label
        return f"{cat.event_value}m"
    for t, label in RANKED_TIMES:
        if t == cat.event_value:
            return label
    return f"{cat.event_value // 600} min"


def _event_label_for_id(event_id: int) -> str:
    """Human label for a raw URL event_id (used in event_discontinued messages)."""
    for d, label in RANKED_DISTANCES:
        if d == event_id:
            return label
    for t, label in RANKED_TIMES:
        if EVENT_IDS_TIME.get(t) == event_id:
            return label
    return str(event_id)


def _cat_label(cat: Category) -> str:
    weight = cat.weight if cat.weight else "A"  # 'A' for "All" (youth)
    return f"{cat.season} {cat.gender} {cat.age_band} {weight} {_event_label(cat)}"


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    valid_event_ids = sorted(
        set(EVENT_IDS_DIST.values()) | set(EVENT_IDS_TIME.values())
    )
    p = argparse.ArgumentParser(
        description="Scrape Concept2 public rankings into c2_rankings/.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--season",
        type=int,
        action="append",
        metavar="Y",
        help="Restrict to this season end-year (repeatable).",
    )
    p.add_argument(
        "--event",
        type=int,
        action="append",
        dest="event_id",
        choices=valid_event_ids,
        metavar="EID",
        help="Restrict to this URL event id (repeatable).",
    )
    p.add_argument(
        "--age",
        action="append",
        choices=AGE_BANDS,
        metavar="BAND",
        help="Restrict to this age band (repeatable).",
    )
    p.add_argument(
        "--weight",
        action="append",
        choices=WEIGHT_CLASSES_ADULT,
        metavar="H|L",
        help="Restrict to this weight class (repeatable). "
        "When set, youth bands are skipped.",
    )
    p.add_argument(
        "--gender",
        action="append",
        choices=GENDERS,
        metavar="M|F",
        help="Restrict to this gender (repeatable).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch pages even if the JSON file exists.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        metavar="N",
        help="Cap pages per category (smoke testing).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the work plan without making any requests.",
    )
    p.add_argument(
        "--rate-limit",
        type=float,
        metavar="SECONDS",
        help=f"Override the minimum request interval (default {RATE_LIMIT} s).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-page status output; keep the final summary.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Startup survey
# ---------------------------------------------------------------------------


def _count_existing_pages(cats: list[Category]) -> tuple[int, int]:
    """Return (categories_with_any_page, total_page_files) already on disk.

    Cheap: one ``Path.exists()`` per category probing ``page_1``. Enough for
    the startup banner; exact skip counts come out of the scraper itself.
    """
    from services.concept2_rankings import page_filename  # local import for speed

    has_any = 0
    total_files = 0
    if not CACHE_DIR.exists():
        return 0, 0
    for cat in cats:
        p1 = page_filename(cat, 1)
        if p1.exists():
            has_any += 1
            # Glob for all page files for this category to get a rough file count.
            glob = p1.name.replace("_page_1.json", "_page_*.json")
            total_files += len(list(CACHE_DIR.glob(glob)))
    return has_any, total_files


# ---------------------------------------------------------------------------
# Progress printer
# ---------------------------------------------------------------------------


class _Printer:
    """Stateful progress printer with in-place category status line + ETA."""

    _ETA_WINDOW = 40  # number of recent page fetches to average

    def __init__(self, *, quiet: bool, total_categories_max: int):
        self.quiet = quiet
        self.total_categories = (
            total_categories_max  # upper bound; early cutoff shrinks actual
        )
        self.current_index = 0
        self.current_cat: Optional[Category] = None
        self.current_total_pages: Optional[int] = None
        self.last_line_len = 0
        self.pages_written = 0
        self.pages_skipped = 0
        self.pages_failed = 0
        self._page_start_ts: Optional[float] = None
        self._page_durations: deque[float] = deque(maxlen=self._ETA_WINDOW)
        self._run_start_ts = time.time()

    def _status_line(self, suffix: str) -> str:
        cat_lbl = _cat_label(self.current_cat) if self.current_cat else "?"
        page_suffix = suffix
        done = self.current_index
        total = self.total_categories
        pct = (done / total * 100.0) if total else 0.0
        eta = self._eta_string()
        return (
            f"[{cat_lbl}] {page_suffix} · {done}/{total} cats · {pct:.1f}% · ETA {eta}"
        )

    def _eta_string(self) -> str:
        if not self._page_durations:
            return "—"
        avg = sum(self._page_durations) / len(self._page_durations)
        # Estimate remaining pages: crude — avg pages/category so far, times
        # categories remaining. Falls back to 8 pages/cat until we have data.
        cats_remaining = max(0, self.total_categories - self.current_index)
        pages_per_cat = 8
        if self.pages_written > 0 and self.current_index > 0:
            pages_per_cat = max(1, self.pages_written / max(1, self.current_index))
        remaining_pages = int(cats_remaining * pages_per_cat)
        seconds = int(remaining_pages * avg)
        return _fmt_duration(seconds)

    def _write(self, line: str, *, inplace: bool) -> None:
        if self.quiet:
            return
        if inplace:
            pad = " " * max(0, self.last_line_len - len(line))
            sys.stdout.write("\r" + line + pad)
            sys.stdout.flush()
            self.last_line_len = len(line)
        else:
            # Clear any in-place line first, then write a fresh line.
            if self.last_line_len:
                sys.stdout.write("\r" + " " * self.last_line_len + "\r")
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            self.last_line_len = 0

    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "run_start":
            # Banner is printed outside the progress callback (pre-flight).
            return
        if kind == "event_discontinued":
            ev_id = event.get("event_id")
            at = event.get("at_season")
            ev_label = _event_label_for_id(ev_id)
            self._write(
                f"• {ev_label} (event_id={ev_id}) discontinued — no entries in {at} or earlier",
                inplace=False,
            )
            return
        if kind == "category_index":
            self.current_index = int(event.get("index", 0))
            self.current_cat = event.get("category")
            self.current_total_pages = None
            return
        if kind == "page_start":
            self._page_start_ts = time.time()
            self._write(
                self._status_line(f"page {event.get('page')} fetching…"), inplace=True
            )
            return
        if kind == "page_done":
            self.pages_written += 1
            if self._page_start_ts is not None:
                self._page_durations.append(time.time() - self._page_start_ts)
                self._page_start_ts = None
            # Refine total-pages estimate from total_count.
            tc = event.get("total_count")
            if isinstance(tc, int) and tc > 0:
                self.current_total_pages = max(1, -(-tc // 50))  # ceil div
            p = event.get("page")
            of = f"/{self.current_total_pages}" if self.current_total_pages else ""
            self._write(
                self._status_line(f"page {p}{of} · {event.get('n_entries')} entries"),
                inplace=True,
            )
            return
        if kind == "page_skipped":
            self.pages_skipped += 1
            p = event.get("page")
            self._write(self._status_line(f"page {p} (cached, skipped)"), inplace=True)
            return
        if kind == "page_failed":
            self.pages_failed += 1
            self._write(
                f"FAIL page {event.get('page')} {event.get('url')}: {event.get('error')}",
                inplace=False,
            )
            return
        if kind == "category_done":
            # Promote the category summary to a permanent line.
            cat = self.current_cat
            written = event.get("pages_written", 0)
            skipped = event.get("pages_skipped", 0)
            fails = event.get("failures", 0)
            if written or fails:
                self._write(
                    f"done {_cat_label(cat)}: +{written} new, {skipped} skipped, {fails} failed",
                    inplace=False,
                )
            return
        if kind == "aborted":
            self._write("aborted.", inplace=False)
            return
        if kind == "run_aborted":
            self._write(
                f"run aborted after {event.get('completed', 0)} categories.",
                inplace=False,
            )
            return
        if kind == "run_done":
            self._write("", inplace=False)  # flush in-place line
            return

    def print_final_summary(self, totals: dict) -> None:
        elapsed = int(time.time() - self._run_start_ts)
        discontinued = totals.get("events_discontinued", 0)
        disc_str = (
            f" · {discontinued} events discontinued early" if discontinued else ""
        )
        print(
            f"\nDone. {totals.get('pages_written', 0):,} new pages · "
            f"{totals.get('pages_skipped', 0):,} skipped · "
            f"{totals.get('failures', 0):,} failed · "
            f"{totals.get('categories_done', 0):,} cats · "
            f"{_fmt_duration(elapsed)} elapsed{disc_str}."
        )
        if totals.get("failures"):
            from services.concept2_rankings import FAILURES_LOG

            print(f"Failures logged to {FAILURES_LOG}")
        if totals.get("aborted"):
            print("Run aborted by user.")


def _fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


# ---------------------------------------------------------------------------
# Abort handling
# ---------------------------------------------------------------------------


_aborted = False


def _install_sigint_handler() -> None:
    def handler(signum, frame):  # noqa: ARG001
        global _aborted
        _aborted = True
        sys.stdout.write("\n^C received — finishing current page then stopping…\n")
        sys.stdout.flush()

    signal.signal(signal.SIGINT, handler)


def _abort_check() -> bool:
    return _aborted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    effective_rate = args.rate_limit if args.rate_limit is not None else RATE_LIMIT
    set_rate_limit(
        effective_rate
    )  # always set so the service uses RATE_LIMIT, not MIN_INTERVAL

    seasons = args.season or all_past_seasons()
    genders = args.gender or GENDERS
    age_bands = args.age or AGE_BANDS
    weights = (
        args.weight
    )  # None = all adult weights + allow youth; explicit list = adults only
    event_ids = args.event_id  # None = all 13

    cats = list(
        iter_all_categories(
            seasons=seasons,
            event_ids=event_ids,
            age_bands=age_bands,
            weights=weights,
            genders=genders,
        )
    )

    # ── Banner ─────────────────────────────────────────────────────────────
    existing_cats, existing_files = _count_existing_pages(cats)
    print("Concept2 Rankings Sync")
    season_range = (
        f"{min(seasons)}..{max(seasons)} ({len(seasons)})" if seasons else "—"
    )
    event_count = (
        len(event_ids) if event_ids else (len(EVENT_IDS_DIST) + len(EVENT_IDS_TIME))
    )
    weight_count = len(weights) if weights else len(WEIGHT_CLASSES_ADULT)
    print(
        f"  Seasons:  {season_range} (newest first; stops early per event when no entries found)"
    )
    print(
        f"  Events:   {event_count}    "
        f"Age bands: {len(age_bands)}    "
        f"Weights: {weight_count}    "
        f"Genders: {len(genders)}"
    )
    print(
        f"  Work:     up to {len(cats):,} categories  "
        f"({existing_cats:,} already started, {existing_files:,} page files on disk)"
    )
    print(f"  Cache:    {CACHE_DIR}/")
    print(f"  Rate:     {effective_rate:.1f} s + up to 0.5 s jitter")
    if args.max_pages:
        print(f"  Pages:    capped at {args.max_pages}/category")
    if args.force:
        print("  Force:    on — will re-fetch cached pages")
    if args.dry_run:
        print("\n(dry-run) — no HTTP requests will be made.")
        print(
            "  Iteration: event-outer, season newest→oldest, stops per event when all-empty."
        )
        for cat in cats[:20]:
            print(f"  would scrape: {_cat_label(cat)}  ->  {page_filename_for(cat, 1)}")
        if len(cats) > 20:
            print(
                f"  … and up to {len(cats) - 20:,} more (early cutoff will reduce this)."
            )
        return 0

    _install_sigint_handler()
    printer = _Printer(quiet=args.quiet, total_categories_max=len(cats))

    totals = scrape_all(
        seasons=seasons,
        event_ids=event_ids,
        age_bands=age_bands,
        weights=weights,
        genders=genders,
        force=args.force,
        max_pages=args.max_pages,
        abort_check=_abort_check,
        on_progress=printer.handle,
    )
    printer.print_final_summary(totals)
    return 2 if totals.get("failures") else (1 if totals.get("aborted") else 0)


def page_filename_for(cat: Category, page: int) -> Path:
    # Tiny re-export so the --dry-run block reads cleanly.
    from services.concept2_rankings import page_filename

    return page_filename(cat, page)


if __name__ == "__main__":
    sys.exit(main())
