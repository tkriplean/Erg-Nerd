"""
Shared HyperDiv component subclasses used across multiple tabs.

Subclassing hd components is HyperDiv's supported way to add CSS properties
that the base component doesn't expose as props. The subclass name must differ
from the base class name; the new prop is forwarded to Shoelace as expected.

Exports:
    radio_group     — hd.radio_group + `size` prop ("small" | "medium" | "large")
    shadowed_box    — hd.box + `box_shadow` CSS prop
    aligned_button  — hd.button + `align` CSS prop (align-items)
    grid_box        — hd.box with CSS Grid layout (display:grid + grid props)
"""

import hyperdiv as hd


class radio_group(hd.radio_group):
    """hd.radio_group with an explicit `size` prop (default "medium")."""

    size = hd.Prop(hd.OneOf("small", "medium", "large"), "medium")


class shadowed_box(hd.box):
    """hd.box with a `box_shadow` CSS prop."""

    box_shadow = hd.Prop(hd.CSSField("box-shadow", hd.String))


class aligned_button(hd.button):
    """hd.button with an `align` CSS prop (maps to align-items)."""

    align = hd.Prop(hd.CSSField("align-items", hd.String))


class grid_box(hd.box):
    """
    hd.box with CSS Grid layout.

    Sets display:grid via an inline style, which overrides the .box class's
    display:flex.  Inherits all Styled props (padding, border, width, etc.)
    and the gap prop from Boxy (which works identically for CSS Grid).

    Key props added here:
        grid_template_columns  — CSS grid-template-columns string
                                 e.g. "10rem 7rem minmax(8rem,1fr) 2.5rem"
        grid_template_rows     — CSS grid-template-rows string
        column_gap             — gap between columns (Size units)
        row_gap                — gap between rows (Size units)
        auto_flow              — CSS grid-auto-flow string
        justify_items          — CSS justify-items string
        grid_column            — CSS grid-column string (for spanning children)
    """

    display = hd.Prop(hd.CSSField("display", hd.String), "grid")
    grid_template_columns = hd.Prop(hd.CSSField("grid-template-columns", hd.String))
    grid_template_rows = hd.Prop(hd.CSSField("grid-template-rows", hd.String))
    column_gap = hd.Prop(hd.CSSField("column-gap", hd.Size))
    row_gap = hd.Prop(hd.CSSField("row-gap", hd.Size))
    auto_flow = hd.Prop(hd.CSSField("grid-auto-flow", hd.String))
    justify_items = hd.Prop(hd.CSSField("justify-items", hd.String))
    grid_column = hd.Prop(hd.CSSField("grid-column", hd.String))
    overflow = hd.Prop(hd.CSSField("overflow", hd.String))
