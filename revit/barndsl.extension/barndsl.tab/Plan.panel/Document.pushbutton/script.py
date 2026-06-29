#! python3
# -*- coding: utf-8 -*-
"""Create drawings from the barndsl model in the active document.

Run after **Build Plan**. Produces, in one transaction (one undo step):

* a floor-plan **view** per level,
* room / door / window **tags** in those views,
* native door / window / room **schedules**,
* a **sheet** per level with the plan placed.

It tags only barndsl-managed elements and is idempotent — re-documenting replaces
the barndsl views/sheets/schedules a previous run made (set ``"replace": false``
in the config to append). The work is in ``barndsl_revit.builder.document``.
"""

from pyrevit import revit, script

from barndsl_revit import builder, report

output = script.get_output()
doc = revit.doc


def main():
    report_obj = builder.document(doc, report.BuildOptions())
    output.print_md("### barndsl documentation")
    output.print_md(report_obj.to_markdown())


main()
