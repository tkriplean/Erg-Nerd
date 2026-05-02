#!/usr/bin/env python3
"""
Offline sync of Concept2 public RowErg rankings into ``.c2_rankings/``.

This is a thin CLI wrapper over ``services.concept2_rankings.scrape_all``. The
heavy lifting — URL building, HTTP, parsing, atomic writes, resumability —
lives in the service module. This file owns argument parsing and terminal
status printing only.

Typical use::

    # Smoke-test the pipeline on a sparse category, 2 pages max.
    python bin/sync_c2_rankings.py --season 2005 --event 500 \\
        --weight H --gender F --max-pages 1

    # See the full work plan without touching the network.
    python bin/sync_c2_rankings.py --dry-run

    # Sync everything missing (may take days). Safe to Ctrl-C and resume.
    python bin/sync_c2_rankings.py

Status output goes to stdout. Failures are appended to
``.c2_rankings/_failures.log``.
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
    CACHE_DIR,
    FetchTarget,
    GENDERS,
    WEIGHT_CLASSES_ADULT,
    all_past_seasons,
    event_ids_dist,
    event_ids_time,
    iter_fetch_targets,
    output_categories_for,
    scrape_all,
    set_rate_limit,
)
from services.rowing_utils import ranked_distances, ranked_times  # noqa: E402

RATE_LIMIT = 3.0

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def _event_label(item) -> str:
    """Human label like '2k' or '30min' for any object exposing
    ``machine``, ``event_kind``, and ``event_value`` (Category or FetchTarget)."""
    machine = item.machine
    if item.event_kind == "dist":
        for d, label in ranked_distances(machine):
            if d == item.event_value:
                return label
        return f"{item.event_value}m"
    for t, label in ranked_times(machine):
        if t == item.event_value:
            return label
    return f"{item.event_value // 600} min"


def _event_label_for_id(event_id: int, machine: str = "rower") -> str:
    """Human label for a raw URL event_id (used in event_discontinued messages)."""
    for d, label in ranked_distances(machine):
        if d == event_id:
            return label
    et = event_ids_time(machine)
    for t, label in ranked_times(machine):
        if et.get(t) == event_id:
            return label
    return str(event_id)


def _target_label(target: FetchTarget) -> str:
    if target.age_band is not None:
        slice_lbl = f"youth-{target.age_band}"
    else:
        slice_lbl = f"adults-{target.weight}"
    return f"{target.season} {target.gender} {slice_lbl} {_event_label(target)}"


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    # Validate against the union of event ids across all supported machines so
    # the CLI accepts any machine's events; the actual machine choice happens
    # below via --machine.
    valid_event_ids = sorted(
        set()
        .union(event_ids_dist("rower").values())
        .union(event_ids_time("rower").values())
        .union(event_ids_dist("skierg").values())
        .union(event_ids_time("skierg").values())
        .union(event_ids_dist("bikeerg").values())
        .union(event_ids_time("bikeerg").values())
    )
    p = argparse.ArgumentParser(
        description="Scrape Concept2 public rankings into .c2_rankings/.",
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
    p.add_argument(
        "--machine",
        choices=["rower", "skierg", "bikeerg"],
        default="rower",
        help="Concept2 machine to scrape rankings for.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Startup survey
# ---------------------------------------------------------------------------


def _count_existing_outputs(targets: list[FetchTarget]) -> tuple[int, int, int]:
    """Return (targets_fully_cached, output_files_present, output_files_total).

    A target is "fully cached" when every output Category's ``page_1.json``
    already exists, so the scraper will skip its HTTP fetch.
    """
    from services.concept2_rankings import page_filename, read_page1_payload  # local imports for speed

    fully_cached = 0
    files_present = 0
    files_total = 0
    cache_exists = CACHE_DIR.exists()
    for target in targets:
        outs = output_categories_for(target)
        files_total += len(outs)
        if not cache_exists:
            continue
        present_for_target = sum(1 for c in outs if page_filename(c, 1).exists())
        files_present += present_for_target
        if present_for_target == len(outs):
            # Only fully cached if all page_1.json files are complete (is_last_page=True).
            # Old-format files with is_last_page=False hold only the first 50 entries.
            if all(
                (p := read_page1_payload(c)) is not None and p.get("is_last_page", False)
                for c in outs
            ):
                fully_cached += 1
    return fully_cached, files_present, files_total


# ---------------------------------------------------------------------------
# Progress printer
# ---------------------------------------------------------------------------


class _Printer:
    """Stateful progress printer with in-place fetch-target status line + ETA."""

    _ETA_WINDOW = 40  # number of recent page fetches to average

    def __init__(self, *, quiet: bool, total_targets_max: int):
        self.quiet = quiet
        self.total_targets = (
            total_targets_max  # upper bound; early cutoff shrinks actual
        )
        self.current_index = 0
        self.current_target: Optional[FetchTarget] = None
        self.last_line_len = 0
        self.pages_written = 0
        self.pages_skipped = 0
        self.pages_failed = 0
        self.targets_fetched = 0
        self._page_start_ts: Optional[float] = None
        self._page_durations: deque[float] = deque(maxlen=self._ETA_WINDOW)
        self._run_start_ts = time.time()

    def _status_line(self, suffix: str) -> str:
        target_lbl = _target_label(self.current_target) if self.current_target else "?"
        done = self.current_index
        total = self.total_targets
        pct = (done / total * 100.0) if total else 0.0
        eta = self._eta_string()
        return (
            f"[{target_lbl}] {suffix} · {done}/{total} targets · {pct:.1f}% · ETA {eta}"
        )

    def _eta_string(self) -> str:
        if not self._page_durations:
            return "—"
        avg = sum(self._page_durations) / len(self._page_durations)
        # Estimate remaining HTTP pages: crude — avg pages/target so far, times
        # targets remaining. Falls back to 4 pages/target until we have data.
        targets_remaining = max(0, self.total_targets - self.current_index)
        pages_per_target = 4
        if self.targets_fetched > 0:
            pages_per_target = max(1, self.pages_written / max(1, self.targets_fetched))
        remaining_pages = int(targets_remaining * pages_per_target)
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
            if self.last_line_len:
                sys.stdout.write("\r" + " " * self.last_line_len + "\r")
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            self.last_line_len = 0

    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "run_start":
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
        if kind == "target_index":
            self.current_index = int(event.get("index", 0))
            self.current_target = event.get("target")
            return
        if kind == "target_skipped":
            self.pages_skipped += int(event.get("n_outputs", 0))
            self._write(
                self._status_line("all outputs cached, skipped"), inplace=True
            )
            return
        if kind == "page_start":
            self._page_start_ts = time.time()
            self._write(
                self._status_line(f"page {event.get('page')} fetching…"), inplace=True
            )
            return
        if kind == "page_done":
            if self._page_start_ts is not None:
                self._page_durations.append(time.time() - self._page_start_ts)
                self._page_start_ts = None
            p = event.get("page")
            self._write(
                self._status_line(f"page {p} · {event.get('n_entries')} entries"),
                inplace=True,
            )
            return
        if kind == "page_failed":
            self.pages_failed += 1
            self._write(
                f"FAIL page {event.get('page')} {event.get('url')}: {event.get('error')}",
                inplace=False,
            )
            return
        if kind == "target_done":
            written = int(event.get("pages_written", 0))
            fetched = int(event.get("pages_fetched", 0))
            fails = int(event.get("failures", 0))
            self.pages_written += written
            self.targets_fetched += 1
            if written or fails:
                self._write(
                    f"done {_target_label(self.current_target)}: "
                    f"fetched {fetched} HTTP pages → wrote {written} files, {fails} failed",
                    inplace=False,
                )
            return
        if kind == "target_capped":
            self._write(
                self._status_line(f"capped at page {event.get('page')}"), inplace=True
            )
            return
        if kind == "aborted":
            self._write("aborted.", inplace=False)
            return
        if kind == "run_aborted":
            self._write(
                f"run aborted after {event.get('completed', 0)} targets.",
                inplace=False,
            )
            return
        if kind == "run_done":
            self._write("", inplace=False)
            return

    def print_final_summary(self, totals: dict) -> None:
        elapsed = int(time.time() - self._run_start_ts)
        discontinued = totals.get("events_discontinued", 0)
        disc_str = (
            f" · {discontinued} events discontinued early" if discontinued else ""
        )
        print(
            f"\nDone. {totals.get('pages_written', 0):,} files written · "
            f"{totals.get('pages_skipped', 0):,} skipped · "
            f"{totals.get('failures', 0):,} failed · "
            f"{totals.get('targets_done', 0):,} targets · "
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
    weights = args.weight  # None = all adult weights + youth; explicit list = adults only
    event_ids = args.event_id  # None = all events

    targets = list(
        iter_fetch_targets(
            seasons=seasons,
            event_ids=event_ids,
            weights=weights,
            genders=genders,
            machine=args.machine,
        )
    )

    # ── Banner ─────────────────────────────────────────────────────────────
    fully_cached, files_present, files_total = _count_existing_outputs(targets)
    print(f"Concept2 Rankings Sync ({args.machine})")
    season_range = (
        f"{min(seasons)}..{max(seasons)} ({len(seasons)})" if seasons else "—"
    )
    event_count = (
        len(event_ids)
        if event_ids
        else (len(event_ids_dist(args.machine)) + len(event_ids_time(args.machine)))
    )
    weight_count = len(weights) if weights else len(WEIGHT_CLASSES_ADULT)
    print(
        f"  Seasons:  {season_range} (newest first; stops early per event when no entries found)"
    )
    print(
        f"  Events:   {event_count}    "
        f"Weights: {weight_count}    "
        f"Genders: {len(genders)}"
    )
    print(
        f"  Work:     up to {len(targets):,} fetch targets → {files_total:,} output files  "
        f"({fully_cached:,} targets fully cached, {files_present:,} files on disk)"
    )
    print(f"  Cache:    {CACHE_DIR}/")
    print(f"  Rate:     {effective_rate:.1f} s + up to 0.5 s jitter")
    if args.max_pages:
        print(f"  Pages:    capped at {args.max_pages} HTTP pages/target")
    if args.force:
        print("  Force:    on — will re-fetch cached targets")
    if args.dry_run:
        print("\n(dry-run) — no HTTP requests will be made.")
        print(
            "  Iteration: event-outer, season newest→oldest, stops per event when all-empty."
        )
        from services.concept2_rankings import build_fetch_url, page_filename
        for target in targets[:20]:
            outs = output_categories_for(target)
            url = build_fetch_url(target, 1)
            print(f"  would fetch: {_target_label(target)}")
            print(f"               URL: {url}")
            print(f"               → {len(outs)} output file(s), e.g. {page_filename(outs[0], 1)}")
        if len(targets) > 20:
            print(
                f"  … and up to {len(targets) - 20:,} more (early cutoff will reduce this)."
            )
        return 0

    _install_sigint_handler()
    printer = _Printer(quiet=args.quiet, total_targets_max=len(targets))

    totals = scrape_all(
        seasons=seasons,
        event_ids=event_ids,
        weights=weights,
        genders=genders,
        force=args.force,
        max_pages=args.max_pages,
        abort_check=_abort_check,
        on_progress=printer.handle,
        machine=args.machine,
    )
    printer.print_final_summary(totals)
    return 2 if totals.get("failures") else (1 if totals.get("aborted") else 0)


if __name__ == "__main__":
    sys.exit(main())
