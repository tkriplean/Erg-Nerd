"""
Server-side storage for opt-in public profiles.

Users can toggle their profile to "public" in the Profile tab.  When ON, a
scrubbed copy of the profile + the full workout history is written to
``.public_profiles/{user_id}/`` so that anyone with the link
``/u/{user_id}`` can browse the dashboard.  Toggling back to private deletes
the directory.

Layout on disk
--------------
    .public_profiles/
      {user_id}/
        profile.json            — scrubbed profile (no DOB)
        workouts.zb64           — compressed workouts (same format as
                                  localStorage "workouts")
        strokes/
          {result_id}.json      — per-workout stroke arrays, cached
                                  on-owner-view (not eager-prefetched)

Scrubbing rules
---------------
- ``dob`` is the only PII never written to disk.  ``age`` is computed from
  ``dob`` via ``services.rowing_utils.age_from_dob`` at publish time and
  stored instead.
- Email and Concept2-internal fields are never included.

Ownership check
---------------
All write/delete entry points call ``owner_is_authenticated(user_id)``,
which returns True iff ``.concept2_token_{user_id}.json`` exists.  The only
way to obtain that file is to complete the Concept2 OAuth flow, so its
presence is a proof of ownership.  This is belt-and-braces: the app is a
single-process HyperDiv server with no external REST endpoints, so the path
from UI → publish is entirely in-process.

Concurrency
-----------
Writes use ``_atomic_write_text`` — write-to-sibling-tempfile, then
``os.replace`` — so readers never see a half-written file.  Two owner tabs
writing simultaneously will race; the last write wins, and since both tabs
compute from the same localStorage the content is near-identical.  No
locking.

Size limits
-----------
- ``MAX_WORKOUTS_ZB64_BYTES`` = 40 MB compressed (covers ~400k workouts).
- ``MAX_STROKES_BYTES``        =  5 MB per workout (covers 100ks).
Writes exceeding these bounds are rejected to keep disk usage bounded.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from services.concept2 import load_token
from services.local_storage_compression import compress_workouts, decompress_workouts
from services.rowing_utils import age_from_dob


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_BASE = (_ROOT / ".public_profiles").resolve()

MAX_WORKOUTS_ZB64_BYTES = 40 * 1024 * 1024  # 20 MB compressed ceiling
MAX_STROKES_BYTES = 5 * 1024 * 1024  # 2 MB per workout ceiling

# Schema v2: stores ``yob`` (year of birth) rather than a frozen ``age`` so
# the derived age stays correct as years pass.  ``age`` is still written for
# convenience / backwards compat readers, but yob is authoritative.
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Path helpers (with traversal guard)
# ---------------------------------------------------------------------------


def _user_dir(user_id: str) -> Path:
    """
    Resolve the per-user directory path and assert the result stays under
    ``_BASE``.  Prevents ``user_id='../etc'`` from escaping the sandbox.
    """
    if not user_id or "/" in user_id or "\\" in user_id:
        raise ValueError(f"Invalid user_id: {user_id!r}")
    candidate = (_BASE / user_id).resolve()
    try:
        candidate.relative_to(_BASE)
    except ValueError:
        raise ValueError(f"user_id escapes sandbox: {user_id!r}")
    return candidate


def _profile_path(user_id: str) -> Path:
    return _user_dir(user_id) / "profile.json"


def _workouts_path(user_id: str) -> Path:
    return _user_dir(user_id) / "workouts.zb64"


def _strokes_dir(user_id: str) -> Path:
    return _user_dir(user_id) / "strokes"


def _strokes_path(user_id: str, result_id) -> Path:
    # Coerce to int to strip any non-numeric noise from the caller.
    rid = int(result_id)
    return _strokes_dir(user_id) / f"{rid}.json"


# ---------------------------------------------------------------------------
# Ownership gate
# ---------------------------------------------------------------------------


def owner_is_authenticated(user_id: str) -> bool:
    """True iff a valid ``.concept2_token_{user_id}.json`` exists."""
    return load_token(user_id) is not None


# ---------------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, data: str) -> None:
    """Write to a sibling tempfile, then os.replace into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Profile scrubbing
# ---------------------------------------------------------------------------


def _scrub_profile(user_id: str, profile: dict, display_name: str) -> dict:
    """
    Narrow a raw localStorage profile dict to the public-safe schema.
    This is the ONLY place in the module that reads ``profile['dob']``.

    Stores ``yob`` (year-of-birth, int) so the derived age stays accurate as
    time passes.  ``age`` is also written as a convenience snapshot at publish
    time — roughly the same PII as yob — but readers should prefer yob.
    """
    dob = profile.get("dob", "") or ""
    yob: Optional[int] = None
    if len(dob) >= 4 and dob[:4].isdigit():
        try:
            yob = int(dob[:4])
        except ValueError:
            yob = None
    age = age_from_dob(dob)
    weight_raw = profile.get("weight") or 0.0
    try:
        weight = float(weight_raw)
    except (TypeError, ValueError):
        weight = 0.0
    mhr = profile.get("max_heart_rate")
    if mhr is not None:
        try:
            mhr = int(mhr)
        except (TypeError, ValueError):
            mhr = None
    return {
        "schema_version": SCHEMA_VERSION,
        "user_id": str(user_id),
        "display_name": display_name or "Rower",
        "yob": yob,
        "age": age if age > 0 else None,  # snapshot at publish time (legacy)
        "gender": profile.get("gender", "") or "",
        "weight": weight,
        "weight_unit": profile.get("weight_unit", "kg") or "kg",
        "weight_class": profile.get("weight_class", "") or "",
        "max_heart_rate": mhr,
        "updated_at": time.time(),
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def publish_profile(user_id: str, profile: dict, display_name: str) -> None:
    """Write the scrubbed profile.json.  Requires owner authentication."""
    if not owner_is_authenticated(user_id):
        raise PermissionError(f"No token on file for user_id={user_id!r}")
    scrubbed = _scrub_profile(user_id, profile, display_name)
    _atomic_write_text(_profile_path(user_id), json.dumps(scrubbed, indent=2))


def publish_workouts(user_id: str, workouts_dict: dict) -> None:
    """
    Compress + validate + write workouts.zb64.
    Requires owner authentication.  Rejects oversize or malformed data.
    """
    if not owner_is_authenticated(user_id):
        raise PermissionError(f"No token on file for user_id={user_id!r}")
    if not isinstance(workouts_dict, dict) or not workouts_dict:
        raise ValueError("workouts_dict is empty or not a dict")
    encoded = compress_workouts(workouts_dict)
    size = len(encoded.encode("utf-8"))
    if size > MAX_WORKOUTS_ZB64_BYTES:
        raise ValueError(
            f"Compressed workouts ({size} bytes) exceeds "
            f"{MAX_WORKOUTS_ZB64_BYTES}-byte ceiling"
        )
    # Round-trip sanity: reject garbage that would render empty.
    if not decompress_workouts(encoded):
        raise ValueError("Compressed workouts round-tripped to empty dict")
    _atomic_write_text(_workouts_path(user_id), encoded)


def publish_all(
    user_id: str, profile: dict, display_name: str, workouts_dict: dict
) -> None:
    """Convenience: publish_profile + publish_workouts in sequence."""
    publish_profile(user_id, profile, display_name)
    publish_workouts(user_id, workouts_dict)


def publish_strokes(user_id: str, result_id, strokes: list) -> None:
    """
    Cache-on-owner-view: write a single workout's stroke array.
    Silently skips (no raise) on empty, oversize, or un-authenticated owner.
    """
    if not strokes:
        return
    if not owner_is_authenticated(user_id):
        return
    try:
        payload = json.dumps(strokes)
    except (TypeError, ValueError):
        return
    if len(payload.encode("utf-8")) > MAX_STROKES_BYTES:
        print(
            f"[public_profiles] strokes for user={user_id} result={result_id} "
            f"exceed {MAX_STROKES_BYTES}-byte ceiling; skipping."
        )
        return
    try:
        _atomic_write_text(_strokes_path(user_id, result_id), payload)
    except Exception as exc:
        print(f"[public_profiles] failed to write strokes: {exc}")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def exists(user_id: str) -> bool:
    """True iff both profile.json and workouts.zb64 are present."""
    try:
        return _profile_path(user_id).is_file() and _workouts_path(user_id).is_file()
    except ValueError:
        return False


def load_public_profile(user_id: str) -> Optional[dict]:
    try:
        return json.loads(_profile_path(user_id).read_text())
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def load_public_workouts(user_id: str) -> Optional[dict]:
    try:
        encoded = _workouts_path(user_id).read_text()
    except (FileNotFoundError, ValueError):
        return None
    decoded = decompress_workouts(encoded)
    return decoded or None


def load_public_strokes(user_id: str, result_id) -> Optional[list]:
    try:
        return json.loads(_strokes_path(user_id, result_id).read_text())
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def has_cached_strokes(user_id: str, result_id) -> bool:
    try:
        return _strokes_path(user_id, result_id).is_file()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def unpublish(user_id: str) -> None:
    """Delete the user's public directory.  Idempotent."""
    try:
        d = _user_dir(user_id)
    except ValueError:
        return
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
