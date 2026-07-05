"""Command-line interface for barndsl.

    barndsl compile FILE.barn
        Compile a DSL file and print compiler-style diagnostics.

    barndsl build FILE.barn [--out FILE.svg] [--frame]
        Compile, and if it's valid, render an annotated 2D floor plan. `--frame`
        auto-places a default post-and-beam structural frame if the source has none.

    barndsl score FILE.barn [--json]
        Compile and print the deterministic 0-100 design score (see score.py)
        with its per-component deductions and their causes (the worst offending
        rooms, by name and number) — the number an agent hill-climbs on.

    barndsl inspect FILE.barn [--json]
        Compile and dump the plan's resolved geometry (see introspect.py):
        room rectangles with exterior walls, the door/adjacency edges, unplaced
        footprint pockets, and the free wall spans an opening can legally use.

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

    barndsl gltf FILE.barn [--out FILE.glb]
        Compile, then lower the plan to a 3D model as glTF 2.0. `.glb` (default)
        is the binary container; `.gltf` writes JSON with an embedded buffer.
        Any glTF viewer opens it — no CAD licence, no plug-in.

    barndsl ifc FILE.barn [--out FILE.ifc]
        Compile, then lower the plan to IFC4 (a hand-written STEP/SPF file, no
        IfcOpenShell dependency): walls with voided door/window openings, floor
        and porch slabs, a roof, frame columns/beams, stairs and one IfcSpace per
        room. Opens in full Revit, ArchiCAD, BIMcollab/Solibri and any IFC viewer.

    barndsl view3d FILE.barn [--out FILE.html] [--open]
        Write a single self-contained HTML file that renders the 3D model with
        orbit/pan/zoom and layer toggles. Works offline by double-clicking it.

    barndsl serve [FILE.barn] [--port 8787] [--open]
        Start the local web playground: a DSL editor with live diagnostics and a
        2D-plan / 3D / elevations viewport, served from a stdlib http.server on
        127.0.0.1 (no new dependency, works offline). An optional FILE preloads
        the editor; `--open` launches a browser. When the agent extra is
        installed (`pip install 'barndsl[agent]'`) and ANTHROPIC_API_KEY is set,
        a chat pane lights up on the left: a brief in, the Claude compile-critique
        -revise loop streamed live, the best-scoring plan landed in the editor.

    barndsl revit FILE.barn [--out FILE.json] [--frame]
        Compile, then lower the plan to the `barndsl.revit/1` exchange JSON
        (levels, deduplicated walls, hosted doors/windows, room seeds, structural
        members) for the pyRevit extension to build inside Revit.

    barndsl revit-import FILE.json [--out FILE.barn]
        The reverse: reconstruct DSL source from a `barndsl.revit/1` exchange
        (e.g. one read back out of Revit). Prints the DSL, or writes it with --out.

    barndsl revit-diff MODEL PLAN [--json] [--tolerance FT]
        Report the drift between a Revit model export and the authored plan:
        added/moved/removed/changed rooms, doors, windows and walls. Either
        argument may be a `.barn` source or a `barndsl.revit/1` `.json` exchange.
        Exit 0 = no drift, 1 = drift found, 2 = unreadable/uncompilable input.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .compiler import compile_file, compile_source
from .render import save_svg


def _harden_stdio() -> None:
    """Make stdout/stderr resilient to the ambient encoding.

    Reports, diagnostics and the Revit-exchange summary contain intentional
    non-ASCII glyphs (em dashes, arrows, ``≥``/``≤``, fractions). When output is
    piped, redirected to a file, or run on a legacy Windows console, Python picks
    the locale encoding (e.g. cp1252) with ``errors="strict"`` and a single such
    glyph raises ``UnicodeEncodeError``, aborting the command. Switch both
    streams to UTF-8 so capable terminals render the glyphs and pipes get valid
    bytes; fall back to replacing unencodable characters if UTF-8 is refused, so
    the command never dies on an encoding error.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # not a TextIOWrapper (e.g. captured/replaced)
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def _load_dotenv() -> None:
    """Load a local ``.env`` into the environment (for the agentic workflow).

    Fills ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` / ``BARNDSL_MODEL`` from
    a ``.env`` file so ``barndsl serve`` and ``barndsl design`` work without a
    manual ``export``. Searches from the current directory upward, so running
    from a subdirectory still finds the repo-root ``.env``.

    Two deliberate properties:

    * **The project ``.env`` is authoritative.** ``override=True`` — values in
      the file win over the ambient environment. This is on purpose:
      ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_BASE_URL`` are a matched pair (a key
      is only valid against its own endpoint). Under ``override=False`` a stray
      ambient ``ANTHROPIC_BASE_URL`` (e.g. one injected by a host tool) would
      shadow the file's while the file's key still loaded, sending the key to the
      wrong endpoint — a 401. Letting the file win keeps the pair consistent.
    * **Soft dependency.** If ``python-dotenv`` is not installed (a trimmed
      install), this silently no-ops rather than failing the command; the user
      just has to export the vars themselves, exactly as before.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True), override=True)


def _repo_example() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(repo_root, "examples", "cedar_ridge.barn")


def _resolve_profile(args: argparse.Namespace):
    """Resolve ``--profile`` to a Profile, or ``None`` for the default.

    Exits the process with code 2 (like a bad file) on an unknown name or an
    unreadable/invalid JSON override, printing the actionable error to stderr.
    """
    spec = getattr(args, "profile", None)
    if not spec:
        return None
    from .profiles import load_profile

    try:
        return load_profile(spec)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _add_profile_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME_OR_JSON",
        help="jurisdiction profile: a built-in name (default/strict/rural, alias "
        "irc-2021) or a path to a JSON override file. Amends the code thresholds "
        "the checks enforce. See `barndsl profiles`.",
    )


def _cmd_profiles(args: argparse.Namespace) -> int:
    """List the built-in jurisdiction profiles and their thresholds."""
    from .profiles import profiles_text

    if getattr(args, "profile", None):
        # Resolve and dump a single profile (built-in or JSON) as JSON — handy
        # for inspecting exactly what a `--profile` argument will enforce.
        prof = _resolve_profile(args)
        import json

        print(json.dumps(prof.to_dict(), indent=2))
        return 0
    print(profiles_text())
    return 0


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
    result = compile_file(args.file, profile=_resolve_profile(args))
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

    profile = _resolve_profile(args)
    result = compile_file(args.file, profile=profile)
    if result.plan is not None and getattr(args, "frame", False) and result.plan.frame_spec is None:
        # `--frame` auto-places a default post-and-beam frame even when the source
        # has no `frame` directive — recompile from the emitted DSL so the new
        # structure passes through the same checks.
        from .emit import emit_dsl

        result.plan.frame()
        result = compile_source(emit_dsl(result.plan), name=result.plan.name, profile=profile)

    fmt = getattr(args, "format", None)
    out = args.out or f"barndo.{fmt or 'svg'}"

    if getattr(args, "json", False):
        import json

        from .score import design_score

        payload = result.to_dict()
        payload["score"] = design_score(result).to_dict()
        if result.plan is not None:
            payload["metrics"] = result.plan.metrics()
            if result.recovered:
                payload["out"] = None
                payload["render_error"] = "parse-error recovery: partial plan not rendered"
            else:
                try:
                    save_render(result.plan, out, fmt)
                    payload["out"] = out
                except (ImportError, ValueError) as exc:
                    payload["out"] = None
                    payload["render_error"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 0 if result.ok else 1

    print(result.report(os.path.basename(args.file)))
    if result.plan is None or result.recovered:
        # A recovered partial plan is for scoring/inspecting, not for output
        # artifacts — keep the pre-recovery contract: no render on parse errors.
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

    result = compile_file(args.file, profile=_resolve_profile(args))
    report = design_score(result)
    if getattr(args, "json", False):
        import json

        print(json.dumps(report.to_dict(), indent=2))
        return 0 if result.plan is not None else 1

    print(result.summary())
    print(f"\nDesign score: {report.total:g} / 100")
    print("  Deductions:")
    for name, points in report.components.items():
        cause = report.details.get(name)
        print(f"    {name:<12} -{points:g}" + (f"  — {cause}" if cause else ""))
    c = report.counts
    print(f"  Diagnostics: {c['error']} error(s), {c['warning']} warning(s), {c['info']} info(s)")
    return 0 if result.plan is not None else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Dump the compiled plan's resolved geometry (the "geometry pack").

    Prints the same summary the agent loop appends to its feedback — rooms,
    door edges, unplaced pockets, free wall spans — or, with ``--json``, the
    raw :func:`~barndsl.introspect.plan_summary` dict. Like ``score``, it only
    fails (exit 1) when there is no plan at all: a plan with diagnostics is
    exactly when you want to look up its geometry.
    """
    from .introspect import plan_summary, summary_text

    result = compile_file(args.file)
    if result.plan is None:
        print(result.report(os.path.basename(args.file)), file=sys.stderr)
        return 1
    summary = plan_summary(result.plan)
    if getattr(args, "json", False):
        import json

        print(json.dumps(summary, indent=2))
        return 0
    print(f"Plan: {result.plan.name}")
    print(summary_text(summary))
    return 0


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
    from .agent import (
        BarndoAgent,
        agent_availability,
        resolve_max_iterations,
        resolve_model,
        resolve_target_score,
    )

    available, reason = agent_availability()
    if not available:
        print(f"error: {reason}", file=sys.stderr)
        return 2

    # Each flag defaults to None so an omitted flag falls through to the matching
    # $BARNDSL_* env var (then the built-in default), keeping the CLI and the
    # playground on one configuration. An explicit flag always wins.
    model = resolve_model(args.model)
    iterations = args.iterations if args.iterations is not None else resolve_max_iterations()
    if args.target_score is None:
        target_score: float | None = resolve_target_score()
    elif args.target_score <= 0:
        target_score = None  # `--target-score 0` disables the gate
    else:
        target_score = args.target_score

    def on_step(step) -> None:
        crit = ""
        if step.critique is not None:
            crit = "  (critic: satisfied)" if step.critique.satisfied else "  (critic: needs work)"
        score = f"  score {step.score.total:g}/100" if step.score is not None else ""
        print(f"  iteration {step.iteration}: {step.result.summary()}{score}{crit}")

    print(f"Designing with {model} (up to {iterations} iteration(s))...\n")
    agent = BarndoAgent(model=model)
    try:
        result = agent.design(
            args.brief,
            max_iterations=iterations,
            critique=not args.no_critique,
            on_step=on_step,
            target_score=target_score,
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
    try:
        result = compile_file(args.file)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    if result.plan is not None and getattr(args, "frame", False) and result.plan.frame_spec is None:
        from .emit import emit_dsl

        result.plan.frame()
        result = compile_source(emit_dsl(result.plan), name=result.plan.name)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None or result.recovered:
        # Never emit an exchange for a partial recovery: the JSON could be
        # imported into Revit regardless of this process's exit code.
        return 1
    from .revit import to_revit_model

    model = to_revit_model(result.plan)
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(model.to_json())
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
        return 2
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

    try:
        with open(args.file, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {args.file} is not valid JSON: {exc}", file=sys.stderr)
        return 2
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
    """Canonically reformat .barn files with the comment-preserving normalizer.

    Unlike an emit round-trip, `fmt` keeps every comment (teaching notes and
    suppression pragmas) — it re-renders each statement line from its own tokens
    and normalizes only spacing/number formatting/keyword case. It refuses to
    touch a file that doesn't compile without parse errors, so it can't mask
    breakage."""
    from .fmt import format_source

    rc = 0
    changed_any = False
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        # Refuse to format a file with parse errors — fmt must never mask breakage.
        result = compile_source(original)
        if result.plan is None or result.recovered:
            print(f"{path}: cannot format — fix parse errors first", file=sys.stderr)
            print(result.report(os.path.basename(path)), file=sys.stderr)
            rc = 2
            continue
        formatted = format_source(original)
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
    if result.plan is None or result.recovered:
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
    if result.plan is None or result.recovered:
        return 1
    save_dxf(result.plan, args.out)
    n_open = len(result.plan.windows) + len(result.plan.exterior_doors)
    print(f"\nDXF: {len(result.plan.rooms)} room(s), {n_open} opening(s)")
    print(f"Wrote {args.out}")
    return 0 if result.ok else 1


def _cmd_ifc(args: argparse.Namespace) -> int:
    from .ifc import write_ifc
    from .revit import to_revit_model

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None or result.recovered:
        # Never emit BIM for a partial recovery — same contract as `dxf`/`gltf`.
        return 1
    out = args.out or f"{os.path.splitext(os.path.basename(args.file))[0]}.ifc"
    write_ifc(result.plan, out)
    model = to_revit_model(result.plan)
    print(
        f"\nIFC4: {len(model.levels)} storey(s), {len(model.walls)} wall(s), "
        f"{len(model.openings)} opening(s), {len(model.rooms)} space(s)"
    )
    print(f"Wrote {out}")
    return 0 if result.ok else 1


def _cmd_gltf(args: argparse.Namespace) -> int:
    from .gltf import write_gltf
    from .revit import to_revit_model

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None or result.recovered:
        # Never emit a 3D model for a partial recovery — same contract as `dxf`.
        return 1
    out = args.out or f"{os.path.splitext(os.path.basename(args.file))[0]}.glb"
    write_gltf(result.plan, out)
    model = to_revit_model(result.plan)
    n_open = len(model.openings)
    print(
        f"\nglTF: {len(model.walls)} wall(s), {n_open} opening(s), "
        f"{len(model.rooms)} room(s), {len(model.columns)} post(s)"
    )
    print(f"Wrote {out}")
    return 0 if result.ok else 1


def _cmd_view3d(args: argparse.Namespace) -> int:
    from .viewer import write_viewer

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None or result.recovered:
        return 1
    out = args.out or f"{os.path.splitext(os.path.basename(args.file))[0]}.html"
    write_viewer(result.plan, out)
    print(f"\nWrote {out} (self-contained 3D viewer)")
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(f"file://{os.path.abspath(out)}")
    return 0 if result.ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the local web playground (editor + live diagnostics + 2D/3D views)."""
    from .playground import run

    source = None
    base_dir = None
    if getattr(args, "file", None):
        try:
            with open(args.file, encoding="utf-8") as fh:
                source = fh.read()
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        # The served file's directory is the resolution root for `use` (cross-file
        # composition); a scratch buffer (no file) leaves parts unresolvable.
        base_dir = os.path.dirname(os.path.abspath(args.file))
    return run(
        initial_source=source, port=args.port, open_browser=getattr(args, "open", False),
        from_file=source is not None, base_dir=base_dir,
    )


def _cmd_elevation(args: argparse.Namespace) -> int:
    from .views import save_elevation

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    out = args.out or f"elevation-{args.side}.svg"
    save_elevation(result.plan, args.side, out)
    print(f"\nWrote {out} ({args.side} elevation)")
    return 0 if result.ok else 1


def _cmd_section(args: argparse.Namespace) -> int:
    from .views import save_section

    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is None:
        return 1
    save_section(result.plan, args.out)
    print(f"\nWrote {args.out} (section)")
    return 0 if result.ok else 1


def _render_pass(args: argparse.Namespace) -> "object":
    """Compile (rendering to SVG if requested) and print the report. Used by watch."""
    result = compile_file(args.file)
    print(result.report(os.path.basename(args.file)))
    if result.plan is not None:
        print("\n" + _program_summary(result.plan))
        if args.out and not result.recovered:
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


def _cmd_compare(args: argparse.Namespace) -> int:
    """Side-by-side of two plans: score, takeoff, resolved/introduced codes."""
    from .compare import compare_plans, comparison_text

    try:
        result_a = compile_file(args.file_a)
        result_b = compile_file(args.file_b)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    names = (os.path.basename(args.file_a), os.path.basename(args.file_b))
    cmp = compare_plans(result_a, result_b, names)
    if getattr(args, "json", False):
        import json

        print(json.dumps(cmp, indent=2))
    else:
        print(comparison_text(cmp))
    return 0 if result_a.plan is not None and result_b.plan is not None else 1


def _cmd_revit_diff(args: argparse.Namespace) -> int:
    """Report drift between a Revit model export and the authored plan.

    Either argument may be a ``.barn`` source or a ``barndsl.revit/1`` ``.json``
    exchange (sniffed by extension/content). Exit 0 = no drift, 1 = drift found,
    2 = unreadable input or a ``.barn`` that doesn't compile cleanly (a diff
    against a half-parsed plan is meaningless — same contract as `compare`).
    """
    from .revitdiff import (
        DEFAULT_TOLERANCE,
        DiffInputError,
        diff_plans,
        diff_text,
        load_diff_input,
    )

    try:
        model = load_diff_input(args.model)
        authored = load_diff_input(args.plan)
    except DiffInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    tol = args.tolerance if args.tolerance is not None else DEFAULT_TOLERANCE
    names = (os.path.basename(args.model), os.path.basename(args.plan))
    try:
        d = diff_plans(model, authored, names=names, tolerance=tol)
    except Exception as exc:
        # A schema-tagged exchange with a malformed body (version skew, partial
        # export) surfaces here — an input problem, not drift: exit 2.
        print(f"error: cannot diff these inputs: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        import json

        print(json.dumps(d, indent=2))
    else:
        print(diff_text(d))
    return 1 if d["drift"] else 0


def _cmd_cost(args: argparse.Namespace) -> int:
    """Assembly-based construction cost estimate from the plan's takeoff."""
    from .cost import cost_text, estimate_cost

    try:
        result = compile_file(args.file)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.plan is None or result.errors:
        # A cost estimate is a deliverable, not a diagnostic: pricing a plan
        # that failed to compile (including the parser's partial recoveries)
        # is misleading — fail like an unreadable file (exit 2), unlike
        # score/compare which still show partial signal.
        print(result.report(os.path.basename(args.file)), file=sys.stderr)
        return 2

    overrides = None
    if args.costs:
        import json

        try:
            with open(args.costs, encoding="utf-8") as fh:
                overrides = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"error: reading --costs: {exc}", file=sys.stderr)
            return 2

    try:
        est = estimate_cost(result, overrides=overrides, multiplier=args.multiplier)
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        import json

        print(json.dumps(est, indent=2))
    else:
        print(cost_text(est))
    return 0


def _cmd_packet(args: argparse.Namespace) -> int:
    """Bind score, plan, schedules, cost and diagnostics into one HTML deliverable."""
    from .packet import save_packet

    try:
        result = compile_file(args.file)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.plan is None or result.errors:
        # Same contract as `cost`: the packet is a client deliverable, and a
        # plan with errors (or a partial recovery) must not ship as one.
        print(result.report(os.path.basename(args.file)), file=sys.stderr)
        return 2

    overrides = None
    if args.costs:
        import json

        try:
            with open(args.costs, encoding="utf-8") as fh:
                overrides = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"error: reading --costs: {exc}", file=sys.stderr)
            return 2

    out = args.out or "packet.html"
    try:
        save_packet(result, out, costs=overrides, multiplier=args.multiplier)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {out}")
    print("Open it in a browser and Print → Save as PDF for a paginated packet.")
    return 0


def _cmd_revit_log(args: argparse.Namespace) -> int:
    """Translate a *.buildlog.json into compile-style diagnostics."""
    from .revitlog import buildlog_issues, issues_to_dict, load_buildlog

    try:
        log = load_buildlog(args.file)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    issues = buildlog_issues(log)
    if getattr(args, "json", False):
        import json

        print(json.dumps(issues_to_dict(issues), indent=2))
    else:
        if not issues:
            print("Revit build clean: every element built as asked.")
        for issue in issues:
            print(issue)
    # Exit like `compile --strict`: failures/skips (warnings) are the signal.
    from .validation import Severity

    return 1 if any(i.severity is not Severity.INFO for i in issues) else 0


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
    _harden_stdio()
    _load_dotenv()
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
    _add_profile_flag(p_compile)
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
    _add_profile_flag(p_build)
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
    _add_profile_flag(p_score)
    p_score.set_defaults(func=_cmd_score)

    p_inspect = sub.add_parser(
        "inspect",
        help="dump the resolved geometry: rooms, door edges, unplaced pockets, "
        "free wall spans",
    )
    p_inspect.add_argument("file", help="path to a .barn DSL file")
    p_inspect.add_argument(
        "--json",
        action="store_true",
        help="emit the geometry summary as machine-readable JSON",
    )
    p_inspect.set_defaults(func=_cmd_inspect)

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
    p_design.add_argument(
        "--iterations", type=int, default=None,
        help="max refine iterations (default: $BARNDSL_MAX_ITERATIONS, else 3)",
    )
    p_design.add_argument(
        "--model", default=None,
        help="model id (default: $BARNDSL_MODEL, else claude-opus-4-8)",
    )
    p_design.add_argument("--no-critique", action="store_true", help="skip the design critic")
    p_design.add_argument(
        "--target-score",
        type=float,
        default=None,
        help="keep iterating while the design score is below this; 0 disables the "
        "gate (default: $BARNDSL_TARGET_SCORE, else 90)",
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
        "fmt", help="canonically reformat .barn files (comments/pragmas preserved)"
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

    p_ifc = sub.add_parser(
        "ifc", help="export a plan to IFC4 (BIM interchange: Revit/ArchiCAD/any IFC viewer)"
    )
    p_ifc.add_argument("file", help="path to a .barn DSL file")
    p_ifc.add_argument(
        "--out", default=None, help="output path (default: FILE stem + .ifc)"
    )
    p_ifc.set_defaults(func=_cmd_ifc)

    p_gltf = sub.add_parser(
        "gltf", help="export a plan to a 3D model (glTF 2.0: .glb binary or .gltf)"
    )
    p_gltf.add_argument("file", help="path to a .barn DSL file")
    p_gltf.add_argument(
        "--out",
        default=None,
        help="output path (default: FILE stem + .glb); .gltf writes an embedded-buffer JSON",
    )
    p_gltf.set_defaults(func=_cmd_gltf)

    p_view3d = sub.add_parser(
        "view3d", help="write a single-file, offline HTML 3D viewer for a plan"
    )
    p_view3d.add_argument("file", help="path to a .barn DSL file")
    p_view3d.add_argument(
        "--out", default=None, help="output HTML path (default: FILE stem + .html)"
    )
    p_view3d.add_argument(
        "--open", action="store_true", help="open the written viewer in a browser"
    )
    p_view3d.set_defaults(func=_cmd_view3d)

    p_serve = sub.add_parser(
        "serve",
        help="start the local web playground (editor, live diagnostics, 2D/3D; "
        "agent chat pane when barndsl[agent] + ANTHROPIC_API_KEY are present)",
    )
    p_serve.add_argument(
        "file", nargs="?", default=None, help="optional .barn file to preload the editor"
    )
    p_serve.add_argument(
        "--port", type=int, default=8787, help="port to bind on 127.0.0.1 (default 8787)"
    )
    p_serve.add_argument(
        "--open", action="store_true", help="open the playground in a browser"
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_elev = sub.add_parser(
        "elevation", help="render a schematic exterior elevation (one face) to SVG"
    )
    p_elev.add_argument("file", help="path to a .barn DSL file")
    p_elev.add_argument(
        "--side", choices=("north", "south", "east", "west"), default="south",
        help="which face to draw (default: south)",
    )
    p_elev.add_argument("--out", default=None, help="output SVG path (default elevation-<side>.svg)")
    p_elev.set_defaults(func=_cmd_elevation)

    p_section = sub.add_parser(
        "section", help="render a schematic transverse section to SVG"
    )
    p_section.add_argument("file", help="path to a .barn DSL file")
    p_section.add_argument("--out", default="section.svg", help="output SVG path")
    p_section.set_defaults(func=_cmd_section)

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

    p_compare = sub.add_parser(
        "compare",
        help="side-by-side of two plans: score, takeoff, resolved/introduced codes",
    )
    p_compare.add_argument("file_a", help="path to scheme A (.barn)")
    p_compare.add_argument("file_b", help="path to scheme B (.barn)")
    p_compare.add_argument("--json", action="store_true", help="emit the comparison as JSON")
    p_compare.set_defaults(func=_cmd_compare)

    p_revit_diff = sub.add_parser(
        "revit-diff",
        help="report drift (moved/added/removed/changed) between a Revit model "
        "export and the authored plan",
    )
    p_revit_diff.add_argument(
        "model", help="the (edited) Revit side: a .json exchange or a .barn source"
    )
    p_revit_diff.add_argument(
        "plan", help="the authored side: a .barn source or a .json exchange"
    )
    p_revit_diff.add_argument(
        "--json", action="store_true", help="emit the diff as machine-readable JSON"
    )
    p_revit_diff.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="geometry tolerance in feet for detecting a move/resize "
        "(default 0.5)",
    )
    p_revit_diff.set_defaults(func=_cmd_revit_diff)

    p_cost = sub.add_parser(
        "cost",
        help="assembly-based construction cost estimate from the plan's takeoff",
    )
    p_cost.add_argument("file", help="path to a .barn DSL file")
    p_cost.add_argument("--json", action="store_true", help="emit the estimate as JSON")
    p_cost.add_argument(
        "--costs",
        default=None,
        help="path to a JSON file overriding any subset of the default unit costs",
    )
    p_cost.add_argument(
        "--multiplier",
        type=float,
        default=1.0,
        help="regional cost factor scaling every unit cost (e.g. 1.15)",
    )
    p_cost.set_defaults(func=_cmd_cost)

    p_packet = sub.add_parser(
        "packet",
        help="bind score, dimensioned plan, schedules, cost and diagnostics into "
        "one print-ready HTML deliverable",
    )
    p_packet.add_argument("file", help="path to a .barn DSL file")
    p_packet.add_argument(
        "-o", "--out", default=None, help="output HTML path (default packet.html)"
    )
    p_packet.add_argument(
        "--costs", default=None, help="JSON file overriding unit costs (see `cost`)"
    )
    p_packet.add_argument(
        "--multiplier", type=float, default=1.0, help="regional cost factor for the estimate"
    )
    p_packet.set_defaults(func=_cmd_packet)

    p_rlog = sub.add_parser(
        "revit-log",
        help="translate a *.buildlog.json from the pyRevit build into diagnostics",
    )
    p_rlog.add_argument("file", help="path to the *.buildlog.json sidecar")
    p_rlog.add_argument("--json", action="store_true", help="emit diagnostics as JSON")
    p_rlog.set_defaults(func=_cmd_revit_log)

    p_explain = sub.add_parser(
        "explain", help="explain a diagnostic code (or list them all)"
    )
    p_explain.add_argument(
        "code", nargs="?", default=None, help="a code like BEDROOM_EGRESS (omit to list all)"
    )
    p_explain.set_defaults(func=_cmd_explain)

    p_profiles = sub.add_parser(
        "profiles",
        help="list the built-in jurisdiction profiles and the thresholds they set",
    )
    p_profiles.add_argument(
        "--profile",
        default=None,
        metavar="NAME_OR_JSON",
        help="instead of the table, dump one resolved profile (built-in name or "
        "JSON override file) as JSON",
    )
    p_profiles.set_defaults(func=_cmd_profiles)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        # A command signalling a fatal input error (e.g. an unresolvable
        # --profile) raises SystemExit(code); surface it as an int return so
        # callers/tests get the exit code uniformly, like the other commands.
        return exc.code if isinstance(exc.code, int) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
