"""Command-line interface for barndsl.

    barndsl compile FILE.barn
        Compile a DSL file and print compiler-style diagnostics.

    barndsl build FILE.barn [--out FILE.svg] [--frame]
        Compile, and if it's valid, render an annotated 2D floor plan. `--frame`
        auto-places a default post-and-beam structural frame if the source has none.

    barndsl score FILE.barn [--json]
        Compile and print the deterministic 0-100 design score (see score.py)
        with its per-component deductions — the number an agent hill-climbs on.

    barndsl demo [--out FILE.svg]
        Compile and render the bundled example (examples/cedar_ridge.barn).

    barndsl layout BRIEF.txt [--out FILE.svg] [--emit] [--no-openings] [--engine fill|greedy]
        Auto-layout: solve room placement from an adjacency brief, then compile
        (and render). Deterministic, no API key. The default `fill` engine
        dissects the envelope (no wasted space, rooms on the perimeter); `greedy`
        is the v1 abutment placer.

    barndsl design "BRIEF" [--out FILE.svg] [--iterations N] [--model ID] [--no-critique]
                   [--target-score S]
        Run the Claude agent: brief → DSL → compile → score → critique → refine,
        keeping the best-scoring iteration. Requires `pip install 'barndsl[agent]'`
        and ANTHROPIC_API_KEY.

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
    print(f"  Foundation (≈):   {m['foundation_concrete_yd3']:.1f} cu yd concrete")
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

        from .score import design_score

        payload = result.to_dict()
        payload["score"] = design_score(result).to_dict()
        print(json.dumps(payload, indent=2))
        return 0 if result.ok else 1
    print(result.report(os.path.basename(args.file)))
    if result.plan is not None:
        print("\n" + _program_summary(result.plan))
        if args.metrics:
            _print_metrics(result.plan)
        if args.show_coords:
            print()
            _print_coords(result.plan)
    return _strict_rc(result, args)


def _strict_rc(result, args) -> int:
    """Exit code honouring --strict / --strict-info (warnings/infos → failure).

    A hard error always fails. With ``--strict`` a clean-but-warned plan also
    fails (CI gate: "this plan must stay warning-free"); ``--strict-info`` extends
    that to the design-quality info nudges too.
    """
    if not result.ok:
        return 1
    strict = getattr(args, "strict", False)
    strict_info = getattr(args, "strict_info", False)
    if strict_info and (result.warnings or result.infos):
        print("\nstrict: failing on warning/info diagnostics (--strict-info).")
        return 1
    if strict and result.warnings:
        print("\nstrict: failing on warning diagnostics (--strict).")
        return 1
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    from .render import save_render

    result = compile_file(args.file)
    if result.plan is not None and getattr(args, "frame", False) and result.plan.frame_spec is None:
        # `--frame` auto-places a default post-and-beam frame even when the source
        # has no `frame` directive — recompile from the emitted DSL so the new
        # structure passes through the same checks.
        from .emit import emit_dsl

        result.plan.frame()
        result = compile_source(emit_dsl(result.plan), name=result.plan.name)

    fmt = getattr(args, "format", None)
    out = args.out or f"barndo.{fmt or 'svg'}"

    if getattr(args, "json", False):
        import json

        from .score import design_score

        payload = result.to_dict()
        payload["score"] = design_score(result).to_dict()
        if result.plan is not None:
            payload["metrics"] = result.plan.metrics()
            try:
                save_render(result.plan, out, fmt)
                payload["out"] = out
            except (ImportError, ValueError) as exc:
                payload["out"] = None
                payload["render_error"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 0 if result.ok else 1

    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    print()
    _print_metrics(result.plan)
    try:
        save_render(result.plan, out, fmt)
    except (ImportError, ValueError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    print(f"\nWrote {out}")
    return 0 if result.ok else 1


def _cmd_score(args: argparse.Namespace) -> int:
    from .score import design_score

    result = compile_file(args.file)
    report = design_score(result)
    if getattr(args, "json", False):
        import json

        print(json.dumps(report.to_dict(), indent=2))
        return 0 if result.plan is not None else 1

    print(result.summary())
    print(f"\nDesign score: {report.total:g} / 100")
    print("  Deductions:")
    for name, points in report.components.items():
        print(f"    {name:<12} -{points:g}")
    c = report.counts
    print(f"  Diagnostics: {c['error']} error(s), {c['warning']} warning(s), {c['info']} info(s)")
    return 0 if result.plan is not None else 1


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

            brief2 = parse_brief2(text)
            if args.no_openings:
                brief2.add_openings = False
            topology = "auto" if args.engine == "fill" else args.engine
            out = solve_layout2(brief2, engine=topology)
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
        score = f"  score {step.score.total:g}/100" if step.score is not None else ""
        print(f"  iteration {step.iteration}: {step.result.summary()}{score}{crit}")

    print(f"Designing with {args.model} (up to {args.iterations} iteration(s))...\n")
    agent = BarndoAgent(model=args.model)
    try:
        result = agent.design(
            args.brief,
            max_iterations=args.iterations,
            critique=not args.no_critique,
            on_step=on_step,
            target_score=args.target_score if args.target_score > 0 else None,
        )
    except Exception as exc:  # pragma: no cover - network/runtime errors
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"\n--- final DSL (best of {result.iterations} iteration(s): "
        f"iteration {result.best_iteration}, score {result.score.total:g}/100) ---"
    )
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


def _cmd_fmt(args: argparse.Namespace) -> int:
    """Canonically reformat .barn files via the compile → emit_dsl round-trip."""
    from .emit import emit_dsl

    rc = 0
    changed_any = False
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        result = compile_source(original)
        if result.plan is None:
            print(f"{path}: cannot format — fix compile errors first", file=sys.stderr)
            print(result.report(os.path.basename(path)), file=sys.stderr)
            rc = 2
            continue
        formatted = emit_dsl(result.plan)
        changed = formatted != original
        if args.check:
            if changed:
                print(f"would reformat {path}")
                changed_any = True
        elif args.write:
            if changed:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(formatted)
                print(f"reformatted {path}")
        else:
            print(formatted, end="")
    if rc:
        return rc
    if args.check and changed_any:
        return 1
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    from .schedule import schedules_csv, schedules_markdown

    result = compile_file(args.file)
    if result.plan is None:
        print(result.report(os.path.basename(args.file)))
        return 1

    # No explicit selection → all three schedules.
    rooms, doors, windows = args.rooms, args.doors, args.windows
    if not (rooms or doors or windows):
        rooms = doors = windows = True

    render = schedules_csv if args.format == "csv" else schedules_markdown
    text = render(result.plan, rooms=rooms, doors=doors, windows=windows)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _slugify(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in name.strip()]
    slug = "".join(keep).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "barndo"


def _cmd_new(args: argparse.Namespace) -> int:
    from .scaffold import starter_dsl

    name = args.name or "My Barndo"
    out = args.out or f"{_slugify(name)}.barn"
    if os.path.exists(out) and not args.force:
        print(f"error: {out} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    src = starter_dsl(name)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(src)
    result = compile_source(src, name=name)
    print(f"Wrote {out}  ({result.summary()})")
    print(f"Next: barndsl build {out} --out plan.svg")
    return 0


def _cmd_dxf(args: argparse.Namespace) -> int:
    from .dxf import save_dxf

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    save_dxf(result.plan, args.out)
    n_open = len(result.plan.windows) + len(result.plan.exterior_doors)
    print(f"\nDXF: {len(result.plan.rooms)} room(s), {n_open} opening(s)")
    print(f"Wrote {args.out}")
    return 0 if result.ok else 1


def _render_pass(args: argparse.Namespace) -> "object":
    """Compile (rendering to SVG if requested) and print the report. Used by watch."""
    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is not None:
        print("\n" + _program_summary(result.plan))
        if args.out:
            try:
                save_svg(result.plan, args.out)
                print(f"Wrote {args.out}")
            except Exception as exc:  # pragma: no cover - render edge cases
                print(f"render error: {exc}", file=sys.stderr)
    return result


def _cmd_watch(args: argparse.Namespace) -> int:
    import time

    print(f"Watching {args.file} (Ctrl-C to stop)...\n")
    last: float | None = None
    try:
        while True:
            try:
                mtime = os.path.getmtime(args.file)
            except OSError:
                mtime = None
            if mtime != last:
                last = mtime
                if mtime is None:
                    print(f"waiting for {args.file} to exist...")
                else:
                    print("\033[2J\033[H", end="")  # clear screen, cursor home
                    print(f"barndsl watch — {args.file}\n")
                    _render_pass(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


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
    p_compile.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on warnings too (CI gate), not only errors",
    )
    p_compile.add_argument(
        "--strict-info",
        action="store_true",
        help="exit non-zero on warnings AND info nudges",
    )
    p_compile.set_defaults(func=_cmd_compile)

    p_build = sub.add_parser("build", help="compile and render a .barn file to SVG/PNG/PDF")
    p_build.add_argument("file", help="path to a .barn DSL file")
    p_build.add_argument(
        "--out",
        default=None,
        help="output path (default barndo.<format>); extension picks the format",
    )
    p_build.add_argument(
        "--format",
        choices=("svg", "png", "pdf"),
        default=None,
        help="output format (default: infer from --out, else svg). "
        "png/pdf need `pip install 'barndsl[raster]'`",
    )
    p_build.add_argument(
        "--json",
        action="store_true",
        help="emit diagnostics + metrics as JSON (still writes the drawing)",
    )
    p_build.add_argument(
        "--frame",
        action="store_true",
        help="auto-place a default post-and-beam frame if the source has none",
    )
    p_build.set_defaults(func=_cmd_build)

    p_score = sub.add_parser(
        "score", help="compile and print the 0-100 design score with its components"
    )
    p_score.add_argument("file", help="path to a .barn DSL file")
    p_score.add_argument(
        "--json",
        action="store_true",
        help="emit the score report as machine-readable JSON",
    )
    p_score.set_defaults(func=_cmd_score)

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
    p_design.add_argument(
        "--target-score",
        type=float,
        default=90.0,
        help="keep iterating while the design score is below this (0 disables the gate)",
    )
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

    p_fmt = sub.add_parser(
        "fmt", help="canonically reformat .barn files (compile → emit)"
    )
    p_fmt.add_argument("files", nargs="+", help="one or more .barn files")
    p_fmt.add_argument(
        "-w", "--write", action="store_true", help="rewrite files in place"
    )
    p_fmt.add_argument(
        "--check",
        action="store_true",
        help="don't write; exit non-zero if any file isn't already formatted",
    )
    p_fmt.set_defaults(func=_cmd_fmt)

    p_sched = sub.add_parser(
        "schedule", help="emit room/door/window schedules (Markdown or CSV), no Revit"
    )
    p_sched.add_argument("file", help="path to a .barn DSL file")
    p_sched.add_argument("--rooms", action="store_true", help="include the room schedule")
    p_sched.add_argument("--doors", action="store_true", help="include the door schedule")
    p_sched.add_argument(
        "--windows", action="store_true", help="include the window schedule"
    )
    p_sched.add_argument(
        "--format", choices=("md", "csv"), default="md", help="output format (default md)"
    )
    p_sched.add_argument("--out", default=None, help="write here instead of stdout")
    p_sched.set_defaults(func=_cmd_schedule)

    p_new = sub.add_parser("new", help="scaffold a starter .barn file")
    p_new.add_argument("name", nargs="?", default=None, help="plan name")
    p_new.add_argument(
        "--out", default=None, help="output path (default: a slug of the name)"
    )
    p_new.add_argument(
        "--force", action="store_true", help="overwrite the file if it exists"
    )
    p_new.set_defaults(func=_cmd_new)

    p_dxf = sub.add_parser("dxf", help="export a plan to DXF (CAD interchange)")
    p_dxf.add_argument("file", help="path to a .barn DSL file")
    p_dxf.add_argument("--out", default="plan.dxf", help="output DXF path")
    p_dxf.set_defaults(func=_cmd_dxf)

    p_watch = sub.add_parser(
        "watch", help="recompile (and optionally re-render) on every save"
    )
    p_watch.add_argument("file", help="path to a .barn DSL file")
    p_watch.add_argument(
        "--out", default=None, help="also re-render to this SVG on each change"
    )
    p_watch.add_argument(
        "--interval", type=float, default=0.5, help="poll interval in seconds"
    )
    p_watch.set_defaults(func=_cmd_watch)

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
