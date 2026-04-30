"""
Shared Concept2-origin data loaders.

Render-top helpers that any Page can call.  Each handles its own lifecycle
(load, fetch, cache, progress/loading UI) so the caller gets the ready data
back or a loading sentinel.

    concept2_sync(client) -> None
        Load, sync, and persist the user's own workout history.  Called once
        per session from ``app.py:_dashboard_view`` (owner mode only); writes
        the resulting ``(workouts_dict, sorted_workouts)`` into
        ``AppContext`` and renders progress / loading UI inline at the
        call site while the API sync runs.  No-op on subsequent renders
        once the snapshot is cached on the context.

    sync_workouts() -> (workouts_dict, sorted_workouts) | None
        Pure read of the cached snapshot from ``AppContext``.  Both modes
        write into the same slots so this helper does not branch on mode.

    get_all_workouts() -> (workouts_dict, filtered_sorted_workouts) | None
        ``sync_workouts()`` + ``GlobalFilters`` (season / machine).

    load_world_record_data(state, profile) -> wr_data | None
        Fetch world-class CP data for the user's (gender, age, weight) bucket.

    strokes_for(workout) -> {'strokes', 'status', 'error'}
        Single-workout stroke fetcher.  Uniform across owner / public modes;
        handles the IndexedDB cache, on-owner-view mirror to the
        public-profile directory, and the "not yet cached" public-mode case.

    strokes_batch(workouts) -> {'by_id', 'done', 'total', 'is_loading',
                                'uncached_count'}
        Queue-with-progress fetcher (race page).  Owner: one-at-a-time API
        fetch; public: synchronous disk reads from .public_profiles.

The mode-aware helpers (``strokes_for``, ``strokes_batch``) read
mode/user_id/client from ``components.app_context.AppContext`` rather than
taking a ``ctx`` argument.

Storage backend
    Owner-mode workouts and strokes live in IndexedDB (see
    ``components.indexed_db``) — a per-record store with 50 MB – 1 GB+
    headroom that replaces the ~5–10 MB localStorage cap and the
    O(N) decompress/recompress cycle the strokes cache used to pay on every
    workout view.  ``mount_indexed_db`` is invoked once per render at the
    top of ``app.py:_dashboard_view`` so all consumers can dispatch ops.

The stroke helpers store **raw** Concept2 API strokes
(``[{t: tenths, d: decimeters, p, spm, hr, …}]``) — workout_page consumes
this shape directly; race_page runs ``normalize_strokes`` at the boundary.
"""

from datetime import datetime

import hyperdiv as hd

from config import SYNTHETIC_MODE
from components import indexed_db
from components.indexed_db import (
    META_STORE,
    SCHEMA_VERSION_KEY,
    STROKES_STORE,
    WORKOUTS_STORE,
)
from services.data_integrity import INTEGRITY_VERSION, normalize_new_workouts
from services.quarantine import write_quarantine
from services.workout_enrichment import enrich_all
from services.concept2_records import (
    age_category as wr_age_category,
    weight_class_str as wr_weight_class_str,
    fetch_wr_data,
)
from services.rowing_utils import age_from_dob, derive_filter_metadata

from services.global_state import GlobalFilters
from components.app_context import AppContext


def _fmt_month_year(date_str: str) -> str:
    """'2019-03-14' → 'Mar 2019'"""
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%b %Y")
    except Exception:
        return date_str[:7]


def get_all_workouts():
    result = sync_workouts()
    if result is None:
        return None
    workouts_dict, all_workouts = result

    # Apply global filters
    gstate = GlobalFilters()
    excluded_seasons = gstate.excluded_seasons
    machine = gstate.machine

    if excluded_seasons:
        all_workouts = [
            w for w in all_workouts if w["season"] not in set(excluded_seasons)
        ]
    if machine != "All":
        all_workouts = [w for w in all_workouts if w.get("type") == machine]

    return workouts_dict, all_workouts


def sync_workouts() -> tuple | None:
    """
    Return (workouts_dict, sorted_workouts) cached on ``AppContext``, or
    ``None`` if the snapshot has not yet been populated.

    Owner mode: ``app.py:_dashboard_view`` calls ``concept2_sync(client)``
    once per session to populate the slots.  Public mode: ``populate_public``
    pre-fills them from ``services.public_profiles``.  Either way this is a
    pure read — no I/O, no sync task.
    """
    ctx = AppContext()
    if ctx.workouts_dict is None:
        return None
    return ctx.workouts_dict, ctx.sorted_workouts


def concept2_sync(client) -> None:
    """
    Load, sync, and persist workout data; write the result into
    ``AppContext.workouts_dict`` / ``sorted_workouts``.  Called once per
    session from ``app.py:_dashboard_view``.  Renders progress / loading UI
    inline at the call site while the API sync runs.  No-op on subsequent
    renders once the snapshot is cached on the context.
    """
    ctx = AppContext()
    if ctx.workouts_dict is not None:
        return  # Already synced this session.

    # ── Step 1: one-time IndexedDB read ──────────────────────────────────────
    sync_state = hd.state(
        written=False,
        initial_workouts=None,
        # Whether the cache we read carried the current INTEGRITY_VERSION.
        # When False the seed is treated as empty for normalization purposes
        # AND we delete the public-profile mirror before republishing so a
        # partial failure can't leave stale-rule data published.
        cache_version_ok=False,
        initial_loaded=False,
        synth_cache=None,
        # Public-profile push-on-sync state. Set once per sync-completion; the
        # publish task reads ``ctx.profile`` and pushes the server snapshot.
        # Bool not timestamp — we only want one push per sync per component
        # render tree.
        published=False,
    )

    if not sync_state.initial_loaded:
        if not indexed_db.is_ready():
            with hd.box(align="center", padding=4):
                hd.spinner()
            return
        # Fire schema-version + getAll in parallel; the IDB queue serialises
        # them on the JS side but both polling handles resolve independently.
        schema_cmd = indexed_db.get(META_STORE, SCHEMA_VERSION_KEY)
        workouts_cmd = indexed_db.get_all(WORKOUTS_STORE)
        if not (schema_cmd.done and workouts_cmd.done):
            with hd.box(align="center", padding=4):
                hd.spinner()
            return

        cached_records = workouts_cmd.result or []
        cached_dict = {item["key"]: item["value"] for item in cached_records}
        cached_version = (
            int(schema_cmd.result) if isinstance(schema_cmd.result, int) else None
        )

        if cached_version == INTEGRITY_VERSION:
            sync_state.initial_workouts = cached_dict
            sync_state.cache_version_ok = True
        else:
            # Missing meta record, pre-IDB cold start, or stale rules: discard
            # so the API does a full pull and every workout passes through the
            # current normalizer.  Strokes are tied to workout shape too, so
            # clear them in lockstep.  The clears are queued behind any
            # outstanding reads and ahead of the upcoming put_many, so order
            # is preserved.
            if cached_dict and cached_version != INTEGRITY_VERSION:
                print(
                    f"[concept2_sync] schema_version "
                    f"{cached_version!r} → {INTEGRITY_VERSION}: "
                    f"discarding {len(cached_dict)} cached workouts"
                )
                indexed_db.clear(WORKOUTS_STORE)
                indexed_db.clear(STROKES_STORE)
            sync_state.initial_workouts = {}
            sync_state.cache_version_ok = False
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

    print("RUNNING WORKOUTS TASK")
    task.run(_fetch, client, sync_state.initial_workouts, progress)

    if not task.done:
        print("STILL FETCHING WORKOUTS")

    # ── Step 3: handle result ────────────────────────────────────────────────
    if task.done and not task.error:
        print("WORKOUTS FETCHED")
        workouts_dict, sorted_workouts = task.result
        # Data-integrity pass: only normalize new/changed entries (those not
        # in the trusted seed) and partition out unmeasured workouts (Rule 5).
        # Workouts in the trusted seed already passed through this exact
        # version of normalize_workout — no need to re-run.
        profile = ctx.profile or {}
        max_hr = profile.get("max_heart_rate")
        trusted = sync_state.initial_workouts if sync_state.cache_version_ok else {}
        workouts_dict, quarantined = normalize_new_workouts(
            workouts_dict, trusted, max_hr=max_hr
        )

        # Persist quarantine sidecar — server-side JSON so the user can
        # inspect held-back workouts directly.
        c2_user_id = getattr(client, "_user_id", "") or ""
        if c2_user_id:
            try:
                write_quarantine(c2_user_id, quarantined)
            except Exception as exc:
                print(f"[concept2_sync] quarantine write failed: {exc}")

        # Persist + public mirror BEFORE enrichment so the canonical
        # normalized shape is what reaches IndexedDB and the public
        # profile directory.  Enrichment fields are derived and re-applied
        # on every load.
        if not sync_state.written:
            if workouts_dict != sync_state.initial_workouts:
                # One transaction per sync — JS does N puts inside a single
                # IDB readwrite txn so the sync write cost is O(1) round-trips.
                indexed_db.put_many(WORKOUTS_STORE, workouts_dict)
                indexed_db.put(META_STORE, SCHEMA_VERSION_KEY, INTEGRITY_VERSION)
            sync_state.written = True

        if not sync_state.published and not SYNTHETIC_MODE and ctx.profile:
            _maybe_push_on_sync(
                client, sync_state, workouts_dict, sync_state.cache_version_ok
            )

        # Detach from the dicts that the queued IDB put_many and the
        # in-flight public-profile push still reference.  enrich_all
        # mutates each workout in place and adds non-JSON-serializable
        # fields (date_dt: datetime.date); persistence must see the
        # un-enriched shape.
        workouts_dict = {k: dict(v) for k, v in workouts_dict.items()}

        # Stage-2 enrichment: attach pace/watts/season/etc. once before
        # workouts reach AppContext.  Mutates in-place.
        enrich_all(workouts_dict)
        sorted_workouts = sorted(
            workouts_dict.values(), key=lambda r: r.get("date", ""), reverse=True
        )

        if SYNTHETIC_MODE:
            if sync_state.synth_cache is None:
                from services.synthetic_data import augment_with_synthetic

                synth_dict, _ = augment_with_synthetic(workouts_dict)
                # augment_with_synthetic returns real workouts (already
                # enriched) plus newly-built synthetic workouts (not).
                # enrich_for_storage is idempotent, so a second pass is safe.
                enrich_all(synth_dict)
                synth_sorted = sorted(
                    synth_dict.values(),
                    key=lambda r: r.get("date", ""),
                    reverse=True,
                )
                sync_state.synth_cache = (synth_dict, synth_sorted)
            ctx.workouts_dict, ctx.sorted_workouts = sync_state.synth_cache
            ctx.all_seasons, ctx.all_machines = derive_filter_metadata(
                ctx.workouts_dict
            )
            return
        ctx.workouts_dict = workouts_dict
        ctx.sorted_workouts = sorted_workouts
        ctx.all_seasons, ctx.all_machines = derive_filter_metadata(workouts_dict)
        return

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
        return

    # Error UI
    if task.error:
        hd.alert(f"Error loading workouts: {task.error}", variant="danger", opened=True)


# ---------------------------------------------------------------------------
# Push-on-sync: mirror the fresh snapshot to the public-profile directory
# ---------------------------------------------------------------------------


def _maybe_push_on_sync(
    client, sync_state, workouts_dict: dict, cache_version_ok: bool
) -> None:
    """
    If the owner has opted in (``profile.public == True``), mirror the freshly
    synced profile + workouts to the server-side public-profile directory.

    When ``cache_version_ok`` is False the localStorage cache was discarded
    due to a data-integrity version bump; the on-disk public mirror may
    still hold stale-rule data, so we delete it first so a partial republish
    failure can't leave wrongly-normalized data published.

    Runs as an ``hd.task`` so the dashboard is not blocked on disk I/O. Flips
    ``sync_state.published`` eagerly so a single sync completion triggers one
    push. Failures are logged to stdout only — a dashboard user should not
    see an error if the public mirror fails.
    """
    from services import public_profiles

    profile = AppContext().profile or {}

    if not profile.get("public"):
        sync_state.published = True  # no-op; don't re-check every render
        return

    # Resolve user_id + display_name from the authenticated client.
    user_id = getattr(client, "_user_id", "") or ""
    if not user_id:
        sync_state.published = True
        return

    push_task = hd.task()

    def _do_push(uid, prof, wkts, force):
        if force:
            try:
                public_profiles.delete_workouts(uid)
            except Exception as exc:
                print(f"[public_profiles] pre-republish delete failed: {exc}")
        # display_name now lives on the profile dict (populated at OAuth /
        # stale refresh), so no extra /users/me call is needed here.
        public_profiles.publish_all(uid, prof, wkts)

    if not push_task.running and not push_task.done:
        push_task.run(_do_push, user_id, profile, workouts_dict, not cache_version_ok)

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

    wr_task = hd.task()
    if not wr_task.running and not wr_task.done:
        wr_task.run(fetch_wr_data, gender_api, age, weight_kg)
        print("STARTING WORLD RECORD CONCEPT2 FETCH")
    elif not wr_task.done:
        print("WORLD RECORD CONCEPT2 FETCH NOT YET DONE")

    if wr_task.done and not state.wr_fetch_done:
        print("WORLD RECORD CONCEPT2 FETCH DONE")

        state.wr_fetch_done = True
        state.wr_data = wr_task.result  # None if API returned nothing

    return state.wr_data


# ---------------------------------------------------------------------------
# Stroke fetching — unified interface for workout_page and race_page
# ---------------------------------------------------------------------------
#
# Cache layout:
#   Persistent:  IndexedDB store ``strokes`` (see components.indexed_db).
#                One record per workout, key = str(workout_id), value = the
#                raw Concept2 strokes list.  Owner-only.
#   Public mirror: ``.public_profiles/{uid}/strokes/{rid}.json`` — written
#                  lazily whenever the owner fetches and ``profile.public``
#                  is True.


def _public_enabled() -> bool:
    """Read ``profile.public`` from ``AppContext``. Synchronous — boot has
    already populated the profile from the combined session LS key."""
    return bool((AppContext().profile or {}).get("public"))


def _mirror_strokes_to_public(
    user_id: str, result_id, strokes: list, public_enabled: bool
) -> None:
    """Cache-on-owner-view mirror.  No-op when the owner is private, the
    strokes list is empty, or the file already exists on disk (cheap
    ``Path.is_file`` check avoids redundant atomic writes when the same
    cached entry is touched across many renders)."""
    if not strokes or not public_enabled:
        return
    try:
        from services import public_profiles

        if public_profiles.has_cached_strokes(user_id, result_id):
            return
        public_profiles.publish_strokes(user_id, result_id, strokes)
    except Exception as exc:
        print(f"[public_profiles] cache-on-view failed: {exc}")


def strokes_for(workout: dict) -> dict:
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
    - ``loading``   : IndexedDB read or API task is in flight.
    - ``loaded``    : ``strokes`` is a list (may be empty if API returned none).
    - ``error``     : the API fetch failed; ``error`` carries the message.
    """
    wid = workout["id"]
    if wid is None or not workout.get("stroke_data"):
        return {"strokes": None, "status": "no_strokes", "error": None}

    ctx = AppContext()
    if ctx.mode == "public":
        from services import public_profiles

        cached = public_profiles.load_public_strokes(ctx.user_id, wid)
        if cached is None:
            return {"strokes": None, "status": "uncached", "error": None}
        return {"strokes": cached, "status": "loaded", "error": None}

    # Owner mode.  Wrap all reads + the fetch task in a per-wid scope so
    # callers can invoke strokes_for(wid=A), strokes_for(wid=B) from the
    # same loop body without colliding on HyperDiv's call-stack-derived
    # component keys.
    with hd.scope(f"strokes_for_{wid}"):
        idb_cmd = indexed_db.get(STROKES_STORE, str(wid))
        if not idb_cmd.done:
            return {"strokes": None, "status": "loading", "error": None}

        public_enabled = _public_enabled()
        cached = idb_cmd.result
        if cached is not None:
            _mirror_strokes_to_public(ctx.user_id, wid, cached, public_enabled)
            return {"strokes": cached, "status": "loaded", "error": None}

        if ctx.client is None:
            return {"strokes": None, "status": "error", "error": "no client"}

        task = hd.task()
        if not task.running and not task.done:
            print(f"FETCHING strokes_for_{wid}")
            task.run(lambda: ctx.client.get_strokes(int(ctx.user_id), wid))
        if task.running:
            return {"strokes": None, "status": "loading", "error": None}
        if task.error:
            return {"strokes": None, "status": "error", "error": str(task.error)}
        if task.done:
            print(f"strokes_for_{wid} DONE")
            strokes = task.result if isinstance(task.result, list) else []
            indexed_db.put(STROKES_STORE, str(wid), strokes)
            _mirror_strokes_to_public(ctx.user_id, wid, strokes, public_enabled)
            # Return the freshly-fetched value directly; the IDB write
            # invalidates the cached read so subsequent renders pick up the
            # same value from storage.
            return {"strokes": strokes, "status": "loaded", "error": None}

        return {"strokes": None, "status": "loading", "error": None}


def strokes_batch(workouts: list) -> dict:
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
    caller can show honest progress; persists each fetched record to
    IndexedDB individually and mirrors to the public-profile directory if
    opted in.

    Public: synchronous disk reads; uncached workouts go in ``uncached_count``
    and are absent from ``by_id`` so the caller can exclude them.
    """
    batch_state = hd.state(batch_key="", queue=(), total=0, done=0, by_id_snapshot={})

    ctx = AppContext()
    ids = tuple(w["id"] for w in workouts if w["id"] and w.get("stroke_data", False))
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

    # Owner mode — one batched IDB read per render covers every requested id.
    id_strs = [str(wid) for wid in ids]
    idb_cmd = indexed_db.get_many(STROKES_STORE, id_strs) if id_strs else None
    if idb_cmd is not None and not idb_cmd.done:
        # IDB read in flight (a fresh request fires after each per-workout
        # write, since writes invalidate the store's cached reads).  Serve
        # the last snapshot + progress so callers see monotonic advancement
        # instead of a flicker to 0/0 between every fetch.
        return {
            "by_id": dict(batch_state.by_id_snapshot),
            "done": batch_state.done,
            "total": batch_state.total,
            "is_loading": True,
            "uncached_count": 0,
        }

    cache_dict = (idb_cmd.result or {}) if idb_cmd is not None else {}

    if batch_key != batch_state.batch_key:
        missing = tuple(wid for wid in ids if str(wid) not in cache_dict)
        batch_state.queue = missing
        batch_state.total = len(missing)
        batch_state.done = 0
        batch_state.batch_key = batch_key

    public_enabled = _public_enabled()

    if batch_state.queue and ctx.client is not None:
        next_id = batch_state.queue[0]
        task = hd.task()
        if not task.running and not task.done:
            print("STILL FETCHING STROKE CACHE")
            task.run(lambda nid=next_id: ctx.client.get_strokes(int(ctx.user_id), nid))
        if task.done:
            print("STROKE CACHE FETCHED")
            if task.error:
                print(f"[strokes_batch] error on {next_id}: {task.error}")
                batch_state.queue = batch_state.queue[1:]
                batch_state.done += 1
            else:
                strokes = task.result if isinstance(task.result, list) else []
                indexed_db.put(STROKES_STORE, str(next_id), strokes)
                _mirror_strokes_to_public(ctx.user_id, next_id, strokes, public_enabled)
                # Reflect the fresh entry in the local view so the snapshot
                # below carries it forward across the IDB cache invalidation
                # that put() just triggered.
                cache_dict[str(next_id)] = strokes
                batch_state.queue = batch_state.queue[1:]
                batch_state.done += 1

    by_id = {str(wid): cache_dict[str(wid)] for wid in ids if str(wid) in cache_dict}
    # Snapshot so the next render can serve the same view while the IDB
    # re-read is in flight (every put() invalidates the store's cached reads
    # for one render tick).
    batch_state.by_id_snapshot = by_id
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
