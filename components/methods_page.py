"""
Methods page — renders the canonical methodology document (docs/methods.md)
inside the app.

The doc is the single source of truth for how every Erg Nerd metric works,
where it came from, and where its honest limits are.  Five-category
provenance legend at the top (Measured / Computed / Estimated / Population
default / Heuristic); per-page sections walk the Power Curve / Volume /
Workout / Intervals / Profile surfaces; a calibration log at the bottom
records constant changes.

This module reads the markdown once per Python process at import time and
re-renders it on every page hit.  No I/O on the hot path.

No services imports — this is a pure rendering wrapper.
"""

from pathlib import Path

import hyperdiv as hd


_METHODS_MD_PATH = Path(__file__).resolve().parent.parent / "docs" / "methods.md"


def _load_methods_markdown() -> str:
    """Read docs/methods.md once per process and cache the contents.

    Re-reading the markdown on every render would be cheap but unnecessary;
    methods.md is a static doc that changes only on deploys.  Fall back to
    a clear error message if the file is missing so the route doesn't crash
    the dashboard.
    """
    try:
        return _METHODS_MD_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "# Methods\n\n"
            "*The methodology document could not be loaded.* "
            f"Expected at `{_METHODS_MD_PATH}`."
        )


_METHODS_MARKDOWN: str = _load_methods_markdown()


def methods_page() -> None:
    """Render the methodology document.

    Constrains the rendered prose to a comfortable reading width so long
    paragraphs don't span the full viewport on wide displays.  The
    enclosing dashboard already provides padding around the box.
    """
    with hd.box(align="center", padding=(1, 0, 4, 0)):
        with hd.box(max_width=60, width="100%", gap=1):
            hd.markdown(_METHODS_MARKDOWN)
