"""
Server-side storage for per-workout time-of-day overrides.

Concept2 lets users add workouts to their logbook manually (for sessions
done away from the rower).  Manually-added workouts come back from the
Concept2 API with their ``date`` field's time component set to
``00:00:00`` — there's no real time-of-day, so the workout shows up at
"midnight" and never clusters with other workouts on the same day.

This module lets the user override the time-of-day on a per-workout
basis.  Overrides are keyed by ``str(workout_id)`` and live outside
``.public_profiles/`` so they're available regardless of whether the
user has opted into a public profile.

Layout on disk::

    .user_overrides/
      {user_id}/
        time_overrides.json     — {"<workout_id>": "HH:MM:SS", ...}

Why a separate file (not folded into ``.public_profiles``)
---------------------------------------------------------
Public viewers don't read this file directly — by the time the owner
has set an override, it's already baked into ``workouts.zb64`` (see
``apply_overrides`` + the sync wiring in
``components.concept2_sync``).  The overrides file is a private record
of user intent that survives Concept2 re-syncs and IndexedDB clears.

Ownership gate mirrors ``services.public_profiles``: every write requires
``owner_is_authenticated(user_id)`` (i.e. a valid OAuth token on file).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from services.concept2 import load_token


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_BASE = (_ROOT / ".user_overrides").resolve()

_OVERRIDES_FILENAME = "time_overrides.json"

#: A workout's ``date`` field ends with this suffix iff Concept2 stored no
#: time-of-day for it (i.e. it was added manually).
_NO_TOD_SUFFIX = " 00:00:00"


# ---------------------------------------------------------------------------
# Path helpers (with traversal guard)
# ---------------------------------------------------------------------------


def _user_dir(user_id: str) -> Path:
    """Resolve the per-user directory and assert the result stays under
    ``_BASE``.  Prevents ``user_id='../etc'`` from escaping the sandbox."""
    if not user_id or "/" in user_id or "\\" in user_id:
        raise ValueError(f"Invalid user_id: {user_id!r}")
    candidate = (_BASE / user_id).resolve()
    try:
        candidate.relative_to(_BASE)
    except ValueError:
        raise ValueError(f"user_id escapes sandbox: {user_id!r}")
    return candidate


def _overrides_path(user_id: str) -> Path:
    return _user_dir(user_id) / _OVERRIDES_FILENAME


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
    """Write to a sibling tempfile, then ``os.replace`` into place."""
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
# Time-input parser
# ---------------------------------------------------------------------------

#: Accepts ``9:30 AM``, ``9:30:15 PM``, ``09:30``, ``21:30:15``.  Trailing
#: meridiem is optional; when absent the input is treated as 24-hour.
_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))(?::(\d{2}))?\s*([AaPp][Mm]?)?\s*$"
)


def parse_time_input(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse a free-form time string into ``HH:MM:SS`` (24-hour).

    Accepted shapes::

        "9:30"          → "09:30:00"
        "9:30 AM"       → "09:30:00"
        "9:30 PM"       → "21:30:00"
        "21:30"         → "21:30:00"
        "9:30:15 PM"    → "21:30:15"

    Returns ``(hhmmss, None)`` on success, ``(None, error_message)`` on
    failure.  Error messages are user-facing.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Enter a time."
    m = _TIME_RE.match(raw)
    if not m:
        return None, 'Use "H:MM AM" or "HH:MM:SS".'
    h_s, mn_s, sec_s, ampm = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        h = int(h_s)
        mn = int(mn_s)
        sec = int(sec_s) if sec_s is not None else 0
    except ValueError:
        return None, f'Could not parse "{raw}".'
    if mn < 0 or mn >= 60 or sec < 0 or sec >= 60:
        return None, f'"{raw}" is out of range.'
    if ampm:
        ampm_l = ampm.lower()
        if h < 1 or h > 12:
            return None, f'"{raw}" is out of range.'
        if ampm_l.startswith("p") and h != 12:
            h += 12
        elif ampm_l.startswith("a") and h == 12:
            h = 0
    else:
        if h < 0 or h >= 24:
            return None, f'"{raw}" is out of range.'
    return f"{h:02d}:{mn:02d}:{sec:02d}", None


# ---------------------------------------------------------------------------
# Load / save / clear
# ---------------------------------------------------------------------------


def load_overrides(user_id: str) -> dict:
    """Return ``{workout_id_str: "HH:MM:SS"}``.  Empty dict on missing file
    or unparseable JSON."""
    try:
        path = _overrides_path(user_id)
    except ValueError:
        return {}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) == 8 and v[2] == ":" and v[5] == ":":
            out[str(k)] = v
    return out


def save_override(user_id: str, workout_id, hhmmss: str) -> None:
    """Set the override for ``workout_id`` to ``hhmmss`` (``HH:MM:SS``).

    Requires owner authentication.  Atomic: the file is rewritten via a
    sibling tempfile + ``os.replace``.
    """
    if not owner_is_authenticated(user_id):
        raise PermissionError(f"No token on file for user_id={user_id!r}")
    if not (
        isinstance(hhmmss, str)
        and len(hhmmss) == 8
        and hhmmss[2] == ":"
        and hhmmss[5] == ":"
    ):
        raise ValueError(f"hhmmss must be 'HH:MM:SS', got {hhmmss!r}")
    overrides = load_overrides(user_id)
    overrides[str(workout_id)] = hhmmss
    _atomic_write_text(_overrides_path(user_id), json.dumps(overrides, indent=2))


def clear_override(user_id: str, workout_id) -> None:
    """Remove the override for ``workout_id``, if present.  No-op if the
    workout was never overridden.  Requires owner authentication."""
    if not owner_is_authenticated(user_id):
        raise PermissionError(f"No token on file for user_id={user_id!r}")
    overrides = load_overrides(user_id)
    key = str(workout_id)
    if key not in overrides:
        return
    del overrides[key]
    _atomic_write_text(_overrides_path(user_id), json.dumps(overrides, indent=2))


# ---------------------------------------------------------------------------
# Apply (mutate workout dict in place)
# ---------------------------------------------------------------------------


def apply_overrides(workouts_dict: dict, overrides: dict) -> int:
    """Mutate ``workouts_dict[wid]['date']`` in place to swap the
    ``00:00:00`` time component for the overridden ``HH:MM:SS``.

    Idempotent: applying the same override twice is a no-op.  Workouts
    whose ``date`` no longer ends with ``00:00:00`` (e.g. the owner has
    edited them in the Concept2 logbook to carry a real time, or a
    previous run already applied the override) are skipped.

    Returns the number of workouts mutated.
    """
    if not overrides:
        return 0
    applied = 0
    for wid, hhmmss in overrides.items():
        w = workouts_dict.get(str(wid))
        if w is None:
            continue
        date = w.get("date") or ""
        if not date.endswith(_NO_TOD_SUFFIX):
            continue
        w["date"] = date[: -len(_NO_TOD_SUFFIX)] + " " + hhmmss
        applied += 1
    return applied
