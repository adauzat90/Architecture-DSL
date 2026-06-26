"""Entry point for fluently authoring a barndominium plan.

The fluent methods live on :class:`barndsl.elements.Barndominium`; this module
just provides a readable factory so DSL scripts open with ``barndominium(...)``.

Example
-------
>>> from barndsl import barndominium, RoomType, Direction
>>> plan = (
...     barndominium("Cedar Ridge")
...     .envelope(width=60, length=40)
...     .ceiling(12)
...     .add_room("great_room", RoomType.LIVING, x=0, y=0, width=30, length=24)
...     .add_room("kitchen", RoomType.KITCHEN, x=30, y=0, width=18, length=16)
...     .connect("great_room", "kitchen", width=8)
...     .entrance("great_room", Direction.SOUTH, width=3, offset=12)
...     .add_window("great_room", Direction.WEST, width=6, offset=8)
... )
"""

from __future__ import annotations

from .elements import Barndominium


def barndominium(name: str) -> Barndominium:
    """Start a new, empty barndominium plan named ``name``."""
    return Barndominium(name=name)
