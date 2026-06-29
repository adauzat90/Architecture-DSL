#! python3
# -*- coding: utf-8 -*-
"""Build a barndsl plan into the active Revit document.

Pick either a compiled exchange (``.json`` written by ``barndsl revit``) or a
``.barn`` source file. A ``.barn`` is compiled and lowered on the spot, which
needs the ``barndsl`` package importable from pyRevit's CPython engine; if it
isn't, export the JSON from a terminal first and pick that instead.

The element creation lives in ``barndsl_revit.builder`` (the extension's lib/);
this script is just the in-Revit UI around it.
"""

import os

from pyrevit import forms, revit, script

from barndsl_revit import builder, exchange

logger = script.get_logger()
output = script.get_output()
doc = revit.doc


def _exchange_from_barn(path):
    """Compile + lower a ``.barn`` here, or guide the user to the JSON route."""
    try:
        from barndsl import compile_file, to_revit_model
    except ImportError:
        forms.alert(
            "Building straight from a .barn file needs the `barndsl` package "
            "importable from pyRevit's CPython engine.\n\n"
            "Either install it there (pip install -e . into that engine), or "
            "export the exchange first from a terminal:\n\n"
            "    barndsl revit %s --out plan.json\n\n"
            "and pick the resulting plan.json here." % os.path.basename(path),
            title="barndsl not available",
            exitscript=True,
        )
    result = compile_file(path)
    if result.plan is None:
        forms.alert(
            "Compile failed — fix the source and retry:\n\n"
            + result.report(os.path.basename(path)),
            title="barndsl compile error",
            exitscript=True,
        )
    if not result.ok:
        # Errors null the plan (handled above); warnings/infos are fine to build.
        logger.info(result.report(os.path.basename(path)))
    return exchange.load(to_revit_model(result.plan).to_dict())


def main():
    path = forms.pick_file(
        files_filter="barndsl plan (*.json, *.barn)|*.json;*.barn",
        title="Pick a barndsl exchange (.json) or source (.barn)",
    )
    if not path:
        return

    if path.lower().endswith(".json"):
        try:
            data = exchange.load_path(path)
        except exchange.ExchangeError as exc:
            forms.alert("Not a valid barndsl exchange:\n\n%s" % exc, exitscript=True)
            return
    else:
        data = _exchange_from_barn(path)

    problems = exchange.validate(data)
    if problems:
        preview = "\n".join("• " + p for p in problems[:10])
        if len(problems) > 10:
            preview += "\n… and %d more" % (len(problems) - 10)
        if not forms.alert(
            "%d caution(s) before building:\n\n%s\n\nBuild anyway?"
            % (len(problems), preview),
            title="barndsl exchange warnings",
            yes=True,
            no=True,
        ):
            return

    summary = builder.build(doc, data)

    output.print_md("### Built barndsl plan")
    output.print_md("**%s**" % summary.as_line())
    if summary.warnings:
        output.print_md("---\n**%d note(s):**" % len(summary.warnings))
        for w in summary.warnings:
            output.print_md("- %s" % w)


main()
