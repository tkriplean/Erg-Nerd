"""
Inline histogram SVG renderer for the Rank Page.

Renders a compact bar-chart histogram of the ranking pool's watts distribution
with a dashed vertical line marking the user's own watts. Returns a
``data:image/svg+xml;base64,…`` URI suitable for ``hd.image(src=…)``.

Shape-only — no axes, ticks, gridlines, or labels. One series, one muted
fill color, one dashed reference line.
"""

from __future__ import annotations

import base64


def distribution_svg(
    bin_counts: list[int],
    user_watts: float,
    min_watts: float,
    max_watts: float,
    *,
    width: int = 140,
    height: int = 32,
    is_dark: bool = False,
) -> str:
    """Return a base64 SVG data URI for the histogram + user marker.

    * ``bin_counts`` — non-negative integer counts, evenly spaced from
      ``min_watts`` to ``max_watts``.
    * ``user_watts`` — drawn as a dashed vertical line. If outside
      [min_watts, max_watts], the line clamps to the nearest edge.
    * Empty histograms render as a single placeholder bar.
    """
    fill = "rgba(148,163,184,0.55)" if is_dark else "rgba(148,163,184,0.7)"
    stroke = "rgba(239,68,68,0.95)" if is_dark else "rgba(220,38,38,0.95)"

    nbins = len(bin_counts)
    rects: list[str] = []
    max_count = max(bin_counts) if bin_counts else 0

    if nbins > 0 and max_count > 0 and max_watts > min_watts:
        bar_w = width / nbins
        for i, c in enumerate(bin_counts):
            if c <= 0:
                continue
            h = (c / max_count) * (height - 2)
            if h < 1:
                h = 1
            x = i * bar_w
            y = height - h
            rects.append(
                f'<rect x="{x:.2f}" y="{y:.2f}"'
                f' width="{bar_w:.2f}" height="{h:.2f}"'
                f' fill="{fill}"/>'
            )
    else:
        rects.append(
            f'<rect x="0" y="{height - 2}" width="{width}" height="2" fill="{fill}"/>'
        )

    # User marker.
    marker = ""
    if max_watts > min_watts:
        uw = max(min_watts, min(max_watts, user_watts))
        x = (uw - min_watts) / (max_watts - min_watts) * width
        marker = (
            f'<line x1="{x:.2f}" y1="0" x2="{x:.2f}" y2="{height}"'
            f' stroke="{stroke}" stroke-width="1.5" stroke-dasharray="3,2"/>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f' viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}">'
        + "".join(rects)
        + marker
        + "</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
