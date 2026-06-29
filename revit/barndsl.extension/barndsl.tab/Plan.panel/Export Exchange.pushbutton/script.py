#! python3
# -*- coding: utf-8 -*-
"""Compile a ``.barn`` and write its ``barndsl.revit/1`` exchange JSON.

A model-free convenience: it does not change the Revit document, it just runs the
same lowering ``Build Plan`` does and saves the result next to the source, so you
can inspect the exchange or feed it back through the JSON-first workflow. Needs
the ``barndsl`` package importable from pyRevit's CPython engine.
"""

import os

from pyrevit import forms, script

output = script.get_output()


def main():
    try:
        from barndsl import compile_file, to_revit_json
    except ImportError:
        forms.alert(
            "This needs the `barndsl` package importable from pyRevit's CPython "
            "engine. Install it there, or run `barndsl revit FILE --out plan.json` "
            "from a terminal instead.",
            title="barndsl not available",
            exitscript=True,
        )
        return

    path = forms.pick_file(file_ext="barn", title="Pick a .barn source file")
    if not path:
        return

    result = compile_file(path)
    if result.plan is None:
        forms.alert(
            "Compile failed:\n\n" + result.report(os.path.basename(path)),
            title="barndsl compile error",
            exitscript=True,
        )
        return

    out_path = os.path.splitext(path)[0] + ".json"
    with open(out_path, "w") as fh:
        fh.write(to_revit_json(result.plan))

    output.print_md("### Exported exchange")
    output.print_md("Wrote `%s`" % out_path)
    if not result.ok:
        output.print_md("\n_(compiled with diagnostics — see below)_")
        output.print_md("```\n%s\n```" % result.report(os.path.basename(path)))


main()
