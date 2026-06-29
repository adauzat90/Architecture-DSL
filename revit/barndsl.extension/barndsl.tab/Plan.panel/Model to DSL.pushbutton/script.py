#! python3
# -*- coding: utf-8 -*-
"""Reconstruct barndsl DSL from the active Revit model (experimental).

The reverse of *Build Plan*: it reads placed Revit Rooms (bounding box → the
room rectangle; name → a guessed room type) and door/window instances (connected
to rooms via FromRoom/ToRoom), assembles a ``barndsl.revit/1`` exchange, and
turns it into ``.barn`` source via the barndsl core. Walls and structure aren't
read back — the reconstruction is room- and opening-driven.

Needs the ``barndsl`` package importable from pyRevit's CPython engine (for the
reconstruction + DSL serialisation). Review the output before trusting it.
"""

import os

from pyrevit import forms, revit, script

from barndsl_revit import builder

output = script.get_output()
doc = revit.doc


def main():
    try:
        import barndsl
    except ImportError:
        forms.alert(
            "Model to DSL needs the `barndsl` package importable from pyRevit's "
            "CPython engine (it does the reconstruction). Install it there and retry.",
            title="barndsl not available",
            exitscript=True,
        )
        return

    exchange, report = builder.read_model(doc)

    output.print_md(report.to_markdown())

    if not exchange["rooms"]:
        forms.alert("No placed rooms found to reconstruct.", title="Nothing to read")
        return

    try:
        plan = barndsl.exchange_to_plan(exchange)
        src = barndsl.emit_dsl(plan)
        result = barndsl.compile_source(src, name=plan.name)
    except Exception as exc:
        forms.alert("Reconstruction failed:\n\n%s" % exc, title="barndsl error", exitscript=True)
        return

    output.print_md("### Reconstructed DSL")
    output.print_md("_%s_" % result.summary().replace("\n", " "))
    output.print_md("```\n%s\n```" % src.rstrip())

    out_path = forms.save_file(file_ext="barn", default_name="%s.barn" % exchange["plan"]["name"])
    if out_path:
        with open(out_path, "w") as fh:
            fh.write(src)
        output.print_md("\n_Wrote_ `%s`" % out_path)
        # A diagnostics JSON alongside, for debugging the read.
        try:
            import json

            with open(os.path.splitext(out_path)[0] + ".readlog.json", "w") as fh:
                fh.write(json.dumps(report.to_dict(), indent=2))
        except Exception:
            pass


main()
