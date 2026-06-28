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
    ProgramSpec,
    Room,
    RoomType,
    Stair,
    Window,
    feet,
    inches,
)
from .compiler import (
    DSL_REFERENCE,
    CompileResult,
    compile_file,
    compile_source,
)
from .emit import emit_dsl
from .layout import (
    LayoutBrief,
    LayoutResult,
    RoomSpec,
    parse_brief,
    solve_layout,
)
from .layout2 import (
    LayoutBrief2,
    RoomSpec2,
    parse_brief2,
    solve_layout2,
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
    "Stair",
    "ProgramSpec",
    "feet",
    "inches",
    # compiler front-end
    "compile_source",
    "compile_file",
    "CompileResult",
    "DSL_REFERENCE",
    "emit_dsl",
    # auto-layout
    "solve_layout",
    "parse_brief",
    "LayoutBrief",
    "LayoutResult",
    "RoomSpec",
    # auto-layout 2.0 (space-filling)
    "solve_layout2",
    "parse_brief2",
    "LayoutBrief2",
    "RoomSpec2",
    # checks
    "validate",
    "ValidationReport",
    "Issue",
    "Severity",
    # rendering
    "render_svg",
    "save_svg",
    "RenderConfig",
    # agent helpers are imported lazily via barndsl.agent to avoid hard deps
]

__version__ = "0.1.0"
