"""
Shared HyperDiv component subclasses used across multiple tabs.

Subclassing hd components is HyperDiv's supported way to add CSS properties
that the base component doesn't expose as props. The subclass name must differ
from the base class name; the new prop is forwarded to Shoelace as expected.

Exports:
    radio_group     — hd.radio_group + `size` prop ("small" | "medium" | "large")
    shadowed_box    — hd.box + `box_shadow` CSS prop
    aligned_button  — hd.button + `align` CSS prop (align-items)
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
