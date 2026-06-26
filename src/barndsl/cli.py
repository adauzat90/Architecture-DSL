"""Command-line interface for barndsl.

    barndsl demo [--out FILE]
        Build the bundled example plan, validate it, and render an SVG.

    barndsl design "BRIEF" [--out FILE] [--iterations N] [--model ID] [--no-critique]
        Run the Claude agent: natural language → validated, rendered plan.
        Requires `pip install 'barndsl[agent]'` and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .render import save_svg
from .validation import validate


def _print_metrics(plan) -> None:
    m = plan.metrics()
    print(f"  Footprint:        {m['footprint_sqft']:.0f} sq ft")
    print(f"  Interior (cond.): {m['interior_sqft']:.0f} sq ft")
    print(f"  Bedrooms/baths:   {int(m['bedroom_count'])} / {m['bathroom_count']:.1f}")
    print(f"  Ext. wall area:   {m['exterior_wall_area_sqft']:.0f} sq ft")
    print(f"  Roof area (≈):    {m['roof_area_sqft']:.0f} sq ft")


def _cmd_demo(args: argparse.Namespace) -> int:
    # Import the bundled example without requiring it to be on sys.path.
    import importlib.util
    import os

    # cli.py lives at <repo>/src/barndsl/cli.py → three levels up is <repo>.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    example_path = os.path.join(repo_root, "examples", "simple_barndo.py")
    if os.path.exists(example_path):
        spec = importlib.util.spec_from_file_location("simple_barndo", example_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        plan = module.build_example()
    else:  # Fallback: a minimal inline plan if the example file is absent.
        plan = _fallback_plan()

    print(f"Plan: {plan.name}\n")
    report = validate(plan)
    print(report)
    print()
    _print_metrics(plan)
    save_svg(plan, args.out)
    print(f"\nWrote {args.out}")
    return 0 if report.is_valid else 1


def _cmd_design(args: argparse.Namespace) -> int:
    try:
        from .agent import BarndoAgent
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def on_step(step) -> None:
        crit = ""
        if step.critique is not None:
            crit = "  satisfied" if step.critique.satisfied else "  needs work"
        print(f"  iteration {step.iteration}: {step.report.summary()}{crit}")

    print(f"Designing with {args.model} (up to {args.iterations} iteration(s))...\n")
    agent = BarndoAgent(model=args.model)
    try:
        result = agent.design(
            args.brief,
            max_iterations=args.iterations,
            critique=not args.no_critique,
            on_step=on_step,
        )
    except Exception as exc:  # pragma: no cover - network/runtime errors
        print(f"error: {exc}", file=sys.stderr)
        return 2

    plan = result.plan
    print(f"\nPlan: {plan.name}  (after {result.iterations} iteration(s))\n")
    print(result.report)
    print()
    _print_metrics(plan)
    if result.history and result.history[-1].critique is not None:
        print("\nArchitect's assessment:")
        print(f"  {result.history[-1].critique.assessment}")
    save_svg(plan, args.out)
    print(f"\nWrote {args.out}")
    return 0 if result.report.is_valid else 1


def _fallback_plan():  # pragma: no cover - only if example file missing
    from .builder import barndominium
    from .elements import Direction as D
    from .elements import RoomType as T

    return (
        barndominium("Demo Barndo")
        .envelope(width=40, length=30)
        .ceiling(10)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=30)
        .add_room("bedroom", T.BEDROOM, x=24, y=0, width=16, length=15)
        .add_room("bath", T.BATHROOM, x=24, y=15, width=16, length=15)
        .connect("living", "bedroom", width=2.67)
        .connect("living", "bath", width=2.67)
        .entrance("living", D.SOUTH, width=3, offset=10)
        .add_window("bedroom", D.EAST, width=4, offset=4)
        .add_window("living", D.WEST, width=8, offset=10)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="barndsl",
        description="DSL and agentic workflow for barndominium floor plans.",
    )
    parser.add_argument("--version", action="version", version=f"barndsl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="render and validate the bundled example plan")
    p_demo.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_demo.set_defaults(func=_cmd_demo)

    p_design = sub.add_parser("design", help="generate a plan from a brief with Claude")
    p_design.add_argument("brief", help="natural-language design brief")
    p_design.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_design.add_argument("--iterations", type=int, default=3, help="max refine iterations")
    p_design.add_argument("--model", default="claude-opus-4-8", help="Claude model id")
    p_design.add_argument("--no-critique", action="store_true", help="skip the design critic")
    p_design.set_defaults(func=_cmd_design)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
