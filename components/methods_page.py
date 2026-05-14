"""
Methods page — renders the canonical methodology document (docs/methods.md)
inside the app, with a sticky table-of-contents drawer on the left.

The doc is the single source of truth for how every Erg Nerd metric, model,
and concept works.  Five-category provenance legend (Measured / Computed /
Estimated / Population default / Heuristic); each entry covers what the
number estimates, where it came from, where it's used in the app, and its
honest limits.

This module reads the markdown once per Python process at import time and
re-renders it on every page hit.  The TOC is hand-curated below; the anchor
slugs match the ``<a id="…"></a>`` tags placed before each ``##`` heading in
``docs/methods.md``.

No services imports — this is a pure rendering wrapper.
"""

from pathlib import Path

import hyperdiv as hd


_METHODS_MD_PATH = Path(__file__).resolve().parent.parent / "docs" / "methods.md"


def _load_methods_markdown() -> str:
    """Read docs/methods.md once per process.  Re-reads on every render are
    cheap but unnecessary; the doc is static.  Fall back to a clear error
    message if the file is missing so the route doesn't crash the dashboard.
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


# Table of contents.  Each entry is (label, anchor_slug, depth) where depth
# 0 = top-level section, 1 = sub-section.  Anchors must match the
# ``<a id="…"></a>`` tags in docs/methods.md.
_TOC: tuple[tuple[str, str, int], ...] = (
    ("Provenance tags", "provenance-tags", 0),
    ("Reference Watts", "reference-watts", 0),
    ("Predictors", "predictors", 0),
    ("Power-Duration", "power-duration", 1),
    ("Log-Log", "log-log", 1),
    ("Paul's Law", "pauls-law", 1),
    ("RowingLevel", "rowinglevel", 1),
    ("Average", "average", 1),
    ("R² and RMSE", "r2-rmse", 0),
    ("Power zones", "power-zones", 0),
    ("HR zones", "hr-zones", 0),
    ("Severity", "severity", 0),
    ("ESS (Erg Stress Score)", "ess", 0),
    ("Intensity signal I(t)", "intensity", 0),
    ("Stimulus dose", "stimulus-dose", 0),
    ("W' / W'bal", "w-prime", 0),
    ("Glycogen Used", "glycogen-used", 0),
    ("CTL / ATL / TSB", "ctl-atl-tsb", 0),
    ("Interval grid", "interval-grid", 0),
    ("Implied glycogen reserve", "glycogen-reserve", 0),
    ("HRmax estimation", "hrmax-estimation", 0),
    ("Sources", "sources", 0),
)


class _sticky_box(hd.box):
    """``hd.box`` with the ``position`` and ``top`` CSS properties exposed.
    Used to pin the TOC drawer so it stays visible while the main column
    scrolls.  Pattern copied from ``components.hyperdiv_extensions``.
    """

    position = hd.Prop(hd.CSSField("position", hd.String))
    top = hd.Prop(hd.CSSField("top", hd.Size))


def methods_page() -> None:
    """Two-column layout: sticky TOC drawer on the left, markdown content
    on the right.  The TOC anchors jump to ``<a id="…"></a>`` tags placed
    inside the markdown.  Browser handles the hash-link scroll natively;
    HyperDiv's SPA route handler ignores fragment-only navigations.
    """
    with hd.hbox(gap=2, align="start", padding=(1, 0, 4, 0)):
        # ── TOC drawer ───────────────────────────────────────────────────
        with _sticky_box(
            width=16,
            position="sticky",
            top=1,
            gap=0.3,
            padding=(0.5, 1, 0.5, 0),
            font_size="small",
        ):
            hd.text(
                "Contents",
                font_weight="bold",
                font_color="primary",
                padding_bottom=0.5,
            )
            for label, anchor, depth in _TOC:
                with hd.scope(anchor):
                    hd.link(
                        label,
                        href=f"#{anchor}",
                        target="_self",
                        underline=False,
                        font_color="neutral-700",
                        font_size="small",
                        padding=(0.15, 0, 0.15, 1.25 if depth else 0),
                    )

        # ── Content ──────────────────────────────────────────────────────
        with hd.box(max_width=56, width="100%", gap=1):
            hd.markdown(_METHODS_MARKDOWN)
