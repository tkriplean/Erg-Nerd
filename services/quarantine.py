"""
services/quarantine.py

Sidecar storage for workouts that fail the data-integrity quarantine pass
(``services.data_integrity.normalize_new_workouts`` Rule 5 — workouts with
no derivable pace).

The clean workouts dict that reaches ``AppContext`` and localStorage never
includes these entries.  Persisting them to a server-side JSON file lets
the user inspect them directly:

    .quarantined_workouts_{user_id}.json

Each value carries a ``_quarantine_reason`` string so a quick ``grep`` /
``jq`` shows why each workout was held back.  The file is rewritten on
every sync so it always reflects the current quarantine state.

Public API
    write_quarantine(user_id, quarantined_dict) -> None
    read_quarantine(user_id) -> dict
    quarantine_path(user_id) -> Path
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


_QUARANTINE_FILENAME_TPL = ".quarantined_workouts_{user_id}.json"


def quarantine_path(user_id: str) -> Path:
    """Filesystem path to the sidecar JSON for ``user_id``."""
    return Path.cwd() / _QUARANTINE_FILENAME_TPL.format(user_id=str(user_id))


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


def write_quarantine(user_id: str, quarantined: dict) -> None:
    """Rewrite the sidecar with the current quarantine state.

    No-op when ``quarantined`` is empty AND no file exists yet (avoids
    creating empty sidecars for users with clean data).  When the dict is
    empty but a file exists, we still write so the file accurately reflects
    "currently no quarantined workouts".
    """
    path = quarantine_path(user_id)
    if not quarantined and not path.exists():
        return
    payload = json.dumps(quarantined, indent=2, default=str)
    _atomic_write_text(path, payload)


def read_quarantine(user_id: str) -> dict:
    """Return the current sidecar contents, or ``{}`` if missing/invalid."""
    path = quarantine_path(user_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
