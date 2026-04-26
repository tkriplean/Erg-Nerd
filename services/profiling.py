"""
services/profiling.py

Lightweight Python-side profiling layer wrapping pyinstrument.

When ``config.PROFILE_ENABLE`` is False (the default), every helper in this
module is effectively a no-op: ``profile_block`` is a pass-through context
manager and ``@profiled`` returns the function unchanged.

When ``PROFILE_ENABLE`` is True, two things happen:

1. **Session profiler** — the first time any ``profile_block`` is entered, a
   long-running ``pyinstrument.Profiler`` is started on the current thread
   (in practice, the HyperDiv worker thread that drives ``main()`` re-renders).
   It accumulates samples across every subsequent block on that thread and
   dumps a combined call-tree report to ``.profiles/session-{ts}.html`` via
   an ``atexit`` hook on shutdown.

2. **Per-block console timing** — every ``profile_block`` body is also bracketed
   with ``time.perf_counter`` and prints a one-line summary
   (``[profile] main-render = 87ms``) so you can see hot renders at a glance
   without opening the HTML.

For functions that run on **worker threads** (e.g. ``build_keyframes`` invoked
via ``hd.task``), ``@profiled`` detects the foreign thread and captures a
self-contained per-call profile to ``.profiles/{qualname}-{ts}.html``. This
matters because ``sys.setprofile`` (which pyinstrument uses) is per-thread,
so the session profiler on the main render thread cannot see worker-thread
call stacks.

The output directory ``.profiles/`` is gitignored.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from config import PROFILE_ENABLE

_PROFILE_DIR = Path(".profiles")

_session_profiler: Any = None
_session_tag: str | None = None
_session_thread: threading.Thread | None = None
_session_lock = threading.Lock()


def _now_tag() -> str:
    # millisecond precision is plenty for filenames and avoids collisions
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _ensure_dir() -> None:
    _PROFILE_DIR.mkdir(exist_ok=True)


def _start_session_on_current_thread() -> None:
    global _session_profiler, _session_tag, _session_thread
    if _session_profiler is not None:
        return
    with _session_lock:
        if _session_profiler is not None:
            return
        try:
            from pyinstrument import Profiler
        except ImportError:
            print(
                "[profile] pyinstrument not installed — "
                "run `poetry install` to enable profiling"
            )
            return
        _ensure_dir()
        prof = Profiler(async_mode="disabled")
        prof.start()
        _session_profiler = prof
        _session_tag = _now_tag()
        _session_thread = threading.current_thread()
        atexit.register(_dump_session)
        print(
            f"[profile] session profiler started on thread "
            f"{_session_thread.name!r} (tag={_session_tag})"
        )


def _dump_session() -> None:
    global _session_profiler
    if _session_profiler is None:
        return
    prof = _session_profiler
    _session_profiler = None
    try:
        prof.stop()
    except Exception:
        pass
    try:
        _ensure_dir()
        out = _PROFILE_DIR / f"session-{_session_tag}.html"
        out.write_text(prof.output_html())
        print(f"[profile] session report -> {out}")
    except Exception as exc:
        print(f"[profile] failed to dump session report: {exc}")


@contextlib.contextmanager
def profile_block(name: str) -> Iterator[None]:
    """Time and profile a block of code.

    No-op when ``PROFILE_ENABLE`` is False. Otherwise, ensures the session
    profiler is running on the current thread and prints a one-line timing
    summary on exit.
    """
    if not PROFILE_ENABLE:
        yield
        return
    _start_session_on_current_thread()
    t0 = time.perf_counter()
    try:
        print("--------------------")
        yield
        print("++++++++++++++++++++")
    finally:
        print("********************************************")
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"[profile] {name} = {ms:.0f}ms")
        _dump_session()


def _profile_on_foreign_thread(name: str, func: Callable[..., Any], args, kwargs):
    """Capture a self-contained per-call profile for a function running on a
    thread the session profiler doesn't cover."""
    try:
        from pyinstrument import Profiler
    except ImportError:
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            print(f"[profile] {name} = {ms:.0f}ms (pyinstrument missing)")

    prof = Profiler(async_mode="disabled")
    prof.start()
    t0 = time.perf_counter()
    try:
        return func(*args, **kwargs)
    finally:
        try:
            prof.stop()
        except Exception:
            pass
        ms = (time.perf_counter() - t0) * 1000.0
        try:
            _ensure_dir()
            out = _PROFILE_DIR / f"{name}-{_now_tag()}.html"
            out.write_text(prof.output_html())
            print(f"[profile] {name} = {ms:.0f}ms -> {out}")
        except Exception as exc:
            print(f"[profile] {name} = {ms:.0f}ms (dump failed: {exc})")


def profiled(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: profile a hot-path function.

    No-op when ``PROFILE_ENABLE`` is False. Otherwise:
      - On the same thread as the session profiler: behaves like
        ``profile_block`` (timing one-liner; samples roll up into the session).
      - On a foreign thread (e.g. an ``hd.task`` worker): captures a
        self-contained per-call HTML report and prints the timing.
    """
    if not PROFILE_ENABLE:
        return func

    name = getattr(func, "__qualname__", func.__name__)
    sanitized = name.replace("/", "_").replace(" ", "_")

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if (
            _session_profiler is not None
            and threading.current_thread() is not _session_thread
        ):
            return _profile_on_foreign_thread(sanitized, func, args, kwargs)
        with profile_block(sanitized):
            return func(*args, **kwargs)

    return wrapper
