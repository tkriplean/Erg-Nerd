# Profile & Public Profiles

## Overview

Erg Nerd defaults to storing every user's profile and workout data entirely
in **browser localStorage** — nothing leaves the user's machine. The Profile
tab lets a user opt-in to publishing a read-only copy of their data at a
shareable URL so coaches, teammates, or anyone curious can browse the full
dashboard from their perspective.

- **Default: private.** No profile or workout data touches the Erg Nerd server.
- **Opt-in: public.** Profile (minus DOB) + workouts are copied to a
  per-user directory on the server. A shareable URL goes live at
  `/u/{concept2_user_id}`. Switching back to private deletes everything.

## URL scheme

| Path                                                                      | Auth     | Behavior                                          |
|---------------------------------------------------------------------------|----------|---------------------------------------------------|
| `/`, `/sessions`, `/volume`, `/intervals`, `/power_curve`, `/race`, `/profile` | owner    | Authenticated owner dashboard (unchanged)         |
| `/session/{id}`                                                           | owner    | Authenticated session detail (unchanged)          |
| `/oauth/callback`                                                         | —        | Concept2 OAuth landing                            |
| `/u/{uid}`                                                                | none     | Public dashboard → default page (Power Curve)     |
| `/u/{uid}/sessions`, `/volume`, `/intervals`, `/power_curve`, `/race`     | none     | Public dashboard tabs                             |
| `/u/{uid}/session/{sid}`                                                  | none     | Public workout detail (with strokes when cached)  |
| `/u/{uid}/profile`                                                        | none     | 404 — settings page has no public meaning         |
| `/u/{uid}` with no published data                                         | none     | Friendly 404 card                                 |

## Opt-in / opt-out flow

On the Profile tab, a "Public profile" section with a toggle controls
publication:

- **OFF → ON:** Immediately publishes the full snapshot (profile + workouts)
  via `services.public_profiles.publish_all(...)`. The share URL, an
  "Open" link, and the switch flip to "public".
- **ON → OFF:** Opens a confirmation dialog. Accepting calls
  `unpublish(...)`, which deletes `.public_profiles/{uid}/` in its entirety.
- The explainer copy on the toggle makes the storage implication unambiguous
  in both directions ("When public … stored on the server" / "When private
  … none of your profile or workout data is stored on the server").

**Disconnect** also calls `unpublish(...)` as a belt-and-braces cleanup, so
tearing down the OAuth token ensures no published data lingers.

## Fields published

The scrubbed public `profile.json` contains:

| Field          | Source                                                        |
|----------------|---------------------------------------------------------------|
| `schema_version` | constant `2`                                                 |
| `user_id`      | Concept2 numeric user id                                      |
| `display_name` | Concept2 `first_name` (or `username` fallback)                |
| `yob`          | Year of birth (int, 4-digit) — derived from `dob` at publish  |
| `age`          | Snapshot of age at publish time (legacy; v1-compatible read)  |
| `gender`       | "Male" / "Female"                                             |
| `weight`       | kg or lbs (see `weight_unit`)                                 |
| `weight_unit`  | "kg" / "lbs"                                                  |
| `weight_class` | "Heavyweight" / "Lightweight"                                 |
| `max_heart_rate` | int or null                                                 |
| `updated_at`   | server unix timestamp                                         |

**DOB is never written to disk.** It is PII; what we publish is `yob` (year
of birth — roughly the same PII as age, but stable as years pass).  In
public-mode rendering, a synthetic mid-year dob (`{yob}-07-01`) is
reconstructed client-side so downstream code can continue calling
`age_from_dob`.  Schema v1 profiles without `yob` fall back to the
`age`-at-publish snapshot.

Also excluded: email, Concept2 tokens, any internal API fields.

## Storage layout

```
.public_profiles/
  {user_id}/
    profile.json            — scrubbed profile (no DOB)
    workouts.zb64           — base64(zlib(json)) workouts dict; same format
                              as the localStorage "workouts" blob
    strokes/
      {result_id}.json      — per-workout stroke arrays (cache-on-owner-view)
```

Writes go via `_atomic_write_text` (write to sibling tempfile, then
`os.replace`) so concurrent readers never see a partial file.

`.public_profiles/` is in `.gitignore` — never commit.

## Sync triggers (when data is written to the server)

| Trigger                                                   | Action                              |
|-----------------------------------------------------------|-------------------------------------|
| Toggle OFF → ON                                           | `publish_all(uid, profile, dn, workouts)` |
| Toggle ON → OFF (after confirmation)                      | `unpublish(uid)`                    |
| Profile page `_save()` while `public=True`                | `publish_profile(uid, profile, dn)` |
| `concept2_sync()` completes while `profile.public=True`   | `publish_all(uid, profile, dn, workouts)` |
| `workout_page` stroke fetch resolves (owner + public=True)| `publish_strokes(uid, rid, strokes)` |
| `race_page` batch fetcher resolves (owner + public=True)  | `publish_strokes(uid, rid, strokes)` |
| Disconnect button                                         | `unpublish(uid)` + token + localStorage cleanup |

## Stroke data (cache-on-owner-view)

Stroke-level data is **not** eager-prefetched on publish — doing so would
mean many minutes of rate-limited Concept2 API calls for users with long
histories. Instead:

- Whenever the owner's dashboard fetches strokes (opening a workout page,
  or race page batch load), we piggyback a write to
  `.public_profiles/{uid}/strokes/{rid}.json` **if** `profile.public = True`.
- The public viewer reads strokes off disk via `load_public_strokes(...)` —
  no API, no rate limit.
- A public viewer opening a session the owner has never looked at sees
  splits and workout metadata, plus a small inline notice:
  "Stroke-level data for this session is not yet available."
- The public race page excludes uncached workouts from its race queue and
  displays a count: "N workouts not yet available — appears after the owner
  views them."

## Ownership check

Every write in `services/public_profiles.py` calls
`owner_is_authenticated(user_id)`, which checks for the existence of
`.concept2_token_{user_id}.json`. That file can only be created by
completing the Concept2 OAuth flow, so its presence is a proof-of-ownership.
Revoked tokens are cleared by the normal refresh failure path in
`services/concept2.py`, so a revoked token naturally loses write ability
on the next sync attempt.

## Path-traversal guard

`_user_dir(user_id)` calls `Path.resolve()` and asserts the resolved path
stays under `.public_profiles/`. Malicious inputs like `../etc` raise
`ValueError` before touching disk. Stroke paths additionally coerce
`result_id` through `int(...)` to strip non-numeric noise.

## Size limits

| Constant                      | Value  | Protects against                      |
|-------------------------------|--------|---------------------------------------|
| `MAX_WORKOUTS_ZB64_BYTES`     | 20 MB  | pathologically long workout history   |
| `MAX_STROKES_BYTES`           |  2 MB  | corrupt or oversized stroke arrays    |

Uploads exceeding these bounds are rejected (workouts) or silently skipped
with a log line (strokes).

## Edge cases

| #  | Case                                                | Mitigation                                                         |
|----|-----------------------------------------------------|--------------------------------------------------------------------|
| 1  | Disconnect leaves stale public data                 | Disconnect handler calls `unpublish(uid)`                          |
| 2  | Two tabs racing a publish                           | `os.replace` atomic; last write wins; content near-identical       |
| 3  | Oversize upload                                     | 20 MB ceiling pre-write                                            |
| 4  | Corrupt workouts blob                               | `decompress_workouts` round-trip check in publisher                |
| 5  | `/u/{uid}` with no server data                      | 404 card with "Go to Erg Nerd" link                                |
| 6  | Corrupt `profile.json` on disk                      | `load_public_profile` returns `None` → 404 card                    |
| 7  | Synthetic mode                                      | `concept2_sync` never publishes synthetic data (only real workouts) |
| 8  | Owner viewing own `/u/{id}`                         | Banner notes "This is how others see your profile"                 |
| 9  | Public viewer opens session owner hasn't seen       | Splits render; "Stroke-level data not yet available" notice        |
| 10 | Public race page with uncached workouts             | Uncached workouts excluded; count shown in info card               |
| 11 | URL tricks (`/u/../..`, `/u/foo/session/../x`)      | `_user_dir` resolve-guard raises; stroke paths coerce to `int`     |
| 12 | `/u/{uid}/profile`                                  | 404 card — no public settings                                      |
