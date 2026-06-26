"""A hand-authored 60×40 ft barndominium, written in the embedded DSL.

Run directly to validate it and write an SVG::

    python examples/simple_barndo.py

It demonstrates the fluent API and is the plan used by ``barndsl demo``.
"""

from __future__ import annotations

from barndsl import Direction as D
from barndsl import RoomType as T
from barndsl import barndominium


def build_example():
    """A 3-bed / 1-bath open-concept barndo with an attached shop bay."""
    return (
        barndominium("Cedar Ridge Barndominium")
        .envelope(width=60, length=40)
        .ceiling(12)
        .note("3 bed / 1 bath, open-concept living with a 14′ shop bay.")
        # --- Open living core (south-west) -------------------------------
        .add_room("great_room", T.LIVING, x=0, y=0, width=28, length=26)
        .add_room("dining", T.DINING, x=28, y=0, width=18, length=14)
        .add_room("kitchen", T.KITCHEN, x=28, y=14, width=18, length=12)
        # --- Shop / garage bay (full-height east) ------------------------
        .add_room("shop", T.SHOP, x=46, y=0, width=14, length=40)
        # --- Hallway spine ----------------------------------------------
        .add_room("hall", T.HALLWAY, x=0, y=26, width=46, length=3)
        # --- Bedrooms + bath (north) ------------------------------------
        .add_room("master_bed", T.BEDROOM, x=0, y=29, width=16, length=11)
        .add_room("bed_2", T.BEDROOM, x=16, y=29, width=12, length=11)
        .add_room("bed_3", T.BEDROOM, x=28, y=29, width=10, length=11)
        .add_room("bath", T.BATHROOM, x=38, y=29, width=8, length=11)
        # --- Interior circulation ---------------------------------------
        .connect("great_room", "dining", width=10)
        .connect("dining", "kitchen", width=8)
        .connect("great_room", "hall", width=4)
        .connect("hall", "master_bed", width=2.67)
        .connect("hall", "bed_2", width=2.67)
        .connect("hall", "bed_3", width=2.67)
        .connect("hall", "bath", width=2.67)
        # --- Entrances --------------------------------------------------
        .entrance("great_room", D.SOUTH, width=3, offset=20)
        .entrance("shop", D.SOUTH, width=10, offset=2, egress=False)
        # --- Glazing ----------------------------------------------------
        .add_window("great_room", D.WEST, width=6, offset=4)
        .add_window("great_room", D.WEST, width=6, offset=16)
        .add_window("great_room", D.SOUTH, width=8, offset=8)
        .add_window("dining", D.SOUTH, width=8, offset=2)
        .add_window("master_bed", D.NORTH, width=5, offset=5)
        .add_window("bed_2", D.NORTH, width=4, offset=4)
        .add_window("bed_3", D.NORTH, width=4, offset=3)
        .add_window("bath", D.NORTH, width=3, offset=2)
        # --- Covered front porch (outside the envelope) ------------------
        .add_porch("front_porch", x=0, y=-8, width=28, length=8, covered=True)
    )


if __name__ == "__main__":
    from barndsl import render_svg, validate

    plan = build_example()
    print(validate(plan))
    with open("cedar_ridge.svg", "w", encoding="utf-8") as fh:
        fh.write(render_svg(plan))
    print("\nWrote cedar_ridge.svg")
