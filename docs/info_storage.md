# Information Storage Design

## Overview

Erg Nerd is a multi-user app where each browser session represents a distinct Concept2 account. All per-user data must be fully isolated. The design splits storage across two locations:

| Data | Location | Rationale |
|---|---|---|
| OAuth token | Server-side file | Never leaves the server; no CORS risk; easy to revoke/refresh |
| Workouts | Browser localStorage | Per-user by nature; large but compressible; no server storage needed |
| Profile (age, weight, gender, max HR) | Browser localStorage | Purely personal; never needs server access |
| Concept2 user ID | Browser localStorage | Tiny string; tells the server which token file to load |
| RowingLevel predictions cache | Server-side file | Keyed by inputs (not user ID); safe to share across users |

---

## OAuth Tokens (Server-Side)

Tokens are stored as JSON files in the project root, one per user:

```
.concept2_token_{user_id}.json   (e.g. .concept2_token_988721.json)
```

The Concept2 numeric user ID is the key because it is stable, unique, and available immediately after the OAuth exchange (via `GET /api/users/me`).

### OAuth callback flow

1. `exchange_code(code)` returns a token dict (access + refresh token). It does **not** write to disk.
2. The callback task builds a temporary `Concept2Client` from the access token.
3. `client.get_user()["data"]["id"]` returns the numeric user ID.
4. `save_token(token_data, user_id)` writes `.concept2_token_{user_id}.json`.
5. `app_state.pending_user_id = user_id` is set on the session-scoped HyperDiv state.
6. On the next render frame, `hd.local_storage.set_item("c2_user_id", user_id)` writes the ID to the browser, and `pending_user_id` is cleared.

Token refresh works transparently — `_refresh_token(token_data, user_id)` receives the same `user_id` and saves the new token to the same file.

### Isolation guarantee

Each browser session loads its own token via `get_client(user_id)`, which reads the user-specific file. No shared mutable state exists between simultaneous users.

---

## Browser localStorage

HyperDiv exposes `hd.local_storage` for async key/value storage in the browser. All values are strings; reads return an `async_command` that requires a `.done` check before use.

### Keys

| Key | Format | Written by |
|---|---|---|
| `c2_user_id` | Plain string, e.g. `"988721"` | `app.py` after OAuth |
| `workouts` | zlib + base64 compressed JSON string | Each tab after sync |
| `profile` | JSON string | `profile_page.py` and `volume_page.py` (HR field) |

### `c2_user_id`

A ~6-character string containing the Concept2 numeric user ID. It is the bridge between the browser and the server-side token file. On disconnect it is cleared along with all other localStorage keys.

### `workouts`

The full workout history dict is compressed before storage using zlib (level 6) + base64:

```python
def compress_workouts(workouts_dict: dict) -> str:
    raw = json.dumps(workouts_dict).encode()
    return base64.b64encode(zlib.compress(raw, level=6)).decode()

def decompress_workouts(stored: str) -> dict:
    return json.loads(zlib.decompress(base64.b64decode(stored)))
```

Workout JSON is highly compressible because keys (`"date"`, `"distance"`, `"workout_type"`, etc.) and values (`"JustRow"`, `"ErgData iOS"`, timezone strings) repeat thousands of times.

**Compression ratio**: ~7× in practice.

| Scenario | Raw JSON | Compressed | Fits in 5 MB (Safari)? |
|---|---|---|---|
| ~2,000 workouts | ~1.9 MB | ~280 KB | ✅ |
| ~20,000 workouts (10× user) | ~19 MB | ~2.7 MB | ✅ |

**Server CPU cost**: `zlib.compress()` on ~2 MB takes ~10 ms; decompression ~2 ms. This runs once per sync, infrequently. Negligible even at 50 simultaneous users.

#### Sync pattern (each tab)

Each tab uses a `sync_state = hd.state(written=False, initial_workouts=None, initial_loaded=False)` guard to avoid infinite re-render loops:

1. On first render: read `workouts` from localStorage (`initial_loaded=True` prevents re-read).
2. Background task calls `client.get_all_results(initial_workouts)`, which returns `(updated_dict, sorted_list)`.
3. On task completion: write compressed blob back to localStorage once (`written=True` prevents repeated writes).

The write happens in the render frame (not the task), which is required because `hd.local_storage.set_item()` cannot be called from a background task.

#### Per-session stroke data

Full stroke-by-stroke data (~50 bytes × 600 strokes per workout) is **not** cached in localStorage — even with 7× compression, 2,000 workouts would produce ~50 MB. Stroke data is fetched on-demand from the Concept2 API when a user views a specific session.

### `profile`

A JSON object persisted immediately on every field change:

```json
{
  "gender": "Male",
  "age": 35,
  "weight": 75.0,
  "weight_unit": "kg",
  "max_heart_rate": 185
}
```

Defaults (from `components/profile_page._PROFILE_DEFAULTS`) are applied when the key is absent or unparseable.

---

## RowingLevel Predictions Cache (Server-Side)

Prediction results from rowinglevel.com are cached in `.rowinglevel_cache.json`, keyed by the combination of inputs (age, weight, gender, reference distance, reference time). This cache is **not** user-specific — two users with identical profiles share the same cached result. No changes were needed for multi-user support.

---

## Disconnect

When the user disconnects:

```python
clear_token(user_id)                         # deletes .concept2_token_{user_id}.json
hd.local_storage.remove_item("c2_user_id")
hd.local_storage.remove_item("workouts")
hd.local_storage.remove_item("profile")
```

Other users' token files and localStorage data are unaffected.

---

## What Happens if the Browser is Cleared

If a user clears their browser's localStorage (or uses a new browser):

- `c2_user_id` is gone → app shows the login screen.
- `workouts` and `profile` are gone → re-populated on next login and sync.
- The server-side token file (`.concept2_token_{user_id}.json`) is **still present** — the user does not need to re-authorize with Concept2 as long as they log back in with the same account; `c2_user_id` is restored after the first successful OAuth handshake.
