"""Command-line interface for barndsl.

    barndsl compile FILE.barn
        Compile a DSL file and print compiler-style diagnostics.

    barndsl build FILE.barn [--out FILE.svg]
        Compile, and if it's valid, render an annotated 2D floor plan.

    barndsl demo [--out FILE.svg]
        Compile and render the bundled example (examples/cedar_ridge.barn).

    barndsl design "BRIEF" [--out FILE.svg] [--iterations N] [--model ID] [--no-critique]
        Run the Claude agent: brief → DSL → compile → critique → refine.
        Requires `pip install 'barndsl[agent]'` and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .compiler import compile_file, compile_source
from .render import save_svg


def _repo_example() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(repo_root, "examples", "cedar_ridge.barn")


def _print_metrics(plan) -> None:
    m = plan.metrics()
    print(f"  Footprint:        {m['footprint_sqft']:.0f} sq ft")
    print(f"  Interior (cond.): {m['interior_sqft']:.0f} sq ft")
    print(f"  Bedrooms/baths:   {int(m['bedroom_count'])} / {m['bathroom_count']:.1f}")
    print(f"  Ext. wall area:   {m['exterior_wall_area_sqft']:.0f} sq ft")
    print(f"  Roof area (≈):    {m['roof_area_sqft']:.0f} sq ft")


def _cmd_compile(args: argparse.Namespace) -> int:
    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    return 0 if result.ok else 1


def _cmd_build(args: argparse.Namespace) -> int:
    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    print()
    _print_metrics(result.plan)
    save_svg(result.plan, args.out)
    print(f"\nWrote {args.out}")
    return 0 if result.ok else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    path = _repo_example()
    if os.path.exists(path):
        result = compile_file(path)
        label = os.path.basename(path)
    else:  # pragma: no cover - example missing
        result = compile_source(_FALLBACK_DSL, name="Demo Barndo")
        label = "<demo>"
    print(result.report(label))
    if result.plan is None:  # pragma: no cover
        return 1
    print()
    _print_metrics(result.plan)
    save_svg(result.plan, args.out)
    print(f"\nWrote {args.out}")
    return 0 if result.ok else 1


def _cmd_design(args: argparse.Namespace) -> int:
    try:
        from .agent import BarndoAgent
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def on_step(step) -> None:
        crit = ""
        if step.critique is not None:
            crit = "  (critic: satisfied)" if step.critique.satisfied else "  (critic: needs work)"
        print(f"  iteration {step.iteration}: {step.result.summary()}{crit}")

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

    print(f"\n--- final DSL (after {result.iterations} iteration(s)) ---")
    print(result.source.rstrip())
    print("\n--- compiler report ---")
    print(result.result.report())
    if result.plan is not None:
        print()
        _print_metrics(result.plan)
        save_svg(result.plan, args.out)
        print(f"\nWrote {args.out}")
    if result.history and result.history[-1].critique is not None:
        print("\nArchitect's assessment:")
        print(f"  {result.history[-1].critique.assessment}")
    return 0 if result.result.ok else 1


_FALLBACK_DSL = """\
plan "Demo Barndo"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 24 x 30
room bedroom: bedroom at 24,0 size 16 x 15
room bath: bathroom at 24,15 size 16 x 15
door living - bedroom width 2.67
door living - bath width 2.67
entry living south width 3 offset 10
window bedroom east width 4 offset 4
window living west width 8 offset 10
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="barndsl",
        description="DSL compiler and agentic workflow for barndominium floor plans.",
    )
    parser.add_argument("--version", action="version", version=f"barndsl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_compile = sub.add_parser("compile", help="compile a .barn file and print diagnostics")
    p_compile.add_argument("file", help="path to a .barn DSL file")
    p_compile.set_defaults(func=_cmd_compile)

    p_build = sub.add_parser("build", help="compile and render a .barn file to SVG")
    p_build.add_argument("file", help="path to a .barn DSL file")
    p_build.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_build.set_defaults(func=_cmd_build)

    p_demo = sub.add_parser("demo", help="compile and render the bundled example")
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
