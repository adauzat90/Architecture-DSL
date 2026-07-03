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
    Beam,
    Direction,
    ExteriorDoor,
    FrameSpec,
    InteriorDoor,
    Lot,
    Porch,
    Post,
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
from .dxf import save_dxf, to_dxf
from .render import RenderConfig, render_svg, save_render, save_svg
from .cost import DEFAULT_RATES, CostReport, estimate_cost
from .views import (
    elevation_svg,
    save_elevation,
    save_section,
    section_svg,
)
from .schedule import (
    door_rows,
    room_rows,
    schedules_csv,
    schedules_markdown,
    window_rows,
)
from .revit import (
    RevitImportError,
    RevitModel,
    exchange_to_dsl,
    exchange_to_plan,
    foundation_plan,
    plan_stair_runs,
    roof_plan,
    structural_grids,
    to_revit_json,
    to_revit_model,
)
from .fixtures import Fixture, fixtures_fit, fixtures_for, plan_room_fixtures
from .score import ScoreReport, design_score
from .structure import place_frame
from .validation import Issue, Severity, ValidationReport, clear_dimensions, validate

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
    "Lot",
    "Stair",
    "ProgramSpec",
    "Post",
    "Beam",
    "FrameSpec",
    "place_frame",
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
    "clear_dimensions",
    # design score (deterministic 0-100, for agents to hill-climb on)
    "design_score",
    "ScoreReport",
    # fixtures / appliances (seeds + clearance)
    "Fixture",
    "fixtures_for",
    "fixtures_fit",
    "plan_room_fixtures",
    # rendering
    "render_svg",
    "save_svg",
    "save_render",
    "RenderConfig",
    # vertical views (elevations + section)
    "elevation_svg",
    "section_svg",
    "save_elevation",
    "save_section",
    # cost estimate (rough order-of-magnitude from the takeoff)
    "estimate_cost",
    "CostReport",
    "DEFAULT_RATES",
    # schedules (room/door/window, no Revit)
    "schedules_markdown",
    "schedules_csv",
    "room_rows",
    "door_rows",
    "window_rows",
    # DXF (CAD interchange)
    "to_dxf",
    "save_dxf",
    # Revit exchange (lowering to a Revit-shaped model + JSON)
    "to_revit_model",
    "to_revit_json",
    "RevitModel",
    # Revit exchange (reverse: reconstruct a plan / DSL from an exchange)
    "exchange_to_plan",
    "exchange_to_dsl",
    "RevitImportError",
    "plan_stair_runs",
    "roof_plan",
    "structural_grids",
    "foundation_plan",
    # agent helpers are imported lazily via barndsl.agent to avoid hard deps
]

__version__ = "0.1.0"
