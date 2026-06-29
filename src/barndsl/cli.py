"""Command-line interface for barndsl.

    barndsl compile FILE.barn
        Compile a DSL file and print compiler-style diagnostics.

    barndsl build FILE.barn [--out FILE.svg] [--frame]
        Compile, and if it's valid, render an annotated 2D floor plan. `--frame`
        auto-places a default post-and-beam structural frame if the source has none.

    barndsl demo [--out FILE.svg]
        Compile and render the bundled example (examples/cedar_ridge.barn).

    barndsl layout BRIEF.txt [--out FILE.svg] [--emit] [--no-openings] [--engine fill|greedy]
        Auto-layout: solve room placement from an adjacency brief, then compile
        (and render). Deterministic, no API key. The default `fill` engine
        dissects the envelope (no wasted space, rooms on the perimeter); `greedy`
        is the v1 abutment placer.

    barndsl design "BRIEF" [--out FILE.svg] [--iterations N] [--model ID] [--no-critique]
        Run the Claude agent: brief → DSL → compile → critique → refine.
        Requires `pip install 'barndsl[agent]'` and ANTHROPIC_API_KEY.

    barndsl revit FILE.barn [--out FILE.json] [--frame]
        Compile, then lower the plan to the `barndsl.revit/1` exchange JSON
        (levels, deduplicated walls, hosted doors/windows, room seeds, structural
        members) for the pyRevit extension to build inside Revit.

    barndsl revit-import FILE.json [--out FILE.barn]
        The reverse: reconstruct DSL source from a `barndsl.revit/1` exchange
        (e.g. one read back out of Revit). Prints the DSL, or writes it with --out.
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
    if plan.frame_spec is not None or plan.posts:
        print(
            f"  Frame:            {int(m['frame_count'])} bents / "
            f"{int(m['post_count'])} posts / {m['beam_linear_ft']:.0f} ft beam"
        )


def _program_summary(plan) -> str:
    """One-line program recap — a clean compile alone doesn't verify this."""
    m = plan.metrics()
    return (
        f"Program: {int(m['bedroom_count'])} bed / {m['bathroom_count']:g} bath · "
        f"{m['interior_sqft']:.0f} sq ft interior · "
        f"{m['habitable_sqft']:.0f} sq ft habitable · "
        f"footprint {m['footprint_sqft']:.0f} sq ft"
    )


def _print_coords(plan) -> None:
    from .validation import exterior_walls

    print("Resolved geometry (ft):")
    for r in plan.rooms:
        ext = ", ".join(w.value for w in exterior_walls(plan, r)) or "—"
        lvl = f" level {r.level}" if getattr(r, "level", 0) else ""
        print(
            f"  {r.id}: ({r.x:g},{r.y:g}) → ({r.x2:g},{r.y2:g})  "
            f"{r.width:g}×{r.length:g}{lvl}  exterior: {ext}"
        )


def _cmd_compile(args: argparse.Namespace) -> int:
    result = compile_file(args.file)
    if getattr(args, "json", False):
        import json

        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.ok else 1
    print(result.report(os.path.basename(args.file)))
    if result.plan is not None:
        print("\n" + _program_summary(result.plan))
        if args.metrics:
            _print_metrics(result.plan)
        if args.show_coords:
            print()
            _print_coords(result.plan)
    return 0 if result.ok else 1


def _cmd_build(args: argparse.Namespace) -> int:
    result = compile_file(args.file)
    if result.plan is not None and getattr(args, "frame", False) and result.plan.frame_spec is None:
        # `--frame` auto-places a default post-and-beam frame even when the source
        # has no `frame` directive — recompile from the emitted DSL so the new
        # structure passes through the same checks.
        from .emit import emit_dsl

        result.plan.frame()
        result = compile_source(emit_dsl(result.plan), name=result.plan.name)
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


def _cmd_layout(args: argparse.Namespace) -> int:
    from .emit import emit_dsl

    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    try:
        if args.engine == "greedy":
            from .layout import parse_brief, solve_layout

            brief = parse_brief(text)
            if args.no_openings:
                brief.add_openings = False
            out = solve_layout(brief)
        else:  # the space-filling v2 engine (default); fill = auto-select topology
            from .layout2 import parse_brief2, solve_layout2

            brief = parse_brief2(text)
            if args.no_openings:
                brief.add_openings = False
            topology = "auto" if args.engine == "fill" else args.engine
            out = solve_layout2(brief, engine=topology)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(out.summary())
    if out.unsatisfied:
        pairs = ", ".join(f"{a}-{b}" for a, b in out.unsatisfied)
        print(f"  could not abut (no shared wall): {pairs}")
    for note in out.notes:
        print(f"  note: {note}")

    src = emit_dsl(out.plan)
    result = compile_source(src, name=out.plan.name)
    print("\n" + result.report(out.plan.name))
    print("\n" + _program_summary(out.plan))

    if args.emit:
        print("\n--- resolved DSL ---")
        print(src.rstrip())
    if args.out:
        save_svg(out.plan, args.out)
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


def _cmd_revit(args: argparse.Namespace) -> int:
    result = compile_file(args.file)
    if result.plan is not None and getattr(args, "frame", False) and result.plan.frame_spec is None:
        from .emit import emit_dsl

        result.plan.frame()
        result = compile_source(emit_dsl(result.plan), name=result.plan.name)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    from .revit import to_revit_model

    model = to_revit_model(result.plan)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(model.to_json())
    n_ext = sum(1 for w in model.walls if w.exterior)
    print(
        f"\nRevit exchange: {len(model.levels)} level(s), {len(model.walls)} wall(s) "
        f"({n_ext} exterior), {len(model.openings)} opening(s), "
        f"{len(model.rooms)} room(s), {len(model.columns)} column(s)"
    )
    print(f"Wrote {args.out}")
    # The exchange is emitted even when the plan has warnings/infos; only a hard
    # compile error (no plan) blocks it, so a clean exchange tracks `result.ok`.
    return 0 if result.ok else 1


def _cmd_revit_import(args: argparse.Namespace) -> int:
    import json

    from .revit import RevitImportError, exchange_to_plan

    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        plan = exchange_to_plan(data)
    except RevitImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Re-compile the reconstructed DSL so the import is validated through the
    # same pipeline, and the user sees any diagnostics on the recovered plan.
    from .emit import emit_dsl

    src = emit_dsl(plan)
    result = compile_source(src, name=plan.name)
    print(result.report(os.path.basename(args.file)))
    print("\n" + _program_summary(plan))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(src)
        print(f"\nWrote {args.out}")
    else:
        print("\n--- reconstructed DSL ---")
        print(src.rstrip())
    return 0 if result.ok else 1


def _cmd_explain(args: argparse.Namespace) -> int:
    from .diagnostics import REGISTRY, explain

    if args.code is None:
        # No code given: list every code, grouped by severity.
        from .validation import Severity

        for sev in (Severity.ERROR, Severity.WARNING, Severity.INFO):
            codes = sorted(c for c, i in REGISTRY.items() if i.severity is sev)
            print(f"{sev.value}:")
            for c in codes:
                print(f"  {c:<20} {REGISTRY[c].title}")
        return 0
    print(explain(args.code))
    return 0 if args.code.strip().upper() in REGISTRY else 1


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
    p_compile.add_argument(
        "--metrics", action="store_true", help="also print the full area/material takeoff"
    )
    p_compile.add_argument(
        "--show-coords",
        action="store_true",
        help="print each room's resolved rectangle and exterior walls",
    )
    p_compile.add_argument(
        "--json",
        action="store_true",
        help="emit diagnostics as machine-readable JSON instead of text",
    )
    p_compile.set_defaults(func=_cmd_compile)

    p_build = sub.add_parser("build", help="compile and render a .barn file to SVG")
    p_build.add_argument("file", help="path to a .barn DSL file")
    p_build.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_build.add_argument(
        "--frame",
        action="store_true",
        help="auto-place a default post-and-beam frame if the source has none",
    )
    p_build.set_defaults(func=_cmd_build)

    p_demo = sub.add_parser("demo", help="compile and render the bundled example")
    p_demo.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_demo.set_defaults(func=_cmd_demo)

    p_layout = sub.add_parser(
        "layout", help="solve room placement from an adjacency brief (no API key)"
    )
    p_layout.add_argument("file", help="path to a textual layout brief")
    p_layout.add_argument(
        "--out", default=None, help="also render the laid-out plan to this SVG path"
    )
    p_layout.add_argument(
        "--emit", action="store_true", help="print the resolved .barn DSL"
    )
    p_layout.add_argument(
        "--no-openings",
        action="store_true",
        help="don't auto-add the entry and windows",
    )
    p_layout.add_argument(
        "--engine",
        choices=("fill", "bands", "slice", "dual", "greedy"),
        default="fill",
        help="'fill' (default): space-filling, auto-selects the best topology; "
        "'bands'/'slice'/'dual': force a v2 topology ('dual' = rectangular dual, "
        "for non-sliceable adjacency graphs); 'greedy': v1 abutment placer",
    )
    p_layout.set_defaults(func=_cmd_layout)

    p_design = sub.add_parser("design", help="generate a plan from a brief with Claude")
    p_design.add_argument("brief", help="natural-language design brief")
    p_design.add_argument("--out", default="barndo.svg", help="output SVG path")
    p_design.add_argument("--iterations", type=int, default=3, help="max refine iterations")
    p_design.add_argument("--model", default="claude-opus-4-8", help="Claude model id")
    p_design.add_argument("--no-critique", action="store_true", help="skip the design critic")
    p_design.set_defaults(func=_cmd_design)

    p_revit = sub.add_parser(
        "revit", help="lower a plan to the Revit exchange JSON for the pyRevit add-in"
    )
    p_revit.add_argument("file", help="path to a .barn DSL file")
    p_revit.add_argument("--out", default="plan.json", help="output JSON path")
    p_revit.add_argument(
        "--frame",
        action="store_true",
        help="auto-place a default post-and-beam frame if the source has none",
    )
    p_revit.set_defaults(func=_cmd_revit)

    p_revit_import = sub.add_parser(
        "revit-import", help="reconstruct DSL from a barndsl.revit/1 exchange JSON"
    )
    p_revit_import.add_argument("file", help="path to a barndsl.revit/1 JSON file")
    p_revit_import.add_argument(
        "--out", default=None, help="write the reconstructed .barn here (else print)"
    )
    p_revit_import.set_defaults(func=_cmd_revit_import)

    p_explain = sub.add_parser(
        "explain", help="explain a diagnostic code (or list them all)"
    )
    p_explain.add_argument(
        "code", nargs="?", default=None, help="a code like BEDROOM_EGRESS (omit to list all)"
    )
    p_explain.set_defaults(func=_cmd_explain)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
