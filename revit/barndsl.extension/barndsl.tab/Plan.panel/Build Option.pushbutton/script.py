#! python3
# -*- coding: utf-8 -*-
"""Build ONE shortlisted candidate into the active Revit Design Option (§3.3).

The agent's ``design()`` loop shortlists several scored iterations; instead of
shipping only the winner, this command lets the architect choose among them **in
Revit** as native Design Options — "the agent shortlisted, you choose in the
model".

**The Revit API cannot create Design Options** (``DB.DesignOption`` is read-only;
option sets are made in the UI only). What it *can* rely on: any element created
while a Design Option is **active** in the UI is automatically assigned to that
option. So the workflow is:

1. In Revit, once: **Manage → Design Options → New** an option set with one
   option per candidate, then **Edit** (activate) the option you want to fill.
2. Run this command, pick a candidate from the manifest — its elements land in
   whatever option is currently active. Repeat for each option/candidate.

Each candidate builds with its managed-element identities **namespaced** to the
candidate's label, so rebuilding one candidate keeps its elements while building
another never purges the first. The pick-list comes from a ``candidates.json``
manifest::

    {"schema": "barndsl.options/1",
     "candidates": [{"label": "iteration-3, score 84", "exchange": "plan_a.json"},
                    {"label": "iteration-7, score 79", "exchange": "plan_b.json"}]}

Produce the per-candidate exchange files with the core CLI
(``barndsl revit plan_a.barn --out plan_a.json``); see ``revit/README.md``.

The element creation lives in ``barndsl_revit.builder``; this script is the UI.
"""

import json
import os

from pyrevit import DB, forms, revit, script

from barndsl_revit import builder, exchange, report

logger = script.get_logger()
output = script.get_output()
doc = revit.doc

OPTIONS_SCHEMA = "barndsl.options/1"


def _load_manifest(path):
    """Read a ``candidates.json`` manifest → a list of ``(label, exchange_path)``.

    ``exchange`` paths are resolved relative to the manifest's directory. Exits
    with an alert on anything malformed."""
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except Exception as exc:
        forms.alert("Could not read manifest:\n\n%s" % exc, exitscript=True)
        return []
    if not isinstance(data, dict) or data.get("schema") != OPTIONS_SCHEMA:
        forms.alert(
            "Not a barndsl candidate manifest (expected schema %r)." % OPTIONS_SCHEMA,
            exitscript=True,
        )
        return []
    base = os.path.dirname(path)
    out = []
    seen = set()
    for i, c in enumerate(data.get("candidates", []) or []):
        if not isinstance(c, dict):
            forms.alert(
                "Candidate entry %d is not an object (expected "
                '{"label": ..., "exchange": ...}).' % i,
                exitscript=True,
            )
            return []
        label = (c.get("label") or "candidate %d" % i).strip()
        if not label:
            # A blank label would fall back to a PLAIN build, whose purge scope
            # is the whole non-candidate model — refuse it outright.
            forms.alert("Candidate entry %d has a blank label." % i, exitscript=True)
            return []
        if label in seen:
            # One label = one identity namespace: a duplicate would make the
            # second build diff against (and purge) the first's elements.
            forms.alert(
                "Duplicate candidate label %r — every candidate needs its own "
                "label (one label = one Design Option = one namespace)." % label,
                exitscript=True,
            )
            return []
        seen.add(label)
        rel = c.get("exchange")
        if not rel:
            forms.alert("Candidate %r has no 'exchange' file." % label, exitscript=True)
            return []
        ex_path = rel if os.path.isabs(rel) else os.path.join(base, rel)
        out.append((label, ex_path))
    if not out:
        forms.alert("The manifest lists no candidates.", exitscript=True)
    return out


def _load_config(source_path, dry_run, candidate):
    """Build options from a sidecar config if present, plus the dry-run choice
    and the candidate label (which namespaces this candidate's identities)."""
    opts = None
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
                output.print_md("_Using config_ `%s`" % os.path.basename(cfg_path))
                break
            except Exception as exc:
                forms.alert("Ignoring bad config %s:\n%s" % (cfg_path, exc))
                break
    if opts is None:
        opts = report.BuildOptions()
    opts.dry_run = dry_run
    opts.candidate = candidate  # per-build: namespaces this candidate's elements
    return opts


def _active_option_note():
    """A best-effort reminder of which Design Option is active (read-only)."""
    try:
        opt_id = DB.DesignOption.GetActiveDesignOptionId(doc)
        if opt_id and opt_id != DB.ElementId.InvalidElementId:
            opt = doc.GetElement(opt_id)
            name = getattr(opt, "Name", None)
            if name:
                return "Active Design Option: **%s**" % name
    except Exception:
        pass
    return (
        "_No Design Option is active — the build lands in the Main Model. To keep "
        "candidates separate, activate an option (Manage → Design Options → Edit) "
        "first._"
    )


def main():
    path = forms.pick_file(
        files_filter="barndsl candidates (candidates.json, *.json)|candidates.json;*.json",
        title="Pick a barndsl candidate manifest (candidates.json)",
    )
    if not path:
        return

    candidates = _load_manifest(path)
    if not candidates:
        return

    labels = [label for label, _ in candidates]
    chosen = forms.SelectFromList.show(
        labels, title="Pick a candidate to build", button_name="Choose candidate",
    )
    if not chosen:
        return
    label, ex_path = next(c for c in candidates if c[0] == chosen)

    try:
        data = exchange.load_path(ex_path)
    except exchange.ExchangeError as exc:
        forms.alert(
            "Candidate %r's exchange is not valid:\n\n%s" % (label, exc), exitscript=True
        )
        return
    except Exception as exc:
        forms.alert("Could not read %s:\n\n%s" % (ex_path, exc), exitscript=True)
        return

    output.print_md("### Build Option — %s" % label)
    output.print_md(_active_option_note())

    choice = forms.CommandSwitchWindow.show(
        ["Build", "Preview (dry run)"],
        message="Build this candidate into the active option, or preview it?",
    )
    if not choice:
        return
    dry_run = choice.startswith("Preview")
    options = _load_config(ex_path, dry_run, label)

    report_obj = builder.build(doc, data, options)
    output.print_md(report_obj.to_markdown(include_created=options.verbose))

    # Write a build log next to the candidate's exchange for easy sharing.
    try:
        log_path = os.path.splitext(ex_path)[0] + ".buildlog.json"
        with open(log_path, "w") as fh:
            fh.write(report_obj.to_json())
        output.print_md("\n_Build log:_ `%s`" % log_path)
    except Exception as exc:
        logger.warning("could not write build log: %s", exc)


main()
