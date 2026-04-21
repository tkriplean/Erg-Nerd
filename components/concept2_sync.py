"""
Shared Concept2-origin data loaders.

Render-top helpers that any Page can call.  Each handles its own lifecycle
(load, fetch, cache, progress/loading UI) so the caller gets the ready data
back or a loading sentinel.

    concept2_sync(client) -> (workouts_dict, sorted_workouts) | None
        Load, sync, and persist the user's own workout history.

    load_world_record_data(state, profile) -> wr_data | None
        Fetch world-class CP data for the user's (gender, age, weight) bucket.

    strokes_for(ctx, workout) -> {'strokes', 'status', 'error'}
        Single-workout stroke fetcher.  Uniform across owner / public modes;
        handles the localStorage cache, on-owner-view mirror to the
        public-profile directory, and the "not yet cached" public-mode case.

    strokes_batch(ctx, workouts) -> {'by_id', 'done', 'total', 'is_loading',
                                     'uncached_count'}
        Queue-with-progress fetcher (race page).  Owner: one-at-a-time API
        fetch; public: synchronous disk reads from .public_profiles.

The stroke helpers store **raw** Concept2 API strokes
(``[{t: tenths, d: decimeters, p, spm, hr, …}]``) — workout_page consumes
this shape directly; race_page runs ``normalize_strokes`` at the boundary.
"""

import json
from datetime import datetime

import hyperdiv as hd

from config import SYNTHETIC_MODE
from services.local_storage_compression import (
    compress_workouts,
    decompress_workouts,
    compress_strokes_cache,
    decompress_strokes_cache,
)
from services.concept2_records import (
    age_category as wr_age_category,
    weight_class_str as wr_weight_class_str,
    fetch_wr_data,
)
from services.rowing_utils import age_from_dob


def _fmt_month_year(date_str: str) -> str:
    """'2019-03-14' → 'Mar 2019'"""
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %Y")
    except Exception:
        return date_str[:7]


def sync_from_context(ctx) -> tuple | None:
    """
    Return (workouts_dict, sorted_workouts) for the active ViewContext.

    Owner mode: delegate to ``concept2_sync(ctx.client)`` (fetches + syncs).
    Public mode: return the pre-loaded snapshot the dashboard wired in from
    ``services.public_profiles`` — no I/O, no sync task.
    """
    if ctx.mode == "public":
        return ctx.public_workouts_dict, ctx.public_sorted_workouts
    return concept2_sync(ctx.client)


def concept2_sync(client) -> tuple | None:
    """
    Load, sync, and persist workout data.  Returns (workouts_dict, sorted_list)
    when ready, or None while the component is still loading.
    """
    # ── Step 1: one-time localStorage read ───────────────────────────────────
    sync_state = hd.state(
        written=False,
        initial_workouts=None,
        initial_loaded=False,
        synth_cache=None,
        # Public-profile push-on-sync state. Set once per sync-completion; the
        # publish task reads ``profile`` from localStorage and pushes the
        # server snapshot. Bool not timestamp — we only want one push per
        # sync per component render tree.
        published=False,
        profile_blob="",
    )

    if not sync_state.initial_loaded:
        ls_wkts = hd.local_storage.get_item("workouts")
        ls_profile = hd.local_storage.get_item("profile")
        if not ls_wkts.done or not ls_profile.done:
            with hd.box(align="center", padding=4):
                hd.spinner()
            return None
        sync_state.initial_workouts = (
            decompress_workouts(ls_wkts.result) if ls_wkts.result else {}
        )
        sync_state.profile_blob = ls_profile.result or ""
        sync_state.initial_loaded = True

    # ── Step 2: background API sync ──────────────────────────────────────────
    progress = hd.state(pages=0, total=0, api_total=None, earliest_date=None)
    task = hd.task()

    def _fetch(client, initial, progress):
        def on_progress(pages_fetched, workouts_cached, api_total, earliest_date):
            progress.pages = pages_fetched
            progress.total = workouts_cached
            progress.api_total = api_total
            progress.earliest_date = earliest_date

        return client.get_all_results(initial, on_progress=on_progress)

    task.run(_fetch, client, sync_state.initial_workouts, progress)

    # ── Step 3: handle result ────────────────────────────────────────────────
    if task.done and not task.error:
        workouts_dict, sorted_workouts = task.result
        if not sync_state.written:
            # Write real data only — synthetic workouts must never reach localStorage.
            hd.local_storage.set_item("workouts", compress_workouts(workouts_dict))
            sync_state.written = True

        # Push-on-sync: if the user has opted in (profile.public=True), mirror
        # the fresh profile + workouts to the server-side public-profile
        # directory. Runs once per component lifetime; failures log but never
        # break the dashboard. Skipped in synthetic mode (no real workouts).
        if not sync_state.published and not SYNTHETIC_MODE and sync_state.profile_blob:
            _maybe_push_on_sync(client, sync_state, workouts_dict)

        if SYNTHETIC_MODE:
            if sync_state.synth_cache is None:
                from services.synthetic_data import augment_with_synthetic

                sync_state.synth_cache = augment_with_synthetic(workouts_dict)
            return sync_state.synth_cache
        return workouts_dict, sorted_workouts

    # Loading UI
    if task.running:
        with hd.box(align="center", padding=4, gap=2):
            if progress.pages >= 2 and progress.api_total:
                pct = min(100, round(progress.total / progress.api_total * 100))
                with hd.box(width=32):
                    hd.progress_bar(value=pct)
            else:
                hd.spinner()
            if progress.pages == 0:
                hd.text("Loading workout history…", font_color="neutral-500")
            else:
                if progress.api_total:
                    count_line = f"Syncing {progress.total:,} of {progress.api_total:,} workouts…"
                else:
                    count_line = f"Syncing {progress.total:,} workouts…"
                hd.text(count_line, font_color="neutral-500")
                if progress.earliest_date:
                    hd.text(
                        f"Back to {_fmt_month_year(progress.earliest_date)}",
                        font_color="neutral-400",
                        font_size="small",
                    )
        return None

    # Error UI
    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)
        return None

    return None


# ---------------------------------------------------------------------------
# Push-on-sync: mirror the fresh snapshot to the public-profile directory
# ---------------------------------------------------------------------------


def _maybe_push_on_sync(client, sync_state, workouts_dict: dict) -> None:
    """
    If the owner has opted in (``profile.public == True``), mirror the freshly
    synced profile + workouts to the server-side public-profile directory.

    Runs as an ``hd.task`` so the dashboard is not blocked on disk I/O. Flips
    ``sync_state.published`` eagerly so a single sync completion triggers one
    push. Failures are logged to stdout only — a dashboard user should not
    see an error if the public mirror fails.
    """
    import json as _json

    from services import public_profiles

    try:
        profile = (
            _json.loads(sync_state.profile_blob) if sync_state.profile_blob else {}
        )
    except Exception:
        profile = {}

    if not profile.get("public"):
        sync_state.published = True  # no-op; don't re-check every render
        return

    # Resolve user_id + display_name from the authenticated client.
    user_id = getattr(client, "_user_id", "") or ""
    if not user_id:
        sync_state.published = True
        return

    push_task = hd.task()

    def _do_push(client_, uid, prof, wkts):
        # Fetch display name inside the task so it doesn't block render.
        try:
            u = client_.get_user().get("data", {})
            dn = (
                u.get("first_name") or u.get("username") or "Rower"
            ).strip() or "Rower"
        except Exception:
            dn = "Rower"
        public_profiles.publish_all(uid, prof, dn, wkts)

    if not push_task.running and not push_task.done:
        push_task.run(_do_push, client, user_id, profile, workouts_dict)

    if push_task.done:
        sync_state.published = True
        if push_task.error:
            print(f"[public_profiles] push-on-sync failed: {push_task.error}")


# ---------------------------------------------------------------------------
# World-class CP data fetch
# ---------------------------------------------------------------------------


def load_world_record_data(state, profile: dict):
    """
    Manage the background task that fetches world-class CP data for the
    user's (gender, age, weight) bucket.

    Caches result in ``state.wr_data`` and flips ``state.wr_fetch_done`` when
    the task completes.  Returns ``state.wr_data`` (``None`` until the fetch
    finishes or if the API returned nothing).

    The Power Curve page's ``manage_animation_bundle`` folds
    ``state.wr_fetch_done`` and ``state.wr_fetch_key`` into its bundle_key so
    that the animation bundle rebuilds once the fetch completes — otherwise
    the y-bounds baked at the pre-fetch render would persist even after WR
    data arrived.
    """
    gender_raw = profile.get("gender", "")  # "Male" or "Female"
    if gender_raw not in ("Male", "Female"):
        return None
    gender_api = "M" if gender_raw == "Male" else "F"
    age = age_from_dob(profile.get("dob", ""))
    weight_raw = profile.get("weight") or 0.0
    weight_unit = profile.get("weight_unit", "kg")
    weight_kg = weight_raw * 0.453592 if weight_unit == "lbs" else float(weight_raw)
    if age is None or weight_kg <= 0:
        return None

    age_cat = wr_age_category(age)
    wt_class = wr_weight_class_str(weight_kg, gender_api, age)
    fetch_key = f"{gender_api}|{age_cat}|{wt_class}"

    # Reset when profile changes so the fetch task re-fires.
    if fetch_key != state.wr_fetch_key:
        state.wr_fetch_key = fetch_key
        state.wr_fetch_done = False
        state.wr_data = None

    with hd.scope(f"wr_task_{fetch_key}"):
        wr_task = hd.task()
        if not wr_task.running and not wr_task.done:
            wr_task.run(fetch_wr_data, gender_api, age, weight_kg)
        if wr_task.done and not state.wr_fetch_done:
            state.wr_fetch_done = True
            state.wr_data = wr_task.result  # None if API returned nothing

    return state.wr_data


# ---------------------------------------------------------------------------
# Stroke fetching — unified interface for workout_page and race_page
# ---------------------------------------------------------------------------
#
# Cache layout:
#   In-memory:   one hd.state() under a stable scope ``_STROKES_SCOPE`` so all
#                strokes_for / strokes_batch calls in the same render tree
#                share a single cache dict.
#   Persistent:  localStorage key ``strokes_cache`` (compressed JSON via
#                compress_strokes_cache).  Owner-only.
#   Public mirror: ``.public_profiles/{uid}/strokes/{rid}.json`` — written
#                  lazily whenever the owner fetches and ``profile.public``
#                  is True.
#
# Migration note: older releases stored race-normalized ``[{t:sec, d:m}]`` in
# the localStorage cache.  We now store raw Concept2 output
# (``[{t:tenths, d:dm, p, spm, hr, …}]``) so workout_page and race_page can
# share one cache.  Legacy normalized entries are detected via the absence of
# ``p``/``spm``/``hr`` keys and discarded on first load so they are re-fetched
# as raw.


_STROKES_LS_KEY = "strokes_cache"


# localStorage is the single source of truth for the stroke cache.
# ``hd.local_storage.get_item`` caches results keyed by the LS key (not by
# call-stack position), so reads are reliably shared across every call in a
# render.  After ``set_item`` the cache entry is invalidated, so subsequent
# reads pick up the new value.  We deliberately avoid ``hd.state`` for the
# cache because state keys are derived from the call stack — calling
# ``hd.state`` from two different helpers yields two unrelated states even
# inside the same ``hd.scope``.


def _is_raw_strokes(strokes) -> bool:
    """Heuristic: raw Concept2 strokes carry ``p``/``spm``/``hr`` keys;
    legacy race-normalized entries carry only ``t`` and ``d``.  Empty lists
    are treated as raw (safe default)."""
    if not strokes:
        return True
    s0 = strokes[0]
    return isinstance(s0, dict) and ("p" in s0 or "spm" in s0 or "hr" in s0)


def _public_enabled() -> bool:
    """Read ``profile.public`` from localStorage.  Returns False while still
    loading or on any parse error."""
    ls = hd.local_storage.get_item("profile")
    if not ls.done or not ls.result:
        return False
    try:
        return bool(json.loads(ls.result).get("public"))
    except Exception:
        return False


def _load_strokes_cache() -> tuple[dict, bool]:
    """
    Return (cache_dict, ready).  Reads directly from localStorage every
    render — cheap because hd.local_storage returns a cached async_command,
    so this is at worst a dict re-decompress.  ``ready=False`` means
    localStorage is still loading.
    """
    ls = hd.local_storage.get_item(_STROKES_LS_KEY)
    if not ls.done:
        return {}, False
    if not ls.result:
        return {}, True
    try:
        cache = decompress_strokes_cache(ls.result)
    except Exception:
        return {}, True
    # Drop legacy normalized entries so they are re-fetched as raw.
    return {k: v for k, v in cache.items() if _is_raw_strokes(v)}, True


def _mirror_strokes_to_public(
    user_id: str, result_id, strokes: list, public_enabled: bool
) -> None:
    """Cache-on-owner-view mirror.  No-op when the owner is private, the
    strokes list is empty, or the file already exists on disk (cheap
    ``Path.is_file`` check avoids redundant atomic writes when the same
    cached entry is touched across many renders).

    ``public_enabled`` is passed by the caller so this helper never calls
    ``hd.local_storage.get_item`` itself — important because it is called
    from loops, and every LS read would register a fresh component with a
    call-stack-derived key, tripping HyperDiv's duplicate-key check."""
    if not strokes or not public_enabled:
        return
    try:
        from services import public_profiles

        if public_profiles.has_cached_strokes(user_id, result_id):
            return
        public_profiles.publish_strokes(user_id, result_id, strokes)
    except Exception as exc:
        print(f"[public_profiles] cache-on-view failed: {exc}")


def _persist_strokes(
    user_id: str, wid, strokes: list, cache: dict, public_enabled: bool
) -> None:
    """Merge one entry into ``cache`` (the render-local dict returned by
    ``_load_strokes_cache``), write the full merged cache back to
    localStorage, and mirror to the public-profile directory if opted in."""
    merged = dict(cache)
    merged[str(wid)] = strokes
    try:
        hd.local_storage.set_item(_STROKES_LS_KEY, compress_strokes_cache(merged))
    except Exception as exc:
        print(f"[strokes_cache] persist failed: {exc}")
    cache[str(wid)] = strokes  # reflect in caller's dict so same-render reads see it
    _mirror_strokes_to_public(user_id, wid, strokes, public_enabled)


def strokes_for(ctx, workout: dict) -> dict:
    """
    Fetch raw stroke data for a single workout.  Uniform across modes.

    Returns a dict::

        {
          "strokes": list | None,
          "status":  "loaded" | "loading" | "error" | "uncached" | "no_strokes",
          "error":   str | None,
        }

    - ``no_strokes``: workout has no stroke_data flag — nothing to fetch.
    - ``uncached``  : public-mode only; the owner has not yet viewed this
                      session so the server has no stroke file.
    - ``loading``   : localStorage read or API task is in flight.
    - ``loaded``    : ``strokes`` is a list (may be empty if API returned none).
    - ``error``     : the API fetch failed; ``error`` carries the message.
    """
    wid = workout.get("id")
    if wid is None or not workout.get("stroke_data"):
        return {"strokes": None, "status": "no_strokes", "error": None}

    if ctx.mode == "public":
        from services import public_profiles

        cached = public_profiles.load_public_strokes(ctx.user_id, wid)
        if cached is None:
            return {"strokes": None, "status": "uncached", "error": None}
        return {"strokes": cached, "status": "loaded", "error": None}

    # Owner mode.  Wrap all LS reads + the fetch task in a per-wid scope so
    # callers can invoke strokes_for(wid=A), strokes_for(wid=B) from the
    # same loop body without colliding on HyperDiv's call-stack-derived
    # component keys.
    with hd.scope(f"strokes_for_{wid}"):
        cache, ready = _load_strokes_cache()
        if not ready:
            return {"strokes": None, "status": "loading", "error": None}

        public_enabled = _public_enabled()

        key = str(wid)
        if key in cache:
            # Cache-hit: still mirror to the public directory — the entry
            # may have been cached before the user opted in.  The mirror
            # helper no-ops when the file already exists on disk.
            _mirror_strokes_to_public(ctx.user_id, wid, cache[key], public_enabled)
            return {"strokes": cache[key], "status": "loaded", "error": None}

        if ctx.client is None:
            return {"strokes": None, "status": "error", "error": "no client"}

        task = hd.task()
        if not task.running and not task.done:
            task.run(lambda: ctx.client.get_strokes(int(ctx.user_id), wid))
        if task.running:
            return {"strokes": None, "status": "loading", "error": None}
        if task.error:
            return {"strokes": None, "status": "error", "error": str(task.error)}
        if task.done:
            strokes = task.result if isinstance(task.result, list) else []
            _persist_strokes(ctx.user_id, wid, strokes, cache, public_enabled)
            return {"strokes": strokes, "status": "loaded", "error": None}

    return {"strokes": None, "status": "loading", "error": None}


def strokes_batch(ctx, workouts: list) -> dict:
    """
    Batch stroke fetcher for views that want a single progress bar.

    Returns::

        {
          "by_id":          dict[str, list],  # raw strokes, keyed by str(id)
          "done":           int,               # completed in the current batch
          "total":          int,               # queued at batch start
          "is_loading":     bool,              # True while a fetch is pending
          "uncached_count": int,               # public-mode disk misses
        }

    Owner: fires at most one ``client.get_strokes`` task per render so the
    caller can show honest progress; persists to localStorage and mirrors
    to the public-profile directory if opted in.

    Public: synchronous disk reads; uncached workouts go in ``uncached_count``
    and are absent from ``by_id`` so the caller can exclude them.
    """
    batch_state = hd.state(batch_key="", queue=(), total=0, done=0)

    ids = tuple(
        w.get("id") for w in workouts if w.get("id") and w.get("stroke_data", False)
    )
    batch_key = str(ids)

    if ctx.mode == "public":
        from services import public_profiles

        if batch_key != batch_state.batch_key:
            batch_state.batch_key = batch_key
            batch_state.queue = ()
            batch_state.total = 0
            batch_state.done = 0

        by_id: dict = {}
        uncached = 0
        for wid in ids:
            key = str(wid)
            cached = public_profiles.load_public_strokes(ctx.user_id, wid)
            if cached is None:
                uncached += 1
            else:
                by_id[key] = cached
        return {
            "by_id": by_id,
            "done": 0,
            "total": 0,
            "is_loading": False,
            "uncached_count": uncached,
        }

    # Owner mode
    cache, ready = _load_strokes_cache()
    if not ready:
        return {
            "by_id": {},
            "done": 0,
            "total": 0,
            "is_loading": True,
            "uncached_count": 0,
        }

    if batch_key != batch_state.batch_key:
        missing = tuple(wid for wid in ids if str(wid) not in cache)
        batch_state.queue = missing
        batch_state.total = len(missing)
        batch_state.done = 0
        batch_state.batch_key = batch_key

    # Read the public-toggle once per render.  ``_public_enabled`` wraps
    # ``hd.local_storage.get_item("profile")`` — calling it inside the
    # mirror loop below would register N components with the same key and
    # trip HyperDiv's duplicate-key check.
    public_enabled = _public_enabled()

    if batch_state.queue and ctx.client is not None:
        next_id = batch_state.queue[0]
        with hd.scope(f"batch_fetch_{next_id}"):
            task = hd.task()
            if not task.running and not task.done:
                task.run(
                    lambda nid=next_id: ctx.client.get_strokes(int(ctx.user_id), nid)
                )
            if task.done:
                if task.error:
                    print(f"[strokes_batch] error on {next_id}: {task.error}")
                    batch_state.queue = batch_state.queue[1:]
                    batch_state.done += 1
                else:
                    strokes = task.result if isinstance(task.result, list) else []
                    _persist_strokes(
                        ctx.user_id, next_id, strokes, cache, public_enabled
                    )
                    batch_state.queue = batch_state.queue[1:]
                    batch_state.done += 1

    by_id = {str(wid): cache[str(wid)] for wid in ids if str(wid) in cache}
    # Cache-hit mirror: ensure every workout visible on this page is also
    # present in the public directory when the owner is opted in.  The
    # mirror helper short-circuits on existing files so this is cheap and
    # (critically) performs no ``hd.local_storage`` reads — those were
    # hoisted into ``public_enabled`` above.
    for wid_str, strokes in by_id.items():
        _mirror_strokes_to_public(ctx.user_id, wid_str, strokes, public_enabled)
    return {
        "by_id": by_id,
        "done": batch_state.done,
        "total": batch_state.total,
        "is_loading": bool(batch_state.queue),
        "uncached_count": 0,
    }
