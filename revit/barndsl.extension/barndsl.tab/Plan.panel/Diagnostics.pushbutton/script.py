#! python3
# -*- coding: utf-8 -*-
"""Report the build environment and the active document's resources.

The first thing to run when a build doesn't produce what you expect: it shows the
Revit / pyRevit / Python versions, whether ``barndsl`` is importable, and the
wall types, floor types, door/window families and structural families the project
offers — each pass auto-picks from these (or you name one in a config). Read-only;
changes nothing.
"""

from pyrevit import revit, script

from barndsl_revit import builder

output = script.get_output()
doc = revit.doc


def _bullets(items):
    if not items:
        return "- _(none)_"
    return "\n".join("- `%s`" % i for i in items)


def main():
    info = builder.diagnose(doc)

    output.print_md("## barndsl diagnostics")
    output.print_md(
        "- **Revit:** %s\n- **pyRevit:** %s\n- **Python:** %s\n"
        "- **Document:** %s\n- **barndsl:** %s"
        % (
            info["revit"],
            info["pyrevit"],
            info["python"],
            info["document"],
            info["barndsl"],
        )
    )

    ready = info["ready"]
    output.print_md("### Readiness")
    output.print_md(
        "\n".join(
            "- %s **%s**" % ("✅" if ok else "⚠️", name)
            for name, ok in sorted(ready.items())
        )
    )

    output.print_md("### Wall types")
    output.print_md(_bullets(info["wall_types"]))
    output.print_md("### Floor types")
    output.print_md(_bullets(info["floor_types"]))
    output.print_md("### Door families")
    output.print_md(_bullets(info["door_families"]))
    output.print_md("### Window families")
    output.print_md(_bullets(info["window_families"]))
    output.print_md("### Structural column families")
    output.print_md(_bullets(info["structural_column_families"]))
    output.print_md("### Structural framing families")
    output.print_md(_bullets(info["structural_framing_families"]))
    output.print_md("### Levels")
    output.print_md(_bullets(info["levels"]))


main()
