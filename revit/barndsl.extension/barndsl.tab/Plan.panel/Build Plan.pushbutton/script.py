#! python3
# -*- coding: utf-8 -*-
"""Build a barndsl plan into the active Revit document (target: Revit 2025).

Pick a compiled exchange (``.json`` from ``barndsl revit``) or a ``.barn`` source
(compiled on the spot, which needs the ``barndsl`` package on pyRevit's CPython
engine). Then choose **Build** or **Preview** — Preview does a real build and
rolls it back, so you see exactly what would be created (and any per-element
errors) without changing the model.

A ``*.buildlog.json`` is written next to the source with the full report, and an
optional ``*.config.json`` sidecar (or ``barndsl_revit.config.json`` beside the
source) can override which wall/floor/family types each pass uses — see
``revit/README.md``.

The element creation lives in ``barndsl_revit.builder``; this script is the UI.
"""

import json
import os

from pyrevit import forms, revit, script

from barndsl_revit import builder, exchange, report

logger = script.get_logger()
output = script.get_output()
doc = revit.doc


def _exchange_from_barn(path):
    try:
        from barndsl import compile_file, to_revit_model
    except ImportError:
        forms.alert(
            "Building from a .barn needs the `barndsl` package importable from "
            "pyRevit's CPython engine.\n\n"
            "Either install it there, or export the exchange from a terminal:\n\n"
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
        logger.info(result.report(os.path.basename(path)))
    return exchange.load(to_revit_model(result.plan).to_dict())


def _load_config(source_path, dry_run):
    """Build options from a sidecar config if present, with the dry-run choice."""
    candidates = [
        os.path.splitext(source_path)[0] + ".config.json",
        os.path.join(os.path.dirname(source_path), "barndsl_revit.config.json"),
    ]
    for cfg_path in candidates:
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r") as fh:
                    cfg = json.load(fh)
                opts = report.BuildOptions.from_dict(cfg)
                opts.dry_run = dry_run
                output.print_md("_Using config_ `%s`" % os.path.basename(cfg_path))
                return opts
            except Exception as exc:
                forms.alert("Ignoring bad config %s:\n%s" % (cfg_path, exc))
                break
    return report.BuildOptions(dry_run=dry_run)


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

    choice = forms.CommandSwitchWindow.show(
        ["Build", "Preview (dry run)"],
        message="Build into the model, or preview without committing?",
    )
    if not choice:
        return
    dry_run = choice.startswith("Preview")
    options = _load_config(path, dry_run)

    report_obj = builder.build(doc, data, options)

    output.print_md(report_obj.to_markdown(include_created=options.verbose))

    # Write a build log next to the source for easy sharing when debugging.
    try:
        log_path = os.path.splitext(path)[0] + ".buildlog.json"
        with open(log_path, "w") as fh:
            fh.write(report_obj.to_json())
        output.print_md("\n_Build log:_ `%s`" % log_path)
    except Exception as exc:
        logger.warning("could not write build log: %s", exc)


main()
