"""barndsl — an embedded Python DSL and agentic workflow for barndominium floor plans.

Quick start (pure engine, no API key needed)::

    from barndsl import barndominium, RoomType, Direction, validate, render_svg

    plan = (
        barndominium("Cedar Ridge")
        .envelope(width=60, length=40)
        .ceiling(12)
        .add_room("great_room", RoomType.LIVING, x=0, y=0, width=30, length=24)
        ...
    )
    print(validate(plan))
    open("plan.svg", "w").write(render_svg(plan))

Agentic workflow (needs `pip install 'barndsl[agent]'` + ANTHROPIC_API_KEY)::

    from barndsl import design
    result = design("3 bed / 2 bath barndominium, ~1800 sq ft, with a shop bay")
"""

from .builder import barndominium
from .elements import (
    Barndominium,
    Direction,
    ExteriorDoor,
    InteriorDoor,
    Porch,
    Room,
    RoomType,
    Window,
    feet,
    inches,
)
from .render import RenderConfig, render_svg, save_svg
from .validation import Issue, Severity, ValidationReport, validate

__all__ = [
    "barndominium",
    "Barndominium",
    "Room",
    "RoomType",
    "Direction",
    "InteriorDoor",
    "ExteriorDoor",
    "Window",
    "Porch",
    "feet",
    "inches",
    "validate",
    "ValidationReport",
    "Issue",
    "Severity",
    "render_svg",
    "save_svg",
    "RenderConfig",
    # agent helpers are imported lazily via barndsl.agent to avoid hard deps
]

__version__ = "0.1.0"
